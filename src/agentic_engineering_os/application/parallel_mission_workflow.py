"""End-to-end coordination of certified Phase 2 and Phase 3 components."""

from __future__ import annotations

from collections.abc import Iterable
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
)
from agentic_engineering_os.infrastructure.worktree_manager import WorktreeManager

from .architect import ArchitectResult
from .certification_service import AcceptanceResult, CertificationContext
from .certifier import (
    CertifierInput,
    CertifierResult,
    CertifierResultValidator,
    CertifierVerdict,
)
from .control_loop import ControlLoop
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
from .integration_gate import (
    IntegrationGate,
    IntegrationGateClassification,
    IntegrationGateContext,
    IntegrationGateResult,
)
from .merge_coordinator import MergeContext, MergeCoordinator, MergeResult, MergeStatus
from .orchestrator import RoleHandoff
from .parallel_implementer_coordinator import (
    ParallelCoordinationInput,
    ParallelGroupResult,
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


class ParallelStoryStage(str, Enum):
    TESTING = "TESTING"
    REVIEW = "REVIEW"
    CERTIFICATION = "CERTIFICATION"
    CERTIFIED = "CERTIFIED"
    BLOCKED = "BLOCKED"
    REMEDIATION_REQUIRED = "REMEDIATION_REQUIRED"


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
        self._implementer_validator = (
            implementer_validator or ImplementerResultValidator()
        )
        self._tester_validator = tester_validator or TesterResultValidator()
        self._reviewer_validator = reviewer_validator or ReviewerResultValidator()
        self._certifier_validator = certifier_validator or CertifierResultValidator()

    def plan_current(self) -> ParallelMissionPlan:
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

    def prepare_group(
        self, plan: ParallelMissionPlan, group_index: int
    ) -> PreparedParallelGroup:
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
        implementer_input: ImplementerInput,
    ) -> ParallelMemberResult:
        return self._parallel.submit_result(
            prepared_group,
            assignment_id,
            result,
            implementer_input=implementer_input,
            current_mission=self._mission_store.load(),
        )

    def complete_group(
        self,
        prepared_group: PreparedParallelGroup,
        member_results: tuple[ParallelMemberResult, ...],
    ) -> ParallelGroupResult:
        return self._parallel.complete_group(prepared_group, member_results)

    def fail_member(
        self, prepared_group: PreparedParallelGroup, assignment_id: str
    ) -> ParallelGroupResult:
        result = self._parallel.fail_member(
            prepared_group,
            assignment_id,
            current_mission=self._mission_store.load(),
        )
        return result

    def integrate_group(
        self,
        plan: ParallelMissionPlan,
        group_result: ParallelGroupResult,
        *,
        updated_at: datetime,
    ) -> ParallelIntegrationAttempt:
        self._require_plan_identity(plan)
        mission = self._mission_store.load()
        gate_context = IntegrationGateContext(
            coordination_input=plan.coordination_input,
            parallel_plan=plan.execution_plan,
            group_result=group_result,
            current_mission_state=mission,
        )
        gate_result = self._gate.evaluate(gate_context)
        merge_result: MergeResult | None = None
        if gate_result.result is IntegrationGateClassification.PASS:
            merge_result = self._merge.merge(
                MergeContext(gate_context=gate_context, gate_result=gate_result)
            )
            if merge_result.result is MergeStatus.MERGED:
                if merge_result.integration_commit is None:
                    raise ParallelMissionWorkflowError(
                        "INVALID_MERGE_RESULT", "MERGED result lacks integration commit"
                    )
                self._observe_new_primary(merge_result.integration_commit, updated_at)
        return ParallelIntegrationAttempt(
            plan=plan,
            group_result=group_result,
            gate_context=gate_context,
            gate_result=gate_result,
            merge_result=merge_result,
        )

    def accept_integrated_implementer(
        self,
        attempt: ParallelIntegrationAttempt,
        user_story_id: str,
        candidate: ImplementerResult,
    ) -> ParallelStoryDossier:
        integration_commit = self._prove_integration(attempt)
        member = self._member(attempt, user_story_id)
        story = self._story(user_story_id)
        if story.status is not UserStoryStatus.IN_PROGRESS:
            raise ParallelMissionWorkflowError(
                "ROLE_CHAIN_VIOLATION", "integrated Implementer requires IN_PROGRESS"
            )
        handoff = self._handoff(
            attempt.plan,
            user_story_id,
            MissionRole.IMPLEMENTER,
            OperatingStep.ACT,
            integration_commit,
        )
        implementer_input = ImplementerInput.from_handoff(handoff, story)
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
        ):
            raise ParallelMissionWorkflowError(
                "INVALID_INTEGRATED_IMPLEMENTER",
                "post-merge Implementer artifact is invalid or differs from gated files",
            )
        self._transition(user_story_id, UserStoryStatus.IMPLEMENTED)
        self._transition(user_story_id, UserStoryStatus.TESTING)
        return ParallelStoryDossier(
            mission_id=attempt.plan.mission_id,
            workflow_generation=attempt.plan.workflow_generation,
            user_story_id=user_story_id,
            integration_commit=integration_commit,
            stage=ParallelStoryStage.TESTING,
            implementer_result=candidate,
        )

    def accept_tester(
        self, dossier: ParallelStoryDossier, candidate: TesterResult
    ) -> ParallelStoryDossier:
        self._require_dossier(dossier, ParallelStoryStage.TESTING)
        story = self._story(dossier.user_story_id)
        handoff = self._dossier_handoff(dossier, MissionRole.TESTER, OperatingStep.VERIFY)
        tester_input = TesterInput.from_handoff(
            handoff, story, dossier.implementer_result
        )
        validation = self._tester_validator.validate(candidate, tester_input=tester_input)
        if not validation.is_valid:
            raise ParallelMissionWorkflowError(
                "INVALID_TESTER_RESULT", "TesterResult failed deterministic validation"
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
            return replace(
                dossier,
                stage=ParallelStoryStage.REMEDIATION_REQUIRED,
                tester_result=candidate,
                blockers=tuple(candidate.findings),
            )
        return replace(
            dossier,
            stage=ParallelStoryStage.BLOCKED,
            tester_result=candidate,
            blockers=tuple(candidate.blockers) or ("TESTER_BLOCKED",),
        )

    def accept_reviewer(
        self, dossier: ParallelStoryDossier, candidate: ReviewerResult
    ) -> ParallelStoryDossier:
        self._require_dossier(dossier, ParallelStoryStage.REVIEW)
        if dossier.tester_result is None:
            raise ParallelMissionWorkflowError(
                "ROLE_CHAIN_VIOLATION", "Reviewer requires TesterResult"
            )
        story = self._story(dossier.user_story_id)
        handoff = self._dossier_handoff(dossier, MissionRole.REVIEWER, OperatingStep.REPORT)
        reviewer_input = ReviewerInput.from_handoff(
            handoff,
            story,
            dossier.implementer_result,
            dossier.tester_result,
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
            return replace(
                dossier,
                stage=ParallelStoryStage.REMEDIATION_REQUIRED,
                reviewer_result=candidate,
                blockers=tuple(item.summary for item in candidate.findings if item.blocking),
            )
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
        certifier_input = CertifierInput.from_handoff(
            handoff,
            story,
            architect_result,
            dossier.implementer_result,
            dossier.tester_result,
            dossier.reviewer_result,
            tuple(state.evidence),
            tuple(state.gates),
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
            return replace(
                dossier,
                stage=ParallelStoryStage.REMEDIATION_REQUIRED,
                certifier_result=candidate,
                blockers=tuple(item.summary for item in candidate.findings),
            )
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
        return self._control_loop.evaluate_gate(
            contract, context=context, evaluated_at=evaluated_at
        )

    def apply_human_approval(
        self, user_story_id: str, evidence_id: str, *, expected_commit: str
    ):
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
