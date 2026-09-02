from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from agentic_engineering_os._authoritative_write import _issue_authoritative_write
from agentic_engineering_os.application import (
    ArchitectDecision,
    ArchitectDecisionKind,
    ArchitectResult,
    ArchitectVerdict,
    CertificationService,
    ContractValidator,
    ControlLoop,
    EvidenceRecorder,
    GateEvaluator,
    MissionAdmission,
    MissionAdmissionStatus,
    MissionLifecycleError,
    MissionLifecycleService,
    MissionPlanningCoordinator,
    MissionPlanningStatus,
    MissionRequest,
    Orchestrator,
    OrchestrationRecord,
    RestartSafeCodexExecutionService,
    RoleExecutionReference,
    SequentialMissionResult,
    SequentialMissionWorkflow,
    SingleRoleCodexExecutor,
    SingleRoleExecutionOutcome,
    StateTransitionService,
    request_fingerprint,
)
from agentic_engineering_os.application.execution_state import (
    EXECUTION_LEDGER_VERSION,
    CodexExecutionLedger,
    CodexExecutionRecord,
    CodexExecutionStatus,
    ExecutionExecutableIdentity,
    canonical_result_json,
    result_json_fingerprint,
)
from agentic_engineering_os.application.orchestration_record import (
    ORCHESTRATION_RECORD_VERSION,
)
from agentic_engineering_os.application.orchestrator import RoleHandoff
from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    HumanApproval,
    MissionStateGitPolicy,
    MaintenanceAdmission,
    MaintenanceAdmissionDecision,
    MaintenanceAdmissionReason,
    MaintenanceOperation,
    MaintenanceState,
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    ProjectState,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
    gitignore_managed_section,
    to_dict,
)
from agentic_engineering_os.infrastructure import (
    CodexRuntimeAdapter,
    CodexRuntimeConfiguration,
    ExecutionGitObserver,
    ExecutionStateStore,
    MissionStateStore,
    ProjectStateStore,
    WorktreeManager,
)
from agentic_engineering_os.infrastructure.orchestration_record_store import (
    OrchestrationRecordStore,
)


NOW = datetime(2026, 9, 2, 14, 0, tzinfo=timezone.utc)
HEAD = "a" * 40
RESULT_FP = "b" * 64
PROJECT = "project-one"
FAKE_CODEX = Path(__file__).parent / "fixtures" / "fake_codex.py"


class Enforcer:
    def __init__(self, expected: MaintenanceAdmission, *, reject: bool = False) -> None:
        self.expected = expected
        self.reject = reject
        self.calls = 0

    def enforce(self, admission, operation):
        self.calls += 1
        if self.reject or admission is not self.expected or operation is not MaintenanceOperation.START_MISSION:
            raise RuntimeError("ADMISSION_REQUIRED")


class MemoryStore:
    def __init__(self, value) -> None:
        self.value = value

    def load(self):
        return self.value


class MemoryRecordStore:
    def __init__(self) -> None:
        self.value = None

    def load(self):
        if self.value is None:
            error = RuntimeError("absent")
            error.code = "ORCHESTRATION_RECORD_ABSENT"
            raise error
        return self.value

    def initialize(self, record):
        self.value = record

    def replace(self, record, *, expected_fingerprint):
        assert self.value.fingerprint == expected_fingerprint
        self.value = record


def maintenance_admission() -> MaintenanceAdmission:
    return MaintenanceAdmission(
        MaintenanceOperation.START_MISSION,
        MaintenanceState.NORMAL,
        MaintenanceAdmissionDecision.ADMITTED,
        (MaintenanceAdmissionReason.NORMAL_OPERATION,),
        "c" * 64,
        NOW,
    )


def request(root: Path) -> MissionRequest:
    return MissionRequest("Add a bounded feature", str(root), ("src",), ())


