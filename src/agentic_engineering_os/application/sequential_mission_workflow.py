"""Sequential coordination of validated role outputs and Control Plane services."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

from agentic_engineering_os._authoritative_write import _issue_authoritative_write
from agentic_engineering_os.domain import (
    Certification,
    CertificationResult,
    Evidence,
    EvidenceType,
    GateResult,
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    ProjectState,
    UserStory,
    UserStoryStatus,
)

from .architect import (
    ArchitectInput,
    ArchitectResult,
    ArchitectResultValidator,
    ArchitectVerdict,
)
from .certification_service import AcceptanceResult, CertificationContext
from .certifier import (
    CertifierInput,
    CertifierResult,
    CertifierResultValidator,
    CertifierVerdict,
)
from .control_loop import ControlLoop
from .evidence_recorder import EvidenceObservation
from .gate_evaluator import GateContract, GateEvaluation, GateEvaluationContext
from .implementer import (
    ImplementerInput,
    ImplementerResult,
    ImplementerResultValidator,
    ImplementerVerdict,
)
from .orchestrator import Orchestrator, RoleHandoff
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


@dataclass(frozen=True, slots=True)
class SequentialMissionResult:
    mission_id: str
    workflow_generation: int
    status: MissionStatus
    current_role: MissionRole
    current_step: OperatingStep
    blockers: tuple[str, ...]
    last_validated_role: MissionRole | None
    recommended_next_action: str
    handoff: RoleHandoff | None = None


class SequentialMissionWorkflowError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class SequentialMissionWorkflow:
    """Coordinate the fixed V1 role chain without duplicating its authorities."""

    def __init__(
        self,
        *,
        orchestrator: Orchestrator,
        mission_store: MissionStateStorePort,
        project_store: ProjectStateStorePort,
        control_loop: ControlLoop,
        architect_validator: ArchitectResultValidator | None = None,
        implementer_validator: ImplementerResultValidator | None = None,
        tester_validator: TesterResultValidator | None = None,
        reviewer_validator: ReviewerResultValidator | None = None,
        certifier_validator: CertifierResultValidator | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._mission_store = mission_store
        self._project_store = project_store
        self._control_loop = control_loop
        self._architect_validator = architect_validator or ArchitectResultValidator()
        self._implementer_validator = (
            implementer_validator or ImplementerResultValidator()
        )
        self._tester_validator = tester_validator or TesterResultValidator()
        self._reviewer_validator = reviewer_validator or ReviewerResultValidator()
        self._certifier_validator = certifier_validator or CertifierResultValidator()

    def route(
        self,
        *,
        current_commit: str,
        updated_at: datetime,
    ) -> SequentialMissionResult:
        result = self._orchestrator.orchestrate(
            current_commit=current_commit,
            updated_at=updated_at,
        )
        mission = result.updated_mission_state or self._mission_store.load()
        if not result.success:
            if result.reason == "HUMAN_REQUIRED" and mission.status is MissionStatus.ACTIVE:
                mission = self._block(
                    mission,
                    "HUMAN_REQUIRED",
                    next_action="Apply attributable Human approval before resuming.",
                    updated_at=updated_at,
                )
            return self._result(
                mission,
                last_validated_role=None,
                handoff=None,
            )
        return self._result(
            mission,
            last_validated_role=None,
            handoff=result.handoff,
        )

    def accept_architect(
        self,
        handoff: RoleHandoff,
        candidate: ArchitectResult,
        *,
        updated_at: datetime,
    ) -> SequentialMissionResult:
        mission = self._require_stage(
            handoff,
            MissionRole.ARCHITECT,
            OperatingStep.UNDERSTAND_CONTRACT,
        )
        architect_input = ArchitectInput.from_handoff(handoff)
        state = self._project_store.load()
        validation = self._architect_validator.validate(
            candidate,
            architect_input=architect_input,
            known_user_story_ids=(story.id for story in state.user_stories),
        )
        self._require_valid(validation.is_valid, "INVALID_ARCHITECT_RESULT")
        if candidate.verdict is ArchitectVerdict.BLOCKED:
            return self._result(
                self._block(
                    mission,
                    *candidate.blockers,
                    next_action="Resolve Architect blockers.",
                    updated_at=updated_at,
                ),
                last_validated_role=MissionRole.ARCHITECT,
            )
        if candidate.verdict is not ArchitectVerdict.READY:
            raise SequentialMissionWorkflowError(
                "ARCHITECT_NOT_READY", "Architect did not produce READY"
            )
        matching = [story for story in candidate.user_stories if story.id == handoff.subject]
        if len(matching) != 1 or len(candidate.user_stories) != 1:
            raise SequentialMissionWorkflowError(
                "AMBIGUOUS_ARCHITECT_STORY",
                "V1 requires exactly one candidate matching the mission subject",
            )
        if any(story.id == handoff.subject for story in state.user_stories):
            raise SequentialMissionWorkflowError(
                "USER_STORY_ALREADY_EXISTS", "Architect cannot replace a User Story"
            )
        self._control_loop.add_user_story(deepcopy(matching[0]))
        self._transition(handoff.subject, UserStoryStatus.PLANNED)
        self._transition(handoff.subject, UserStoryStatus.READY)
        persisted = self._story(handoff.subject)
        if persisted.human_approval.required and not persisted.human_approval.approved:
            waiting = self._advance(
                mission,
                OperatingStep.ACT,
                "Ask Orchestrator to evaluate the Human requirement.",
                updated_at,
            )
            self.route(
                current_commit=waiting.observed_commit,
                updated_at=updated_at,
            )
            return self._result(
                self._mission_store.load(),
                last_validated_role=MissionRole.ARCHITECT,
            )
        self._transition(handoff.subject, UserStoryStatus.IN_PROGRESS)
        advanced = self._advance(
            mission,
            OperatingStep.ACT,
            "Route the validated User Story to Implementer.",
            updated_at,
        )
        return self._result(advanced, last_validated_role=MissionRole.ARCHITECT)

    def resume_after_human_approval(
        self,
        *,
        evidence_id: str,
        current_commit: str,
        updated_at: datetime,
    ) -> SequentialMissionResult:
        mission = self._mission_store.load()
        if mission.status is not MissionStatus.BLOCKED or not any(
            blocker.split(":", 1)[0].strip().casefold() == "human_required"
            for blocker in mission.blockers
        ):
            raise SequentialMissionWorkflowError(
                "HUMAN_RESUME_NOT_REQUIRED",
                "mission is not waiting for Human approval",
            )
        if current_commit != mission.observed_commit:
            raise SequentialMissionWorkflowError(
                "COMMIT_DIVERGENCE_RECONSTRUCT_REQUIRED",
                "Human resume cannot bypass repository reconstruction",
            )
        self._control_loop.apply_human_approval(
            mission.subject,
            evidence_id,
            expected_commit=current_commit,
        )
        persisted = self._story(mission.subject)
        if not persisted.human_approval.approved:
            raise SequentialMissionWorkflowError(
                "HUMAN_APPROVAL_NOT_APPLIED",
                "Human Evidence did not produce an applied approval",
            )
        if persisted.status is UserStoryStatus.READY:
            self._transition(persisted.id, UserStoryStatus.IN_PROGRESS)
        resumed = replace(
            mission,
            status=MissionStatus.ACTIVE,
            role=MissionRole.ORCHESTRATOR,
            operating_step=OperatingStep.ACT,
            blockers=[],
            next_action="Route the approved User Story to Implementer.",
            observed_commit=current_commit,
            updated_at=updated_at,
        )
        self._save_mission(mission, resumed, operation="RESUME_AFTER_HUMAN")
        return self._result(resumed, last_validated_role=MissionRole.ARCHITECT)

    def accept_implementer(
        self,
        handoff: RoleHandoff,
        candidate: ImplementerResult,
        *,
        updated_at: datetime,
    ) -> SequentialMissionResult:
        mission = self._require_stage(
            handoff,
            MissionRole.IMPLEMENTER,
            OperatingStep.ACT,
        )
        implementer_input = ImplementerInput.from_handoff(
            handoff,
            self._story(handoff.subject),
        )
        validation = self._implementer_validator.validate(
            candidate,
            implementer_input=implementer_input,
        )
        self._require_valid(validation.is_valid, "INVALID_IMPLEMENTER_RESULT")
        if candidate.verdict is ImplementerVerdict.BLOCKED:
            self._transition(handoff.subject, UserStoryStatus.BLOCKED)
            blocked = self._block(
                mission,
                *candidate.blockers,
                next_action="Resolve Implementer blockers.",
                updated_at=updated_at,
            )
            return self._result(
                blocked,
                last_validated_role=MissionRole.IMPLEMENTER,
            )
        if candidate.verdict is not ImplementerVerdict.READY_FOR_TEST:
            raise SequentialMissionWorkflowError(
                "IMPLEMENTER_NOT_READY", "Implementer did not produce READY_FOR_TEST"
            )
        self._transition(handoff.subject, UserStoryStatus.IMPLEMENTED)
        self._transition(handoff.subject, UserStoryStatus.TESTING)
        advanced = self._advance(
            mission,
            OperatingStep.VERIFY,
            "Route the implemented User Story to Tester.",
            updated_at,
        )
        return self._result(advanced, last_validated_role=MissionRole.IMPLEMENTER)

    def accept_tester(
        self,
        handoff: RoleHandoff,
        candidate: TesterResult,
        *,
        implementer_result: ImplementerResult,
        updated_at: datetime,
    ) -> SequentialMissionResult:
        mission = self._require_stage(
            handoff,
            MissionRole.TESTER,
            OperatingStep.VERIFY,
        )
        tester_input = TesterInput.from_handoff(
            handoff,
            self._story(handoff.subject),
            implementer_result,
        )
        validation = self._tester_validator.validate(
            candidate,
            tester_input=tester_input,
        )
        self._require_valid(validation.is_valid, "INVALID_TESTER_RESULT")
        if candidate.verdict is TesterVerdict.READY_FOR_REVIEW:
            self._transition(handoff.subject, UserStoryStatus.REVIEW)
            advanced = self._advance(
                mission,
                OperatingStep.REPORT,
                "Route the verified User Story to Reviewer.",
                updated_at,
            )
            return self._result(advanced, last_validated_role=MissionRole.TESTER)
        if candidate.verdict is TesterVerdict.REMEDIATION_REQUIRED:
            return self._remediate(
                mission,
                handoff.subject,
                MissionRole.TESTER,
                updated_at,
            )
        if candidate.verdict is TesterVerdict.BLOCKED:
            blocked = self._block(
                mission,
                *candidate.blockers,
                next_action="Resolve Tester blockers without entering review.",
                updated_at=updated_at,
            )
            return self._result(blocked, last_validated_role=MissionRole.TESTER)
        raise SequentialMissionWorkflowError(
            "TESTER_VERDICT_UNKNOWN", "Tester verdict is not supported"
        )

    def record_acceptance_evidence(
        self,
        tester_result: TesterResult,
        observation: EvidenceObservation,
        *,
        evidence_id: str,
        timestamp: datetime,
    ) -> Evidence:
        matches = [
            item
            for item in tester_result.acceptance_results
            if item.acceptance_criterion_id == observation.subject
        ]
        if len(matches) != 1:
            raise SequentialMissionWorkflowError(
                "ACCEPTANCE_RESULT_MISSING",
                "Evidence subject must resolve one Tester acceptance result",
            )
        expected = {
            GateResult.PASS: True,
            GateResult.FAIL: False,
        }.get(matches[0].result)
        if (
            observation.evidence_type is not EvidenceType.ACCEPTANCE_CRITERION_CHECK
            or not isinstance(observation.result, bool)
            or expected is None
            or observation.result is not expected
        ):
            raise SequentialMissionWorkflowError(
                "ACCEPTANCE_EVIDENCE_CONTRADICTION",
                "Acceptance Evidence must be a strict boolean matching Tester",
            )
        return self._control_loop.record_evidence(
            observation,
            evidence_id=evidence_id,
            timestamp=timestamp,
        )

    def record_evidence(
        self,
        observation: EvidenceObservation,
        *,
        evidence_id: str,
        timestamp: datetime,
    ) -> Evidence:
        """Delegate explicit non-role Evidence recording to the Control Plane."""

        return self._control_loop.record_evidence(
            observation,
            evidence_id=evidence_id,
            timestamp=timestamp,
        )

    def evaluate_gate(
        self,
        contract: GateContract,
        *,
        context: GateEvaluationContext,
        evaluated_at: datetime,
    ) -> GateEvaluation:
        """Delegate authoritative Gate calculation and persistence."""

        return self._control_loop.evaluate_gate(
            contract,
            context=context,
            evaluated_at=evaluated_at,
        )

    def accept_reviewer(
        self,
        handoff: RoleHandoff,
        candidate: ReviewerResult,
        *,
        implementer_result: ImplementerResult,
        tester_result: TesterResult,
        updated_at: datetime,
    ) -> SequentialMissionResult:
        mission = self._require_stage(
            handoff,
            MissionRole.REVIEWER,
            OperatingStep.REPORT,
        )
        reviewer_input = ReviewerInput.from_handoff(
            handoff,
            self._story(handoff.subject),
            implementer_result,
            tester_result,
        )
        validation = self._reviewer_validator.validate(
            candidate,
            reviewer_input=reviewer_input,
        )
        self._require_valid(validation.is_valid, "INVALID_REVIEWER_RESULT")
        if candidate.verdict is ReviewerVerdict.READY_FOR_CERTIFICATION:
            self._transition(handoff.subject, UserStoryStatus.CERTIFICATION)
            advanced = self._advance(
                mission,
                OperatingStep.CONTROLLED_TRANSITION,
                "Route the reviewed dossier to Certifier.",
                updated_at,
            )
            return self._result(advanced, last_validated_role=MissionRole.REVIEWER)
        if candidate.verdict is ReviewerVerdict.REMEDIATION_REQUIRED:
            return self._remediate(
                mission,
                handoff.subject,
                MissionRole.REVIEWER,
                updated_at,
            )
        if candidate.verdict is ReviewerVerdict.BLOCKED:
            blocked = self._block(
                mission,
                *candidate.blockers,
                next_action="Resolve Reviewer blockers without certification.",
                updated_at=updated_at,
            )
            return self._result(blocked, last_validated_role=MissionRole.REVIEWER)
        raise SequentialMissionWorkflowError(
            "REVIEWER_VERDICT_UNKNOWN", "Reviewer verdict is not supported"
        )

    def submit_control_plane(
        self,
        handoff: RoleHandoff,
        candidate: CertifierResult,
        *,
        architect_result: ArchitectResult,
        implementer_result: ImplementerResult,
        tester_result: TesterResult,
        reviewer_result: ReviewerResult,
        acceptance_results: Iterable[AcceptanceResult],
        certification_context: CertificationContext,
        certifier: str,
        current_commit: str,
        updated_at: datetime,
        authorized_not_applicable_gate_ids: frozenset[str] = frozenset(),
        certification_id: str | None = None,
    ) -> SequentialMissionResult:
        mission = self._require_stage(
            handoff,
            MissionRole.CERTIFIER,
            OperatingStep.CONTROLLED_TRANSITION,
        )
        if current_commit != mission.observed_commit:
            raise SequentialMissionWorkflowError(
                "COMMIT_DIVERGENCE_RECONSTRUCT_REQUIRED",
                "Control Plane submission cannot bypass reconstruction",
            )
        state = self._project_store.load()
        story = self._unique_story(state, handoff.subject)
        certifier_input = CertifierInput.from_handoff(
            handoff,
            story,
            architect_result,
            implementer_result,
            tester_result,
            reviewer_result,
            tuple(state.evidence),
            tuple(state.gates),
            authorized_not_applicable_gate_ids=(
                authorized_not_applicable_gate_ids
            ),
        )
        validation = self._certifier_validator.validate(
            candidate,
            certifier_input=certifier_input,
        )
        self._require_valid(validation.is_valid, "INVALID_CERTIFIER_RESULT")
        if candidate.verdict is CertifierVerdict.REMEDIATION_REQUIRED:
            return self._remediate(
                mission,
                handoff.subject,
                MissionRole.CERTIFIER,
                updated_at,
            )
        if candidate.verdict is CertifierVerdict.BLOCKED:
            blocked = self._block(
                mission,
                *candidate.blockers,
                next_action="Resolve Certifier blockers without Control Plane.",
                updated_at=updated_at,
            )
            return self._result(blocked, last_validated_role=MissionRole.CERTIFIER)
        if candidate.verdict is not CertifierVerdict.READY_FOR_CONTROL_PLANE:
            raise SequentialMissionWorkflowError(
                "CERTIFIER_NOT_READY",
                "Certifier did not authorize Control Plane submission",
            )
        certification = self._control_loop.certify_user_story(
            story.id,
            current_commit,
            tuple(acceptance_results),
            certifier=certifier,
            context=certification_context,
            certification_id=certification_id,
            certified_at=updated_at,
        )
        return self._apply_certification_outcome(
            mission,
            certification,
            current_commit,
            updated_at,
        )

    def _apply_certification_outcome(
        self,
        mission: MissionState,
        certification: Certification,
        current_commit: str,
        updated_at: datetime,
    ) -> SequentialMissionResult:
        if certification.result is CertificationResult.CERTIFIED:
            self._control_loop.transition_user_story(
                certification.subject,
                UserStoryStatus.CERTIFIED,
                context=TransitionContext(target_commit=current_commit),
            )
            if self._story(certification.subject).status is not UserStoryStatus.CERTIFIED:
                raise SequentialMissionWorkflowError(
                    "CERTIFIED_STATE_NOT_PERSISTED",
                    "trusted transition did not persist CERTIFIED",
                )
            completed = replace(
                mission,
                status=MissionStatus.COMPLETED,
                role=MissionRole.CERTIFIER,
                operating_step=OperatingStep.REPORT,
                blockers=[],
                next_action="Mission completed with authoritative Certification.",
                observed_commit=current_commit,
                updated_at=updated_at,
            )
            self._save_mission(mission, completed, operation="COMPLETE_MISSION")
            return self._result(
                completed,
                last_validated_role=MissionRole.CERTIFIER,
            )
        if certification.result is CertificationResult.REJECTED:
            return self._remediate(
                mission,
                certification.subject,
                MissionRole.CERTIFIER,
                updated_at,
            )
        blocked = self._block(
            mission,
            "CONTROL_PLANE_BLOCKED",
            next_action="Resolve the incomplete certification dossier.",
            updated_at=updated_at,
        )
        return self._result(blocked, last_validated_role=MissionRole.CERTIFIER)

    def _remediate(
        self,
        mission: MissionState,
        subject: str,
        role: MissionRole,
        updated_at: datetime,
    ) -> SequentialMissionResult:
        self._transition(subject, UserStoryStatus.REJECTED)
        self._transition(subject, UserStoryStatus.REMEDIATION_REQUIRED)
        self._transition(subject, UserStoryStatus.READY)
        self._transition(subject, UserStoryStatus.IN_PROGRESS)
        advanced = self._advance(
            mission,
            OperatingStep.ACT,
            "Route the explicit remediation to Implementer, then re-test.",
            updated_at,
            workflow_generation=mission.workflow_generation + 1,
        )
        return self._result(advanced, last_validated_role=role)

    def _transition(self, subject: str, target: UserStoryStatus) -> None:
        state = self._project_store.load()
        story = self._unique_story(state, subject)
        dependencies = {
            dependency: self._unique_story(state, dependency).status
            for dependency in story.depends_on
        }
        self._control_loop.transition_user_story(
            subject,
            target,
            context=TransitionContext(
                preconditions_proven=True,
                dependency_statuses=dependencies,
            ),
        )

    def _require_stage(
        self,
        handoff: RoleHandoff,
        role: MissionRole,
        step: OperatingStep,
    ) -> MissionState:
        mission = self._mission_store.load()
        if (
            not isinstance(handoff, RoleHandoff)
            or mission.status is not MissionStatus.ACTIVE
            or mission.role is not role
            or mission.operating_step is not step
            or handoff.from_role is not MissionRole.ORCHESTRATOR
            or handoff.to_role is not role
            or handoff.operating_step is not step
            or handoff.mission_id != mission.mission_id
            or handoff.workflow_generation != mission.workflow_generation
            or handoff.subject != mission.subject
            or handoff.objective != mission.objective
            or handoff.blockers != tuple(mission.blockers)
            or handoff.observed_commit != mission.observed_commit
        ):
            raise SequentialMissionWorkflowError(
                "ROLE_CHAIN_VIOLATION",
                f"{role.value} cannot run outside its persisted stage",
            )
        return mission

    def _advance(
        self,
        mission: MissionState,
        step: OperatingStep,
        next_action: str,
        updated_at: datetime,
        *,
        workflow_generation: int | None = None,
    ) -> MissionState:
        candidate = replace(
            mission,
            workflow_generation=(
                mission.workflow_generation
                if workflow_generation is None
                else workflow_generation
            ),
            status=MissionStatus.ACTIVE,
            role=MissionRole.ORCHESTRATOR,
            operating_step=step,
            blockers=[],
            next_action=next_action,
            updated_at=updated_at,
        )
        self._save_mission(mission, candidate, operation="ADVANCE_MISSION")
        return candidate

    def _block(
        self,
        mission: MissionState,
        *blockers: str,
        next_action: str,
        updated_at: datetime,
    ) -> MissionState:
        reasons = [item for item in blockers if isinstance(item, str) and item.strip()]
        if not reasons:
            reasons = ["WORKFLOW_BLOCKED"]
        candidate = replace(
            mission,
            status=MissionStatus.BLOCKED,
            role=MissionRole.ORCHESTRATOR,
            operating_step=mission.operating_step,
            blockers=reasons,
            next_action=next_action,
            updated_at=updated_at,
        )
        self._save_mission(mission, candidate, operation="BLOCK_MISSION")
        return candidate

    def _save_mission(
        self,
        current: MissionState,
        candidate: MissionState,
        *,
        operation: str,
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

    def _story(self, subject: str) -> UserStory:
        return self._unique_story(self._project_store.load(), subject)

    @staticmethod
    def _unique_story(state: ProjectState, subject: str) -> UserStory:
        matches = [story for story in state.user_stories if story.id == subject]
        if len(matches) != 1:
            raise SequentialMissionWorkflowError(
                "USER_STORY_MISSING_OR_AMBIGUOUS",
                "workflow subject must resolve exactly one User Story",
            )
        return matches[0]

    @staticmethod
    def _require_valid(valid: bool, code: str) -> None:
        if not valid:
            raise SequentialMissionWorkflowError(
                code,
                "role output failed its deterministic validator",
            )

    @staticmethod
    def _result(
        mission: MissionState,
        *,
        last_validated_role: MissionRole | None,
        handoff: RoleHandoff | None = None,
    ) -> SequentialMissionResult:
        return SequentialMissionResult(
            mission_id=mission.mission_id,
            workflow_generation=mission.workflow_generation,
            status=mission.status,
            current_role=mission.role,
            current_step=mission.operating_step,
            blockers=tuple(mission.blockers),
            last_validated_role=last_validated_role,
            recommended_next_action=mission.next_action,
            handoff=handoff,
        )
