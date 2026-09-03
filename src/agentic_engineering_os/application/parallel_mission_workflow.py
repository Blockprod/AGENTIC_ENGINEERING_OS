"""End-to-end coordination of certified Phase 2 and Phase 3 components."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Protocol

from agentic_engineering_os._authoritative_write import _issue_authoritative_write
from agentic_engineering_os.domain import (
    CertificationResult,
    ConflictAnalysis,
    DAGSnapshot,
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    ParallelExecutionPlan,
    ProjectState,
    ReadinessSnapshot,
    UserStory,
    UserStoryStatus,
    WavePlan,
    WorktreeStatus,
    to_dict,
)
from agentic_engineering_os.infrastructure._negative_outcome_store import (
    _NegativeOutcomeStore,
    _fingerprint,
)
from agentic_engineering_os.infrastructure.project_state_store import PersistenceError
from agentic_engineering_os.infrastructure.worktree_manager import WorktreeManager

from .architect import ArchitectResult
from .certification_service import AcceptanceResult, CertificationContext
from .certifier import (
    CertifierInput,
    CertifierResult,
    CertifierResultValidator,
    CertifierVerdict,
)
from .control_loop import ControlLoop, _candidate_state, _candidate_story
from .dag_validator import DAGValidator
from .evidence_recorder import EvidenceObservation
from .execution_conflict_analyzer import ExecutionConflictAnalyzer
from .gate_evaluator import GateContract, GateEvaluation, GateEvaluationContext
from .implementer import (
    ImplementerInput,
    ImplementerResult,
    ImplementerResultValidator,
    ImplementerVerdict,
)
from .integrated_story_context import IntegratedStoryContext
from .integration_gate import (
    IntegrationGate,
    IntegrationGateClassification,
    IntegrationGateContext,
    IntegrationGateResult,
)
from .merge_coordinator import (
    MergeContext,
    MergeCoordinationError,
    MergeCoordinator,
    MergeResult,
    MergeStatus,
)
from .orchestrator import RoleHandoff
from .parallel_implementer_coordinator import (
    ParallelCoordinationInput,
    ParallelGroupResult,
    ParallelGroupStatus,
    ParallelImplementerCoordinator,
    ParallelMemberResult,
    PreparedParallelGroup,
)
from .readiness_engine import ReadinessEngine
from .reviewer import (
    ReviewerInput,
    ReviewerResult,
    ReviewerResultValidator,
    ReviewerVerdict,
)
from .state_transition_service import TransitionContext
from .tester import (
    TesterInput,
    TesterResult,
    TesterResultValidator,
    TesterVerdict,
)
from .wave_planner import WavePlanner


class MissionStateStorePort(Protocol):
    def load(self) -> MissionState: ...

    def save(
        self,
        state: MissionState,
        *,
        authorization: object | None = None,
        operation: str | None = None,
    ) -> Path: ...


class ProjectStateStorePort(Protocol):
    def load(self) -> ProjectState: ...

    def save(
        self,
        state: ProjectState,
        *,
        authorization: object | None = None,
        operation: str | None = None,
    ) -> Path: ...


class ParallelStoryStage(str, Enum):
    TESTING = "TESTING"
    REVIEW = "REVIEW"
    CERTIFICATION = "CERTIFICATION"
    CERTIFIED = "CERTIFIED"
    BLOCKED = "BLOCKED"
    REMEDIATION_REQUIRED = "REMEDIATION_REQUIRED"


class ParallelRemediationStage(str, Enum):
    IMPLEMENTER = "IMPLEMENTER"
    INTEGRATION_GATE = "INTEGRATION_GATE"
    MERGE = "MERGE"
    TESTER = "TESTER"
    REVIEWER = "REVIEWER"
    CERTIFIER = "CERTIFIER"


class ParallelRecoveryStatus(str, Enum):
    READY = "READY"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class ParallelMissionPlan:
    mission_id: str
    workflow_generation: int
    baseline_commit: str
    dag: DAGSnapshot
    readiness: ReadinessSnapshot
    waves: WavePlan
    conflicts: ConflictAnalysis
    coordination_input: ParallelCoordinationInput
    execution_plan: ParallelExecutionPlan


@dataclass(frozen=True, slots=True)
class ParallelIntegrationAttempt:
    plan: ParallelMissionPlan
    group_result: ParallelGroupResult
    gate_context: IntegrationGateContext
    gate_result: IntegrationGateResult
    merge_result: MergeResult | None


@dataclass(frozen=True, slots=True)
class ParallelStoryDossier:
    mission_id: str
    workflow_generation: int
    user_story_id: str
    integration_commit: str
    stage: ParallelStoryStage
    implementer_result: ImplementerResult
    integrated_context: IntegratedStoryContext
    tester_result: TesterResult | None = None
    reviewer_result: ReviewerResult | None = None
    certifier_result: CertifierResult | None = None
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ParallelMissionResult:
    mission_id: str
    workflow_generation: int
    status: MissionStatus
    current_wave: int | None
    current_group: int | None
    blockers: tuple[str, ...]
    recommended_next_action: str


@dataclass(frozen=True, slots=True)
class ParallelRemediationPlan:
    mission_id: str
    previous_generation: int
    new_generation: int
    triggering_stage: ParallelRemediationStage
    affected_user_story_ids: tuple[str, ...]
    reexecution_user_story_ids: tuple[str, ...]
    baseline_commit: str
    preserved_artifacts: tuple[str, ...]
    stale_artifacts: tuple[str, ...]
    recommended_next_action: str


@dataclass(frozen=True, slots=True)
class ParallelRecoveryInspection:
    mission_id: str
    active_generation: int
    status: ParallelRecoveryStatus
    primary_commit: str
    stale_assignment_ids: tuple[str, ...]
    failed_assignment_ids: tuple[str, ...]
    anomalies: tuple[str, ...]
    recommended_recovery_action: str


class ParallelMissionWorkflowError(RuntimeError):
    """The parallel workflow cannot prove a requested progression."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class ParallelMissionWorkflow:
    """Coordinate the parallel pipeline without replacing component authority."""

    def __init__(
        self,
        *,
        mission_store: MissionStateStorePort,
        project_store: ProjectStateStorePort,
        control_loop: ControlLoop,
        worktree_manager: WorktreeManager,
        dag_validator: DAGValidator | None = None,
        readiness_engine: ReadinessEngine | None = None,
        wave_planner: WavePlanner | None = None,
        conflict_analyzer: ExecutionConflictAnalyzer | None = None,
        parallel_coordinator: ParallelImplementerCoordinator | None = None,
        integration_gate: IntegrationGate | None = None,
        merge_coordinator: MergeCoordinator | None = None,
        implementer_validator: ImplementerResultValidator | None = None,
        tester_validator: TesterResultValidator | None = None,
        reviewer_validator: ReviewerResultValidator | None = None,
        certifier_validator: CertifierResultValidator | None = None,
    ) -> None:
        if not isinstance(worktree_manager, WorktreeManager):
            raise ParallelMissionWorkflowError(
                "INVALID_CONFIGURATION", "WorktreeManager is required"
            )
        self._mission_store = mission_store
        self._project_store = project_store
        self._control_loop = control_loop
        self._manager = worktree_manager
        self._dag = dag_validator or DAGValidator()
        self._readiness = readiness_engine or ReadinessEngine()
        self._waves = wave_planner or WavePlanner()
        self._conflicts = conflict_analyzer or ExecutionConflictAnalyzer()
        self._parallel = parallel_coordinator or ParallelImplementerCoordinator(
            worktree_manager=worktree_manager,
            conflict_analyzer=self._conflicts,
        )
        self._gate = integration_gate or IntegrationGate(
            worktree_manager=worktree_manager,
            conflict_analyzer=self._conflicts,
        )
        self._merge = merge_coordinator or MergeCoordinator(
            worktree_manager=worktree_manager,
            integration_gate=self._gate,
        )
        self._negative_outcomes = _NegativeOutcomeStore(
            worktree_manager.repository_root
        )
        self._implementer_validator = (
            implementer_validator or ImplementerResultValidator()
        )
        self._tester_validator = tester_validator or TesterResultValidator()
        self._reviewer_validator = reviewer_validator or ReviewerResultValidator()
        self._certifier_validator = certifier_validator or CertifierResultValidator()

    def plan_current(self) -> ParallelMissionPlan:
        self._require_no_pending_transaction()
        mission, state = self._authoritative_context()
        primary = self._manager.inspect_primary()
        if not primary.clean or primary.head_commit != mission.observed_commit.casefold():
            raise ParallelMissionWorkflowError(
                "STALE_PRIMARY", "primary must be clean and equal MissionState.observed_commit"
            )
        dag = self._dag.build(state)
        readiness = self._readiness.evaluate(dag, state)
        waves = self._waves.plan(dag, readiness, state)
        conflicts = self._conflicts.analyze(waves, state)
        wave_index = waves.waves[0].wave_index if waves.waves else 0
        coordination = ParallelCoordinationInput(
            mission_id=mission.mission_id,
            workflow_generation=mission.workflow_generation,
            wave_index=wave_index,
            wave_plan=waves,
            conflict_analysis=conflicts,
            project_state=state,
            mission_state=mission,
            baseline_commit=primary.head_commit,
        )
        execution = self._parallel.plan(coordination)
        return ParallelMissionPlan(
            mission_id=mission.mission_id,
            workflow_generation=mission.workflow_generation,
            baseline_commit=primary.head_commit,
            dag=dag,
            readiness=readiness,
            waves=waves,
            conflicts=conflicts,
            coordination_input=coordination,
            execution_plan=execution,
        )

    def primary_inspection(self):
        """Expose the manager-owned read-only primary observation to composition."""

        return self._manager.inspect_primary()

    def prepare_group(
        self, plan: ParallelMissionPlan, group_index: int
    ) -> PreparedParallelGroup:
        self._require_no_pending_transaction()
        if not self._is_exact_active_group(plan, group_index):
            self._require_current_plan(plan)
        else:
            self._require_plan_identity(plan)
        prepared = self._parallel.prepare_group(
            plan.execution_plan,
            group_index,
            coordination_input=plan.coordination_input,
        )
        for story_id in prepared.user_story_ids:
            story = self._story(story_id)
            if story.status is UserStoryStatus.PLANNED:
                self._transition(story_id, UserStoryStatus.READY)
                story = self._story(story_id)
            if story.status is UserStoryStatus.READY:
                self._transition(story_id, UserStoryStatus.IN_PROGRESS)
            elif story.status is not UserStoryStatus.IN_PROGRESS:
                raise ParallelMissionWorkflowError(
                    "STORY_NOT_EXECUTABLE",
                    "prepared member is not in a resumable execution state",
                )
        return prepared

    def submit_member(
        self,
        prepared_group: PreparedParallelGroup,
        assignment_id: str,
        result: ImplementerResult,
        *,
        execution_id: str,
        implementer_input: ImplementerInput,
    ) -> ParallelMemberResult:
        self._require_no_pending_transaction()
        return self._parallel.submit_result(
            prepared_group,
            assignment_id,
            result,
            execution_id=execution_id,
            implementer_input=implementer_input,
            current_mission=self._mission_store.load(),
        )

    def complete_group(
        self,
        prepared_group: PreparedParallelGroup,
        member_results: tuple[ParallelMemberResult, ...],
    ) -> ParallelGroupResult:
        self._require_no_pending_transaction()
        return self._parallel.complete_group(prepared_group, member_results)

    def reconstruct_group(
        self, assignment_ids: tuple[str, ...]
    ) -> tuple[ParallelMissionPlan, PreparedParallelGroup]:
        """Rebuild one claimed group without replaying planning or worktree creation."""

        self._require_no_pending_transaction()
        if not assignment_ids or len(set(assignment_ids)) != len(assignment_ids):
            raise ParallelMissionWorkflowError(
                "ASSIGNMENT_MISMATCH", "exact durable assignment references are required"
            )
        mission, state = self._authoritative_context()
        registry = self._manager.registry_store.load()
        by_id = {item.assignment_id: item for item in registry.assignments}
        assignments = tuple(by_id.get(identifier) for identifier in assignment_ids)
        if any(item is None for item in assignments):
            raise ParallelMissionWorkflowError(
                "ASSIGNMENT_MISMATCH", "referenced assignment is absent"
            )
        resolved = tuple(item for item in assignments if item is not None)
        baseline = resolved[0].baseline_commit
        if any(
            item.mission_id != mission.mission_id
            or item.workflow_generation != mission.workflow_generation
            or item.baseline_commit != baseline
            or item.status not in {WorktreeStatus.ACTIVE, WorktreeStatus.COMPLETED}
            for item in resolved
        ):
            raise ParallelMissionWorkflowError(
                "ASSIGNMENT_MISMATCH", "assignment set is stale or heterogeneous"
            )
        primary = self._manager.inspect_primary()
        if not primary.clean or (
            primary.head_commit != baseline
            and not all(item.status is WorktreeStatus.COMPLETED for item in resolved)
        ):
            raise ParallelMissionWorkflowError(
                "RECONSTRUCT_REQUIRED", "primary/assignment state cannot be reconstructed"
            )
        projected = _candidate_state(state)
        claimed = {item.user_story_id for item in resolved}
        for index, story in enumerate(projected.user_stories):
            if story.id in claimed:
                if story.status not in {
                    UserStoryStatus.IN_PROGRESS,
                    UserStoryStatus.IMPLEMENTED,
                    UserStoryStatus.TESTING,
                }:
                    raise ParallelMissionWorkflowError(
                        "STORY_NOT_EXECUTABLE", "claimed story has an incompatible status"
                    )
                projected.user_stories[index] = replace(
                    _candidate_story(story), status=UserStoryStatus.READY
                )
        projected_mission = replace(mission, observed_commit=baseline)
        dag = self._dag.build(projected)
        readiness = self._readiness.evaluate(dag, projected)
        waves = self._waves.plan(dag, readiness, projected)
        conflicts = self._conflicts.analyze(waves, projected)
        wave_index = waves.waves[0].wave_index if waves.waves else 0
        coordination = ParallelCoordinationInput(
            mission.mission_id,
            mission.workflow_generation,
            wave_index,
            waves,
            conflicts,
            projected,
            projected_mission,
            baseline,
        )
        execution = self._parallel.plan(coordination)
        matches = tuple(
            group
            for group in execution.groups
            if group.user_story_ids == tuple(item.user_story_id for item in resolved)
        )
        if len(matches) != 1:
            raise ParallelMissionWorkflowError(
                "PLAN_STALE", "assignments do not resolve to one canonical SAFE group"
            )
        plan = ParallelMissionPlan(
            mission.mission_id,
            mission.workflow_generation,
            baseline,
            dag,
            readiness,
            waves,
            conflicts,
            coordination,
            execution,
        )
        prepared = self._parallel.reconstruct_group(
            execution,
            matches[0].group_index,
            coordination_input=coordination,
            assignment_ids=assignment_ids,
        )
        return plan, prepared

    def claimed_assignment_ids(
        self,
        *,
        mission_id: str,
        workflow_generation: int,
        baseline_commit: str,
        user_story_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Read the exact current assignment set without granting Git mutation access."""

        registry = self._manager.registry_store.load()
        by_story = {
            item.user_story_id: item
            for item in registry.assignments
            if item.mission_id == mission_id
            and item.workflow_generation == workflow_generation
            and item.baseline_commit == baseline_commit
            and item.status is not WorktreeStatus.CLEANED
            and item.user_story_id in user_story_ids
        }
        if len(by_story) != len(
            [
                item
                for item in registry.assignments
                if item.mission_id == mission_id
                and item.workflow_generation == workflow_generation
                and item.baseline_commit == baseline_commit
                and item.status is not WorktreeStatus.CLEANED
                and item.user_story_id in user_story_ids
            ]
        ):
            raise ParallelMissionWorkflowError(
                "ASSIGNMENT_MISMATCH", "assignment references are ambiguous"
            )
        return tuple(
            by_story[story].assignment_id
            for story in user_story_ids
            if story in by_story
        )

    def assignment(self, assignment_id: str):
        """Expose one immutable registry fact for composition revalidation."""

        matches = tuple(
            item
            for item in self._manager.registry_store.load().assignments
            if item.assignment_id == assignment_id
        )
        if len(matches) != 1:
            raise ParallelMissionWorkflowError(
                "ASSIGNMENT_MISMATCH", "assignment is absent or ambiguous"
            )
        return matches[0]

    def fail_member(
        self, prepared_group: PreparedParallelGroup, assignment_id: str
    ) -> ParallelGroupResult:
        self._require_no_pending_transaction()
        result = self._parallel.fail_member(
            prepared_group,
            assignment_id,
            current_mission=self._mission_store.load(),
        )
        return result

    def record_blocked_member(
        self,
        prepared_group: PreparedParallelGroup,
        assignment_id: str,
        candidate: ImplementerResult,
        *,
        implementer_input: ImplementerInput,
    ) -> ParallelGroupResult:
        """Preserve an explicit BLOCKED Implementer outcome as a failed assignment."""

        contexts = [
            item
            for item in prepared_group.contexts
            if item.assignment_id == assignment_id
        ]
        if len(contexts) != 1:
            raise ParallelMissionWorkflowError(
                "ASSIGNMENT_MISMATCH", "blocked result must identify one prepared member"
            )
        context = contexts[0]
        validation = self._implementer_validator.validate(
            candidate, implementer_input=implementer_input
        )
        if (
            not validation.is_valid
            or candidate.verdict is not ImplementerVerdict.BLOCKED
            or candidate.user_story_id != context.user_story_id
            or candidate.workflow_generation != prepared_group.workflow_generation
            or candidate.observed_commit != prepared_group.baseline_commit
        ):
            raise ParallelMissionWorkflowError(
                "INVALID_BLOCKED_IMPLEMENTER",
                "blocked Implementer artifact is invalid or stale",
            )
        return self.fail_member(prepared_group, assignment_id)

    def remediate_failed_group(
        self,
        plan: ParallelMissionPlan,
        group_result: ParallelGroupResult,
        *,
        affected_user_story_ids: Iterable[str],
        updated_at: datetime,
    ) -> ParallelRemediationPlan:
        """Open a new generation after a proven pre-merge member failure."""

        self._require_plan_identity(plan)
        if (
            not isinstance(group_result, ParallelGroupResult)
            or group_result.status is not ParallelGroupStatus.FAILED
        ):
            raise ParallelMissionWorkflowError(
                "IMPLEMENTER_FAILURE_NOT_PROVEN",
                "group remediation requires a FAILED ParallelGroupResult",
            )
        group_ids = self._plan_group_ids(plan, group_result.group_index)
        registry = self._manager.registry_store.load()
        assignments = [
            item for item in registry.assignments if item.assignment_id in group_result.assignment_ids
        ]
        if (
            len(assignments) != len(group_result.assignment_ids)
            or any(item.workflow_generation != plan.workflow_generation for item in assignments)
            or not any(item.status is WorktreeStatus.FAILED for item in assignments)
        ):
            raise ParallelMissionWorkflowError(
                "IMPLEMENTER_FAILURE_NOT_PROVEN",
                "registry does not prove the failed group in the active generation",
            )
        failed_story_ids = {
            item.user_story_id
            for item in assignments
            if item.status is WorktreeStatus.FAILED
        }
        affected = self._explicit_affected(
            affected_user_story_ids, group_ids, required=failed_story_ids
        )
        preserved = tuple(
            sorted(
                {
                    *(f"assignment:{item.assignment_id}" for item in assignments),
                    *(
                        f"commit:{item.result_commit}"
                        for item in assignments
                        if item.result_commit is not None
                    ),
                }
            )
        )
        return self._start_remediation(
            ParallelRemediationStage.IMPLEMENTER,
            affected,
            group_ids,
            preserved,
            updated_at,
            authority={
                "kind": "IMPLEMENTER_GROUP_FAILURE",
                "plan": to_dict(plan),
                "result": to_dict(group_result),
            },
            consume_outcome=False,
        )

    def remediate_integration(
        self,
        attempt: ParallelIntegrationAttempt,
        *,
        affected_user_story_ids: Iterable[str],
        updated_at: datetime,
    ) -> ParallelRemediationPlan:
        """Open a new generation after a proven Gate FAIL or Merge FAILED."""

        if not isinstance(attempt, ParallelIntegrationAttempt):
            raise ParallelMissionWorkflowError(
                "INVALID_FAILURE_CONTEXT", "integration attempt is required"
            )
        self._require_plan_identity(attempt.plan)
        group_ids = tuple(
            item.user_story_id for item in attempt.group_result.member_results
        )
        if attempt.gate_result.result is IntegrationGateClassification.UNKNOWN:
            self._require_replayed_gate(attempt)
            raise ParallelMissionWorkflowError(
                "RECOVERY_REQUIRED",
                "Integration Gate UNKNOWN requires explicit recovery, not remediation",
            )
        if attempt.gate_result.result is IntegrationGateClassification.FAIL:
            self._require_replayed_gate(attempt)
            if attempt.merge_result is not None:
                raise ParallelMissionWorkflowError(
                    "INVALID_FAILURE_CONTEXT", "Gate FAIL must not have a MergeResult"
                )
            stage = ParallelRemediationStage.INTEGRATION_GATE
            attributable = {
                member
                for finding in attempt.gate_result.findings
                for member in finding.members
                if member in group_ids
            }
        elif (
            attempt.gate_result.result is IntegrationGateClassification.PASS
            and isinstance(attempt.merge_result, MergeResult)
            and attempt.merge_result.result is MergeStatus.FAILED
        ):
            self._require_negative_merge_outcome(attempt)
            stage = ParallelRemediationStage.MERGE
            attributable = set(group_ids)
        elif (
            isinstance(attempt.merge_result, MergeResult)
            and attempt.merge_result.result is MergeStatus.BLOCKED
        ):
            raise ParallelMissionWorkflowError(
                "RECOVERY_REQUIRED",
                "Merge BLOCKED requires reconstruct/replan, not remediation",
            )
        else:
            raise ParallelMissionWorkflowError(
                "REMEDIATION_NOT_PROVEN",
                "attempt does not prove a remediable Gate or Merge failure",
            )
        affected = self._explicit_affected(
            affected_user_story_ids,
            group_ids,
            required=attributable,
        )
        preserved = tuple(
            sorted(
                {
                    *(f"assignment:{item.assignment_id}" for item in attempt.group_result.member_results),
                    *(f"commit:{item.result_commit}" for item in attempt.group_result.member_results),
                    f"integration-gate:g{attempt.gate_result.workflow_generation}:w{attempt.gate_result.wave_index}:g{attempt.gate_result.group_index}",
                    *(
                        (f"integration-commit:{attempt.merge_result.integration_commit}",)
                        if attempt.merge_result is not None
                        and attempt.merge_result.integration_commit is not None
                        else ()
                    ),
                }
            )
        )
        return self._start_remediation(
            stage,
            affected,
            group_ids,
            preserved,
            updated_at,
            authority=(
                to_dict(attempt.merge_result)
                if stage is ParallelRemediationStage.MERGE
                else {
                    "kind": "INTEGRATION_GATE_FAILURE",
                    "gate": to_dict(attempt.gate_result),
                }
            ),
            consume_outcome=stage is ParallelRemediationStage.MERGE,
        )

    def remediate_dossier(
        self,
        dossier: ParallelStoryDossier,
        *,
        updated_at: datetime,
    ) -> ParallelRemediationPlan:
        """Open forward remediation for a validated post-merge role failure."""

        if (
            not isinstance(dossier, ParallelStoryDossier)
            or dossier.stage is not ParallelStoryStage.REMEDIATION_REQUIRED
        ):
            raise ParallelMissionWorkflowError(
                "REMEDIATION_NOT_PROVEN", "remediation dossier is required"
            )
        self._require_remediation_dossier_fresh(dossier)
        self._require_authoritative_negative_dossier(dossier)
        if (
            dossier.tester_result is not None
            and dossier.tester_result.verdict is TesterVerdict.REMEDIATION_REQUIRED
            and dossier.reviewer_result is None
        ):
            stage = ParallelRemediationStage.TESTER
        elif (
            dossier.reviewer_result is not None
            and dossier.reviewer_result.verdict is ReviewerVerdict.REMEDIATION_REQUIRED
            and dossier.certifier_result is None
        ):
            stage = ParallelRemediationStage.REVIEWER
        elif (
            dossier.certifier_result is not None
            and dossier.certifier_result.verdict is CertifierVerdict.REMEDIATION_REQUIRED
        ):
            stage = ParallelRemediationStage.CERTIFIER
        else:
            raise ParallelMissionWorkflowError(
                "REMEDIATION_NOT_PROVEN",
                "dossier does not contain a validated remediation verdict",
            )
        preserved = (
            f"integration-commit:{dossier.integration_commit}",
            f"implementer-result:{dossier.user_story_id}:g{dossier.workflow_generation}",
            *(
                (f"tester-result:{dossier.user_story_id}:g{dossier.workflow_generation}",)
                if dossier.tester_result is not None
                else ()
            ),
            *(
                (f"reviewer-result:{dossier.user_story_id}:g{dossier.workflow_generation}",)
                if dossier.reviewer_result is not None
                else ()
            ),
        )
        return self._start_remediation(
            stage,
            (dossier.user_story_id,),
            (dossier.user_story_id,),
            tuple(sorted(preserved)),
            updated_at,
            authority=to_dict(dossier),
            consume_outcome=True,
        )

    def block_for_recovery(
        self,
        attempt: ParallelIntegrationAttempt,
        *,
        updated_at: datetime,
    ) -> ParallelRecoveryInspection:
        """Persist an explicit technical recovery boundary without retrying."""

        if not isinstance(attempt, ParallelIntegrationAttempt):
            raise ParallelMissionWorkflowError(
                "INVALID_FAILURE_CONTEXT", "integration attempt is required"
            )
        self._require_plan_identity(attempt.plan)
        gate_unknown = (
            attempt.gate_result.result is IntegrationGateClassification.UNKNOWN
        )
        merge_blocked = (
            isinstance(attempt.merge_result, MergeResult)
            and attempt.merge_result.result is MergeStatus.BLOCKED
        )
        if gate_unknown:
            self._require_replayed_gate(attempt)
        if merge_blocked:
            self._require_negative_merge_outcome(attempt)
        if not gate_unknown and not merge_blocked:
            raise ParallelMissionWorkflowError(
                "RECOVERY_NOT_REQUIRED", "attempt does not prove UNKNOWN or BLOCKED"
            )
        reason = "INTEGRATION_GATE_UNKNOWN" if gate_unknown else "MERGE_BLOCKED"
        authority: Mapping[str, object] = (
            to_dict(attempt.merge_result)
            if merge_blocked
            else {
                "kind": "INTEGRATION_GATE_UNKNOWN",
                "gate": to_dict(attempt.gate_result),
            }
        )
        self._start_recovery_transaction(
            stage=(
                ParallelRemediationStage.MERGE
                if merge_blocked
                else ParallelRemediationStage.INTEGRATION_GATE
            ),
            affected=tuple(
                item.user_story_id for item in attempt.group_result.member_results
            ),
            reason=reason,
            updated_at=updated_at,
            authority=authority,
            consume_outcome=merge_blocked,
        )
        return self.inspect_recovery()

    def inspect_recovery(self) -> ParallelRecoveryInspection:
        mission = self._mission_store.load()
        primary = self._manager.inspect_primary()
        registry = self._manager.registry_store.load()
        reconciliation = self._manager.inspect_all(
            current_generation=mission.workflow_generation
        )
        stale = tuple(
            sorted(
                item.assignment_id
                for item in registry.assignments
                if item.workflow_generation != mission.workflow_generation
                and item.status is not WorktreeStatus.CLEANED
            )
        )
        failed = tuple(
            sorted(
                item.assignment_id
                for item in registry.assignments
                if item.workflow_generation == mission.workflow_generation
                and item.status is WorktreeStatus.FAILED
            )
        )
        anomalies = set(reconciliation.anomalies)
        pending = self._pending_transaction()
        if pending is not None:
            anomalies.add("PENDING_REMEDIATION_TRANSACTION")
        if not primary.clean:
            anomalies.add("PRIMARY_DIRTY")
        if primary.head_commit != mission.observed_commit.casefold():
            anomalies.add("PRIMARY_DRIFT")
        status = (
            ParallelRecoveryStatus.BLOCKED
            if anomalies or failed or mission.status is MissionStatus.BLOCKED
            else ParallelRecoveryStatus.READY
        )
        action = (
            "Reconstruct registry and Git divergence before any retry."
            if anomalies
            else "Begin an explicit new-generation remediation."
            if failed
            else "Resolve the recorded technical blocker, then resume explicitly."
            if mission.status is MissionStatus.BLOCKED
            else "Reconstruct the current plan from authoritative state."
        )
        return ParallelRecoveryInspection(
            mission_id=mission.mission_id,
            active_generation=mission.workflow_generation,
            status=status,
            primary_commit=primary.head_commit,
            stale_assignment_ids=stale,
            failed_assignment_ids=failed,
            anomalies=tuple(sorted(anomalies)),
            recommended_recovery_action=action,
        )

    def resume_recovery(self, *, updated_at: datetime) -> ParallelRecoveryInspection:
        """Explicitly leave a technical block after physical state is coherent."""

        pending = self._pending_transaction()
        if pending is not None:
            self._resume_pending_transaction(pending)
            return self.inspect_recovery()
        mission = self._mission_store.load()
        inspection = self.inspect_recovery()
        if (
            mission.status is not MissionStatus.BLOCKED
            or inspection.anomalies
            or inspection.failed_assignment_ids
            or not mission.blockers
            or any(
                item not in {"INTEGRATION_GATE_UNKNOWN", "MERGE_BLOCKED"}
                for item in mission.blockers
            )
        ):
            raise ParallelMissionWorkflowError(
                "RECOVERY_NOT_READY", "technical recovery conditions are not proven"
            )
        candidate = replace(
            mission,
            status=MissionStatus.ACTIVE,
            role=MissionRole.ORCHESTRATOR,
            operating_step=OperatingStep.ACT,
            blockers=[],
            next_action="Reconstruct and explicitly retry the failed technical observation.",
            updated_at=updated_at,
        )
        self._save_mission(mission, candidate, "RESUME_PARALLEL_RECOVERY")
        return self.inspect_recovery()

    def integrate_group(
        self,
        plan: ParallelMissionPlan,
        group_result: ParallelGroupResult,
        *,
        updated_at: datetime,
    ) -> ParallelIntegrationAttempt:
        attempt = self.evaluate_group(plan, group_result)
        if attempt.gate_result.result is not IntegrationGateClassification.PASS:
            return attempt
        return self.merge_gated_group(attempt, updated_at=updated_at)

    def evaluate_group(
        self,
        plan: ParallelMissionPlan,
        group_result: ParallelGroupResult,
    ) -> ParallelIntegrationAttempt:
        self._require_no_pending_transaction()
        self._require_plan_identity(plan)
        mission = self._mission_store.load()
        gate_context = IntegrationGateContext(
            coordination_input=plan.coordination_input,
            parallel_plan=plan.execution_plan,
            group_result=group_result,
            current_mission_state=mission,
        )
        gate_result = self._gate.evaluate(gate_context)
        return ParallelIntegrationAttempt(
            plan=plan,
            group_result=group_result,
            gate_context=gate_context,
            gate_result=gate_result,
            merge_result=None,
        )

    def merge_gated_group(
        self,
        attempt: ParallelIntegrationAttempt,
        *,
        updated_at: datetime,
    ) -> ParallelIntegrationAttempt:
        if (
            not isinstance(attempt, ParallelIntegrationAttempt)
            or attempt.merge_result is not None
            or attempt.gate_result.result is not IntegrationGateClassification.PASS
        ):
            raise ParallelMissionWorkflowError(
                "GATE_NOT_PASS", "only one authoritative Gate PASS may reach merge"
            )
        merge_result = self._merge.merge(
            MergeContext(
                gate_context=attempt.gate_context,
                gate_result=attempt.gate_result,
            )
        )
        completed = replace(attempt, merge_result=merge_result)
        if merge_result.result is MergeStatus.MERGED:
            if merge_result.integration_commit is None:
                raise ParallelMissionWorkflowError(
                    "INVALID_MERGE_RESULT", "MERGED result lacks integration commit"
                )
            self._observe_new_primary(merge_result.integration_commit, updated_at)
        return completed

    def recover_merged_group(
        self,
        plan: ParallelMissionPlan,
        group_result: ParallelGroupResult,
        *,
        gate_fingerprint: str,
        updated_at: datetime,
    ) -> ParallelIntegrationAttempt:
        mission = replace(self._mission_store.load(), observed_commit=plan.baseline_commit)
        gate_context = IntegrationGateContext(
            plan.coordination_input,
            plan.execution_plan,
            group_result,
            mission,
        )
        try:
            gate_result, merge_result = self._merge.recover_merged(
                gate_context, expected_gate_fingerprint=gate_fingerprint
            )
        except MergeCoordinationError as error:
            raise ParallelMissionWorkflowError(error.code, error.message) from error
        self._observe_new_primary(merge_result.integration_commit or "", updated_at)
        return ParallelIntegrationAttempt(
            plan, group_result, gate_context, gate_result, merge_result
        )

    def resume_gated_group(
        self,
        plan: ParallelMissionPlan,
        group_result: ParallelGroupResult,
        *,
        gate_fingerprint: str,
        updated_at: datetime,
    ) -> ParallelIntegrationAttempt:
        """Resume after a durable Gate reference without bypassing Gate or Merge."""

        primary = self._manager.inspect_primary()
        if primary.head_commit == plan.baseline_commit:
            from .integration_gate import integration_gate_fingerprint

            attempt = self.evaluate_group(plan, group_result)
            if integration_gate_fingerprint(attempt.gate_result) != gate_fingerprint:
                raise ParallelMissionWorkflowError(
                    "STALE_INTEGRATION_GATE", "current Gate differs from durable reference"
                )
            return self.merge_gated_group(attempt, updated_at=updated_at)
        return self.recover_merged_group(
            plan,
            group_result,
            gate_fingerprint=gate_fingerprint,
            updated_at=updated_at,
        )

    def accept_integrated_implementer(
        self,
        attempt: ParallelIntegrationAttempt,
        user_story_id: str,
        candidate: ImplementerResult,
        *,
        integrated_context: IntegratedStoryContext | None = None,
    ) -> ParallelStoryDossier:
        self._require_no_pending_transaction()
        integration_commit = self._prove_integration(attempt)
        member = self._member(attempt, user_story_id)
        story = self._story(user_story_id)
        if story.status not in {
            UserStoryStatus.IN_PROGRESS,
            UserStoryStatus.IMPLEMENTED,
            UserStoryStatus.TESTING,
        }:
            raise ParallelMissionWorkflowError(
                "ROLE_CHAIN_VIOLATION", "integrated Implementer requires IN_PROGRESS or exact TESTING replay"
            )
        if candidate != member.implementer_result:
            raise ParallelMissionWorkflowError(
                "INVALID_INTEGRATED_IMPLEMENTER",
                "historical Implementer artifact differs from the gated member",
            )
        implementer_input = member.implementer_input
        validation = self._implementer_validator.validate(
            candidate, implementer_input=implementer_input
        )
        gate_member = next(
            item
            for item in attempt.gate_result.member_commits
            if item.user_story_id == user_story_id
        )
        if (
            not validation.is_valid
            or candidate.verdict is not ImplementerVerdict.READY_FOR_TEST
            or candidate.files_changed != gate_member.changed_files
            or member.user_story_id != user_story_id
            or not isinstance(integrated_context, IntegratedStoryContext)
            or integrated_context.mission_id != attempt.plan.mission_id
            or integrated_context.workflow_generation != attempt.plan.workflow_generation
            or integrated_context.user_story_id != user_story_id
            or integrated_context.assignment_id != member.assignment_id
            or integrated_context.implementation_commit != member.result_commit
            or integrated_context.integrated_commit != integration_commit
        ):
            raise ParallelMissionWorkflowError(
                "INVALID_INTEGRATED_IMPLEMENTER",
                "post-merge Implementer artifact is invalid or differs from gated files",
            )
        if story.status is UserStoryStatus.IN_PROGRESS:
            self._transition(user_story_id, UserStoryStatus.IMPLEMENTED)
            self._transition(user_story_id, UserStoryStatus.TESTING)
        elif story.status is UserStoryStatus.IMPLEMENTED:
            self._transition(user_story_id, UserStoryStatus.TESTING)
        elif story.status is not UserStoryStatus.TESTING:
            raise ParallelMissionWorkflowError(
                "ROLE_CHAIN_VIOLATION", "integrated story is not ready for Tester"
            )
        return ParallelStoryDossier(
            mission_id=attempt.plan.mission_id,
            workflow_generation=attempt.plan.workflow_generation,
            user_story_id=user_story_id,
            integration_commit=integration_commit,
            stage=ParallelStoryStage.TESTING,
            implementer_result=candidate,
            integrated_context=integrated_context,
        )

    def runtime_handoff(
        self,
        dossier: ParallelStoryDossier,
        role: MissionRole,
    ) -> RoleHandoff:
        """Derive the only post-merge handoff allowed by the current P3 dossier."""

        expected = {
            ParallelStoryStage.TESTING: (MissionRole.TESTER, OperatingStep.VERIFY),
            ParallelStoryStage.REVIEW: (MissionRole.REVIEWER, OperatingStep.REPORT),
            ParallelStoryStage.CERTIFICATION: (
                MissionRole.CERTIFIER,
                OperatingStep.CONTROLLED_TRANSITION,
            ),
        }.get(dossier.stage if isinstance(dossier, ParallelStoryDossier) else None)
        if expected is None or role is not expected[0]:
            raise ParallelMissionWorkflowError(
                "ROLE_CHAIN_VIOLATION",
                "post-merge runtime role must match the authoritative dossier stage",
            )
        self._require_dossier(dossier, dossier.stage)
        return self._dossier_handoff(dossier, role, expected[1])

    def accept_tester(
        self, dossier: ParallelStoryDossier, candidate: TesterResult
    ) -> ParallelStoryDossier:
        self._require_no_pending_transaction()
        self._require_dossier(dossier, ParallelStoryStage.TESTING)
        story = self._story(dossier.user_story_id)
        handoff = self._dossier_handoff(dossier, MissionRole.TESTER, OperatingStep.VERIFY)
        tester_input = TesterInput.from_integrated_handoff(
            handoff, story, dossier.implementer_result, dossier.integrated_context
        )
        validation = self._tester_validator.validate(candidate, tester_input=tester_input)
        primary = self._manager.inspect_primary()
        if (
            not validation.is_valid
            or candidate.test_files_changed != ()
            or primary.head_commit != dossier.integration_commit
            or not primary.clean
        ):
            raise ParallelMissionWorkflowError(
                "INVALID_TESTER_RESULT",
                "post-merge Tester must be valid, non-mutating, and bound to clean primary",
            )
        if candidate.verdict is TesterVerdict.READY_FOR_REVIEW:
            self._transition(story.id, UserStoryStatus.REVIEW)
            return replace(
                dossier,
                stage=ParallelStoryStage.REVIEW,
                tester_result=candidate,
                blockers=(),
            )
        if candidate.verdict is TesterVerdict.REMEDIATION_REQUIRED:
            self._mark_remediation(story.id)
            outcome = replace(
                dossier,
                stage=ParallelStoryStage.REMEDIATION_REQUIRED,
                tester_result=candidate,
                blockers=tuple(candidate.findings),
            )
            self._record_negative_dossier(outcome)
            return outcome
        return replace(
            dossier,
            stage=ParallelStoryStage.BLOCKED,
            tester_result=candidate,
            blockers=tuple(candidate.blockers) or ("TESTER_BLOCKED",),
        )

    def accept_reviewer(
        self, dossier: ParallelStoryDossier, candidate: ReviewerResult
    ) -> ParallelStoryDossier:
        self._require_no_pending_transaction()
        self._require_dossier(dossier, ParallelStoryStage.REVIEW)
        if dossier.tester_result is None:
            raise ParallelMissionWorkflowError(
                "ROLE_CHAIN_VIOLATION", "Reviewer requires TesterResult"
            )
        story = self._story(dossier.user_story_id)
        handoff = self._dossier_handoff(dossier, MissionRole.REVIEWER, OperatingStep.REPORT)
        reviewer_input = ReviewerInput.from_integrated_handoff(
            handoff,
            story,
            dossier.implementer_result,
            dossier.tester_result,
            dossier.integrated_context,
        )
        validation = self._reviewer_validator.validate(
            candidate, reviewer_input=reviewer_input
        )
        if not validation.is_valid:
            raise ParallelMissionWorkflowError(
                "INVALID_REVIEWER_RESULT", "ReviewerResult failed deterministic validation"
            )
        if candidate.verdict is ReviewerVerdict.READY_FOR_CERTIFICATION:
            self._transition(story.id, UserStoryStatus.CERTIFICATION)
            return replace(
                dossier,
                stage=ParallelStoryStage.CERTIFICATION,
                reviewer_result=candidate,
                blockers=(),
            )
        if candidate.verdict is ReviewerVerdict.REMEDIATION_REQUIRED:
            self._mark_remediation(story.id)
            outcome = replace(
                dossier,
                stage=ParallelStoryStage.REMEDIATION_REQUIRED,
                reviewer_result=candidate,
                blockers=tuple(item.summary for item in candidate.findings if item.blocking),
            )
            self._record_negative_dossier(outcome)
            return outcome
        return replace(
            dossier,
            stage=ParallelStoryStage.BLOCKED,
            reviewer_result=candidate,
            blockers=tuple(candidate.blockers) or ("REVIEWER_BLOCKED",),
        )

    def submit_certifier(
        self,
        dossier: ParallelStoryDossier,
        candidate: CertifierResult,
        *,
        architect_result: ArchitectResult,
        acceptance_results: Iterable[AcceptanceResult],
        certification_context: CertificationContext,
        certifier: str,
        updated_at: datetime,
        certification_id: str | None = None,
        authorized_not_applicable_gate_ids: frozenset[str] = frozenset(),
    ) -> ParallelStoryDossier:
        self._require_no_pending_transaction()
        self._require_dossier(dossier, ParallelStoryStage.CERTIFICATION)
        if dossier.tester_result is None or dossier.reviewer_result is None:
            raise ParallelMissionWorkflowError(
                "ROLE_CHAIN_VIOLATION", "Certifier requires Tester and Reviewer artifacts"
            )
        story = self._story(dossier.user_story_id)
        state = self._project_store.load()
        handoff = self._dossier_handoff(
            dossier, MissionRole.CERTIFIER, OperatingStep.CONTROLLED_TRANSITION
        )
        certifier_input = CertifierInput.from_integrated_handoff(
            handoff,
            story,
            architect_result,
            dossier.implementer_result,
            dossier.tester_result,
            dossier.reviewer_result,
            tuple(state.evidence),
            tuple(state.gates),
            dossier.integrated_context,
            authorized_not_applicable_gate_ids=authorized_not_applicable_gate_ids,
        )
        validation = self._certifier_validator.validate(
            candidate, certifier_input=certifier_input
        )
        if not validation.is_valid:
            raise ParallelMissionWorkflowError(
                "INVALID_CERTIFIER_RESULT", "CertifierResult failed deterministic validation"
            )
        if candidate.verdict is CertifierVerdict.REMEDIATION_REQUIRED:
            self._mark_remediation(story.id)
            outcome = replace(
                dossier,
                stage=ParallelStoryStage.REMEDIATION_REQUIRED,
                certifier_result=candidate,
                blockers=tuple(item.summary for item in candidate.findings),
            )
            self._record_negative_dossier(outcome)
            return outcome
        if candidate.verdict is not CertifierVerdict.READY_FOR_CONTROL_PLANE:
            return replace(
                dossier,
                stage=ParallelStoryStage.BLOCKED,
                certifier_result=candidate,
                blockers=tuple(candidate.blockers) or ("CERTIFIER_BLOCKED",),
            )
        certification = self._control_loop.certify_user_story(
            story.id,
            dossier.integration_commit,
            tuple(acceptance_results),
            certifier=certifier,
            context=certification_context,
            certification_id=certification_id,
            certified_at=updated_at,
        )
        if certification.result is not CertificationResult.CERTIFIED:
            return replace(
                dossier,
                stage=ParallelStoryStage.BLOCKED,
                certifier_result=candidate,
                blockers=(f"CONTROL_PLANE_{certification.result.value}",),
            )
        self._control_loop.transition_user_story(
            story.id,
            UserStoryStatus.CERTIFIED,
            context=TransitionContext(target_commit=dossier.integration_commit),
        )
        return replace(
            dossier,
            stage=ParallelStoryStage.CERTIFIED,
            certifier_result=candidate,
            blockers=(),
        )

    def record_evidence(
        self,
        observation: EvidenceObservation,
        *,
        evidence_id: str,
        timestamp: datetime,
    ):
        self._require_no_pending_transaction()
        return self._control_loop.record_evidence(
            observation, evidence_id=evidence_id, timestamp=timestamp
        )

    def evaluate_gate(
        self,
        contract: GateContract,
        *,
        context: GateEvaluationContext,
        evaluated_at: datetime,
    ) -> GateEvaluation:
        self._require_no_pending_transaction()
        return self._control_loop.evaluate_gate(
            contract, context=context, evaluated_at=evaluated_at
        )

    def apply_human_approval(
        self, user_story_id: str, evidence_id: str, *, expected_commit: str
    ):
        self._require_no_pending_transaction()
        return self._control_loop.apply_human_approval(
            user_story_id, evidence_id, expected_commit=expected_commit
        )

    def result(
        self,
        *,
        current_wave: int | None = None,
        current_group: int | None = None,
        blockers: tuple[str, ...] = (),
    ) -> ParallelMissionResult:
        mission, state = self._authoritative_context(require_active=False)
        complete = bool(state.user_stories) and all(
            item.status is UserStoryStatus.CERTIFIED for item in state.user_stories
        )
        status = MissionStatus.COMPLETED if complete else (
            MissionStatus.BLOCKED if blockers else MissionStatus.ACTIVE
        )
        action = (
            "Finalize the mission."
            if complete
            else "Resolve blockers before continuing."
            if blockers
            else "Reconstruct and plan the current certified dependency frontier."
        )
        return ParallelMissionResult(
            mission_id=mission.mission_id,
            workflow_generation=mission.workflow_generation,
            status=status,
            current_wave=current_wave,
            current_group=current_group,
            blockers=blockers,
            recommended_next_action=action,
        )

    def finalize(self, *, current_commit: str, updated_at: datetime) -> ParallelMissionResult:
        self._require_no_pending_transaction()
        result = self.result()
        mission = self._mission_store.load()
        primary = self._manager.inspect_primary()
        if (
            result.status is not MissionStatus.COMPLETED
            or current_commit.casefold() != primary.head_commit
            or not primary.clean
        ):
            raise ParallelMissionWorkflowError(
                "MISSION_INCOMPLETE", "all stories must be CERTIFIED at the clean primary HEAD"
            )
        candidate = replace(
            mission,
            status=MissionStatus.COMPLETED,
            role=MissionRole.CERTIFIER,
            operating_step=OperatingStep.REPORT,
            blockers=[],
            next_action="Parallel mission completed with authoritative Certifications.",
            observed_commit=primary.head_commit,
            updated_at=updated_at,
        )
        self._save_mission(mission, candidate, "COMPLETE_PARALLEL_MISSION")
        return replace(result, status=MissionStatus.COMPLETED)

    def _require_current_plan(self, supplied: ParallelMissionPlan) -> None:
        current = self.plan_current()
        if supplied != current:
            raise ParallelMissionWorkflowError(
                "PLAN_STALE", "parallel plan differs from current authoritative state"
            )

    def _is_exact_active_group(
        self, plan: ParallelMissionPlan, group_index: int
    ) -> bool:
        groups = [
            group for group in plan.execution_plan.groups if group.group_index == group_index
        ]
        if len(groups) != 1:
            return False
        expected = set(groups[0].user_story_ids)
        assignments = [
            item
            for item in self._manager.registry_store.load().assignments
            if item.mission_id == plan.mission_id
            and item.workflow_generation == plan.workflow_generation
            and item.baseline_commit == plan.baseline_commit
            and item.status.value == "ACTIVE"
            and item.user_story_id in expected
        ]
        return len(assignments) == len(expected) and {
            item.user_story_id for item in assignments
        } == expected

    def _require_plan_identity(self, plan: ParallelMissionPlan) -> None:
        mission = self._mission_store.load()
        if (
            plan.mission_id != mission.mission_id
            or plan.workflow_generation != mission.workflow_generation
            or plan.baseline_commit != mission.observed_commit.casefold()
        ):
            raise ParallelMissionWorkflowError(
                "PLAN_STALE", "plan identity differs from current mission context"
            )

    @staticmethod
    def _plan_group_ids(plan: ParallelMissionPlan, group_index: int) -> tuple[str, ...]:
        groups = [
            item
            for item in plan.execution_plan.groups
            if item.group_index == group_index
        ]
        if len(groups) != 1:
            raise ParallelMissionWorkflowError(
                "GROUP_MISMATCH", "failure must identify one planned group"
            )
        return groups[0].user_story_ids

    @staticmethod
    def _explicit_affected(
        supplied: Iterable[str],
        group_ids: tuple[str, ...],
        *,
        required: set[str],
    ) -> tuple[str, ...]:
        affected = tuple(dict.fromkeys(supplied))
        if (
            not affected
            or any(not isinstance(item, str) or not item for item in affected)
            or not set(affected).issubset(set(group_ids))
            or not required.issubset(set(affected))
        ):
            raise ParallelMissionWorkflowError(
                "MULTI_STORY_REMEDIATION_REQUIRED",
                "affected stories must be explicit and cover every attributable member",
            )
        return tuple(item for item in group_ids if item in set(affected))

    def _start_remediation(
        self,
        stage: ParallelRemediationStage,
        affected: tuple[str, ...],
        initial_reexecution: tuple[str, ...],
        preserved: tuple[str, ...],
        updated_at: datetime,
        *,
        authority: Mapping[str, object],
        consume_outcome: bool,
    ) -> ParallelRemediationPlan:
        self._require_no_pending_transaction()
        mission, state = self._authoritative_context()
        primary = self._manager.inspect_primary()
        if not primary.clean or primary.head_commit != mission.observed_commit.casefold():
            raise ParallelMissionWorkflowError(
                "RECONSTRUCT_REQUIRED",
                "remediation baseline must be the clean authoritative primary HEAD",
            )
        registry = self._manager.registry_store.load()
        assigned_current = {
            item.user_story_id
            for item in registry.assignments
            if item.mission_id == mission.mission_id
            and item.workflow_generation == mission.workflow_generation
            and item.status is not WorktreeStatus.CLEANED
        }
        requested = set(initial_reexecution) | assigned_current
        reexecution = tuple(
            item.id
            for item in state.user_stories
            if item.id in requested
            and item.status not in {UserStoryStatus.CERTIFIED, UserStoryStatus.CANCELLED}
        )
        if not set(affected).issubset(set(reexecution)):
            raise ParallelMissionWorkflowError(
                "REMEDIATION_TARGET_INVALID",
                "affected stories must remain non-terminal and replayable",
            )
        project_candidate = self._project_candidate_for_reexecution(state, reexecution)
        stale_assignments = tuple(
            sorted(
                f"assignment:{item.assignment_id}"
                for item in registry.assignments
                if item.mission_id == mission.mission_id
                and item.workflow_generation == mission.workflow_generation
                and item.status is not WorktreeStatus.CLEANED
            )
        )
        stale = (
            f"parallel-execution-plan:g{mission.workflow_generation}",
            f"prepared-groups:g{mission.workflow_generation}",
            f"implementer-results:g{mission.workflow_generation}",
            f"integration-gates:g{mission.workflow_generation}",
            f"merge-results:g{mission.workflow_generation}",
            f"tester-results:g{mission.workflow_generation}",
            f"reviewer-results:g{mission.workflow_generation}",
            f"certifier-results:g{mission.workflow_generation}",
            *stale_assignments,
        )
        new_generation = mission.workflow_generation + 1
        mission_candidate = replace(
            mission,
            workflow_generation=new_generation,
            status=MissionStatus.ACTIVE,
            role=MissionRole.ORCHESTRATOR,
            operating_step=OperatingStep.ACT,
            blockers=[],
            next_action=(
                f"Execute generation {new_generation} remediation from {stage.value} "
                f"for {', '.join(affected)}; replay {', '.join(reexecution)}."
            ),
            observed_commit=primary.head_commit,
            updated_at=updated_at,
        )
        self._validate_candidates(project_candidate, mission_candidate)
        transaction = self._transaction_intent(
            authority=authority,
            consume_outcome=consume_outcome,
            mission=mission,
            mission_candidate=mission_candidate,
            state=state,
            project_candidate=project_candidate,
            stage=stage,
            affected=affected,
            reexecution=reexecution,
            baseline=primary.head_commit,
            operation="BEGIN_PARALLEL_REMEDIATION",
            updated_at=updated_at,
        )
        transaction_id = self._claim_transaction(transaction, authority)
        self._apply_pending_transaction(transaction_id, transaction)
        return ParallelRemediationPlan(
            mission_id=mission.mission_id,
            previous_generation=mission.workflow_generation,
            new_generation=new_generation,
            triggering_stage=stage,
            affected_user_story_ids=affected,
            reexecution_user_story_ids=reexecution,
            baseline_commit=primary.head_commit,
            preserved_artifacts=preserved,
            stale_artifacts=tuple(stale),
            recommended_next_action=(
                "Reconstruct a new plan and create generation-specific worktrees; "
                "then rerun Gate, Merge, Tester, Reviewer and Certifier."
            ),
        )

    def _start_recovery_transaction(
        self,
        *,
        stage: ParallelRemediationStage,
        affected: tuple[str, ...],
        reason: str,
        updated_at: datetime,
        authority: Mapping[str, object],
        consume_outcome: bool,
    ) -> None:
        self._require_no_pending_transaction()
        mission, state = self._authoritative_context()
        primary = self._manager.inspect_primary()
        if not primary.clean or primary.head_commit != mission.observed_commit.casefold():
            raise ParallelMissionWorkflowError(
                "RECONSTRUCT_REQUIRED",
                "recovery baseline must be the clean authoritative primary HEAD",
            )
        project_candidate = _candidate_state(state)
        mission_candidate = replace(
            mission,
            status=MissionStatus.BLOCKED,
            role=MissionRole.ORCHESTRATOR,
            blockers=[reason],
            next_action="Resolve the technical uncertainty, then explicitly resume recovery.",
            updated_at=updated_at,
        )
        self._validate_candidates(project_candidate, mission_candidate)
        transaction = self._transaction_intent(
            authority=authority,
            consume_outcome=consume_outcome,
            mission=mission,
            mission_candidate=mission_candidate,
            state=state,
            project_candidate=project_candidate,
            stage=stage,
            affected=affected,
            reexecution=(),
            baseline=primary.head_commit,
            operation="BLOCK_PARALLEL_RECOVERY",
            updated_at=updated_at,
        )
        transaction_id = self._claim_transaction(transaction, authority)
        self._apply_pending_transaction(transaction_id, transaction)

    def _project_candidate_for_reexecution(
        self, state: ProjectState, reexecution: tuple[str, ...]
    ) -> ProjectState:
        candidate = _candidate_state(state)
        for story_id in reexecution:
            matches = [
                (index, story)
                for index, story in enumerate(candidate.user_stories)
                if story.id == story_id
            ]
            if len(matches) != 1:
                raise ParallelMissionWorkflowError(
                    "STORY_MISSING_OR_AMBIGUOUS", "story must resolve exactly once"
                )
            index, story = matches[0]
            candidate.user_stories[index] = _candidate_story(story)
            self._ready_candidate_for_reexecution(candidate, story_id)
        return candidate

    def _ready_candidate_for_reexecution(
        self, candidate: ProjectState, story_id: str
    ) -> None:
        story = self._unique_story(candidate, story_id)
        status = story.status
        if status is UserStoryStatus.PLANNED:
            self._apply_candidate_transition(candidate, story, UserStoryStatus.READY)
            return
        if status is UserStoryStatus.READY:
            return
        if status is UserStoryStatus.BLOCKED:
            self._apply_candidate_transition(candidate, story, UserStoryStatus.READY)
            return
        if status is UserStoryStatus.IN_PROGRESS:
            self._apply_candidate_transition(candidate, story, UserStoryStatus.BLOCKED)
            self._apply_candidate_transition(candidate, story, UserStoryStatus.READY)
            return
        if status is UserStoryStatus.IMPLEMENTED:
            self._apply_candidate_transition(candidate, story, UserStoryStatus.TESTING)
            status = UserStoryStatus.TESTING
        if status in {
            UserStoryStatus.TESTING,
            UserStoryStatus.REVIEW,
            UserStoryStatus.CERTIFICATION,
        }:
            self._apply_candidate_transition(candidate, story, UserStoryStatus.REJECTED)
            self._apply_candidate_transition(
                candidate, story, UserStoryStatus.REMEDIATION_REQUIRED
            )
            self._apply_candidate_transition(candidate, story, UserStoryStatus.READY)
            return
        if status is UserStoryStatus.REJECTED:
            self._apply_candidate_transition(
                candidate, story, UserStoryStatus.REMEDIATION_REQUIRED
            )
            self._apply_candidate_transition(candidate, story, UserStoryStatus.READY)
            return
        if status is UserStoryStatus.REMEDIATION_REQUIRED:
            self._apply_candidate_transition(candidate, story, UserStoryStatus.READY)
            return
        raise ParallelMissionWorkflowError(
            "REMEDIATION_TARGET_INVALID",
            f"story {story_id} cannot enter remediation from {status.value}",
        )

    def _apply_candidate_transition(
        self,
        candidate: ProjectState,
        story: UserStory,
        target: UserStoryStatus,
    ) -> None:
        dependencies = {
            dependency: self._unique_story(candidate, dependency).status
            for dependency in story.depends_on
        }
        result = self._control_loop._transition_service.apply(
            story,
            target,
            context=TransitionContext(
                preconditions_proven=True,
                dependency_statuses=dependencies,
            ),
        )
        if not result.allowed or story.status is not target:
            raise ParallelMissionWorkflowError(
                "REMEDIATION_TARGET_INVALID",
                f"story {story.id} cannot enter {target.value}",
            )

    def _transaction_intent(
        self,
        *,
        authority: Mapping[str, object],
        consume_outcome: bool,
        mission: MissionState,
        mission_candidate: MissionState,
        state: ProjectState,
        project_candidate: ProjectState,
        stage: ParallelRemediationStage,
        affected: tuple[str, ...],
        reexecution: tuple[str, ...],
        baseline: str,
        operation: str,
        updated_at: datetime,
    ) -> dict[str, object]:
        authority_fingerprint, _ = _fingerprint(authority)
        return {
            "authority_fingerprint": authority_fingerprint,
            "consume_outcome": consume_outcome,
            "mission_id": mission.mission_id,
            "source_generation": mission.workflow_generation,
            "target_generation": mission_candidate.workflow_generation,
            "triggering_stage": stage.value,
            "affected_user_story_ids": list(affected),
            "reexecution_user_story_ids": list(reexecution),
            "baseline_commit": baseline,
            "operation": operation,
            "updated_at": updated_at.isoformat(),
            "project_before_fingerprint": self._state_fingerprint(state),
            "project_after_fingerprint": self._state_fingerprint(project_candidate),
            "mission_before_fingerprint": self._state_fingerprint(mission),
            "mission_after_fingerprint": self._state_fingerprint(mission_candidate),
        }

    @staticmethod
    def _state_fingerprint(state: ProjectState | MissionState) -> str:
        fingerprint, _ = _fingerprint(to_dict(state))
        return fingerprint

    def _claim_transaction(
        self,
        transaction: Mapping[str, object],
        authority: Mapping[str, object],
    ) -> str:
        try:
            return self._negative_outcomes._claim(transaction, authority=authority)
        except PersistenceError as error:
            code = (
                "RECOVERY_PENDING"
                if error.code == "TRANSACTION_PENDING"
                else "TRANSACTION_PERSISTENCE_FAILED"
            )
            raise ParallelMissionWorkflowError(code, error.message) from error

    def _pending_transaction(self) -> dict[str, object] | None:
        try:
            return self._negative_outcomes._pending()
        except PersistenceError as error:
            raise ParallelMissionWorkflowError(
                "TRANSACTION_AUTHORITY_UNAVAILABLE", error.message
            ) from error

    def _require_no_pending_transaction(self) -> None:
        if self._pending_transaction() is not None:
            raise ParallelMissionWorkflowError(
                "RECOVERY_PENDING",
                "pending remediation transaction must be resumed before progression",
            )

    def _resume_pending_transaction(self, record: Mapping[str, object]) -> None:
        fingerprint = record.get("fingerprint")
        transaction = record.get("intent")
        if not isinstance(fingerprint, str) or not isinstance(transaction, Mapping):
            raise ParallelMissionWorkflowError(
                "BLOCKED_INCONSISTENT", "pending transaction is malformed"
            )
        self._apply_pending_transaction(fingerprint, transaction)

    def _apply_pending_transaction(
        self, fingerprint: str, transaction: Mapping[str, object]
    ) -> None:
        mission = self._mission_store.load()
        state = self._project_store.load()
        if mission.mission_id != transaction["mission_id"]:
            raise ParallelMissionWorkflowError(
                "BLOCKED_INCONSISTENT", "pending transaction mission does not match"
            )
        project_observed = self._state_fingerprint(state)
        mission_observed = self._state_fingerprint(mission)
        project_before = transaction["project_before_fingerprint"]
        project_after = transaction["project_after_fingerprint"]
        mission_before = transaction["mission_before_fingerprint"]
        mission_after = transaction["mission_after_fingerprint"]
        if project_observed not in {project_before, project_after} or mission_observed not in {
            mission_before,
            mission_after,
        }:
            raise ParallelMissionWorkflowError(
                "BLOCKED_INCONSISTENT",
                "business state differs from both transaction before and after snapshots",
            )

        project_candidate: ProjectState | None = None
        mission_candidate: MissionState | None = None
        if project_observed == project_before:
            if transaction["operation"] == "BEGIN_PARALLEL_REMEDIATION":
                project_candidate = self._project_candidate_for_reexecution(
                    state,
                    tuple(transaction["reexecution_user_story_ids"]),
                )
            else:
                project_candidate = _candidate_state(state)
            if self._state_fingerprint(project_candidate) != project_after:
                raise ParallelMissionWorkflowError(
                    "BLOCKED_INCONSISTENT",
                    "reconstructed ProjectState does not match transaction intent",
                )
        if mission_observed == mission_before:
            mission_candidate = self._mission_candidate_from_transaction(
                mission, transaction
            )
            if self._state_fingerprint(mission_candidate) != mission_after:
                raise ParallelMissionWorkflowError(
                    "BLOCKED_INCONSISTENT",
                    "reconstructed MissionState does not match transaction intent",
                )
        self._validate_candidates(
            project_candidate or state,
            mission_candidate or mission,
        )

        if project_candidate is not None and project_before != project_after:
            self._save_project(state, project_candidate, str(transaction["operation"]))
            if self._state_fingerprint(self._project_store.load()) != project_after:
                raise ParallelMissionWorkflowError(
                    "BLOCKED_INCONSISTENT", "ProjectState write was not durable"
                )
        if mission_candidate is not None and mission_before != mission_after:
            self._save_mission(mission, mission_candidate, str(transaction["operation"]))
            if self._state_fingerprint(self._mission_store.load()) != mission_after:
                raise ParallelMissionWorkflowError(
                    "BLOCKED_INCONSISTENT", "MissionState write was not durable"
                )
        observed_project = self._state_fingerprint(self._project_store.load())
        observed_mission = self._state_fingerprint(self._mission_store.load())
        if observed_project != project_after or observed_mission != mission_after:
            raise ParallelMissionWorkflowError(
                "BLOCKED_INCONSISTENT", "transaction business state is not coherent"
            )
        try:
            self._negative_outcomes._finalize(fingerprint)
        except PersistenceError as error:
            raise ParallelMissionWorkflowError(
                "TRANSACTION_FINALIZATION_FAILED", error.message
            ) from error

    def _mission_candidate_from_transaction(
        self, mission: MissionState, transaction: Mapping[str, object]
    ) -> MissionState:
        updated_at = datetime.fromisoformat(
            str(transaction["updated_at"]).replace("Z", "+00:00")
        )
        if transaction["operation"] == "BEGIN_PARALLEL_REMEDIATION":
            target_generation = int(transaction["target_generation"])
            affected = tuple(transaction["affected_user_story_ids"])
            reexecution = tuple(transaction["reexecution_user_story_ids"])
            return replace(
                mission,
                workflow_generation=target_generation,
                status=MissionStatus.ACTIVE,
                role=MissionRole.ORCHESTRATOR,
                operating_step=OperatingStep.ACT,
                blockers=[],
                next_action=(
                    f"Execute generation {target_generation} remediation from "
                    f"{transaction['triggering_stage']} for {', '.join(affected)}; "
                    f"replay {', '.join(reexecution)}."
                ),
                observed_commit=str(transaction["baseline_commit"]),
                updated_at=updated_at,
            )
        reason = (
            "MERGE_BLOCKED"
            if transaction["triggering_stage"] == ParallelRemediationStage.MERGE.value
            else "INTEGRATION_GATE_UNKNOWN"
        )
        return replace(
            mission,
            status=MissionStatus.BLOCKED,
            role=MissionRole.ORCHESTRATOR,
            blockers=[reason],
            next_action="Resolve the technical uncertainty, then explicitly resume recovery.",
            updated_at=updated_at,
        )

    def _validate_candidates(
        self, state: ProjectState, mission: MissionState
    ) -> None:
        self._validate_store_candidate(self._project_store, state)
        self._validate_store_candidate(self._mission_store, mission)

    @staticmethod
    def _validate_store_candidate(store: object, candidate: object) -> None:
        current = store
        for _ in range(3):
            validator = getattr(current, "_validate_state", None)
            if callable(validator):
                validator(candidate)
                return
            current = getattr(current, "delegate", None)
            if current is None:
                break
        raise ParallelMissionWorkflowError(
            "INVALID_CONFIGURATION", "state store does not expose deterministic validation"
        )

    def _save_project(
        self, current: ProjectState, candidate: ProjectState, operation: str
    ) -> None:
        authorization = _issue_authoritative_write(
            store_kind="PROJECT_STATE",
            store=self._project_store,
            before_state=current,
            candidate_state=candidate,
            operation=operation,
        )
        self._project_store.save(
            candidate,
            authorization=authorization,
            operation=operation,
        )

    def _require_remediation_dossier_fresh(
        self, dossier: ParallelStoryDossier
    ) -> None:
        mission = self._mission_store.load()
        primary = self._manager.inspect_primary()
        if (
            dossier.mission_id != mission.mission_id
            or dossier.workflow_generation != mission.workflow_generation
            or dossier.integration_commit != mission.observed_commit.casefold()
            or primary.head_commit != dossier.integration_commit
            or not primary.clean
        ):
            raise ParallelMissionWorkflowError(
                "STALE_ROLE_ARTIFACT",
                "remediation dossier is stale against generation or integrated primary",
            )

    def _require_replayed_gate(self, attempt: ParallelIntegrationAttempt) -> None:
        observed = self._gate.evaluate(attempt.gate_context)
        if observed != attempt.gate_result:
            raise ParallelMissionWorkflowError(
                "STALE_INTEGRATION_GATE",
                "failure handling requires the exact currently reproducible Gate result",
            )

    def _require_negative_merge_outcome(
        self, attempt: ParallelIntegrationAttempt
    ) -> None:
        if not isinstance(attempt.merge_result, MergeResult):
            raise ParallelMissionWorkflowError(
                "UNTRUSTED_MERGE_OUTCOME", "negative MergeResult is required"
            )
        try:
            self._merge._validate_negative_outcome(
                MergeContext(
                    gate_context=attempt.gate_context,
                    gate_result=attempt.gate_result,
                ),
                attempt.merge_result,
            )
        except MergeCoordinationError as error:
            raise ParallelMissionWorkflowError(error.code, error.message) from error

    def _consume_negative_merge_outcome(
        self, attempt: ParallelIntegrationAttempt
    ) -> None:
        assert isinstance(attempt.merge_result, MergeResult)
        try:
            self._merge._consume_negative_outcome(
                MergeContext(
                    gate_context=attempt.gate_context,
                    gate_result=attempt.gate_result,
                ),
                attempt.merge_result,
            )
        except MergeCoordinationError as error:
            raise ParallelMissionWorkflowError(error.code, error.message) from error

    def _record_negative_dossier(self, dossier: ParallelStoryDossier) -> None:
        try:
            self._negative_outcomes._record(to_dict(dossier))
        except PersistenceError as error:
            raise ParallelMissionWorkflowError(
                "OUTCOME_PERSISTENCE_FAILED",
                "negative role outcome could not be recorded authoritatively",
            ) from error

    def _require_authoritative_negative_dossier(
        self, dossier: ParallelStoryDossier
    ) -> None:
        try:
            authorized = self._negative_outcomes._contains_unconsumed(to_dict(dossier))
        except PersistenceError as error:
            raise ParallelMissionWorkflowError(
                "NEGATIVE_OUTCOME_AUTHORITY_UNAVAILABLE",
                "negative role outcome authority cannot be read",
            ) from error
        if not authorized:
            raise ParallelMissionWorkflowError(
                "UNTRUSTED_NEGATIVE_OUTCOME",
                "remediation dossier was not emitted by the authoritative role path",
            )

    def _consume_authoritative_negative_dossier(
        self, dossier: ParallelStoryDossier
    ) -> None:
        try:
            self._negative_outcomes._consume(to_dict(dossier))
        except PersistenceError as error:
            raise ParallelMissionWorkflowError(
                "UNTRUSTED_NEGATIVE_OUTCOME",
                "remediation dossier is absent, altered, or already consumed",
            ) from error

    def _prove_integration(self, attempt: ParallelIntegrationAttempt) -> str:
        if (
            not isinstance(attempt, ParallelIntegrationAttempt)
            or not isinstance(attempt.gate_result, IntegrationGateResult)
            or attempt.gate_result.result is not IntegrationGateClassification.PASS
            or not isinstance(attempt.merge_result, MergeResult)
            or attempt.merge_result.result is not MergeStatus.MERGED
            or attempt.merge_result.integration_commit is None
        ):
            raise ParallelMissionWorkflowError(
                "MERGE_NOT_PROVEN", "post-merge roles require a real MERGED result"
            )
        observed = self._merge.merge(
            MergeContext(
                gate_context=attempt.gate_context,
                gate_result=attempt.gate_result,
            )
        )
        primary = self._manager.inspect_primary()
        if (
            observed.result is not MergeStatus.MERGED
            or observed.integration_commit != attempt.merge_result.integration_commit
            or primary.head_commit != observed.integration_commit
            or not primary.clean
        ):
            raise ParallelMissionWorkflowError(
                "MERGE_NOT_PROVEN", "Git does not prove the supplied integrated group"
            )
        return observed.integration_commit

    def _observe_new_primary(self, integration_commit: str, updated_at: datetime) -> None:
        mission = self._mission_store.load()
        primary = self._manager.inspect_primary()
        if primary.head_commit != integration_commit or not primary.clean:
            raise ParallelMissionWorkflowError(
                "PRIMARY_NOT_INTEGRATED", "primary does not match the MergeResult"
            )
        if mission.observed_commit.casefold() == integration_commit:
            return
        candidate = replace(
            mission,
            observed_commit=integration_commit,
            next_action="Run post-merge Implementer attestation, Tester, Reviewer and Certifier.",
            updated_at=updated_at,
        )
        self._save_mission(mission, candidate, "OBSERVE_PARALLEL_MERGE")

    def _authoritative_context(
        self, *, require_active: bool = True
    ) -> tuple[MissionState, ProjectState]:
        mission = self._mission_store.load()
        state = self._project_store.load()
        if not isinstance(mission, MissionState) or not isinstance(state, ProjectState):
            raise ParallelMissionWorkflowError(
                "INVALID_PERSISTED_STATE", "ProjectState and MissionState are required"
            )
        if require_active and mission.status is not MissionStatus.ACTIVE:
            raise ParallelMissionWorkflowError(
                "MISSION_NOT_ACTIVE", "parallel planning requires an ACTIVE mission"
            )
        return mission, state

    def _transition(self, story_id: str, target: UserStoryStatus) -> None:
        state = self._project_store.load()
        story = self._unique_story(state, story_id)
        dependencies = {
            dependency: self._unique_story(state, dependency).status
            for dependency in story.depends_on
        }
        self._control_loop.transition_user_story(
            story_id,
            target,
            context=TransitionContext(
                preconditions_proven=True,
                dependency_statuses=dependencies,
            ),
        )

    def _mark_remediation(self, story_id: str) -> None:
        self._transition(story_id, UserStoryStatus.REJECTED)
        self._transition(story_id, UserStoryStatus.REMEDIATION_REQUIRED)

    def _story(self, story_id: str) -> UserStory:
        return self._unique_story(self._project_store.load(), story_id)

    @staticmethod
    def _unique_story(state: ProjectState, story_id: str) -> UserStory:
        matches = [item for item in state.user_stories if item.id == story_id]
        if len(matches) != 1:
            raise ParallelMissionWorkflowError(
                "STORY_MISSING_OR_AMBIGUOUS", "story must resolve exactly once"
            )
        return matches[0]

    @staticmethod
    def _member(
        attempt: ParallelIntegrationAttempt, user_story_id: str
    ) -> ParallelMemberResult:
        matches = [
            item
            for item in attempt.group_result.member_results
            if item.user_story_id == user_story_id
        ]
        if len(matches) != 1:
            raise ParallelMissionWorkflowError(
                "CROSS_STORY_ARTIFACT", "story is not an exact integrated group member"
            )
        return matches[0]

    @staticmethod
    def _handoff(
        plan: ParallelMissionPlan,
        story_id: str,
        role: MissionRole,
        step: OperatingStep,
        observed_commit: str,
    ) -> RoleHandoff:
        return RoleHandoff(
            from_role=MissionRole.ORCHESTRATOR,
            to_role=role,
            mission_id=plan.mission_id,
            workflow_generation=plan.workflow_generation,
            subject=story_id,
            objective=plan.coordination_input.mission_state.objective,
            observed_commit=observed_commit,
            operating_step=step,
            blockers=(),
            instructions=f"Validate {story_id} on the integrated primary without authority bypass.",
        )

    def _dossier_handoff(
        self,
        dossier: ParallelStoryDossier,
        role: MissionRole,
        step: OperatingStep,
    ) -> RoleHandoff:
        mission = self._mission_store.load()
        if (
            dossier.mission_id != mission.mission_id
            or dossier.workflow_generation != mission.workflow_generation
            or dossier.integration_commit != mission.observed_commit.casefold()
        ):
            raise ParallelMissionWorkflowError(
                "STALE_ROLE_ARTIFACT", "dossier differs from current mission generation or commit"
            )
        return RoleHandoff(
            from_role=MissionRole.ORCHESTRATOR,
            to_role=role,
            mission_id=dossier.mission_id,
            workflow_generation=dossier.workflow_generation,
            subject=dossier.user_story_id,
            objective=mission.objective,
            observed_commit=dossier.integration_commit,
            operating_step=step,
            blockers=(),
            instructions=f"Validate {dossier.user_story_id} on the integrated primary.",
        )

    def _require_dossier(
        self, dossier: ParallelStoryDossier, stage: ParallelStoryStage
    ) -> None:
        if not isinstance(dossier, ParallelStoryDossier) or dossier.stage is not stage:
            raise ParallelMissionWorkflowError(
                "ROLE_CHAIN_VIOLATION", f"role requires dossier stage {stage.value}"
            )
        primary = self._manager.inspect_primary()
        mission = self._mission_store.load()
        if (
            dossier.mission_id != mission.mission_id
            or dossier.workflow_generation != mission.workflow_generation
            or dossier.integration_commit != mission.observed_commit.casefold()
            or primary.head_commit != dossier.integration_commit
            or not primary.clean
        ):
            raise ParallelMissionWorkflowError(
                "STALE_ROLE_ARTIFACT", "dossier is stale against mission or primary"
            )

    def _save_mission(
        self, current: MissionState, candidate: MissionState, operation: str
    ) -> None:
        authorization = _issue_authoritative_write(
            store_kind="MISSION_STATE",
            store=self._mission_store,
            before_state=current,
            candidate_state=candidate,
            operation=operation,
        )
        self._mission_store.save(
            candidate,
            authorization=authorization,
            operation=operation,
        )