def admission(root: Path, maintenance: MaintenanceAdmission | None = None) -> MissionAdmission:
    item = request(root)
    return MissionAdmission(
        MissionAdmissionStatus.ADMITTED,
        request_fingerprint(item),
        "d" * 64,
        HEAD,
        PROJECT,
        (),
        (),
        "start",
        maintenance or maintenance_admission(),
    )


def lifecycle(root: Path, *, enforcer=None):
    project_store = ProjectStateStore(root)
    project_store.initialize(project_id=PROJECT)
    mission_store = MissionStateStore(root)
    maintenance = enforcer or Enforcer(maintenance_admission())
    service = MissionLifecycleService(
        mission_store=mission_store,
        project_store=project_store,
        maintenance=maintenance,
    )
    return service, mission_store, project_store, maintenance


def test_lifecycle_creates_restart_safe_initial_mission(tmp_path: Path) -> None:
    fact = maintenance_admission()
    service, mission_store, _, enforcer = lifecycle(tmp_path, enforcer=Enforcer(fact))
    result = service.start(request(tmp_path), admission(tmp_path, fact), updated_at=NOW)
    loaded = MissionStateStore(tmp_path).load()
    assert result.mission == loaded
    assert loaded.subject == "US-0001"
    assert loaded.operating_step is OperatingStep.UNDERSTAND_CONTRACT
    assert loaded.role is MissionRole.ORCHESTRATOR
    assert not result.replaced_terminal_mission
    assert enforcer.calls == 1


def test_lifecycle_refuses_non_admitted_or_unenforced_start(tmp_path: Path) -> None:
    fact = maintenance_admission()
    service, _, _, _ = lifecycle(tmp_path, enforcer=Enforcer(fact, reject=True))
    with pytest.raises(RuntimeError, match="ADMISSION_REQUIRED"):
        service.start(request(tmp_path), admission(tmp_path, fact), updated_at=NOW)


def test_lifecycle_refuses_existing_active_mission(tmp_path: Path) -> None:
    fact = maintenance_admission()
    service, _, _, _ = lifecycle(tmp_path, enforcer=Enforcer(fact))
    admitted = admission(tmp_path, fact)
    service.start(request(tmp_path), admitted, updated_at=NOW)
    with pytest.raises(MissionLifecycleError) as captured:
        service.start(request(tmp_path), admitted, updated_at=NOW)
    assert captured.value.code == "MISSION_NOT_TERMINAL"


def test_lifecycle_replaces_only_terminal_mission(tmp_path: Path) -> None:
    fact = maintenance_admission()
    service, mission_store, _, _ = lifecycle(tmp_path, enforcer=Enforcer(fact))
    first = service.start(request(tmp_path), admission(tmp_path, fact), updated_at=NOW).mission
    terminal = MissionState(
        first.schema_version,
        first.mission_id,
        first.workflow_generation,
        MissionStatus.COMPLETED,
        MissionRole.ORCHESTRATOR,
        first.objective,
        first.subject,
        OperatingStep.REPORT,
        "complete",
        first.observed_commit,
        NOW,
        [],
    )
    authority = _issue_authoritative_write(store_kind="MISSION_STATE", store=mission_store, before_state=first, candidate_state=terminal, operation="TEST_COMPLETE")
    mission_store.save(terminal, authorization=authority, operation="TEST_COMPLETE")
    result = service.start(MissionRequest("Second objective", str(tmp_path)), admission_for(MissionRequest("Second objective", str(tmp_path)), fact), updated_at=NOW)
    assert result.replaced_terminal_mission
    assert result.mission.mission_id != first.mission_id


def admission_for(item: MissionRequest, fact: MaintenanceAdmission) -> MissionAdmission:
    return MissionAdmission(MissionAdmissionStatus.ADMITTED, request_fingerprint(item), "d" * 64, HEAD, PROJECT, (), (), "start", fact)


