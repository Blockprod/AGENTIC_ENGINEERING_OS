"""Deterministic coordination of isolated Implementer execution groups."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from agentic_engineering_os.domain import (
    ConflictAnalysis,
    ConflictClassification,
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    ParallelExecutionGroup,
    ParallelExecutionPlan,
    ProjectState,
    UserStory,
    WavePlan,
    WorktreeAssignment,
    WorktreeRegistry,
    WorktreeStatus,
    to_dict,
)

from .contract_validator import ContractValidator
from .execution_conflict_analyzer import ExecutionConflictAnalyzer
from .implementer import (
    ImplementerInput,
    ImplementerResult,
    ImplementerResultValidator,
    ImplementerVerdict,
)
from .orchestrator import RoleHandoff


_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class WorktreeManagerPort(Protocol):
    @property
    def registry_store(self) -> object: ...

    def current_primary_commit(self) -> str: ...
    def plan_assignment(
        self, *, mission: MissionState, user_story: UserStory, baseline_commit: str
    ) -> WorktreeAssignment: ...
    def activate(self, assignment_id: str, *, current_generation: int) -> WorktreeAssignment: ...
    def resume(self, assignment_id: str, *, current_generation: int) -> object: ...
    def complete(self, assignment_id: str, *, current_generation: int) -> WorktreeAssignment: ...
    def mark_failed(self, assignment_id: str, *, current_generation: int) -> WorktreeAssignment: ...


@dataclass(frozen=True, slots=True)
class ParallelCoordinationInput:
    mission_id: str
    workflow_generation: int
    wave_index: int
    wave_plan: WavePlan
    conflict_analysis: ConflictAnalysis
    project_state: ProjectState
    mission_state: MissionState
    baseline_commit: str


@dataclass(frozen=True, slots=True)
class PreparedImplementerContext:
    assignment_id: str
    user_story_id: str
    worktree_path: str
    branch_name: str
    baseline_commit: str
    workflow_generation: int
    handoff: RoleHandoff


@dataclass(frozen=True, slots=True)
class PreparedParallelGroup:
    group_index: int
    wave_index: int
    user_story_ids: tuple[str, ...]
    assignment_ids: tuple[str, ...]
    worktree_paths: tuple[str, ...]
    branch_names: tuple[str, ...]
    baseline_commit: str
    workflow_generation: int
    contexts: tuple[PreparedImplementerContext, ...]


@dataclass(frozen=True, slots=True)
class ParallelMemberResult:
    assignment_id: str
    user_story_id: str
    result_commit: str
    implementer_input: ImplementerInput
    implementer_result: ImplementerResult


class ParallelGroupStatus(str, Enum):
    PREPARED = "PREPARED"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ParallelGroupResult:
    group_index: int
    status: ParallelGroupStatus
    member_results: tuple[ParallelMemberResult, ...]
    assignment_ids: tuple[str, ...]
    result_commits: tuple[str, ...]


class ParallelCoordinationError(RuntimeError):
    """The requested parallel operation cannot be proven safe."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        subjects: tuple[str, ...] = (),
        prepared_assignment_ids: tuple[str, ...] = (),
    ) -> None:
        self.code = code
        self.message = message
        self.subjects = subjects
        self.prepared_assignment_ids = prepared_assignment_ids
        super().__init__(f"{code}: {message}")


