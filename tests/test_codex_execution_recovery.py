from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    CodexApprovalPolicy,
    CodexExecutionBinding,
    CodexExecutionStatus,
    CodexSandboxMode,
    ExecutionExecutableIdentity,
    ExecutionStateError,
    RestartDisposition,
    RestartSafeCodexExecutionService,
)
from agentic_engineering_os.domain import MissionRole
from agentic_engineering_os.infrastructure import ExecutionGitObserver, ExecutionStateStore, PersistenceError
from test_codex_result_intake import git, intake_case


EXECUTABLE = ExecutionExecutableIdentity("C:/tools/codex.exe", "codex-cli test", "b" * 64)


class FakeRuntime:
    def __init__(self, observation, callback=None, error: Exception | None = None) -> None:
        self.observation = observation
        self.callback = callback
        self.error = error
        self.calls = 0

    def execute(self, compiled_prompt, binding, *, cancellation=None):
        self.calls += 1
        if self.callback:
            self.callback()
        if self.error:
            raise self.error
        return self.observation


def binding(case, **changes) -> CodexExecutionBinding:
    value = CodexExecutionBinding(
        case.compiled.request_id,
        case.compiled.context_fingerprint,
        case.compiled.mission_id,
        case.compiled.workflow_generation,
        case.compiled.role,
        case.compiled.subject,
        case.compiled.repository_root,
        case.compiled.observed_commit,
        CodexSandboxMode.READ_ONLY,
        CodexApprovalPolicy.NEVER,
        10.0,
        output_schema_path=case.context.output_schema_path,
    )
    return replace(value, **changes)


def harness(tmp_path: Path, role: MissionRole = MissionRole.ARCHITECT):
    case = intake_case(tmp_path, role)
    store = ExecutionStateStore(case.compiled.repository_root)
    store.initialize()
    runtime = FakeRuntime(case.observation)
    service = RestartSafeCodexExecutionService(store, runtime, ExecutionGitObserver())
    return case, store, runtime, service


def planned(harness_value):
    case, store, runtime, service = harness_value
    record = service.plan(case.compiled, binding(case), EXECUTABLE)
    return case, store, runtime, service, record


def observed(harness_value):
    case, store, runtime, service, record = planned(harness_value)
    service.execute(record.execution_id, case.compiled, binding(case))
    return case, store, runtime, service, record


def test_intent_is_durable_before_runtime_launch(tmp_path: Path) -> None:
    case, store, runtime, service = harness(tmp_path)
    record = service.plan(case.compiled, binding(case), EXECUTABLE)
    runtime.callback = lambda: (
        store.load().records[0].status is CodexExecutionStatus.RUNNING
        or pytest.fail("RUNNING was not durable before runtime launch")
    )

    service.execute(record.execution_id, case.compiled, binding(case))

    assert store.load().records[0].status is CodexExecutionStatus.OBSERVED


def test_duplicate_plan_is_idempotent_but_no_second_attempt_is_created(tmp_path: Path) -> None:
    case, store, _, service, record = planned(harness(tmp_path))

    duplicate = service.plan(case.compiled, binding(case), EXECUTABLE)

    assert duplicate == record
    assert len(store.load().records) == 1


def test_planned_restart_proves_process_was_not_started(tmp_path: Path) -> None:
    case, _, runtime, service, record = planned(harness(tmp_path))

    inspection = service.inspect_restart(record.execution_id, case.compiled, binding(case), EXECUTABLE)

    assert inspection.disposition is RestartDisposition.SAFE_NOT_STARTED
    assert inspection.can_execute_current_request
    assert runtime.calls == 0


@pytest.mark.parametrize(
    "field",
    [
        "request_id",
        "context_fingerprint",
        "mission_id",
        "workflow_generation",
        "role",
        "subject",
        "observed_commit",
        "expected_result_contract",
        "prompt_text",
    ],
)
def test_stale_or_cross_bound_prompt_is_rejected(tmp_path: Path, field: str) -> None:
    case, _, _, service, record = planned(harness(tmp_path))
    value = {
        "request_id": "other-request",
        "context_fingerprint": "c" * 64,
        "mission_id": "other-mission",
        "workflow_generation": 99,
        "role": MissionRole.REVIEWER,
        "subject": "other",
        "observed_commit": "f" * 40,
        "expected_result_contract": "reviewer-result@1.0",
        "prompt_text": "changed",
    }[field]
    stale = replace(case.compiled, **{field: value})

    inspection = service.inspect_restart(record.execution_id, stale, binding(case), EXECUTABLE)

    assert inspection.disposition is RestartDisposition.STALE_OR_INCONSISTENT
    assert not inspection.blind_retry_allowed