def story(subject: str) -> UserStory:
    return UserStory(
        "1.0", subject, "Bounded story", "Implement objective", UserStoryStatus.PROPOSED,
        1, RiskLevel.LOW, (), UserStoryScope(("src",), ()),
        (AcceptanceCriterion("AC-001", "Behavior is verified", True),), (),
        HumanApproval(False, False, None, None),
        UserStoryMetadata(NOW, "Codex/Architect", NOW),
    )


def architect_result(mission: MissionState) -> ArchitectResult:
    return ArchitectResult(
        mission.mission_id, mission.workflow_generation, mission.subject,
        mission.observed_commit, "Plan ready", (),
        (ArchitectDecision(ArchitectDecisionKind.ARCHITECTURAL, "Use existing components"),),
        (), (), (story(mission.subject),), MissionRole.IMPLEMENTER, ArchitectVerdict.READY,
    )


class PlanningWorkflow:
    def __init__(self, missions: MemoryStore, projects: MemoryStore) -> None:
        self.missions = missions
        self.projects = projects
        self.route_calls = 0
        self.accept_calls = 0

    def route(self, *, current_commit, updated_at):
        self.route_calls += 1
        mission = self.missions.value
        handoff = RoleHandoff(MissionRole.ORCHESTRATOR, MissionRole.ARCHITECT, mission.mission_id, mission.workflow_generation, mission.subject, mission.objective, mission.observed_commit, OperatingStep.UNDERSTAND_CONTRACT, (), "Specify the contract")
        self.missions.value = MissionState(mission.schema_version, mission.mission_id, mission.workflow_generation, mission.status, MissionRole.ARCHITECT, mission.objective, mission.subject, mission.operating_step, mission.next_action, mission.observed_commit, updated_at, [])
        return SequentialMissionResult(mission.mission_id, mission.workflow_generation, mission.status, MissionRole.ARCHITECT, mission.operating_step, (), None, "execute", handoff)

    def accept_architect(self, handoff, candidate, *, updated_at):
        self.accept_calls += 1
        self.projects.value.user_stories.append(
            replace(deepcopy(candidate.user_stories[0]), status=UserStoryStatus.IN_PROGRESS)
        )
        mission = self.missions.value
        self.missions.value = MissionState(mission.schema_version, mission.mission_id, mission.workflow_generation, mission.status, MissionRole.ORCHESTRATOR, mission.objective, mission.subject, OperatingStep.ACT, "implement", mission.observed_commit, updated_at, [])
        return SequentialMissionResult(mission.mission_id, mission.workflow_generation, mission.status, MissionRole.ORCHESTRATOR, OperatingStep.ACT, (), MissionRole.ARCHITECT, "implement")


class ArchitectExecutor:
    def __init__(self, missions: MemoryStore, ledger: MemoryStore) -> None:
        self.missions = missions
        self.ledger = ledger
        self.calls = 0

    def execute(self, handoff, *, request_id):
        self.calls += 1
        mission = self.missions.value
        execution_id = "exec-architect"
        result = architect_result(mission)
        result_json = canonical_result_json(to_dict(result))
        record = CodexExecutionRecord(execution_id, "e" * 64, request_id, "f" * 64, mission.mission_id, mission.workflow_generation, MissionRole.ARCHITECT, mission.subject, str(Path.cwd()), None, str(Path.cwd()), mission.observed_commit, "1" * 64, "architect-result@1.0", ExecutionExecutableIdentity(str((Path.cwd() / "codex.exe").resolve()), "1", "2" * 64), CodexExecutionStatus.VALIDATED, NOW, NOW, validated_result_json=result_json, validated_result_fingerprint=result_json_fingerprint(result_json))
        self.ledger.value = CodexExecutionLedger(EXECUTION_LEDGER_VERSION, (record,))
        return SingleRoleExecutionOutcome(request_id, execution_id, MissionRole.ARCHITECT, CodexExecutionStatus.VALIDATED, True, result, False, False, ())


