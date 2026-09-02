"""Bounded parallel execution of one canonical P3 SAFE Implementer group."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from enum import Enum
from threading import Event
from typing import Protocol

from agentic_engineering_os.domain import (
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    ParallelExecutionPlan,
    ProjectState,
    UserStoryStatus,
)

from .implementer import ImplementerResult, ImplementerVerdict
from .codex_capabilities import (
    CodexCapability,
    CodexCapabilityAssessment,
    CodexCapabilityStatus,
)
from .parallel_implementer_coordinator import (
    ParallelCoordinationInput,
    ParallelImplementerCoordinator,
    PreparedImplementerContext,
    PreparedParallelGroup,
)
from .single_role_codex import (
    SingleRoleArtifacts,
    SingleRoleCodexExecutor,
    SingleRoleExecutionOutcome,
)


_MAX_CONCURRENCY = 8


class MissionStateReader(Protocol):
    def load(self) -> object: ...


class ParallelProjectStateReader(Protocol):
    def load(self) -> object: ...


class ParallelMemberExecutorFactory(Protocol):
    def create(
        self,
        context: PreparedImplementerContext,
        mission_store: MissionStateReader,
    ) -> SingleRoleCodexExecutor: ...

    def assess_parallel_capability(self) -> CodexCapabilityAssessment: ...


class ParallelCodexGroupStatus(str, Enum):
    READY_FOR_P3_HANDOFF = "READY_FOR_P3_HANDOFF"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True, slots=True)
class ParallelCodexMemberExecution:
    user_story_id: str
    assignment_id: str
    request_id: str
    execution_id: str | None
    execution_outcome: SingleRoleExecutionOutcome | None
    implementer_result: ImplementerResult | None
    ready_for_test: bool
    blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.ready_for_test != (
            self.implementer_result is not None
            and self.implementer_result.verdict is ImplementerVerdict.READY_FOR_TEST
        ):
            raise ValueError("ready_for_test must match the validated ImplementerResult")


@dataclass(frozen=True, slots=True)
class ParallelCodexGroupExecution:
    group_index: int
    status: ParallelCodexGroupStatus
    max_concurrency: int
    members: tuple[ParallelCodexMemberExecution, ...]

    @property
    def successful(self) -> bool:
        return (
            self.status is ParallelCodexGroupStatus.READY_FOR_P3_HANDOFF
            and bool(self.members)
            and all(item.ready_for_test for item in self.members)
        )


class ParallelCodexExecutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class ParallelCodexImplementerExecutor:
    """Execute only an already prepared canonical SAFE group through P4.8."""

    def __init__(
        self,
        *,
        parallel_coordinator: ParallelImplementerCoordinator,
        mission_store: MissionStateReader,
        project_store: ParallelProjectStateReader,
        executor_factory: ParallelMemberExecutorFactory,
        max_concurrency: int = 4,
    ) -> None:
        if (
            not isinstance(max_concurrency, int)
            or isinstance(max_concurrency, bool)
            or not 1 <= max_concurrency <= _MAX_CONCURRENCY
        ):
            raise ValueError(f"max_concurrency must be between 1 and {_MAX_CONCURRENCY}")
        self._parallel = parallel_coordinator
        self._mission_store = mission_store
        self._project_store = project_store
        self._factory = executor_factory
        self._max_concurrency = max_concurrency

    def execute_group(
        self,
        plan: ParallelExecutionPlan,
        prepared_group: PreparedParallelGroup,
        *,
        coordination_input: ParallelCoordinationInput,
        request_id_prefix: str,
        cancellation: Event | None = None,
        member_cancellations: Mapping[str, Event] | None = None,
    ) -> ParallelCodexGroupExecution:
        if not isinstance(request_id_prefix, str) or not request_id_prefix.strip():
            raise ParallelCodexExecutionError(
                "INVALID_REQUEST_ID", "request_id_prefix must be explicit"
            )
        validated = self._parallel.validate_prepared_group(
            plan,
            prepared_group,
            coordination_input=coordination_input,
        )
        current_mission = self._current_mission(validated)
        self._require_current_project(validated, coordination_input)
        cancellations = dict(member_cancellations or {})
        assignment_ids = set(validated.assignment_ids)
        if (
            any(key not in assignment_ids for key in cancellations)
            or any(not isinstance(value, Event) for value in cancellations.values())
        ):
            raise ParallelCodexExecutionError(
                "INVALID_CANCELLATION",
                "member cancellations must target exact group assignments",
            )
        workers = min(self._max_concurrency, len(validated.contexts))
        if workers < 1:
            raise ParallelCodexExecutionError(
                "EMPTY_GROUP", "a parallel Codex group must contain members"
            )
        if workers > 1 and not self._parallel_supported(workers):
            workers = 1

        results: dict[str, ParallelCodexMemberExecution] = {}
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="agentic-codex-implementer",
        ) as pool:
            futures = {
                pool.submit(
                    self._execute_member,
                    context,
                    current_mission,
                    request_id_prefix,
                    cancellations.get(context.assignment_id, cancellation),
                ): context.assignment_id
                for context in validated.contexts
            }
            for future in as_completed(futures):
                assignment_id = futures[future]
                try:
                    results[assignment_id] = future.result()
                except Exception as error:
                    context = next(
                        item
                        for item in validated.contexts
                        if item.assignment_id == assignment_id
                    )
                    results[assignment_id] = _failed_member(
                        context,
                        _request_id(request_id_prefix, assignment_id),
                        error,
                    )

        ordered = tuple(results[item.assignment_id] for item in validated.contexts)
        execution_ids = tuple(
            item.execution_id for item in ordered if item.execution_id is not None
        )
        if len(execution_ids) != len(set(execution_ids)):
            ordered = tuple(
                replace(
                    item,
                    implementer_result=None,
                    ready_for_test=False,
                    blockers=(*item.blockers, "DUPLICATE_EXECUTION_IDENTITY"),
                )
                for item in ordered
            )
        complete = all(item.ready_for_test for item in ordered)
        return ParallelCodexGroupExecution(
            group_index=validated.group_index,
            status=(
                ParallelCodexGroupStatus.READY_FOR_P3_HANDOFF
                if complete
                else ParallelCodexGroupStatus.INCOMPLETE
            ),
            max_concurrency=workers,
            members=ordered,
        )

    def _parallel_supported(self, workers: int) -> bool:
        provider = getattr(self._factory, "assess_parallel_capability", None)
        if not callable(provider):
            return False
        try:
            assessment = provider()
        except Exception:
            return False
        return (
            isinstance(assessment, CodexCapabilityAssessment)
            and assessment.authentically_discovered
            and assessment.status(CodexCapability.INDEPENDENT_PROCESS_PARALLELISM)
            is CodexCapabilityStatus.SUPPORTED
            and assessment.tested_parallelism is not None
            and assessment.tested_parallelism >= workers
        )

    def _current_mission(self, group: PreparedParallelGroup) -> MissionState:
        mission = self._mission_store.load()
        expected_mission = group.contexts[0].handoff.mission_id
        if not isinstance(mission, MissionState) or (
            mission.mission_id != expected_mission
            or mission.workflow_generation != group.workflow_generation
            or mission.observed_commit != group.baseline_commit
            or mission.status is not MissionStatus.ACTIVE
            or mission.role is not MissionRole.ORCHESTRATOR
            or mission.operating_step is not OperatingStep.ACT
            or mission.blockers
        ):
            raise ParallelCodexExecutionError(
                "MISSION_STATE_MISMATCH",
                "current MissionState no longer authorizes the prepared group",
            )
        return mission

    def _require_current_project(
        self,
        group: PreparedParallelGroup,
        coordination_input: ParallelCoordinationInput,
    ) -> None:
        current = self._project_store.load()
        if not isinstance(current, ProjectState):
            raise ParallelCodexExecutionError(
                "PROJECT_STATE_MISMATCH", "current ProjectState is unavailable"
            )
        current_by_id = {story.id: story for story in current.user_stories}
        planned_by_id = {
            story.id: story for story in coordination_input.project_state.user_stories
        }
        if len(current_by_id) != len(current.user_stories):
            raise ParallelCodexExecutionError(
                "PROJECT_STATE_MISMATCH", "current ProjectState contains duplicate stories"
            )
        for identifier in group.user_story_ids:
            observed = current_by_id.get(identifier)
            planned = planned_by_id.get(identifier)
            if (
                observed is None
                or planned is None
                or observed.status is not UserStoryStatus.IN_PROGRESS
                or observed.scope != planned.scope
                or observed.depends_on != planned.depends_on
            ):
                raise ParallelCodexExecutionError(
                    "PROJECT_STATE_MISMATCH",
                    "prepared member is absent, not IN_PROGRESS, or changed scope/dependencies",
                )

    def _execute_member(
        self,
        context: PreparedImplementerContext,
        current_mission: MissionState,
        request_id_prefix: str,
        cancellation: Event | None,
    ) -> ParallelCodexMemberExecution:
        mission_reader = _BoundImplementerMissionReader(
            self._mission_store, current_mission, context
        )
        executor = self._factory.create(context, mission_reader)
        if not isinstance(executor, SingleRoleCodexExecutor):
            raise ParallelCodexExecutionError(
                "INVALID_EXECUTOR", "factory must return SingleRoleCodexExecutor"
            )
        request_id = _request_id(request_id_prefix, context.assignment_id)
        outcome = executor.execute(
            context.handoff,
            request_id=request_id,
            artifacts=SingleRoleArtifacts(),
            cancellation=cancellation,
        )
        if (
            outcome.request_id != request_id
            or outcome.role is not MissionRole.IMPLEMENTER
            or not outcome.execution_id
        ):
            raise ParallelCodexExecutionError(
                "EXECUTION_BINDING_MISMATCH",
                "SingleRole execution outcome differs from the member binding",
            )
        result = (
            outcome.validated_result
            if isinstance(outcome.validated_result, ImplementerResult)
            else None
        )
        ready = result is not None and result.verdict is ImplementerVerdict.READY_FOR_TEST
        blockers = outcome.blockers
        if outcome.validated and result is None:
            blockers = (*blockers, "ROLE_RESULT_MISMATCH")
        elif result is not None and not ready:
            blockers = (*blockers, "IMPLEMENTER_NOT_READY_FOR_TEST")
        return ParallelCodexMemberExecution(
            user_story_id=context.user_story_id,
            assignment_id=context.assignment_id,
            request_id=request_id,
            execution_id=outcome.execution_id,
            execution_outcome=outcome,
            implementer_result=result,
            ready_for_test=ready,
            blockers=blockers,
        )


class _BoundImplementerMissionReader:
    """Read-only member projection of the current canonical P3 mission authority."""

    def __init__(
        self,
        source: MissionStateReader,
        expected: MissionState,
        context: PreparedImplementerContext,
    ) -> None:
        self._source = source
        self._expected = expected
        self._context = context

    def load(self) -> object:
        current = self._source.load()
        if not isinstance(current, MissionState) or current != self._expected:
            raise ParallelCodexExecutionError(
                "MISSION_STATE_MISMATCH", "mission changed after group validation"
            )
        return replace(
            current,
            role=MissionRole.IMPLEMENTER,
            subject=self._context.user_story_id,
            operating_step=OperatingStep.ACT,
            next_action=self._context.handoff.instructions,
            blockers=list(current.blockers),
        )


def _request_id(prefix: str, assignment_id: str) -> str:
    return f"{prefix.strip()}/{assignment_id}"


def _failed_member(
    context: PreparedImplementerContext,
    request_id: str,
    error: Exception,
) -> ParallelCodexMemberExecution:
    code = getattr(error, "code", type(error).__name__)
    return ParallelCodexMemberExecution(
        user_story_id=context.user_story_id,
        assignment_id=context.assignment_id,
        request_id=request_id,
        execution_id=None,
        execution_outcome=None,
        implementer_result=None,
        ready_for_test=False,
        blockers=(f"{code}: {error}",),
    )