def test_runtime_crash_leaves_running_and_forbids_blind_retry(tmp_path: Path) -> None:
    case, store, runtime, service, record = planned(harness(tmp_path))
    runtime.error = RuntimeError("simulated crash")

    with pytest.raises(ExecutionStateError, match="RUNTIME_OUTCOME_UNCERTAIN"):
        service.execute(record.execution_id, case.compiled, binding(case))

    assert store.load().records[0].status is CodexExecutionStatus.RUNNING
    inspection = service.inspect_restart(record.execution_id, case.compiled, binding(case), EXECUTABLE)
    assert inspection.disposition is RestartDisposition.NEW_REQUEST_REQUIRED
    assert not inspection.can_execute_current_request
    assert not inspection.blind_retry_allowed


def test_crash_after_observation_allows_intake_replay_without_runtime_rerun(tmp_path: Path) -> None:
    case, store, runtime, service, record = observed(harness(tmp_path))
    restarted_runtime = FakeRuntime(case.observation, error=AssertionError("must not run"))
    restarted = RestartSafeCodexExecutionService(store, restarted_runtime, ExecutionGitObserver())

    inspection = restarted.inspect_restart(record.execution_id, case.compiled, binding(case), EXECUTABLE)
    outcome = restarted.replay_intake(record.execution_id, case.compiled, case.context)

    assert inspection.disposition is RestartDisposition.INTAKE_REPLAY_AVAILABLE
    assert outcome.accepted
    assert restarted_runtime.calls == 0
    assert store.load().records[0].status is CodexExecutionStatus.VALIDATED


def test_validated_result_is_revalidated_and_never_rerun(tmp_path: Path) -> None:
    case, store, _, service, record = observed(harness(tmp_path))
    service.replay_intake(record.execution_id, case.compiled, case.context)
    restarted_runtime = FakeRuntime(case.observation, error=AssertionError("must not run"))
    restarted = RestartSafeCodexExecutionService(store, restarted_runtime, ExecutionGitObserver())

    inspection = restarted.inspect_restart(
        record.execution_id, case.compiled, binding(case), EXECUTABLE, validation_context=case.context
    )

    assert inspection.disposition is RestartDisposition.VALIDATED_NO_RERUN
    with pytest.raises(ExecutionStateError, match="BLIND_RETRY_FORBIDDEN"):
        restarted.execute(record.execution_id, case.compiled, binding(case))
    assert restarted_runtime.calls == 0


def test_git_drift_after_uncertain_crash_requires_operator_recovery(tmp_path: Path) -> None:
    case, _, runtime, service, record = planned(harness(tmp_path))
    runtime.error = RuntimeError("crash")
    with pytest.raises(ExecutionStateError):
        service.execute(record.execution_id, case.compiled, binding(case))
    root = Path(case.compiled.repository_root)
    (root / "partial.txt").write_text("side effect", encoding="utf-8")

    inspection = service.inspect_restart(record.execution_id, case.compiled, binding(case), EXECUTABLE)

    assert inspection.disposition is RestartDisposition.RECOVERY_REQUIRED
    assert inspection.operator_intervention_required


@pytest.mark.parametrize("dirty", [False, True])
def test_timeout_requires_new_request_or_recovery_according_to_git(tmp_path: Path, dirty: bool) -> None:
    case, store, runtime, service, record = planned(harness(tmp_path))
    root = Path(case.compiled.repository_root)
    if dirty:
        (root / "partial.txt").write_text("side effect", encoding="utf-8")
    runtime.observation = replace(
        case.observation,
        timed_out=True,
        interrupted=True,
        exit_code=None,
        git_after=replace(case.observation.git_after, clean=not dirty),
    )

    service.execute(record.execution_id, case.compiled, binding(case))
    inspection = service.inspect_restart(record.execution_id, case.compiled, binding(case), EXECUTABLE)

    assert store.load().records[0].status is CodexExecutionStatus.INTERRUPTED
    assert inspection.disposition is (RestartDisposition.RECOVERY_REQUIRED if dirty else RestartDisposition.NEW_REQUEST_REQUIRED)
    assert not inspection.blind_retry_allowed


def test_commit_then_crash_before_role_result_requires_recovery(tmp_path: Path) -> None:
    case, _, runtime, service, record = planned(harness(tmp_path))
    root = Path(case.compiled.repository_root)

    def commit_side_effect() -> None:
        (root / "partial.txt").write_text("committed side effect", encoding="utf-8")
        git(root, "add", "partial.txt")
        git(root, "commit", "-m", "test: partial codex side effect")

    runtime.callback = commit_side_effect
    runtime.error = RuntimeError("crash after commit")
    with pytest.raises(ExecutionStateError):
        service.execute(record.execution_id, case.compiled, binding(case))

    inspection = service.inspect_restart(record.execution_id, case.compiled, binding(case), EXECUTABLE)
    assert inspection.current_git.head_commit != case.compiled.observed_commit
    assert inspection.disposition is RestartDisposition.RECOVERY_REQUIRED