class MemoryLifecycle:
    def __init__(self, mission: MissionState) -> None:
        self.mission = mission

    def start(self, request, admission, *, updated_at):
        from agentic_engineering_os.application import MissionLifecycleStart
        return MissionLifecycleStart(self.mission, False)


def planning_harness(tmp_path: Path):
    req = request(tmp_path)
    mission = MissionState("1.0", "mission-test", 0, MissionStatus.ACTIVE, MissionRole.ORCHESTRATOR, req.objective, "US-0001", OperatingStep.UNDERSTAND_CONTRACT, "architect", HEAD, NOW, [])
    missions = MemoryStore(mission)
    projects = MemoryStore(ProjectState("1.0", project_id=PROJECT))
    ledger = MemoryStore(CodexExecutionLedger(EXECUTION_LEDGER_VERSION, ()))
    records = MemoryRecordStore()
    workflow = PlanningWorkflow(missions, projects)
    executor = ArchitectExecutor(missions, ledger)
    coordinator = MissionPlanningCoordinator(lifecycle=MemoryLifecycle(mission), workflow=workflow, architect_executor=executor, mission_store=missions, project_store=projects, execution_store=ledger, record_store=records)
    return req, coordinator, missions, projects, ledger, records, workflow, executor


def test_planning_coordinator_persists_only_exact_architect_reference(tmp_path: Path) -> None:
    req, coordinator, _, projects, _, records, workflow, executor = planning_harness(tmp_path)
    result = coordinator.start(req, admission(tmp_path), updated_at=NOW)
    assert result.status is MissionPlanningStatus.PLANNED
    assert len(projects.value.user_stories) == 1
    assert records.value.execution_references == (result.execution_reference,)
    assert records.value.plan_fingerprint is not None
    assert records.value.user_story_ids == ("US-0001",)
    assert result.architect_result is not None
    assert workflow.route_calls == workflow.accept_calls == executor.calls == 1


def test_restart_after_architect_does_not_replay_role(tmp_path: Path) -> None:
    req, coordinator, _, _, _, records, workflow, executor = planning_harness(tmp_path)
    first = coordinator.start(req, admission(tmp_path), updated_at=NOW)
    resumed = coordinator.resume(first.mission_id, updated_at=NOW)
    assert resumed == first
    assert workflow.route_calls == workflow.accept_calls == executor.calls == 1


def test_resume_refuses_foreign_mission_stale_request_and_wrong_generation(
    tmp_path: Path,
) -> None:
    req, coordinator, _, _, _, records, _, _ = planning_harness(tmp_path)
    first = coordinator.start(req, admission(tmp_path), updated_at=NOW)
    original = records.value

    with pytest.raises(Exception) as foreign:
        coordinator.resume("mission-foreign", updated_at=NOW)
    assert getattr(foreign.value, "code", None) == "MISSION_BINDING_MISMATCH"

    stale_request = MissionRequest("Different objective", str(tmp_path), ("src",), ())
    records.value = replace(
        original,
        request=stale_request,
        request_fingerprint=request_fingerprint(stale_request),
    )
    with pytest.raises(Exception) as stale:
        coordinator.resume(first.mission_id, updated_at=NOW)
    assert getattr(stale.value, "code", None) == "STALE_ORCHESTRATION_RECORD"

    records.value = replace(original, workflow_generation=1)
    with pytest.raises(Exception) as generation:
        coordinator.resume(first.mission_id, updated_at=NOW)
    assert getattr(generation.value, "code", None) == "STALE_ORCHESTRATION_RECORD"


def test_resume_refuses_ambiguous_architect_execution(tmp_path: Path) -> None:
    req, coordinator, _, _, ledger, records, _, _ = planning_harness(tmp_path)
    first = coordinator.start(req, admission(tmp_path), updated_at=NOW)
    record = ledger.value.records[0]
    ledger.value = CodexExecutionLedger(
        EXECUTION_LEDGER_VERSION,
        (record, replace(record, execution_id="exec-architect-duplicate")),
    )
    records.value = replace(
        records.value,
        plan_fingerprint=None,
        execution_references=(),
        user_story_ids=(),
    )

    with pytest.raises(Exception) as captured:
        coordinator.resume(first.mission_id, updated_at=NOW)
    assert getattr(captured.value, "code", None) == "ARCHITECT_EXECUTION_AMBIGUOUS"