class ParallelImplementerCoordinator:
    """Group compatible stories and delegate physical isolation to WorktreeManager."""

    def __init__(
        self,
        *,
        worktree_manager: WorktreeManagerPort,
        conflict_analyzer: ExecutionConflictAnalyzer | None = None,
        contract_validator: ContractValidator | None = None,
        result_validator: ImplementerResultValidator | None = None,
    ) -> None:
        self._manager = worktree_manager
        self._conflict_analyzer = conflict_analyzer or ExecutionConflictAnalyzer()
        self._contract_validator = contract_validator or ContractValidator()
        self._result_validator = result_validator or ImplementerResultValidator()

    def plan(self, coordination_input: ParallelCoordinationInput) -> ParallelExecutionPlan:
        canonical_analysis = self._validate_input(coordination_input)
        wave = next(
            (item for item in coordination_input.wave_plan.waves if item.wave_index == coordination_input.wave_index),
            None,
        )
        if wave is None:
            if coordination_input.wave_index == 0 and not coordination_input.wave_plan.waves:
                member_ids: tuple[str, ...] = ()
            else:
                raise ParallelCoordinationError("INVALID_WAVE", "target Wave is absent")
        else:
            first_index = coordination_input.wave_plan.waves[0].wave_index
            if coordination_input.wave_index != first_index:
                raise ParallelCoordinationError(
                    "FUTURE_WAVE", "only the current first canonical Wave may be coordinated"
                )
            member_ids = tuple(member.user_story_id for member in wave.members)

        classifications = _pair_classifications(canonical_analysis)
        groups: list[list[str]] = []
        for identifier in member_ids:
            for group in groups:
                if all(
                    classifications.get(_pair_key(identifier, other))
                    is ConflictClassification.SAFE
                    for other in group
                ):
                    group.append(identifier)
                    break
            else:
                groups.append([identifier])
        plan = ParallelExecutionPlan(
            mission_id=coordination_input.mission_id,
            workflow_generation=coordination_input.workflow_generation,
            baseline_commit=coordination_input.baseline_commit,
            wave_index=coordination_input.wave_index,
            groups=tuple(
                ParallelExecutionGroup(index, coordination_input.wave_index, tuple(group))
                for index, group in enumerate(groups)
            ),
            source_fingerprint=_fingerprint(coordination_input),
        )
        self._validate_plan(plan, member_ids, classifications)
        return plan

    def prepare_group(
        self,
        plan: ParallelExecutionPlan,
        group_index: int,
        *,
        coordination_input: ParallelCoordinationInput,
    ) -> PreparedParallelGroup:
        canonical = self.plan(coordination_input)
        if plan != canonical:
            raise ParallelCoordinationError("PLAN_STALE", "plan differs from current canonical context")
        group = _group(plan, group_index)
        self._require_prior_groups_complete(plan, group_index)
        if self._manager.current_primary_commit() != plan.baseline_commit:
            raise ParallelCoordinationError(
                "PLAN_STALE", "primary repository no longer matches the explicit baseline"
            )
        stories = {story.id: story for story in coordination_input.project_state.user_stories}
        active: list[WorktreeAssignment] = []
        try:
            for identifier in group.user_story_ids:
                assignment = self._existing_assignment(
                    mission_id=plan.mission_id,
                    user_story_id=identifier,
                    generation=plan.workflow_generation,
                    baseline=plan.baseline_commit,
                )
                if assignment is None:
                    assignment = self._manager.plan_assignment(
                        mission=coordination_input.mission_state,
                        user_story=stories[identifier],
                        baseline_commit=plan.baseline_commit,
                    )
                if assignment.status is WorktreeStatus.PLANNED:
                    assignment = self._manager.activate(
                        assignment.assignment_id,
                        current_generation=plan.workflow_generation,
                    )
                elif assignment.status is WorktreeStatus.ACTIVE:
                    self._manager.resume(
                        assignment.assignment_id,
                        current_generation=plan.workflow_generation,
                    )
                else:
                    raise ParallelCoordinationError(
                        "ASSIGNMENT_MISMATCH",
                        "group preparation requires PLANNED or ACTIVE assignments",
                        subjects=(assignment.assignment_id,),
                    )
                if assignment.status is not WorktreeStatus.ACTIVE:
                    raise ParallelCoordinationError(
                        "WORKTREE_PREPARATION_FAILED", "assignment did not become ACTIVE"
                    )
                active.append(assignment)
        except ParallelCoordinationError as error:
            if not active or error.prepared_assignment_ids:
                raise
            raise ParallelCoordinationError(
                error.code,
                error.message,
                subjects=error.subjects,
                prepared_assignment_ids=tuple(item.assignment_id for item in active),
            ) from error
        except Exception as error:
            raise ParallelCoordinationError(
                "WORKTREE_PREPARATION_FAILED",
                f"worktree preparation failed: {getattr(error, 'code', type(error).__name__)}",
                prepared_assignment_ids=tuple(item.assignment_id for item in active),
            ) from error

        contexts = tuple(
            PreparedImplementerContext(
                assignment_id=item.assignment_id,
                user_story_id=item.user_story_id,
                worktree_path=item.worktree_path,
                branch_name=item.branch_name,
                baseline_commit=item.baseline_commit,
                workflow_generation=item.workflow_generation,
                handoff=RoleHandoff(
                    from_role=MissionRole.ORCHESTRATOR,
                    to_role=MissionRole.IMPLEMENTER,
                    mission_id=plan.mission_id,
                    workflow_generation=plan.workflow_generation,
                    subject=item.user_story_id,
                    objective=coordination_input.mission_state.objective,
                    observed_commit=plan.baseline_commit,
                    operating_step=OperatingStep.ACT,
                    blockers=(),
                    instructions=(
                        "Implement only the assigned User Story scope in the assigned "
                        f"worktree {item.worktree_path}. This handoff grants no Control Plane authority."
                    ),
                ),
            )
            for item in active
        )
        return PreparedParallelGroup(
            group_index=group.group_index,
            wave_index=group.wave_index,
            user_story_ids=group.user_story_ids,
            assignment_ids=tuple(item.assignment_id for item in active),
            worktree_paths=tuple(item.worktree_path for item in active),
            branch_names=tuple(item.branch_name for item in active),
            baseline_commit=plan.baseline_commit,
            workflow_generation=plan.workflow_generation,
            contexts=contexts,
        )

    def submit_result(
        self,
        prepared_group: PreparedParallelGroup,
        assignment_id: str,
        result: ImplementerResult | Mapping[str, object],
        *,
        implementer_input: ImplementerInput,
        current_mission: MissionState,
    ) -> ParallelMemberResult:
        self._validate_prepared_structure(prepared_group)
        context = next(
            (item for item in prepared_group.contexts if item.assignment_id == assignment_id),
            None,
        )
        if context is None:
            raise ParallelCoordinationError("ASSIGNMENT_MISMATCH", "assignment is not in prepared group")
        self._validate_current_mission(current_mission, prepared_group)
        self._assignment_for_context(context, expected_status=WorktreeStatus.ACTIVE)
        if (
            implementer_input.mission_id != current_mission.mission_id
            or implementer_input.workflow_generation != prepared_group.workflow_generation
            or implementer_input.user_story.id != context.user_story_id
            or implementer_input.observed_commit.casefold() != prepared_group.baseline_commit
        ):
            raise ParallelCoordinationError("ASSIGNMENT_MISMATCH", "ImplementerInput differs from worktree context")
        try:
            self._manager.resume(assignment_id, current_generation=prepared_group.workflow_generation)
        except Exception as error:
            raise ParallelCoordinationError(
                "ASSIGNMENT_MISMATCH",
                f"assignment/worktree is not resumable: {getattr(error, 'code', type(error).__name__)}",
            ) from error
        validation = self._result_validator.validate(result, implementer_input=implementer_input)
        if not validation.is_valid:
            raise ParallelCoordinationError(
                "INVALID_IMPLEMENTER_RESULT",
                "ImplementerResult failed deterministic validation",
                subjects=tuple(sorted({issue.code for issue in validation.errors})),
            )
        if not isinstance(result, ImplementerResult):
            raise ParallelCoordinationError(
                "INVALID_IMPLEMENTER_RESULT", "submission requires a typed ImplementerResult"
            )
        if result.verdict is not ImplementerVerdict.READY_FOR_TEST:
            raise ParallelCoordinationError(
                "INVALID_IMPLEMENTER_RESULT",
                "only READY_FOR_TEST can proceed to Git completion",
            )
        try:
            completed = self._manager.complete(
                assignment_id, current_generation=prepared_group.workflow_generation
            )
        except Exception as error:
            raise ParallelCoordinationError(
                "GROUP_INCOMPLETE",
                f"Git completion could not be proven: {getattr(error, 'code', type(error).__name__)}",
            ) from error
        if completed.result_commit is None or completed.status is not WorktreeStatus.COMPLETED:
            raise ParallelCoordinationError("GROUP_INCOMPLETE", "completion lacks an observed result commit")
        return ParallelMemberResult(
            assignment_id=assignment_id,
            user_story_id=context.user_story_id,
            result_commit=completed.result_commit,
            implementer_input=implementer_input,
            implementer_result=result,
        )

    def complete_group(
        self,
        prepared_group: PreparedParallelGroup,
        member_results: tuple[ParallelMemberResult, ...],
    ) -> ParallelGroupResult:
        self._validate_prepared_structure(prepared_group)
        if not isinstance(member_results, tuple) or not all(
            isinstance(item, ParallelMemberResult) for item in member_results
        ):
            raise ParallelCoordinationError(
                "GROUP_INCOMPLETE", "typed member results are required"
            )
        expected = prepared_group.assignment_ids
        actual = tuple(item.assignment_id for item in member_results)
        if len(set(actual)) != len(actual) or set(actual) != set(expected):
            raise ParallelCoordinationError("GROUP_INCOMPLETE", "every assignment needs exactly one validated result")
        registry = self._registry()
        by_id = {item.assignment_id: item for item in registry.assignments}
        for item in member_results:
            assignment = by_id.get(item.assignment_id)
            context = next(
                candidate
                for candidate in prepared_group.contexts
                if candidate.assignment_id == item.assignment_id
            )
            validation = self._result_validator.validate(
                item.implementer_result,
                implementer_input=item.implementer_input,
            )
            if (
                assignment is None
                or assignment.status is not WorktreeStatus.COMPLETED
                or assignment.result_commit != item.result_commit
                or assignment.user_story_id != item.user_story_id
                or assignment.workflow_generation != prepared_group.workflow_generation
                or assignment.mission_id != context.handoff.mission_id
                or assignment.baseline_commit != context.baseline_commit
                or assignment.branch_name != context.branch_name
                or assignment.worktree_path != context.worktree_path
                or item.implementer_input.user_story.id != context.user_story_id
                or item.implementer_result.verdict is not ImplementerVerdict.READY_FOR_TEST
                or not validation.is_valid
            ):
                raise ParallelCoordinationError("GROUP_INCOMPLETE", "registry does not prove every member completion")
        ordered = tuple(sorted(member_results, key=lambda item: expected.index(item.assignment_id)))
        return ParallelGroupResult(
            group_index=prepared_group.group_index,
            status=ParallelGroupStatus.COMPLETED,
            member_results=ordered,
            assignment_ids=expected,
            result_commits=tuple(item.result_commit for item in ordered),
        )

    def fail_member(
        self,
        prepared_group: PreparedParallelGroup,
        assignment_id: str,
        *,
        current_mission: MissionState,
    ) -> ParallelGroupResult:
        self._validate_prepared_structure(prepared_group)
        self._validate_current_mission(current_mission, prepared_group)
        if assignment_id not in prepared_group.assignment_ids:
            raise ParallelCoordinationError("ASSIGNMENT_MISMATCH", "assignment is not in prepared group")
        context = next(
            item for item in prepared_group.contexts if item.assignment_id == assignment_id
        )
        self._assignment_for_context(context, expected_status=WorktreeStatus.ACTIVE)
        try:
            self._manager.mark_failed(
                assignment_id, current_generation=prepared_group.workflow_generation
            )
        except Exception as error:
            raise ParallelCoordinationError(
                "GROUP_INCOMPLETE", f"failure could not be recorded: {getattr(error, 'code', type(error).__name__)}"
            ) from error
        return ParallelGroupResult(
            group_index=prepared_group.group_index,
            status=ParallelGroupStatus.FAILED,
            member_results=(),
            assignment_ids=prepared_group.assignment_ids,
            result_commits=(),
        )

    def _validate_input(self, value: ParallelCoordinationInput) -> ConflictAnalysis:
        if not isinstance(value, ParallelCoordinationInput):
            raise ParallelCoordinationError("INVALID_INPUT", "ParallelCoordinationInput is required")
        mission = value.mission_state
        if not isinstance(mission, MissionState):
            raise ParallelCoordinationError("MISSION_STATE_MISMATCH", "MissionState is required")
        try:
            mission_validation = self._contract_validator.validate(
                "mission-state", to_dict(mission)
            )
        except Exception as error:
            raise ParallelCoordinationError(
                "MISSION_STATE_MISMATCH",
                f"MissionState validation failed: {type(error).__name__}",
            ) from error
        if (
            not mission_validation.is_valid
            or not isinstance(value.workflow_generation, int)
            or isinstance(value.workflow_generation, bool)
            or value.workflow_generation < 0
            or value.mission_id != mission.mission_id
            or value.workflow_generation != mission.workflow_generation
            or mission.status is not MissionStatus.ACTIVE
            or mission.role is not MissionRole.ORCHESTRATOR
            or mission.operating_step is not OperatingStep.ACT
            or mission.blockers
        ):
            raise ParallelCoordinationError("MISSION_STATE_MISMATCH", "MissionState does not authorize coordination")
        baseline = value.baseline_commit.casefold() if isinstance(value.baseline_commit, str) else ""
        if not _COMMIT_PATTERN.fullmatch(baseline) or baseline != value.baseline_commit:
            raise ParallelCoordinationError("BASELINE_MISMATCH", "baseline must be a lowercase full Git SHA")
        if mission.observed_commit.casefold() != baseline:
            raise ParallelCoordinationError("BASELINE_MISMATCH", "MissionState observed_commit differs from baseline")
        if not isinstance(value.wave_index, int) or isinstance(value.wave_index, bool) or value.wave_index < 0:
            raise ParallelCoordinationError("INVALID_WAVE", "wave_index must be a non-negative integer")
        try:
            canonical = self._conflict_analyzer.analyze(value.wave_plan, value.project_state)
        except Exception as error:
            raise ParallelCoordinationError(
                "PLAN_STALE", f"canonical reconstruction failed: {getattr(error, 'code', type(error).__name__)}"
            ) from error
        if value.conflict_analysis != canonical:
            raise ParallelCoordinationError("CONFLICT_ANALYSIS_MISMATCH", "ConflictAnalysis is not canonical")
        return canonical

    def _validate_plan(
        self,
        plan: ParallelExecutionPlan,
        member_ids: tuple[str, ...],
        classifications: dict[tuple[str, str], ConflictClassification],
    ) -> None:
        flattened = tuple(identifier for group in plan.groups for identifier in group.user_story_ids)
        if len(set(flattened)) != len(flattened) or set(flattened) != set(member_ids):
            raise ParallelCoordinationError("INVALID_PLAN", "Wave members are omitted or duplicated")
        for expected_index, group in enumerate(plan.groups):
            if group.group_index != expected_index or group.wave_index != plan.wave_index:
                raise ParallelCoordinationError("INVALID_PLAN", "group indexes are not canonical")
            for index, left in enumerate(group.user_story_ids):
                for right in group.user_story_ids[index + 1 :]:
                    if classifications.get(_pair_key(left, right)) is not ConflictClassification.SAFE:
                        raise ParallelCoordinationError(
                            "CONFLICT_NOT_SAFE", "group contains a pair not proven SAFE", subjects=(left, right)
                        )
        validation = self._contract_validator.validate("parallel-execution-plan", to_dict(plan))
        if not validation.is_valid:
            raise ParallelCoordinationError("INVALID_PLAN", "ParallelExecutionPlan violates its schema")

    def _require_prior_groups_complete(self, plan: ParallelExecutionPlan, group_index: int) -> None:
        if group_index == 0:
            return
        registry = self._registry()
        by_story = {
            item.user_story_id: item
            for item in registry.assignments
            if item.mission_id == plan.mission_id
            and item.workflow_generation == plan.workflow_generation
            and item.baseline_commit == plan.baseline_commit
        }
        prior_ids = tuple(
            identifier
            for group in plan.groups[:group_index]
            for identifier in group.user_story_ids
        )
        if any(
            identifier not in by_story
            or by_story[identifier].status is not WorktreeStatus.COMPLETED
            or by_story[identifier].result_commit is None
            for identifier in prior_ids
        ):
            raise ParallelCoordinationError("GROUP_INCOMPLETE", "prior execution groups are not completed")

    def _existing_assignment(
        self, *, mission_id: str, user_story_id: str, generation: int, baseline: str
    ) -> WorktreeAssignment | None:
        matches = tuple(
            item
            for item in self._registry().assignments
            if item.mission_id == mission_id
            and item.user_story_id == user_story_id
            and item.workflow_generation == generation
            and item.baseline_commit == baseline
            and item.status is not WorktreeStatus.CLEANED
        )
        if len(matches) > 1:
            raise ParallelCoordinationError("ASSIGNMENT_MISMATCH", "multiple assignments match one story")
        return matches[0] if matches else None

    def _assignment_for_context(
        self,
        context: PreparedImplementerContext,
        *,
        expected_status: WorktreeStatus,
    ) -> WorktreeAssignment:
        matches = tuple(
            item
            for item in self._registry().assignments
            if item.assignment_id == context.assignment_id
        )
        if len(matches) != 1:
            raise ParallelCoordinationError("ASSIGNMENT_MISMATCH", "assignment is absent or ambiguous")
        assignment = matches[0]
        if (
            assignment.status is not expected_status
            or assignment.user_story_id != context.user_story_id
            or assignment.mission_id != context.handoff.mission_id
            or assignment.workflow_generation != context.workflow_generation
            or assignment.baseline_commit != context.baseline_commit
            or assignment.branch_name != context.branch_name
            or assignment.worktree_path != context.worktree_path
        ):
            raise ParallelCoordinationError("ASSIGNMENT_MISMATCH", "prepared context differs from registry")
        return assignment

    @staticmethod
    def _validate_prepared_structure(group: PreparedParallelGroup) -> None:
        if not isinstance(group, PreparedParallelGroup):
            raise ParallelCoordinationError("ASSIGNMENT_MISMATCH", "PreparedParallelGroup is required")
        contexts = group.contexts
        if (
            not isinstance(group.group_index, int)
            or isinstance(group.group_index, bool)
            or group.group_index < 0
            or not isinstance(group.wave_index, int)
            or isinstance(group.wave_index, bool)
            or group.wave_index < 0
            or not isinstance(group.workflow_generation, int)
            or isinstance(group.workflow_generation, bool)
            or group.workflow_generation < 0
            or not _COMMIT_PATTERN.fullmatch(group.baseline_commit)
            or not all(isinstance(item, PreparedImplementerContext) for item in contexts)
            or not contexts
            or len(set(group.user_story_ids)) != len(group.user_story_ids)
            or len(set(group.assignment_ids)) != len(group.assignment_ids)
            or len(contexts) != len(group.user_story_ids)
            or tuple(item.user_story_id for item in contexts) != group.user_story_ids
            or tuple(item.assignment_id for item in contexts) != group.assignment_ids
            or tuple(item.worktree_path for item in contexts) != group.worktree_paths
            or tuple(item.branch_name for item in contexts) != group.branch_names
            or any(item.baseline_commit != group.baseline_commit for item in contexts)
            or any(item.workflow_generation != group.workflow_generation for item in contexts)
        ):
            raise ParallelCoordinationError("ASSIGNMENT_MISMATCH", "prepared group is structurally inconsistent")

    def _registry(self) -> WorktreeRegistry:
        try:
            store = self._manager.registry_store
            registry = store.load()  # type: ignore[attr-defined]
        except Exception as error:
            raise ParallelCoordinationError(
                "ASSIGNMENT_MISMATCH", f"worktree registry unavailable: {getattr(error, 'code', type(error).__name__)}"
            ) from error
        if not isinstance(registry, WorktreeRegistry):
            raise ParallelCoordinationError("ASSIGNMENT_MISMATCH", "worktree registry is invalid")
        return registry

    @staticmethod
    def _validate_current_mission(mission: MissionState, group: PreparedParallelGroup) -> None:
        expected_mission = group.contexts[0].handoff.mission_id if group.contexts else None
        if not isinstance(mission, MissionState) or (
            expected_mission is not None and mission.mission_id != expected_mission
        ):
            raise ParallelCoordinationError("MISSION_STATE_MISMATCH", "mission differs from prepared group")
        if (
            not isinstance(mission.workflow_generation, int)
            or isinstance(mission.workflow_generation, bool)
            or mission.workflow_generation != group.workflow_generation
            or mission.status is not MissionStatus.ACTIVE
            or not isinstance(mission.observed_commit, str)
            or mission.observed_commit.casefold() != group.baseline_commit
        ):
            raise ParallelCoordinationError("MISSION_STATE_MISMATCH", "mission generation or baseline is stale")