def test_generation_drift_and_executable_drift_are_fail_closed(tmp_path: Path) -> None:
    case, _, _, service, record = planned(harness(tmp_path))

    generation = replace(case.compiled, workflow_generation=case.compiled.workflow_generation + 1)
    changed_executable = replace(EXECUTABLE, sha256="c" * 64)

    assert service.inspect_restart(record.execution_id, generation, binding(case), EXECUTABLE).disposition is RestartDisposition.STALE_OR_INCONSISTENT
    assert service.inspect_restart(record.execution_id, case.compiled, binding(case), changed_executable).disposition is RestartDisposition.STALE_OR_INCONSISTENT


def test_wrong_worktree_is_fail_closed(tmp_path: Path) -> None:
    case, _, _, service, record = planned(harness(tmp_path))
    wrong = str(Path(case.compiled.repository_root).parent)
    inspection = service.inspect_restart(record.execution_id, case.compiled, binding(case, cwd=wrong), EXECUTABLE)
    assert inspection.disposition is RestartDisposition.STALE_OR_INCONSISTENT


def test_corrupt_truncated_duplicate_and_oversized_state_fail_closed(tmp_path: Path) -> None:
    case, store, _, _, _ = planned(harness(tmp_path))
    path = store.ledger_path
    for payload in ('{"schema_version":', '{"schema_version":"1.0","schema_version":"1.0","records":[]}', "x" * 16_000_001):
        path.write_text(payload, encoding="utf-8")
        with pytest.raises(PersistenceError):
            store.load()


def test_forged_record_and_duplicate_semantic_record_fail_closed(tmp_path: Path) -> None:
    _, store, _, _, _ = planned(harness(tmp_path))
    data = json.loads(store.ledger_path.read_text(encoding="utf-8"))
    forged = json.loads(json.dumps(data))
    forged["records"][0]["semantic_fingerprint"] = "c" * 64
    store.ledger_path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(PersistenceError, match="FORGED_EXECUTION_IDENTITY"):
        store.load()

    store.ledger_path.write_text(json.dumps(data), encoding="utf-8")
    duplicate = json.loads(json.dumps(data))
    duplicate["records"].append(duplicate["records"][0])
    store.ledger_path.write_text(json.dumps(duplicate), encoding="utf-8")
    with pytest.raises(PersistenceError, match="DUPLICATE_EXECUTION"):
        store.load()


def test_plan_and_running_persistence_failures_leave_previous_durable_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case, store, _, service = harness(tmp_path)
    original = store._write
    monkeypatch.setattr(store, "_write", lambda ledger: (_ for _ in ()).throw(PersistenceError("WRITE_FAILED", "simulated")))
    with pytest.raises(PersistenceError):
        service.plan(case.compiled, binding(case), EXECUTABLE)
    assert store.load().records == ()

    monkeypatch.setattr(store, "_write", original)
    record = service.plan(case.compiled, binding(case), EXECUTABLE)
    monkeypatch.setattr(store, "_write", lambda ledger: (_ for _ in ()).throw(PersistenceError("WRITE_FAILED", "simulated")))
    with pytest.raises(PersistenceError):
        service.execute(record.execution_id, case.compiled, binding(case))
    assert store.load().records[0].status is CodexExecutionStatus.PLANNED


def test_observation_persistence_failure_leaves_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case, store, _, service, record = planned(harness(tmp_path))
    original = store._write
    calls = 0

    def fail_after_running(ledger):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PersistenceError("WRITE_FAILED", "simulated")
        return original(ledger)

    monkeypatch.setattr(store, "_write", fail_after_running)
    with pytest.raises(ExecutionStateError, match="OBSERVATION_PERSISTENCE_FAILED"):
        service.execute(record.execution_id, case.compiled, binding(case))

    assert store.load().records[0].status is CodexExecutionStatus.RUNNING


def test_intake_persistence_failure_leaves_observed_for_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case, store, _, service, record = observed(harness(tmp_path))
    monkeypatch.setattr(store, "_write", lambda ledger: (_ for _ in ()).throw(PersistenceError("WRITE_FAILED", "simulated")))

    with pytest.raises(ExecutionStateError, match="INTAKE_PERSISTENCE_FAILED"):
        service.replay_intake(record.execution_id, case.compiled, case.context)

    assert store.load().records[0].status is CodexExecutionStatus.OBSERVED


def test_ledger_has_no_public_arbitrary_save_and_runtime_facts_have_no_authority(tmp_path: Path) -> None:
    case, store, _, service, record = observed(harness(tmp_path))

    assert not hasattr(store, "save")
    persisted = store.load().records[0]
    assert persisted.status is CodexExecutionStatus.OBSERVED
    for forbidden in ("evidence", "gate", "certification", "mission_status", "project_state"):
        assert not hasattr(persisted, forbidden)

    candidate = replace(
        store.load(),
        records=(replace(persisted, status=CodexExecutionStatus.FAILED, failure_reasons=("forged",)),),
    )
    with pytest.raises(PersistenceError, match="WRITE_NOT_AUTHORIZED"):
        store._replace_authorized(candidate, authorization=object(), operation="RECORD_INTAKE")