def test_resume_refuses_malformed_persisted_architect_result(tmp_path: Path) -> None:
    req, coordinator, _, _, ledger, records, _, _ = planning_harness(tmp_path)
    first = coordinator.start(req, admission(tmp_path), updated_at=NOW)
    malformed_json = canonical_result_json({})
    malformed_fingerprint = result_json_fingerprint(malformed_json)
    execution = replace(
        ledger.value.records[0],
        validated_result_json=malformed_json,
        validated_result_fingerprint=malformed_fingerprint,
    )
    ledger.value = CodexExecutionLedger(EXECUTION_LEDGER_VERSION, (execution,))
    reference = replace(
        records.value.execution_references[0],
        result_fingerprint=malformed_fingerprint,
    )
    records.value = replace(records.value, execution_references=(reference,))

    with pytest.raises(Exception) as captured:
        coordinator.resume(first.mission_id, updated_at=NOW)
    assert getattr(captured.value, "code", None) == "ARCHITECT_RESULT_INVALID"


def test_resume_refuses_stale_architect_reference(tmp_path: Path) -> None:
    req, coordinator, _, _, _, records, _, _ = planning_harness(tmp_path)
    first = coordinator.start(req, admission(tmp_path), updated_at=NOW)
    stale = replace(
        records.value.execution_references[0], result_fingerprint="0" * 64
    )
    records.value = replace(records.value, execution_references=(stale,))

    with pytest.raises(Exception) as captured:
        coordinator.resume(first.mission_id, updated_at=NOW)
    assert getattr(captured.value, "code", None) == "ARCHITECT_REFERENCE_MISMATCH"


