"""Minimal bridge from validated Codex executions to certified P2/P3 workflows."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from threading import Event
from typing import Protocol

from agentic_engineering_os.domain import (
    MissionRole,
    MissionState,
    MissionStatus,
)

from .architect import ArchitectResult
from .certification_service import AcceptanceResult, CertificationContext
from .certifier import CertifierResult
from .implementer import ImplementerInput, ImplementerResult
from .orchestrator import RoleHandoff
from .parallel_codex_implementers import (
    ParallelCodexGroupExecution,
    ParallelCodexImplementerExecutor,
)
from .parallel_implementer_coordinator import (
    ParallelGroupResult,
    PreparedParallelGroup,
)
from .parallel_mission_workflow import (
    ParallelMissionPlan,
    ParallelMissionWorkflow,
    ParallelStoryDossier,
)
from .reviewer import ReviewerResult
from .sequential_mission_workflow import SequentialMissionResult, SequentialMissionWorkflow
from .single_role_codex import (
    SingleRoleArtifacts,
    SingleRoleCodexExecutor,
    SingleRoleExecutionOutcome,
)
from .tester import TesterResult


@dataclass(frozen=True, slots=True)
class ControlPlaneSubmission:
    """Explicit non-Codex inputs required by the authoritative certification boundary."""

    acceptance_results: tuple[AcceptanceResult, ...]
    certification_context: CertificationContext
    certifier: str
    current_commit: str
    authorized_not_applicable_gate_ids: frozenset[str] = frozenset()
    certification_id: str | None = None


@dataclass(frozen=True, slots=True)
class SequentialCodexRuntimeResult:
    execution: SingleRoleExecutionOutcome
    workflow: SequentialMissionResult | None

    @property
    def handed_off(self) -> bool:
        return self.workflow is not None


@dataclass(frozen=True, slots=True)
class ParallelCodexRuntimeResult:
    execution: ParallelCodexGroupExecution
    group_result: ParallelGroupResult | None

    @property
    def handed_off(self) -> bool:
        return self.group_result is not None


@dataclass(frozen=True, slots=True)
class ParallelDossierCodexRuntimeResult:
    execution: SingleRoleExecutionOutcome
    dossier: ParallelStoryDossier | None

    @property
    def handed_off(self) -> bool:
        return self.dossier is not None


class _MissionReader(Protocol):
    def load(self) -> object: ...


class ParallelDossierExecutorFactory(Protocol):
    def create(
        self,
        handoff: RoleHandoff,
        mission_store: _MissionReader,
    ) -> SingleRoleCodexExecutor: ...


class CodexEndToEndRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class CodexEndToEndRuntime:
    """Replace manual RoleResult transport while leaving all decisions to P2/P3."""

    def __init__(
        self,
        *,
        single_executor: SingleRoleCodexExecutor,
        sequential_workflow: SequentialMissionWorkflow,
        parallel_executor: ParallelCodexImplementerExecutor | None = None,
        parallel_workflow: ParallelMissionWorkflow | None = None,
        parallel_mission_store: _MissionReader | None = None,
        dossier_executor_factory: ParallelDossierExecutorFactory | None = None,
    ) -> None:
        self._single = single_executor
        self._sequential = sequential_workflow
        self._parallel = parallel_executor
        self._parallel_workflow = parallel_workflow
        self._parallel_mission_store = parallel_mission_store
        self._dossier_factory = dossier_executor_factory

    def execute_sequential_role(
        self,
        handoff: RoleHandoff,
        *,
        request_id: str,
        artifacts: SingleRoleArtifacts = SingleRoleArtifacts(),
        updated_at: datetime,
        control_plane: ControlPlaneSubmission | None = None,
        cancellation: Event | None = None,
    ) -> SequentialCodexRuntimeResult:
        """Execute one canonical handoff and submit only a validated result."""

        execution = self._single.execute(
            handoff,
            request_id=request_id,
            artifacts=artifacts,
            cancellation=cancellation,
        )
        if not execution.validated or execution.validated_result is None:
            return SequentialCodexRuntimeResult(execution, None)
        if execution.role is not handoff.to_role:
            raise CodexEndToEndRuntimeError(
                "ROLE_RESULT_BINDING_MISMATCH",
                "execution role differs from the authoritative handoff",
            )
        result = execution.validated_result
        role = handoff.to_role
        if role is MissionRole.ARCHITECT and isinstance(result, ArchitectResult):
            workflow = self._sequential.accept_architect(
                handoff, result, updated_at=updated_at
            )
        elif role is MissionRole.IMPLEMENTER and isinstance(result, ImplementerResult):
            workflow = self._sequential.accept_implementer(
                handoff, result, updated_at=updated_at
            )
        elif role is MissionRole.TESTER and isinstance(result, TesterResult):
            if artifacts.implementer_result is None:
                raise CodexEndToEndRuntimeError(
                    "UPSTREAM_RESULT_MISSING", "Tester workflow requires ImplementerResult"
                )
            workflow = self._sequential.accept_tester(
                handoff,
                result,
                implementer_result=artifacts.implementer_result,
                updated_at=updated_at,
            )
        elif role is MissionRole.REVIEWER and isinstance(result, ReviewerResult):
            if artifacts.implementer_result is None or artifacts.tester_result is None:
                raise CodexEndToEndRuntimeError(
                    "UPSTREAM_RESULT_MISSING",
                    "Reviewer workflow requires ImplementerResult and TesterResult",
                )
            workflow = self._sequential.accept_reviewer(
                handoff,
                result,
                implementer_result=artifacts.implementer_result,
                tester_result=artifacts.tester_result,
                updated_at=updated_at,
            )
        elif role is MissionRole.CERTIFIER and isinstance(result, CertifierResult):
            if (
                artifacts.architect_result is None
                or artifacts.implementer_result is None
                or artifacts.tester_result is None
                or artifacts.reviewer_result is None
            ):
                raise CodexEndToEndRuntimeError(
                    "UPSTREAM_RESULT_MISSING", "Certifier workflow requires the full role chain"
                )
            if control_plane is None:
                raise CodexEndToEndRuntimeError(
                    "CONTROL_PLANE_INPUT_MISSING",
                    "Codex output cannot supply authoritative certification inputs",
                )
            workflow = self._sequential.submit_control_plane(
                handoff,
                result,
                architect_result=artifacts.architect_result,
                implementer_result=artifacts.implementer_result,
                tester_result=artifacts.tester_result,
                reviewer_result=artifacts.reviewer_result,
                acceptance_results=control_plane.acceptance_results,
                certification_context=control_plane.certification_context,
                certifier=control_plane.certifier,
                current_commit=control_plane.current_commit,
                updated_at=updated_at,
                authorized_not_applicable_gate_ids=(
                    control_plane.authorized_not_applicable_gate_ids
                ),
                certification_id=control_plane.certification_id,
            )
        else:
            raise CodexEndToEndRuntimeError(
                "ROLE_RESULT_TYPE_MISMATCH",
                "validated RoleResult type differs from the authoritative role",
            )
        return SequentialCodexRuntimeResult(execution, workflow)

    def execute_parallel_implementers(
        self,
        plan: ParallelMissionPlan,
        prepared_group: PreparedParallelGroup,
        *,
        request_id_prefix: str,
        cancellation: Event | None = None,
    ) -> ParallelCodexRuntimeResult:
        """Execute a P3-prepared SAFE group, then return it to the P3 workflow."""

        if self._parallel is None or self._parallel_workflow is None:
            raise CodexEndToEndRuntimeError(
                "PARALLEL_RUNTIME_UNAVAILABLE", "parallel P4.9/P3 services are not configured"
            )
        if not isinstance(plan, ParallelMissionPlan):
            raise CodexEndToEndRuntimeError(
                "INVALID_PARALLEL_PLAN", "canonical ParallelMissionPlan is required"
            )
        execution = self._parallel.execute_group(
            plan.execution_plan,
            prepared_group,
            coordination_input=plan.coordination_input,
            request_id_prefix=request_id_prefix,
            cancellation=cancellation,
        )
        if not isinstance(execution, ParallelCodexGroupExecution):
            raise CodexEndToEndRuntimeError(
                "INVALID_PARALLEL_EXECUTION",
                "P4.9 must return ParallelCodexGroupExecution",
            )
        if not execution.successful:
            return ParallelCodexRuntimeResult(execution, None)

        contexts = {item.assignment_id: item for item in prepared_group.contexts}
        stories = {item.id: item for item in plan.coordination_input.project_state.user_stories}
        observed_assignments = tuple(item.assignment_id for item in execution.members)
        if (
            len(contexts) != len(prepared_group.contexts)
            or len(observed_assignments) != len(set(observed_assignments))
            or set(observed_assignments) != set(contexts)
        ):
            raise CodexEndToEndRuntimeError(
                "PARALLEL_RESULT_SET_MISMATCH",
                "parallel execution must return the exact prepared assignment set",
            )
        member_results = []
        for member in execution.members:
            context = contexts.get(member.assignment_id)
            story = stories.get(member.user_story_id)
            if (
                context is None
                or story is None
                or context.user_story_id != member.user_story_id
                or member.implementer_result is None
            ):
                raise CodexEndToEndRuntimeError(
                    "PARALLEL_RESULT_BINDING_MISMATCH",
                    "parallel result does not match its prepared assignment and story",
                )
            member_results.append(
                self._parallel_workflow.submit_member(
                    prepared_group,
                    member.assignment_id,
                    member.implementer_result,
                    implementer_input=ImplementerInput.from_handoff(
                        context.handoff, story
                    ),
                )
            )
        group_result = self._parallel_workflow.complete_group(
            prepared_group, tuple(member_results)
        )
        return ParallelCodexRuntimeResult(execution, group_result)

    def execute_parallel_dossier_role(
        self,
        dossier: ParallelStoryDossier,
        role: MissionRole,
        *,
        request_id: str,
        artifacts: SingleRoleArtifacts,
        updated_at: datetime,
        architect_result: ArchitectResult | None = None,
        control_plane: ControlPlaneSubmission | None = None,
        cancellation: Event | None = None,
    ) -> ParallelDossierCodexRuntimeResult:
        """Execute the canonical Tester/Reviewer/Certifier stage after P3 merge."""

        if (
            self._parallel_workflow is None
            or self._parallel_mission_store is None
            or self._dossier_factory is None
        ):
            raise CodexEndToEndRuntimeError(
                "PARALLEL_DOSSIER_RUNTIME_UNAVAILABLE",
                "post-merge P3 runtime services are not configured",
            )
        handoff = self._parallel_workflow.runtime_handoff(dossier, role)
        mission_reader = _BoundParallelDossierMissionReader(
            self._parallel_mission_store, handoff
        )
        executor = self._dossier_factory.create(handoff, mission_reader)
        if not isinstance(executor, SingleRoleCodexExecutor):
            raise CodexEndToEndRuntimeError(
                "INVALID_DOSSIER_EXECUTOR",
                "factory must return SingleRoleCodexExecutor",
            )
        execution = executor.execute(
            handoff,
            request_id=request_id,
            artifacts=artifacts,
            cancellation=cancellation,
        )
        if not execution.validated or execution.validated_result is None:
            return ParallelDossierCodexRuntimeResult(execution, None)
        if execution.role is not role:
            raise CodexEndToEndRuntimeError(
                "ROLE_RESULT_BINDING_MISMATCH",
                "execution role differs from the authoritative dossier handoff",
            )
        result = execution.validated_result
        if role is MissionRole.TESTER and isinstance(result, TesterResult):
            updated = self._parallel_workflow.accept_tester(dossier, result)
        elif role is MissionRole.REVIEWER and isinstance(result, ReviewerResult):
            updated = self._parallel_workflow.accept_reviewer(dossier, result)
        elif role is MissionRole.CERTIFIER and isinstance(result, CertifierResult):
            if architect_result is None or control_plane is None:
                raise CodexEndToEndRuntimeError(
                    "CONTROL_PLANE_INPUT_MISSING",
                    "parallel Certifier requires ArchitectResult and external control inputs",
                )
            updated = self._parallel_workflow.submit_certifier(
                dossier,
                result,
                architect_result=architect_result,
                acceptance_results=control_plane.acceptance_results,
                certification_context=control_plane.certification_context,
                certifier=control_plane.certifier,
                updated_at=updated_at,
                certification_id=control_plane.certification_id,
                authorized_not_applicable_gate_ids=(
                    control_plane.authorized_not_applicable_gate_ids
                ),
            )
        else:
            raise CodexEndToEndRuntimeError(
                "ROLE_RESULT_TYPE_MISMATCH",
                "validated RoleResult type differs from the dossier role",
            )
        return ParallelDossierCodexRuntimeResult(execution, updated)


class _BoundParallelDossierMissionReader:
    """Read-only role projection of current P3 authority for P4 context building."""

    def __init__(self, source: _MissionReader, handoff: RoleHandoff) -> None:
        self._source = source
        self._handoff = handoff

    def load(self) -> object:
        mission = self._source.load()
        handoff = self._handoff
        if not isinstance(mission, MissionState) or (
            mission.mission_id != handoff.mission_id
            or mission.workflow_generation != handoff.workflow_generation
            or mission.observed_commit != handoff.observed_commit
            or mission.status is not MissionStatus.ACTIVE
            or mission.role is not MissionRole.ORCHESTRATOR
            or mission.blockers
        ):
            raise CodexEndToEndRuntimeError(
                "MISSION_STATE_MISMATCH",
                "current P3 mission no longer authorizes the dossier handoff",
            )
        return replace(
            mission,
            role=handoff.to_role,
            subject=handoff.subject,
            operating_step=handoff.operating_step,
            next_action=handoff.instructions,
            blockers=[],
        )
