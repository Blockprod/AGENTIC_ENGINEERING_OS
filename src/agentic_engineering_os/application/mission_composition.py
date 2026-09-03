"""Production composition root for the public mission facade."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from agentic_engineering_os.domain import (
    MaintenanceAdmission,
    MaintenanceAdmissionDecision,
    MaintenanceAdmissionReason,
    MaintenanceOperation,
    MaintenanceState,
    MissionRole,
    OPERATIONAL_EVENT_SCHEMA_VERSION,
    OperationalCorrelation,
    OperationalEvent,
    OperationalEventPayload,
    OperationalEventType,
    OperationalProvenance,
    OperationalProvenanceKind,
    OperationalSeverity,
)
from agentic_engineering_os.infrastructure.codex_capability_discovery import (
    CodexCapabilityDiscovery,
)
from agentic_engineering_os.infrastructure.codex_runtime_adapter import (
    CodexOperationalCapabilityProver,
    CodexRuntimeAdapter,
    CodexRuntimeConfiguration,
    _environment_fingerprint,
)
from agentic_engineering_os.infrastructure.execution_git_observer import (
    ExecutionGitObserver,
)
from agentic_engineering_os.infrastructure.execution_state_store import (
    ExecutionStateStore,
)
from agentic_engineering_os.infrastructure.maintenance_state_store import (
    MaintenanceStateStore,
)
from agentic_engineering_os.infrastructure.mission_state_store import MissionStateStore
from agentic_engineering_os.infrastructure.orchestration_record_store import (
    OrchestrationRecordStore,
)
from agentic_engineering_os.infrastructure.operational_event_store import (
    OperationalEventStore,
    StructuredEventLogger,
)
from agentic_engineering_os.infrastructure.platform_environment import (
    RUNTIME_ENVIRONMENT_ALLOWLIST,
    build_bounded_environment,
)
from agentic_engineering_os.infrastructure.project_configuration import (
    ProjectConfigurationLoader,
)
from agentic_engineering_os.infrastructure.project_state_store import ProjectStateStore
from agentic_engineering_os.infrastructure.verification_command_runner import (
    SubprocessVerificationCommandRunner,
)
from agentic_engineering_os.infrastructure.worktree_manager import WorktreeManager

from .certification_service import CertificationService
from .codex_capabilities import CodexOperationalCapabilityClass
from .codex_e2e_runtime import CodexEndToEndRuntime
from .codex_runtime import (
    CodexApprovalPolicy,
    CodexExecutionBinding,
    CodexSandboxMode,
)
from .contract_validator import ContractValidator
from .control_loop import ControlLoop
from .evidence_recorder import EvidenceRecorder
from .execution_recovery import RestartSafeCodexExecutionService
from .execution_budget_boundary import ExecutionBudgetBoundary
from .execution_observability import project_terminal_execution_events
from .execution_state import CodexExecutionStatus, ExecutionExecutableIdentity
from .gate_evaluator import GateEvaluator
from .mission_admission import (
    MissionCapabilitySnapshot,
    MissionReadinessPrecheck,
    MissionRequest,
)
from .mission_certification import MissionCertificationCoordinator
from .mission_integration import MissionIntegrationCoordinator
from .mission_integration import MissionIntegrationResult
from .mission_lifecycle import MissionLifecycleService
from .mission_planning import MissionPlanningCoordinator
from .mission_runner import (
    MissionContinuationError,
    MissionRoleLaunchResult,
    MissionRunResult,
    MissionRunStatus,
    MissionRunner,
)
from .orchestrator import Orchestrator
from .parallel_codex_implementers import ParallelCodexImplementerExecutor
from .parallel_implementer_coordinator import ParallelImplementerCoordinator
from .parallel_mission_workflow import ParallelMissionWorkflow, ParallelStoryDossier
from .result_intake import reconstruct_persisted_role_result
from .sequential_mission_workflow import SequentialMissionWorkflow
from .single_role_codex import SingleRoleArtifacts, SingleRoleCodexExecutor
from .state_transition_service import StateTransitionService
from .verification_coordinator import VerificationCoordinator


class MissionCompositionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class _PersistedMaintenanceBoundary:
    """Admit only the exact persisted NORMAL maintenance boundary."""

    def __init__(self, store: MaintenanceStateStore) -> None:
        self._store = store

    def evaluate_start_mission(self, **values) -> MaintenanceAdmission:
        record = self._store.load()
        exact = (
            record.state is MaintenanceState.NORMAL
            and record.repository_head == values["repository_head"]
            and record.scope.repository_root
            == os.path.normcase(str(Path(values["repository_root"]).resolve()))
            and record.scope.project_id == values["project_state"].project_id
        )
        return MaintenanceAdmission(
            MaintenanceOperation.START_MISSION,
            record.state,
            (
                MaintenanceAdmissionDecision.ADMITTED
                if exact
                else MaintenanceAdmissionDecision.REFUSED
            ),
            (
                MaintenanceAdmissionReason.NORMAL_OPERATION
                if exact
                else MaintenanceAdmissionReason.SOURCE_SCOPE_MISMATCH
            ,),
            record.fingerprint,
            values["evaluated_at"],
        )

    def enforce(self, admission: object, operation: MaintenanceOperation) -> None:
        current = self._store.load()
        if (
            not isinstance(admission, MaintenanceAdmission)
            or operation is not MaintenanceOperation.START_MISSION
            or admission.operation is not operation
            or admission.decision is not MaintenanceAdmissionDecision.ADMITTED
            or admission.maintenance_fingerprint != current.fingerprint
            or current.state is not MaintenanceState.NORMAL
        ):
            raise MissionCompositionError(
                "MAINTENANCE_ADMISSION_REQUIRED",
                "exact current NORMAL maintenance admission is required",
            )


class _CodexCapabilityProvider:
    def __init__(self, configuration: CodexRuntimeConfiguration) -> None:
        self.configuration = configuration
        self.discovery = CodexCapabilityDiscovery()
        self.prover = CodexOperationalCapabilityProver()

    def inspect(self, request: MissionRequest, configuration) -> MissionCapabilitySnapshot:
        child = build_bounded_environment(os.environ, RUNTIME_ENVIRONMENT_ALLOWLIST)
        runtime = self.configuration
        assessment = self.discovery.assess(
            executable=runtime.executable,
            expected_path=runtime.expected_executable_path,
            expected_sha256=runtime.expected_executable_sha256,
            expected_version=runtime.expected_executable_version,
            launcher_arguments=runtime.launcher_arguments,
            environment=os.environ,
            project_root=request.repository_root,
        )
        if assessment is None:
            raise MissionCompositionError(
                "CODEX_IDENTITY_UNPROVEN", "Codex executable identity cannot be proven"
            )
        binding = CodexExecutionBinding(
            request_id="mission-capability-preflight",
            context_fingerprint="0" * 64,
            mission_id="mission-capability-preflight",
            workflow_generation=0,
            role=MissionRole.IMPLEMENTER,
            subject="mission-capability-preflight",
            cwd=request.repository_root,
            expected_commit=_head(Path(request.repository_root)),
            sandbox=CodexSandboxMode.WORKSPACE_WRITE,
            approval_policy=CodexApprovalPolicy.NEVER,
            timeout_seconds=runtime.operational_probe_timeout_seconds,
        )
        executable = Path(runtime.expected_executable_path)
        proofs = tuple(
            self.prover.prove(
                configuration=runtime,
                executable=executable,
                executable_sha256=runtime.expected_executable_sha256,
                executable_version=runtime.expected_executable_version,
                environment=child,
                cwd=Path(request.repository_root),
                binding=binding,
                capability_class=capability,
            )
            for capability in (
                CodexOperationalCapabilityClass.REPOSITORY_READ,
                CodexOperationalCapabilityClass.WORKSPACE_EDIT,
                CodexOperationalCapabilityClass.COMMAND_EXECUTION,
                CodexOperationalCapabilityClass.GIT_OBSERVATION,
            )
        )
        return MissionCapabilitySnapshot(
            assessment, proofs, _environment_fingerprint(child)
        )


class _ContinuationAdmissionBoundary:
    """Reconstruct operational facts before every durable mission frontier."""

    def __init__(
        self,
        *,
        root: Path,
        project_id: str,
        maintenance_store: MaintenanceStateStore,
        mission_store: MissionStateStore,
        project_store: ProjectStateStore,
        record_store: OrchestrationRecordStore,
        execution_store: ExecutionStateStore,
        worktrees: WorktreeManager,
        event_store: OperationalEventStore,
    ) -> None:
        self._root = root
        self._project_id = project_id
        self._maintenance = maintenance_store
        self._missions = mission_store
        self._projects = project_store
        self._records = record_store
        self._executions = execution_store
        self._worktrees = worktrees
        self._events = event_store

    def authorize(self) -> None:
        maintenance = self._maintenance.load()
        mission = self._missions.load()
        project = self._projects.load()
        record = self._records.load()
        ledgers = [self._executions.load()]
        primary = self._worktrees.inspect_primary()
        reconciliation = self._worktrees.inspect_all(
            current_generation=mission.workflow_generation
        )
        registry = self._worktrees.registry_store.load()
        for assignment in registry.assignments:
            if assignment.workflow_generation != mission.workflow_generation:
                continue
            try:
                ledgers.append(ExecutionStateStore(assignment.worktree_path).load())
            except Exception as error:
                if getattr(error, "code", None) != "LEDGER_ABSENT":
                    raise MissionContinuationError(
                        "RECOVERY_REQUIRED",
                        "a current-generation worktree execution ledger is unreadable",
                    ) from error
        self._events.read()
        coherent = (
            maintenance.state is MaintenanceState.NORMAL
            and maintenance.scope.project_id == self._project_id
            and maintenance.scope.repository_root
            == os.path.normcase(str(self._root)).casefold()
            and getattr(project, "project_id", None) == self._project_id
            and record.mission_id == mission.mission_id
            and record.workflow_generation == mission.workflow_generation
            and primary.clean
            and primary.head_commit == mission.observed_commit.casefold()
            and not reconciliation.anomalies
        )
        if not coherent:
            raise MissionContinuationError(
                "RECOVERY_REQUIRED",
                "continuation health facts are unknown, divergent, or maintenance-blocked",
            )
        uncertain = tuple(
            item.execution_id
            for ledger in ledgers
            for item in ledger.records
            if item.mission_id == mission.mission_id
            and item.workflow_generation == mission.workflow_generation
            and item.status in {
                CodexExecutionStatus.RUNNING,
                CodexExecutionStatus.INTERRUPTED,
            }
        )
        if uncertain:
            raise MissionContinuationError(
                "RECOVERY_REQUIRED",
                "active generation contains an uncertain execution outcome",
            )


class _ExecutorFactory:
    def __init__(self, create, assessment) -> None:
        self._create = create
        self._assessment = assessment

    def create(self, context, mission_store):
        return self._create(mission_store)

    def assess_parallel_capability(self):
        return self._assessment()


class _DossierRoleExecutor:
    def __init__(self, workflow, create_executor, records, executions) -> None:
        self._workflow = workflow
        self._create = create_executor
        self._records = records
        self._executions = executions

    def execute(
        self,
        dossier: ParallelStoryDossier,
        role: MissionRole,
        *,
        request_id: str,
        updated_at,
    ) -> MissionRoleLaunchResult:
        del updated_at
        handoff = self._workflow.runtime_handoff(dossier, role)
        artifacts = SingleRoleArtifacts(
            architect_result=self._architect() if role is MissionRole.CERTIFIER else None,
            implementer_result=dossier.implementer_result,
            tester_result=dossier.tester_result,
            reviewer_result=dossier.reviewer_result,
        )
        outcome = self._create(None).execute(
            handoff, request_id=request_id, artifacts=artifacts
        )
        return MissionRoleLaunchResult(outcome.validated, outcome.blockers)

    def _architect(self):
        record = self._records.load()
        references = tuple(
            item for item in record.execution_references
            if item.role is MissionRole.ARCHITECT
        )
        ledger = self._executions.load()
        if len(references) != 1:
            raise MissionCompositionError(
                "ARCHITECT_REFERENCE_INVALID", "Architect reference is not unique"
            )
        matches = tuple(
            item for item in ledger.records
            if item.execution_id == references[0].execution_id
            and item.validated_result_json is not None
        )
        if len(matches) != 1:
            raise MissionCompositionError(
                "ARCHITECT_EXECUTION_INVALID", "Architect execution is unavailable"
            )
        return reconstruct_persisted_role_result(
            matches[0].validated_result_json, MissionRole.ARCHITECT
        )


class _OperationalMissionEventSink:
    def __init__(
        self,
        root: Path,
        project_id: str,
        execution_store: ExecutionStateStore,
        project_store: ProjectStateStore,
        record_store: OrchestrationRecordStore,
        worktrees: WorktreeManager,
    ) -> None:
        self._root = root
        self._store = OperationalEventStore(root)
        self._logger = StructuredEventLogger(self._store)
        self._project_id = project_id
        self._executions = execution_store
        self._projects = project_store
        self._records = record_store
        self._worktrees = worktrees

    def record(self, result: MissionRunResult, *, occurred_at: datetime) -> None:
        self._project_execution_events()
        self._project_control_plane_events(occurred_at)
        self._project_worktree_events(occurred_at)
        operation = (
            "FINISHED"
            if result.status is MissionRunStatus.COMPLETED
            else "STATUS_OBSERVED"
            if result.status is MissionRunStatus.ACTIVE
            else "BLOCKED"
        )
        severity = (
            OperationalSeverity.INFO
            if result.status in {MissionRunStatus.ACTIVE, MissionRunStatus.COMPLETED}
            else OperationalSeverity.WARNING
        )
        self._event(
            OperationalEventType.MISSION_LIFECYCLE,
            operation,
            OperationalCorrelation(
                mission_id=result.mission_id,
                workflow_generation=result.generation,
                repository_commit=result.repository_head,
            ),
            occurred_at,
            severity=severity,
            outcome=result.status.value,
            reason_code=_event_reason(result.blockers),
            producer="MissionRunner",
        )

    def mission_started(self, mission_id: str, *, occurred_at: datetime) -> None:
        mission = self._records.load()
        if mission.mission_id != mission_id:
            return
        self._event(
            OperationalEventType.MISSION_LIFECYCLE,
            "STARTED",
            OperationalCorrelation(
                mission_id=mission_id,
                workflow_generation=mission.workflow_generation,
                repository_commit=mission.baseline_commit,
            ),
            occurred_at,
            outcome="ACTIVE",
            producer="MissionRunner",
        )

    def mission_resumed(self, mission, *, occurred_at: datetime) -> None:
        self._event(
            OperationalEventType.MISSION_LIFECYCLE,
            "STATUS_OBSERVED",
            OperationalCorrelation(
                mission_id=mission.mission_id,
                workflow_generation=mission.workflow_generation,
                repository_commit=mission.observed_commit.casefold(),
            ),
            occurred_at,
            outcome="RESUMED",
            producer="MissionRunner",
        )

    def human_approved(self, evidence, *, occurred_at: datetime) -> None:
        record = self._records.load()
        self._event(
            OperationalEventType.CONTROL_PLANE_DECISION,
            "HUMAN_APPROVAL_RECORDED",
            OperationalCorrelation(
                mission_id=record.mission_id,
                workflow_generation=record.workflow_generation,
                user_story_id=evidence.subject,
                repository_commit=evidence.commit,
            ),
            evidence.timestamp,
            outcome="APPROVED" if evidence.result is True else "REFUSED",
            producer="ControlPlane",
            source_ref=evidence.evidence_id,
        )
        self._event(
            OperationalEventType.HUMAN_WAITING,
            "WAITING_FINISHED",
            OperationalCorrelation(
                mission_id=record.mission_id,
                workflow_generation=record.workflow_generation,
                user_story_id=evidence.subject,
                repository_commit=evidence.commit,
            ),
            occurred_at,
            outcome="EVIDENCE_APPLIED",
            producer="HumanApprovalService",
            source_ref=evidence.evidence_id,
        )

    def integration(
        self, result: MissionIntegrationResult, *, occurred_at: datetime
    ) -> None:
        self._project_worktree_events(occurred_at)
        attempt = result.remediation_attempt
        if attempt is None:
            return
        gate = attempt.gate_result
        finding = gate.findings[0].code.value if gate.findings else None
        correlation = OperationalCorrelation(
            mission_id=gate.mission_id,
            workflow_generation=gate.workflow_generation,
            wave_index=gate.wave_index,
            group_index=gate.group_index,
            repository_commit=gate.baseline_commit,
        )
        self._event(
            OperationalEventType.INTEGRATION_GATE,
            "EVALUATED",
            correlation,
            occurred_at,
            severity=(
                OperationalSeverity.INFO
                if gate.result.value == "PASS"
                else OperationalSeverity.WARNING
            ),
            outcome=gate.result.value,
            reason_code=finding,
            producer="IntegrationGate",
        )
        merge = attempt.merge_result
        if merge is None:
            return
        operation = "FINISHED" if merge.result.value == "MERGED" else "FAILED"
        merge_reason = merge.findings[0].code.value if merge.findings else None
        self._event(
            OperationalEventType.MERGE_OPERATION,
            operation,
            OperationalCorrelation(
                mission_id=merge.mission_id,
                workflow_generation=merge.workflow_generation,
                wave_index=merge.wave_index,
                group_index=merge.group_index,
                repository_commit=merge.integration_commit or merge.primary_after,
            ),
            occurred_at,
            severity=(
                OperationalSeverity.INFO
                if operation == "FINISHED"
                else OperationalSeverity.WARNING
            ),
            outcome=merge.result.value,
            reason_code=merge_reason,
            producer="MergeCoordinator",
        )

    def verification(self, result, *, occurred_at: datetime) -> None:
        del result
        self._project_control_plane_events(occurred_at)

    def certification(self, result, *, occurred_at: datetime) -> None:
        self._project_control_plane_events(occurred_at)
        if any("HUMAN" in blocker for blocker in result.blockers):
            self._event(
                OperationalEventType.HUMAN_WAITING,
                "WAITING_STARTED",
                OperationalCorrelation(
                    mission_id=result.mission_id,
                    workflow_generation=result.workflow_generation,
                    user_story_id=result.user_story_id,
                    repository_commit=result.integration_commit,
                ),
                occurred_at,
                outcome=result.status.value,
                reason_code=_event_reason(result.blockers),
                producer="MissionCertificationCoordinator",
            )

    def role_execution(
        self, dossier, role, result, *, occurred_at: datetime
    ) -> None:
        correlation = OperationalCorrelation(
            mission_id=dossier.mission_id,
            workflow_generation=dossier.workflow_generation,
            user_story_id=dossier.user_story_id,
            role=role,
            repository_commit=dossier.integration_commit,
        )
        self._event(
            OperationalEventType.ROLE_EXECUTION,
            "STARTED",
            correlation,
            occurred_at,
            outcome="LAUNCHED",
            producer="MissionRoleExecutor",
        )
        self._event(
            OperationalEventType.ROLE_EXECUTION,
            "FINISHED" if result.validated else "FAILED",
            correlation,
            occurred_at,
            severity=(
                OperationalSeverity.INFO
                if result.validated
                else OperationalSeverity.WARNING
            ),
            outcome="VALIDATED" if result.validated else "REFUSED",
            reason_code=_event_reason(result.blockers),
            producer="MissionRoleExecutor",
        )

    def remediation(self, result, *, occurred_at: datetime) -> None:
        self._event(
            OperationalEventType.REMEDIATION_RECOVERY,
            "STARTED",
            OperationalCorrelation(
                mission_id=result.mission_id,
                workflow_generation=result.new_generation,
                repository_commit=result.baseline_commit,
            ),
            occurred_at,
            outcome=result.triggering_stage.value,
            producer="ParallelMissionWorkflow",
        )

    def _project_execution_events(self) -> None:
        stores = [(self._executions, self._root)]
        try:
            assignments = self._worktrees.registry_store.load().assignments
        except Exception:
            assignments = ()
        for assignment in assignments:
            try:
                stores.append(
                    (
                        ExecutionStateStore(assignment.worktree_path),
                        Path(assignment.worktree_path),
                    )
                )
            except Exception:
                continue
        for store, repository_root in stores:
            try:
                projected = project_terminal_execution_events(
                    store.load(),
                    project_id=self._project_id,
                    repository_root=repository_root,
                )
            except Exception:
                continue
            for event in projected:
                self._append_once(event)

    def _project_worktree_events(self, occurred_at: datetime) -> None:
        try:
            assignments = self._worktrees.registry_store.load().assignments
        except Exception:
            return
        operations = {
            "PLANNED": "PLANNED",
            "ACTIVE": "CREATED",
            "COMPLETED": "COMPLETED",
            "FAILED": "FAILED",
            "CLEANED": "CLEANED",
        }
        for assignment in assignments:
            correlation = OperationalCorrelation(
                mission_id=assignment.mission_id,
                workflow_generation=assignment.workflow_generation,
                user_story_id=assignment.user_story_id,
                assignment_id=assignment.assignment_id,
                repository_commit=assignment.result_commit or assignment.baseline_commit,
            )
            if assignment.status.value in {"ACTIVE", "COMPLETED", "FAILED"}:
                self._event(
                    OperationalEventType.WORKTREE_LIFECYCLE,
                    "CREATED",
                    correlation,
                    occurred_at,
                    outcome="OBSERVED",
                    producer="WorktreeManager",
                )
            operation = operations[assignment.status.value]
            self._event(
                OperationalEventType.WORKTREE_LIFECYCLE,
                operation,
                correlation,
                occurred_at,
                severity=(
                    OperationalSeverity.WARNING
                    if operation == "FAILED"
                    else OperationalSeverity.INFO
                ),
                outcome=assignment.status.value,
                producer="WorktreeManager",
            )

    def _project_control_plane_events(self, occurred_at: datetime) -> None:
        project = self._projects.load()
        record = self._records.load()
        story_ids = set(record.user_story_ids)
        for evidence in project.evidence:
            self._event(
                OperationalEventType.CONTROL_PLANE_DECISION,
                "EVIDENCE_RECORDED",
                OperationalCorrelation(
                    mission_id=record.mission_id,
                    workflow_generation=record.workflow_generation,
                    user_story_id=(
                        evidence.subject if evidence.subject in story_ids else None
                    ),
                    repository_commit=evidence.commit,
                ),
                evidence.timestamp,
                outcome=evidence.evidence_type.value,
                producer="ControlPlane",
                source_ref=evidence.evidence_id,
            )
        for gate in project.gates:
            self._event(
                OperationalEventType.CONTROL_PLANE_DECISION,
                "GATE_EVALUATED",
                OperationalCorrelation(
                    mission_id=record.mission_id,
                    workflow_generation=record.workflow_generation,
                    user_story_id=gate.subject if gate.subject in story_ids else None,
                    gate_id=gate.gate_id,
                ),
                gate.evaluated_at,
                outcome=gate.result.value,
                producer="ControlPlane",
                source_ref=gate.gate_id,
            )
        for certification in project.certifications:
            reference = next(
                (
                    item
                    for item in record.certification_references
                    if item.certification_id == certification.certification_id
                ),
                None,
            )
            generation = (
                reference.workflow_generation
                if reference is not None
                else record.workflow_generation
            )
            self._event(
                OperationalEventType.CONTROL_PLANE_DECISION,
                "CERTIFICATION_RECORDED",
                OperationalCorrelation(
                    mission_id=record.mission_id,
                    workflow_generation=generation,
                    user_story_id=certification.subject,
                    certification_id=certification.certification_id,
                    repository_commit=certification.commit,
                ),
                certification.certified_at,
                outcome=certification.result.value,
                producer="ControlPlane",
                source_ref=certification.certification_id,
            )

    def _event(
        self,
        event_type: OperationalEventType,
        operation: str,
        correlation: OperationalCorrelation,
        occurred_at: datetime,
        *,
        severity: OperationalSeverity = OperationalSeverity.INFO,
        outcome: str | None = None,
        reason_code: str | None = None,
        producer: str,
        source_ref: str | None = None,
    ) -> None:
        identity = json.dumps(
            {
                "type": event_type.value,
                "operation": operation,
                "correlation": repr(correlation),
                "outcome": outcome,
                "reason": reason_code,
                "source": source_ref,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self._append_once(
            OperationalEvent(
                OPERATIONAL_EVENT_SCHEMA_VERSION,
                str(uuid5(NAMESPACE_URL, identity)),
                event_type,
                occurred_at,
                severity,
                "mission-observability-projector",
                self._project_id,
                correlation,
                OperationalEventPayload(
                    operation,
                    outcome=outcome,
                    reason_code=reason_code,
                ),
                OperationalProvenance(
                    OperationalProvenanceKind.DETERMINISTIC_COMPONENT,
                    producer,
                    source_ref,
                ),
            )
        )

    def _append_once(self, event: OperationalEvent) -> None:
        if event.event_id in {item.event_id for item in self._store.read()}:
            return
        self._logger.record(event)


def build_production_mission_runner(repository: Path) -> MissionRunner:
    root = repository.resolve(strict=True)
    configuration_store = ProjectConfigurationLoader(root)
    configuration = configuration_store.load()
    runtime_configuration = _runtime_configuration(root)
    mission_store = MissionStateStore(root)
    project_store = ProjectStateStore(root)
    execution_store = ExecutionStateStore(root)
    record_store = OrchestrationRecordStore(root)
    maintenance_store = MaintenanceStateStore(root)
    maintenance = _PersistedMaintenanceBoundary(maintenance_store)

    validator = ContractValidator()
    control = ControlLoop(
        state_store=project_store,
        evidence_recorder_factory=lambda target: EvidenceRecorder(
            target, validator=validator
        ),
        gate_evaluator=GateEvaluator(validator=validator),
        certification_service=CertificationService(validator=validator),
        transition_service=StateTransitionService(),
    )
    worktree_root = _worktree_root(root)
    manager = WorktreeManager(repository_root=root, worktree_root=worktree_root)
    parallel_coordinator = ParallelImplementerCoordinator(worktree_manager=manager)
    parallel_workflow = ParallelMissionWorkflow(
        mission_store=mission_store,
        project_store=project_store,
        control_loop=control,
        worktree_manager=manager,
        parallel_coordinator=parallel_coordinator,
    )
    sequential = SequentialMissionWorkflow(
        orchestrator=Orchestrator(
            repository_root=root,
            mission_store=mission_store,
            project_state_store=project_store,
        ),
        mission_store=mission_store,
        project_store=project_store,
        control_loop=control,
    )
    adapter = CodexRuntimeAdapter(runtime_configuration)
    execution_service = RestartSafeCodexExecutionService(
        execution_store, adapter, ExecutionGitObserver()
    )
    identity = ExecutionExecutableIdentity(
        runtime_configuration.expected_executable_path,
        runtime_configuration.expected_executable_version,
        runtime_configuration.expected_executable_sha256,
    )
    execution_budget = ExecutionBudgetBoundary(
        repository_root=root,
        configuration=configuration,
        mission_store=mission_store,
        execution_store=execution_store,
    )

    def create_executor(bound_mission_store=None):
        return SingleRoleCodexExecutor(
            mission_store=bound_mission_store or mission_store,
            project_store=project_store,
            repository=manager,
            execution_service=execution_service,
            executable_identity=identity,
            execution_budget=execution_budget,
        )

    capability_provider = _CodexCapabilityProvider(runtime_configuration)
    executor_factory = _ExecutorFactory(
        create_executor,
        lambda: capability_provider.discovery.assess(
            executable=runtime_configuration.executable,
            expected_path=runtime_configuration.expected_executable_path,
            expected_sha256=runtime_configuration.expected_executable_sha256,
            expected_version=runtime_configuration.expected_executable_version,
            environment=os.environ,
            project_root=str(root),
        ),
    )
    parallel_executor = ParallelCodexImplementerExecutor(
        parallel_coordinator=parallel_coordinator,
        mission_store=mission_store,
        project_store=project_store,
        executor_factory=executor_factory,
        max_concurrency=configuration.codex_constraints.maximum_parallel_executions,
        execution_budget=execution_budget,
    )
    runtime = CodexEndToEndRuntime(
        single_executor=create_executor(),
        sequential_workflow=sequential,
        parallel_executor=parallel_executor,
        parallel_workflow=parallel_workflow,
        parallel_mission_store=mission_store,
    )
    integration = MissionIntegrationCoordinator(
        workflow=parallel_workflow,
        runtime=runtime,
        mission_store=mission_store,
        project_store=project_store,
        record_store=record_store,
    )
    planning = MissionPlanningCoordinator(
        lifecycle=MissionLifecycleService(
            mission_store=mission_store,
            project_store=project_store,
            maintenance=maintenance,
        ),
        workflow=sequential,
        architect_executor=create_executor(),
        mission_store=mission_store,
        project_store=project_store,
        execution_store=execution_store,
        record_store=record_store,
    )
    certification = MissionCertificationCoordinator(
        workflow=parallel_workflow,
        integration=integration,
        mission_store=mission_store,
        project_store=project_store,
        execution_store=execution_store,
        record_store=record_store,
    )
    verification = VerificationCoordinator(
        root,
        control_loop=control,
        runner=SubprocessVerificationCommandRunner(),
        git_observer=manager,
    )
    continuation = _ContinuationAdmissionBoundary(
        root=root,
        project_id=configuration.project_id,
        maintenance_store=maintenance_store,
        mission_store=mission_store,
        project_store=project_store,
        record_store=record_store,
        execution_store=execution_store,
        worktrees=manager,
        event_store=OperationalEventStore(root),
    )
    return MissionRunner(
        admission=MissionReadinessPrecheck(
            capability_provider=capability_provider,
            maintenance_provider=maintenance,
        ),
        planning=planning,
        integration=integration,
        certification=certification,
        verification=verification,
        control_loop=control,
        role_executor=_DossierRoleExecutor(
            parallel_workflow, create_executor, record_store, execution_store
        ),
        workflow=parallel_workflow,
        mission_store=mission_store,
        project_store=project_store,
        record_store=record_store,
        configuration_store=configuration_store,
        event_sink=_OperationalMissionEventSink(
            root,
            configuration.project_id,
            execution_store,
            project_store,
            record_store,
            manager,
        ),
        continuation_admission=continuation,
    )


def _runtime_configuration(root: Path) -> CodexRuntimeConfiguration:
    executable_text = shutil.which("codex")
    if executable_text is None:
        raise MissionCompositionError("CODEX_UNAVAILABLE", "codex is absent from PATH")
    executable = Path(executable_text).resolve(strict=True)
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            shell=False,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MissionCompositionError(
            "CODEX_IDENTITY_UNPROVEN", "codex version cannot be observed"
        ) from error
    version = result.stdout.strip()
    if result.returncode != 0 or not version or len(version) > 1_000:
        raise MissionCompositionError(
            "CODEX_IDENTITY_UNPROVEN", "codex version observation is invalid"
        )
    with executable.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return CodexRuntimeConfiguration(
        executable=str(executable),
        expected_executable_path=str(executable),
        expected_executable_version=version,
        expected_executable_sha256=digest,
    )


def _worktree_root(root: Path) -> Path:
    identity = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()[:20]
    base = Path(tempfile.gettempdir()).resolve() / "agentic-engineering-os-worktrees"
    target = base / identity
    if base.is_symlink() or target.is_symlink():
        raise MissionCompositionError(
            "UNSAFE_WORKTREE_ROOT", "external worktree root cannot be a symlink"
        )
    target.mkdir(parents=True, exist_ok=True)
    return target.resolve(strict=True)


def _head(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )
    value = result.stdout.strip().casefold()
    if result.returncode != 0 or len(value) != 40:
        raise MissionCompositionError("GIT_HEAD_UNAVAILABLE", "Git HEAD is unavailable")
    return value


def _event_reason(blockers: tuple[str, ...]) -> str | None:
    if not blockers:
        return None
    value = re.sub(r"[^A-Z0-9_]", "_", blockers[0].upper()).strip("_")
    return (value or "MISSION_BLOCKED")[:128]


__all__ = ["MissionCompositionError", "build_production_mission_runner"]
