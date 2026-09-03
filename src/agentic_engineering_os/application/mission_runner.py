"""Bounded restart-safe composition of the complete supported mission path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from agentic_engineering_os.domain import (
    Evidence,
    EvidenceType,
    GateResult,
    MissionRole,
    MissionState,
    MissionStatus,
    ProjectConfiguration,
    ProjectState,
    UserStoryStatus,
)

from .mission_admission import (
    MissionAdmission,
    MissionAdmissionStatus,
    MissionRequest,
)
from .evidence_recorder import EvidenceObservation, EvidenceProvenance, ProvenanceKind
from .mission_certification import (
    MissionCertificationCoordinator,
    MissionCertificationError,
    MissionCertificationResult,
    MissionCertificationStatus,
)
from .mission_integration import (
    MissionIntegrationCoordinator,
    MissionIntegrationError,
    MissionIntegrationResult,
    MissionIntegrationStatus,
)
from .mission_planning import (
    MissionPlanningCoordinator,
    MissionPlanningStatus,
)
from .orchestration_record import OrchestrationRecord
from .parallel_mission_workflow import ParallelMissionWorkflow, ParallelStoryDossier
from .parallel_mission_workflow import ParallelMissionWorkflowError
from .verification_coordinator import (
    VerificationCoordinationError,
    VerificationRunResult,
)


class MissionRunStatus(str, Enum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    REMEDIATION_REQUIRED = "REMEDIATION_REQUIRED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    BLOCKED = "BLOCKED"
    REFUSED = "REFUSED"


class MissionPhase(str, Enum):
    ADMISSION = "ADMISSION"
    PLANNING = "PLANNING"
    IMPLEMENTATION = "IMPLEMENTATION"
    VERIFICATION = "VERIFICATION"
    TESTING = "TESTING"
    REVIEW = "REVIEW"
    CERTIFICATION = "CERTIFICATION"
    REPORT = "REPORT"


@dataclass(frozen=True, slots=True)
class MissionRoleLaunchResult:
    validated: bool
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MissionRunResult:
    mission_id: str | None
    status: MissionRunStatus
    phase: MissionPhase
    generation: int | None
    current_story_ids: tuple[str, ...]
    completed_story_ids: tuple[str, ...]
    blockers: tuple[str, ...]
    next_action: str
    repository_head: str | None
    evidence_references: tuple[str, ...] = ()
    blocker_details: tuple[str, ...] = ()


class MissionRunnerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class MissionContinuationError(RuntimeError):
    """A reconstructed operational fact refuses continued mutation."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class _AdmissionPort(Protocol):
    def evaluate(self, request: MissionRequest) -> MissionAdmission: ...


class _RoleExecutorPort(Protocol):
    def execute(
        self,
        dossier: ParallelStoryDossier,
        role: MissionRole,
        *,
        request_id: str,
        updated_at: datetime,
    ) -> MissionRoleLaunchResult: ...


class _Reader(Protocol):
    def load(self) -> object: ...


class _VerificationPort(Protocol):
    def verify(
        self,
        configuration: ProjectConfiguration,
        user_story: object,
        *,
        mission_id: str,
        workflow_generation: int,
        integrated_commit: str,
    ) -> VerificationRunResult: ...