def test_record_persistence_failure_never_launches_architect(tmp_path: Path) -> None:
    req, coordinator, _, _, _, _, workflow, executor = planning_harness(tmp_path)

    class FailingRecordStore(MemoryRecordStore):
        def initialize(self, record):
            raise OSError("simulated persistence failure")

    coordinator._records = FailingRecordStore()
    with pytest.raises(OSError, match="simulated persistence failure"):
        coordinator.start(req, admission(tmp_path), updated_at=NOW)
    assert workflow.route_calls == executor.calls == 0


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _copy_architect_contracts(root: Path) -> None:
    source = Path(__file__).parents[1]
    for relative in (
        "AGENTS.md",
        "docs/02-invariants.md",
        "docs/03-fail-closed-policy.md",
        "docs/04-authority-model.md",
        "docs/12-codex-operating-contract.md",
        "docs/16-architect.md",
        "docs/35-codex-execution-contract.md",
        "docs/PHASE-3-CERTIFICATION.md",
        "roles/architect.md",
        "schemas/architect-result.schema.json",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    (root / ".gitignore").write_text(
        gitignore_managed_section(MissionStateGitPolicy.IGNORED),
        encoding="utf-8",
    )
    (root / "src").mkdir()


def _authoritative_workflow(
    root: Path, mission_store: MissionStateStore, project_store: ProjectStateStore
) -> SequentialMissionWorkflow:
    validator = ContractValidator()
    control = ControlLoop(
        state_store=project_store,
        evidence_recorder_factory=lambda target: EvidenceRecorder(
            target, validator=validator, clock=lambda: NOW
        ),
        gate_evaluator=GateEvaluator(validator=validator, clock=lambda: NOW),
        certification_service=CertificationService(
            validator=validator, clock=lambda: NOW
        ),
        transition_service=StateTransitionService(),
    )
    return SequentialMissionWorkflow(
        orchestrator=Orchestrator(
            repository_root=root,
            mission_store=mission_store,
            project_state_store=project_store,
        ),
        mission_store=mission_store,
        project_store=project_store,
        control_loop=control,
    )


class _CountingRuntime:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.calls = 0

    def execute(self, compiled_prompt, binding, *, cancellation=None):
        self.calls += 1
        return self.delegate.execute(
            compiled_prompt, binding, cancellation=cancellation
        )


class _DynamicArchitectExecutor:
    def __init__(self, delegate, result_path: Path) -> None:
        self.delegate = delegate
        self.result_path = result_path

    def execute(self, handoff, *, request_id):
        candidate = ArchitectResult(
            handoff.mission_id,
            handoff.workflow_generation,
            handoff.subject,
            handoff.observed_commit,
            "One bounded User Story is planned.",
            (),
            (),
            (),
            (),
            (story(handoff.subject),),
            MissionRole.IMPLEMENTER,
            ArchitectVerdict.READY,
        )
        self.result_path.write_text(
            json.dumps(to_dict(candidate), ensure_ascii=False), encoding="utf-8"
        )
        return self.delegate.execute(handoff, request_id=request_id)


class _ForbiddenArchitectExecutor:
    def execute(self, handoff, *, request_id):
        raise AssertionError("Architect must not execute during reconstruction")


def test_process_restart_reconstructs_authoritative_plan_without_architect_replay(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    _copy_architect_contracts(root)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "M2 Test")
    _git(root, "config", "user.email", "m2@example.invalid")
    project_store = ProjectStateStore(root)
    project_store.initialize(project_id=PROJECT)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "test baseline")
    head = _git(root, "rev-parse", "HEAD").casefold()

    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    manager = WorktreeManager(
        repository_root=root, worktree_root=worktree_root
    )
    manager.initialize_registry()
    mission_store = MissionStateStore(root)
    execution_store = ExecutionStateStore(root)
    execution_store.initialize()
    record_store = OrchestrationRecordStore(root)
    maintenance_fact = maintenance_admission()
    lifecycle_service = MissionLifecycleService(
        mission_store=mission_store,
        project_store=project_store,
        maintenance=Enforcer(maintenance_fact),
    )
    result_path = tmp_path / "architect-result.json"
    executable = Path(sys.executable).resolve()
    executable_digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    runtime = _CountingRuntime(
        CodexRuntimeAdapter(
            CodexRuntimeConfiguration(
                executable=str(executable),
                expected_executable_path=str(executable),
                expected_executable_version="fake-codex 1.0",
                expected_executable_sha256=executable_digest,
                launcher_arguments=(
                    str(FAKE_CODEX),
                    "--fake-mode",
                    "role-result",
                    "--fake-result-file",
                    str(result_path),
                ),
                test_executable_injection=True,
            )
        )
    )
    execution_service = RestartSafeCodexExecutionService(
        execution_store, runtime, ExecutionGitObserver()
    )
    role_executor = _DynamicArchitectExecutor(
        SingleRoleCodexExecutor(
            mission_store=mission_store,
            project_store=project_store,
            repository=manager,
            execution_service=execution_service,
            executable_identity=ExecutionExecutableIdentity(
                str(executable), "fake-codex 1.0", executable_digest
            ),
            timeout_seconds=10,
        ),
        result_path,
    )
    coordinator = MissionPlanningCoordinator(
        lifecycle=lifecycle_service,
        workflow=_authoritative_workflow(root, mission_store, project_store),
        architect_executor=role_executor,
        mission_store=mission_store,
        project_store=project_store,
        execution_store=execution_store,
        record_store=record_store,
    )
    mission_request = MissionRequest(
        "Plan one bounded feature", str(root), ("src",), ()
    )
    admitted = MissionAdmission(
        MissionAdmissionStatus.ADMITTED,
        request_fingerprint(mission_request),
        "d" * 64,
        head,
        PROJECT,
        (),
        (),
        "start",
        maintenance_fact,
    )

    first = coordinator.start(mission_request, admitted, updated_at=NOW)
    assert first.status is MissionPlanningStatus.PLANNED
    assert runtime.calls == 1
    state_before = project_store.state_path.read_bytes()
    mission_before = mission_store.mission_path.read_bytes()
    execution_before = execution_store.ledger_path.read_bytes()
    record_before = record_store.record_path.read_bytes()
    expected_story = ProjectStateStore(root).load().user_stories[0]
    expected_reference = first.execution_reference
    expected_architect = first.architect_result
    mission_id = first.mission_id

    del coordinator, role_executor, execution_service, execution_store
    del lifecycle_service, record_store, manager, mission_store, project_store

    restarted_mission_store = MissionStateStore(root)
    restarted_project_store = ProjectStateStore(root)
    restarted_execution_store = ExecutionStateStore(root)
    restarted_record_store = OrchestrationRecordStore(root)
    restarted = MissionPlanningCoordinator(
        lifecycle=MissionLifecycleService(
            mission_store=restarted_mission_store,
            project_store=restarted_project_store,
            maintenance=Enforcer(maintenance_admission()),
        ),
        workflow=_authoritative_workflow(
            root, restarted_mission_store, restarted_project_store
        ),
        architect_executor=_ForbiddenArchitectExecutor(),
        mission_store=restarted_mission_store,
        project_store=restarted_project_store,
        execution_store=restarted_execution_store,
        record_store=restarted_record_store,
    )
    resumed = restarted.resume(mission_id, updated_at=NOW)

    assert resumed.execution_reference == expected_reference
    assert resumed.architect_result == expected_architect
    assert restarted_project_store.load().user_stories == [expected_story]
    assert restarted_mission_store.load().workflow_generation == 0
    assert restarted_mission_store.load().operating_step is OperatingStep.ACT
    assert restarted_project_store.state_path.read_bytes() == state_before
    assert restarted_mission_store.mission_path.read_bytes() == mission_before
    assert restarted_execution_store.ledger_path.read_bytes() == execution_before
    assert restarted_record_store.record_path.read_bytes() == record_before