def _group(plan: ParallelExecutionPlan, index: object) -> ParallelExecutionGroup:
    if not isinstance(index, int) or isinstance(index, bool) or index < 0 or index >= len(plan.groups):
        raise ParallelCoordinationError("INVALID_GROUP", "group_index is absent")
    group = plan.groups[index]
    if group.group_index != index:
        raise ParallelCoordinationError("INVALID_PLAN", "group index is inconsistent")
    return group


def _pair_key(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left < right else (right, left)


def _pair_classifications(analysis: ConflictAnalysis) -> dict[tuple[str, str], ConflictClassification]:
    result: dict[tuple[str, str], ConflictClassification] = {}
    for pair in analysis.pairs:
        key = _pair_key(pair.left_user_story_id, pair.right_user_story_id)
        if key in result:
            raise ParallelCoordinationError("CONFLICT_ANALYSIS_MISMATCH", "duplicate conflict pair")
        result[key] = pair.classification
    return result


def _fingerprint(value: ParallelCoordinationInput) -> str:
    payload = {
        "mission_id": value.mission_id,
        "workflow_generation": value.workflow_generation,
        "wave_index": value.wave_index,
        "baseline_commit": value.baseline_commit,
        "wave_plan": to_dict(value.wave_plan),
        "conflict_analysis": to_dict(value.conflict_analysis),
        "project_state": to_dict(value.project_state),
        "mission_state": to_dict(value.mission_state),
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