class _ControlLoopPort(Protocol):
    def record_evidence(
        self,
        observation: EvidenceObservation,
        *,
        evidence_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> Evidence: ...

    def apply_human_approval(
        self, user_story_id: str, evidence_id: str, *, expected_commit: str
    ) -> object: ...


class _MissionEventSink(Protocol):
    def record(self, result: MissionRunResult, *, occurred_at: datetime) -> None: ...


class _ContinuationAdmissionPort(Protocol):
    def authorize(self) -> None: ...


class _RecordStore(_Reader, Protocol):
    def replace(
        self, record: OrchestrationRecord, *, expected_fingerprint: str
    ) -> object: ...


class MissionRunner:
    """Drive existing authorities until completion or an explicit wait boundary."""

    def __init__(
        self,
        *,
        admission: _AdmissionPort,
        planning: MissionPlanningCoordinator,
        integration: MissionIntegrationCoordinator,
        certification: MissionCertificationCoordinator,
        verification: _VerificationPort,
        control_loop: _ControlLoopPort,
        role_executor: _RoleExecutorPort,
        workflow: ParallelMissionWorkflow,
        mission_store: _Reader,
        project_store: _Reader,
        record_store: _RecordStore,
        configuration_store: _Reader,
        event_sink: _MissionEventSink | None = None,
        continuation_admission: _ContinuationAdmissionPort | None = None,
        maximum_steps: int = 256,
    ) -> None:
        if not isinstance(maximum_steps, int) or isinstance(maximum_steps, bool) or maximum_steps <= 0:
            raise ValueError("maximum_steps must be a positive integer")
        self._admission = admission
        self._planning = planning
        self._integration = integration
        self._certification = certification
        self._verification = verification
        self._control = control_loop
        self._roles = role_executor
        self._workflow = workflow
        self._missions = mission_store
        self._projects = project_store
        self._records = record_store
        self._configuration = configuration_store
        self._events = event_sink
        self._continuation_admission = continuation_admission
        self._maximum_steps = maximum_steps

    def run(self, request: MissionRequest, *, updated_at: datetime) -> MissionRunResult:
        admission = self._admission.evaluate(request)
        if admission.status is not MissionAdmissionStatus.ADMITTED:
            status = (
                MissionRunStatus.WAITING_FOR_HUMAN
                if admission.status is MissionAdmissionStatus.HUMAN_REQUIRED
                else MissionRunStatus.REFUSED
            )
            return self._record_result(MissionRunResult(
                None,
                status,
                MissionPhase.ADMISSION,
                None,
                (),
                (),
                tuple(item.code for item in admission.blockers),
                admission.next_action,
                admission.repository_head,
                blocker_details=tuple(
                    f"{item.code}:{item.detail}" for item in admission.blockers
                ),
            ), updated_at)
        planning = self._planning.start(request, admission, updated_at=updated_at)
        if planning.status is not MissionPlanningStatus.PLANNED:
            return self._record_result(self._result(
                MissionRunStatus.BLOCKED,
                MissionPhase.PLANNING,
                planning.blockers,
                "Resolve Architect planning blockers, then resume explicitly.",
            ), updated_at)
        self._observe("mission_started", planning.mission_id, occurred_at=updated_at)
        return self._record_result(
            self._continue(
                planning.mission_id, updated_at=updated_at, allow_remediation=False
            ), updated_at
        )

    def resume(
        self,
        mission_id: str,
        *,
        updated_at: datetime,
        human_evidence: Evidence | None = None,
    ) -> MissionRunResult:
        if not isinstance(mission_id, str) or not mission_id.strip():
            raise MissionRunnerError("INVALID_MISSION_ID", "mission_id is required")
        record = self._record()
        mission = self._mission()
        record = self._reconcile_partial_remediation(record, mission)
        if record.mission_id != mission_id or mission.mission_id != mission_id:
            raise MissionRunnerError(
                "MISSION_BINDING_MISMATCH", "requested mission is not current"
            )
        self._observe("mission_resumed", mission, occurred_at=updated_at)
        if human_evidence is not None:
            self._apply_human_evidence(
                human_evidence, mission=mission, record=record
            )
            self._observe("human_approved", human_evidence, occurred_at=updated_at)
        if record.plan_fingerprint is None:
            planning = self._planning.resume(mission_id, updated_at=updated_at)
            if planning.status is not MissionPlanningStatus.PLANNED:
                return self._result(
                    MissionRunStatus.BLOCKED,
                    MissionPhase.PLANNING,
                    planning.blockers,
                    "Resolve Architect planning blockers, then resume explicitly.",
                )
        return self._record_result(
            self._continue(
                mission_id, updated_at=updated_at, allow_remediation=True
            ), updated_at
        )

    def _reconcile_partial_remediation(
        self, record: OrchestrationRecord, mission: MissionState
    ) -> OrchestrationRecord:
        """Complete only the narrowly provable record half of a remediation commit."""

        if record.workflow_generation == mission.workflow_generation:
            return record
        primary = self._workflow.primary_inspection()
        exact_successor = (
            mission.mission_id == record.mission_id
            and mission.workflow_generation == record.workflow_generation + 1
            and mission.status is MissionStatus.ACTIVE
            and mission.role is MissionRole.ORCHESTRATOR
            and not mission.blockers
            and primary.clean
            and primary.head_commit == mission.observed_commit.casefold()
        )
        if not exact_successor:
            raise MissionRunnerError(
                "RECOVERY_REQUIRED",
                "mission and orchestration generations cannot be reconciled exactly",
            )
        successor = record.for_remediation_generation(
            workflow_generation=mission.workflow_generation,
            baseline_commit=mission.observed_commit.casefold(),
        )
        self._records.replace(successor, expected_fingerprint=record.fingerprint)
        return self._record()

    def _record_result(
        self, result: MissionRunResult, occurred_at: datetime
    ) -> MissionRunResult:
        if self._events is not None:
            try:
                self._events.record(result, occurred_at=occurred_at)
            except Exception:
                # Observability never acquires business authority over the durable result.
                pass
        return result

    def _apply_human_evidence(
        self,
        evidence: Evidence,
        *,
        mission: MissionState,
        record: OrchestrationRecord,
    ) -> None:
        if (
            not isinstance(evidence, Evidence)
            or evidence.evidence_type is not EvidenceType.HUMAN_APPROVAL
            or evidence.subject not in record.user_story_ids
            or evidence.commit != mission.observed_commit
        ):
            raise MissionRunnerError(
                "HUMAN_EVIDENCE_BINDING_MISMATCH",
                "Human Evidence must target an exact mission story and current commit",
            )
        project = self._project()
        story = next(
            (item for item in project.user_stories if item.id == evidence.subject), None
        )
        if story is None or not story.human_approval.required:
            raise MissionRunnerError(
                "HUMAN_EVIDENCE_NOT_REQUIRED",
                "target story does not require Human approval",
            )
        existing = tuple(
            item for item in project.evidence if item.evidence_id == evidence.evidence_id
        )
        if existing and existing != (evidence,):
            raise MissionRunnerError(
                "HUMAN_EVIDENCE_COLLISION",
                "persisted Evidence differs from the submitted document",
            )
        if not existing:
            recorded = self._control.record_evidence(
                EvidenceObservation(
                    EvidenceType.HUMAN_APPROVAL,
                    evidence.subject,
                    evidence.result,
                    EvidenceProvenance(
                        ProvenanceKind.HUMAN, evidence.source, evidence.producer
                    ),
                    True,
                    command=evidence.command,
                    exit_code=evidence.exit_code,
                    artifact=evidence.artifact,
                    commit=evidence.commit,
                ),
                evidence_id=evidence.evidence_id,
                timestamp=evidence.timestamp,
            )
            if recorded != evidence:
                raise MissionRunnerError(
                    "HUMAN_EVIDENCE_NOT_DURABLE",
                    "recorded Human Evidence differs from submission",
                )
        refreshed = next(
            item for item in self._project().user_stories if item.id == evidence.subject
        )
        if refreshed.human_approval.approved:
            if refreshed.human_approval.evidence_ref != evidence.evidence_id:
                raise MissionRunnerError(
                    "HUMAN_APPROVAL_COLLISION",
                    "story already has a different Human approval",
                )
            return
        self._control.apply_human_approval(
            evidence.subject,
            evidence.evidence_id,
            expected_commit=mission.observed_commit,
        )

    def status(self, mission_id: str | None = None) -> MissionRunResult:
        mission = self._mission()
        if mission_id is not None and mission.mission_id != mission_id:
            raise MissionRunnerError(
                "MISSION_BINDING_MISMATCH", "requested mission is not current"
            )
        if mission.status is MissionStatus.COMPLETED:
            return self._result(
                MissionRunStatus.COMPLETED,
                MissionPhase.REPORT,
                (),
                "Mission is complete; inspect its authoritative evidence.",
            )
        if mission.status is MissionStatus.BLOCKED:
            status = _blocked_status(tuple(mission.blockers))
            return self._result(status, _phase(mission, self._project()), tuple(mission.blockers), mission.next_action)
        return self._result(
            MissionRunStatus.ACTIVE,
            _phase(mission, self._project()),
            (),
            mission.next_action,
        )

    @staticmethod
    def read_status(
        *,
        mission_store: _Reader,
        project_store: _Reader,
        record_store: _Reader,
        mission_id: str | None = None,
    ) -> MissionRunResult:
        """Project status from durable stores without constructing mutating services."""

        mission = mission_store.load()
        project = project_store.load()
        record = record_store.load()
        if (
            not isinstance(mission, MissionState)
            or not isinstance(project, ProjectState)
            or not isinstance(record, OrchestrationRecord)
        ):
            raise MissionRunnerError(
                "MISSION_STATE_INVALID", "durable mission authorities are unavailable"
            )
        if (
            (mission_id is not None and mission.mission_id != mission_id)
            or record.mission_id != mission.mission_id
            or record.workflow_generation != mission.workflow_generation
        ):
            raise MissionRunnerError(
                "MISSION_BINDING_MISMATCH", "requested mission authorities disagree"
            )
        stories = _mission_stories(project, record)
        completed = tuple(
            item.id for item in stories if item.status is UserStoryStatus.CERTIFIED
        )
        current = tuple(
            item.id for item in stories if item.status is not UserStoryStatus.CERTIFIED
        )
        evidence = tuple(
            sorted(
                {
                    *(item.evidence_id for item in project.evidence),
                    *(item.certification_id for item in project.certifications),
                }
            )
        )
        if mission.status is MissionStatus.COMPLETED:
            status = MissionRunStatus.COMPLETED
            phase = MissionPhase.REPORT
        elif mission.status is MissionStatus.BLOCKED:
            status = _blocked_status(tuple(mission.blockers))
            phase = _phase(mission, project)
        else:
            status = MissionRunStatus.ACTIVE
            phase = _phase(mission, project)
        return MissionRunResult(
            mission.mission_id,
            status,
            phase,
            mission.workflow_generation,
            current,
            completed,
            tuple(mission.blockers),
            mission.next_action,
            mission.observed_commit,
            evidence,
        )

    def _continue(
        self,
        mission_id: str,
        *,
        updated_at: datetime,
        allow_remediation: bool,
    ) -> MissionRunResult:
        try:
            return self._drive(
                mission_id,
                updated_at=updated_at,
                allow_remediation=allow_remediation,
            )
        except (
            MissionIntegrationError,
            MissionCertificationError,
            ParallelMissionWorkflowError,
            VerificationCoordinationError,
            MissionContinuationError,
        ) as error:
            return self._controlled_failure(error)

    def _drive(
        self,
        mission_id: str,
        *,
        updated_at: datetime,
        allow_remediation: bool,
    ) -> MissionRunResult:
        for _ in range(self._maximum_steps):
            self._authorize_continuation()
            mission = self._mission()
            record = self._record()
            project = self._project()
            self._require_bindings(mission_id, mission, record)
            stories = _mission_stories(project, record)
            if stories and all(item.status is UserStoryStatus.CERTIFIED for item in stories):
                completed = self._workflow.finalize(
                    current_commit=mission.observed_commit, updated_at=updated_at
                )
                if completed.status is not MissionStatus.COMPLETED:
                    raise MissionRunnerError(
                        "FINALIZATION_REFUSED", "workflow did not complete the mission"
                    )
                return self._result(
                    MissionRunStatus.COMPLETED,
                    MissionPhase.REPORT,
                    (),
                    "Mission completed with authoritative Certifications.",
                )

            if record.parallel_integration is None:
                integration = self._integration.resume(
                    mission_id, updated_at=updated_at
                )
                self._observe("integration", integration, occurred_at=updated_at)
                if integration.status is not MissionIntegrationStatus.READY_FOR_TESTER:
                    if self._begin_integration_remediation(
                        integration,
                        record=self._record(),
                        updated_at=updated_at,
                        allowed=allow_remediation,
                    ):
                        allow_remediation = False
                        continue
                    return self._blocked_integration(integration.blockers)
                continue

            integration = self._integration.resume(mission_id, updated_at=updated_at)
            self._observe("integration", integration, occurred_at=updated_at)
            if integration.status is not MissionIntegrationStatus.READY_FOR_TESTER:
                if self._begin_integration_remediation(
                    integration,
                    record=self._record(),
                    updated_at=updated_at,
                    allowed=allow_remediation,
                ):
                    allow_remediation = False
                    continue
                return self._blocked_integration(integration.blockers)
            for story_id in integration.user_story_ids:
                project = self._project()
                story = next(
                    (item for item in project.user_stories if item.id == story_id), None
                )
                configuration = self._configuration.load()
                if story is None or not isinstance(configuration, ProjectConfiguration):
                    raise MissionRunnerError(
                        "VERIFICATION_CONTEXT_INVALID",
                        "story or ProjectConfiguration is unavailable",
                    )
                verified = self._verification.verify(
                    configuration,
                    story,
                    mission_id=mission_id,
                    workflow_generation=record.workflow_generation,
                    integrated_commit=integration.integrated_commit or "",
                )
                self._observe("verification", verified, occurred_at=updated_at)
                failed_gates = tuple(
                    item.gate.gate_id
                    for item in verified.gates
                    if item.gate.result is not GateResult.PASS
                )
                if verified.blockers or failed_gates:
                    blockers = (
                        *verified.blockers,
                        *(f"GATE_NOT_PASS:{item}" for item in failed_gates),
                    )
                    return self._result(
                        MissionRunStatus.REMEDIATION_REQUIRED,
                        MissionPhase.VERIFICATION,
                        blockers,
                        "Start an explicit new-generation remediation before resuming.",
                    )
                while True:
                    self._authorize_continuation()
                    certification = self._certification.resume(
                        mission_id, story_id, updated_at=updated_at
                    )
                    self._observe(
                        "certification", certification, occurred_at=updated_at
                    )
                    if certification.status is MissionCertificationStatus.CERTIFIED:
                        break
                    role = _waiting_role(certification)
                    if role is None:
                        if (
                            certification.status
                            is MissionCertificationStatus.REMEDIATION_REQUIRED
                            and allow_remediation
                        ):
                            previous = self._record()
                            remediation = self._workflow.remediate_dossier(
                                certification.dossier, updated_at=updated_at
                            )
                            self._observe(
                                "remediation", remediation, occurred_at=updated_at
                            )
                            successor = previous.for_remediation_generation(
                                workflow_generation=remediation.new_generation,
                                baseline_commit=remediation.baseline_commit,
                            )
                            self._records.replace(
                                successor,
                                expected_fingerprint=previous.fingerprint,
                            )
                            allow_remediation = False
                            break
                        status = (
                            MissionRunStatus.REMEDIATION_REQUIRED
                            if certification.status
                            is MissionCertificationStatus.REMEDIATION_REQUIRED
                            else MissionRunStatus.BLOCKED
                        )
                        return self._result(
                            status,
                            _certification_phase(certification),
                            certification.blockers,
                            "Start an explicit new-generation remediation before resuming."
                            if status is MissionRunStatus.REMEDIATION_REQUIRED
                            else "Resolve the recorded certification blocker.",
                        )
                    launched = self._roles.execute(
                        certification.dossier,
                        role,
                        request_id=(
                            f"{mission_id}-{role.value.casefold()}-"
                            f"{story_id}-g{record.workflow_generation}"
                        ),
                        updated_at=updated_at,
                    )
                    self._observe(
                        "role_execution",
                        certification.dossier,
                        role,
                        launched,
                        occurred_at=updated_at,
                    )
                    if not isinstance(launched, MissionRoleLaunchResult) or not launched.validated:
                        blockers = (
                            launched.blockers
                            if isinstance(launched, MissionRoleLaunchResult)
                            else ("ROLE_EXECUTION_RESULT_INVALID",)
                        )
                        return self._result(
                            MissionRunStatus.BLOCKED,
                            _role_phase(role),
                            blockers or (f"{role.value}_RESULT_UNAVAILABLE",),
                            "Inspect the failed role execution, then resume explicitly.",
                        )

                if allow_remediation is False and self._record().workflow_generation != record.workflow_generation:
                    break

            if self._record().workflow_generation != record.workflow_generation:
                continue

            current = self._record()
            current_project = self._project()
            integrated_ids = set(integration.user_story_ids)
            if not integrated_ids or any(
                item.status is not UserStoryStatus.CERTIFIED
                for item in current_project.user_stories
                if item.id in integrated_ids
            ):
                raise MissionRunnerError(
                    "CERTIFICATION_CHECKPOINT_INCOMPLETE",
                    "integrated group is not fully certified",
                )
            self._records.replace(
                current.without_parallel_integration(),
                expected_fingerprint=current.fingerprint,
            )

        return self._result(
            MissionRunStatus.RECOVERY_REQUIRED,
            MissionPhase.IMPLEMENTATION,
            ("MISSION_STEP_LIMIT_EXCEEDED",),
            "Inspect persisted progress before an explicit resume.",
        )

    def _authorize_continuation(self) -> None:
        if self._continuation_admission is not None:
            self._continuation_admission.authorize()

    def _begin_integration_remediation(
        self,
        integration: MissionIntegrationResult,
        *,
        record: OrchestrationRecord,
        updated_at: datetime,
        allowed: bool,
    ) -> bool:
        attempt = integration.remediation_attempt
        if not allowed or attempt is None:
            return False
        if attempt.gate_result.result.value != "FAIL" and (
            attempt.merge_result is None
            or attempt.merge_result.result.value != "FAILED"
        ):
            return False
        attributable = {
            member
            for finding in attempt.gate_result.findings
            for member in finding.members
            if member in integration.user_story_ids
        }
        affected = tuple(
            item
            for item in integration.user_story_ids
            if item in (attributable or set(integration.user_story_ids))
        )
        remediation = self._workflow.remediate_integration(
            attempt,
            affected_user_story_ids=affected,
            updated_at=updated_at,
        )
        self._observe("remediation", remediation, occurred_at=updated_at)
        successor = record.for_remediation_generation(
            workflow_generation=remediation.new_generation,
            baseline_commit=remediation.baseline_commit,
        )
        self._records.replace(successor, expected_fingerprint=record.fingerprint)
        return True

    def _observe(self, method: str, *values: object, occurred_at: datetime) -> None:
        if self._events is None:
            return
        observer = getattr(self._events, method, None)
        if not callable(observer):
            return
        try:
            observer(*values, occurred_at=occurred_at)
        except Exception:
            # Operational observations remain non-authoritative and fail isolated.
            pass

    def _controlled_failure(self, error: RuntimeError) -> MissionRunResult:
        code = str(getattr(error, "code", type(error).__name__))
        folded = code.casefold()
        if any(token in folded for token in ("recovery", "unknown", "diverg", "stale", "merge")):
            status = MissionRunStatus.RECOVERY_REQUIRED
            action = "Reconstruct the recorded technical boundary before resuming."
        elif any(token in folded for token in ("remediation", "gate", "verification", "test")):
            status = MissionRunStatus.REMEDIATION_REQUIRED
            action = "Start an explicit new-generation remediation before resuming."
        elif "human" in folded:
            status = MissionRunStatus.WAITING_FOR_HUMAN
            action = "Apply attributable Human Evidence before resuming."
        else:
            status = MissionRunStatus.BLOCKED
            action = "Resolve the recorded controlled failure before resuming."
        return self._result(
            status,
            MissionPhase.VERIFICATION
            if isinstance(error, VerificationCoordinationError)
            else MissionPhase.CERTIFICATION
            if isinstance(error, MissionCertificationError)
            else MissionPhase.IMPLEMENTATION,
            (code,),
            action,
        )

    def _blocked_integration(self, blockers: tuple[str, ...]) -> MissionRunResult:
        project = self._project()
        waiting_human = any(
            item.status is not UserStoryStatus.CERTIFIED
            and item.human_approval.required
            and not item.human_approval.approved
            for item in project.user_stories
        )
        recovery = any(
            "UNKNOWN" in item
            or "RECOVERY" in item
            or "MERGE_BLOCKED" in item
            or "MERGE_NOT_COMPLETED" in item
            for item in blockers
        )
        remediation = any(
            "INTEGRATION_GATE_FAIL" in item or "MERGE_FAILED" in item
            for item in blockers
        )
        status = (
            MissionRunStatus.WAITING_FOR_HUMAN
            if waiting_human
            else MissionRunStatus.RECOVERY_REQUIRED
            if recovery
            else MissionRunStatus.REMEDIATION_REQUIRED
            if remediation
            else MissionRunStatus.BLOCKED
        )
        return self._result(
            status,
            MissionPhase.IMPLEMENTATION,
            blockers,
            "Apply attributable Human Evidence before resuming."
            if waiting_human
            else "Reconstruct the recorded technical boundary before resuming."
            if recovery
            else "Start an explicit new-generation remediation before resuming."
            if remediation
            else "Resolve implementation blockers, then resume explicitly.",
        )

    def _result(
        self,
        status: MissionRunStatus,
        phase: MissionPhase,
        blockers: tuple[str, ...],
        next_action: str,
    ) -> MissionRunResult:
        mission = self._mission()
        project = self._project()
        record = self._record()
        stories = _mission_stories(project, record)
        completed = tuple(
            item.id for item in stories if item.status is UserStoryStatus.CERTIFIED
        )
        current = tuple(
            item.id for item in stories if item.status is not UserStoryStatus.CERTIFIED
        )
        evidence = tuple(
            sorted(
                {
                    *(item.evidence_id for item in project.evidence),
                    *(item.certification_id for item in project.certifications),
                }
            )
        )
        return MissionRunResult(
            mission.mission_id,
            status,
            phase,
            mission.workflow_generation,
            current,
            completed,
            tuple(blockers),
            next_action,
            mission.observed_commit,
            evidence,
        )

    def _mission(self) -> MissionState:
        value = self._missions.load()
        if not isinstance(value, MissionState):
            raise MissionRunnerError("MISSION_STATE_INVALID", "MissionState is unavailable")
        return value

    def _project(self) -> ProjectState:
        value = self._projects.load()
        if not isinstance(value, ProjectState):
            raise MissionRunnerError("PROJECT_STATE_INVALID", "ProjectState is unavailable")
        return value

    def _record(self) -> OrchestrationRecord:
        value = self._records.load()
        if not isinstance(value, OrchestrationRecord):
            raise MissionRunnerError(
                "ORCHESTRATION_RECORD_INVALID", "orchestration record is unavailable"
            )
        return value

    @staticmethod
    def _require_bindings(
        mission_id: str, mission: MissionState, record: OrchestrationRecord
    ) -> None:
        if (
            mission.mission_id != mission_id
            or record.mission_id != mission_id
            or mission.workflow_generation != record.workflow_generation
            or mission.objective != record.request.objective
        ):
            raise MissionRunnerError(
                "MISSION_BINDING_MISMATCH", "mission authorities disagree"
            )


def _mission_stories(project: ProjectState, record: OrchestrationRecord):
    by_id = {item.id: item for item in project.user_stories}
    if not record.user_story_ids:
        if record.plan_fingerprint is None:
            return ()
        raise MissionRunnerError(
            "PLANNED_STORY_UNRESOLVED", "planned orchestration has no stories"
        )
    if any(identifier not in by_id for identifier in record.user_story_ids):
        raise MissionRunnerError(
            "PLANNED_STORY_UNRESOLVED", "orchestration stories are absent"
        )
    return tuple(by_id[identifier] for identifier in record.user_story_ids)


def _waiting_role(result: MissionCertificationResult) -> MissionRole | None:
    return {
        MissionCertificationStatus.WAITING_FOR_TESTER: MissionRole.TESTER,
        MissionCertificationStatus.WAITING_FOR_REVIEWER: MissionRole.REVIEWER,
        MissionCertificationStatus.WAITING_FOR_CERTIFIER: MissionRole.CERTIFIER,
    }.get(result.status)


def _role_phase(role: MissionRole) -> MissionPhase:
    return {
        MissionRole.TESTER: MissionPhase.TESTING,
        MissionRole.REVIEWER: MissionPhase.REVIEW,
        MissionRole.CERTIFIER: MissionPhase.CERTIFICATION,
    }.get(role, MissionPhase.IMPLEMENTATION)


def _certification_phase(result: MissionCertificationResult) -> MissionPhase:
    role = _waiting_role(result)
    return _role_phase(role) if role is not None else MissionPhase.CERTIFICATION


def _blocked_status(blockers: tuple[str, ...]) -> MissionRunStatus:
    folded = tuple(item.casefold() for item in blockers)
    if any("human_required" in item for item in folded):
        return MissionRunStatus.WAITING_FOR_HUMAN
    if any("remediation" in item for item in folded):
        return MissionRunStatus.REMEDIATION_REQUIRED
    if any("recovery" in item or "unknown" in item for item in folded):
        return MissionRunStatus.RECOVERY_REQUIRED
    return MissionRunStatus.BLOCKED


def _phase(mission: MissionState, project: ProjectState) -> MissionPhase:
    statuses = {item.status for item in project.user_stories}
    if UserStoryStatus.CERTIFICATION in statuses:
        return MissionPhase.CERTIFICATION
    if UserStoryStatus.REVIEW in statuses:
        return MissionPhase.REVIEW
    if UserStoryStatus.TESTING in statuses:
        return MissionPhase.TESTING
    if mission.operating_step.value == "UNDERSTAND_CONTRACT":
        return MissionPhase.PLANNING
    return MissionPhase.IMPLEMENTATION