def test_orchestration_record_round_trip_and_optimistic_replace(tmp_path: Path) -> None:
    req = request(tmp_path)
    record = OrchestrationRecord(ORCHESTRATION_RECORD_VERSION, "mission-one", req, request_fingerprint(req), HEAD, 0)
    store = OrchestrationRecordStore(tmp_path)
    store.initialize(record)
    restarted = OrchestrationRecordStore(tmp_path)
    assert restarted.load() == record
    reference = RoleExecutionReference(MissionRole.ARCHITECT, "US-0001", 0, "request-one", "execution-one", RESULT_FP)
    updated = record.with_reference(
        reference,
        plan_fingerprint="e" * 64,
        user_story_ids=("US-0001",),
    )
    restarted.replace(updated, expected_fingerprint=record.fingerprint)
    assert store.load() == updated


def test_orchestration_record_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / ".agentic-engineering-os"
    path.mkdir()
    (path / "orchestration.json").write_text('{"schema_version":"1.0","schema_version":"1.0"}', encoding="utf-8")
    with pytest.raises(Exception) as captured:
        OrchestrationRecordStore(tmp_path).load()
    assert getattr(captured.value, "code", None) == "INVALID_ORCHESTRATION_RECORD"


def test_orchestration_store_refuses_foreign_repository_binding(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    req = request(source)
    record = OrchestrationRecord(
        ORCHESTRATION_RECORD_VERSION,
        "mission-one",
        req,
        request_fingerprint(req),
        HEAD,
        0,
    )

    with pytest.raises(Exception) as captured:
        OrchestrationRecordStore(target).initialize(record)
    assert getattr(captured.value, "code", None) == "FOREIGN_ORCHESTRATION_RECORD"
