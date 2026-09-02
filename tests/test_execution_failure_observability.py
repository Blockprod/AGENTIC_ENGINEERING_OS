from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_engineering_os import cli
from agentic_engineering_os.application import (
    CodexExecutionStatus,
    ExecutionObservabilityError,
    ExecutionOperationalEventReader,
    MetricsEngine,
    project_terminal_execution_events,
)
from agentic_engineering_os.domain import (
    MetricName,
    MetricsScope,
    MissionRole,
    MissionState,
    MissionStateGitPolicy,
    MissionStatus,
    OperatingStep,
)
from agentic_engineering_os.infrastructure import (
    ExecutionStateStore,
    MissionStateStore,
    OperationalEventStore,
    ProjectConfigurationValidator,
    ProjectStateStore,
    WorktreeRegistryStore,
)
from test_codex_execution_recovery import EXECUTABLE, binding, harness, observed


PROJECT = "execution-observability-project"


def _install_product_context(
    root: Path, compiled, *, mission_generation: int | None = None
) -> None:
    state = root / ".agentic-engineering-os"
    configuration = ProjectConfigurationValidator().validate(
        {
            "config_version": "1.0",
            "project_id": PROJECT,
            "repository_root_policy": "CONFIG_PARENT_GIT_ROOT",
            "toolchains": [],
            "verification_commands": [],
            "path_policy": {
                "allowed_paths": [],
                "protected_paths": [],
                "forbidden_paths": [],
            },
            "context_sources": [],
            "codex_constraints": {
                "maximum_sandbox": "read-only",
                "approval_policy": "never",
                "require_clean_git": True,
                "maximum_parallel_executions": 2,
            },
            "mission_state_git_policy": MissionStateGitPolicy.TRACKED.value,
        }
    )
    (state / "config.json").write_text(
        ProjectConfigurationValidator().serialize(configuration), encoding="utf-8"
    )
    ProjectStateStore(root).initialize(project_id=PROJECT)
    WorktreeRegistryStore(root).initialize()
    MissionStateStore(root).initialize(
        MissionState(
            "1.0",
            compiled.mission_id,
            compiled.workflow_generation
            if mission_generation is None
            else mission_generation,
            MissionStatus.ACTIVE,
            MissionRole.ARCHITECT,
            "Observe failed execution",
            compiled.subject,
            OperatingStep.ACT,
            "inspect execution failure",
            compiled.observed_commit,
            datetime.now(timezone.utc),
        )
    )


def _invoke(capsys, command: str, root: Path):
    code = cli.main([command, "--repository", str(root), "--json"])
    captured = capsys.readouterr()
    raw = captured.out if captured.out else captured.err
    return code, json.loads(raw)


def _metric(snapshot, name: MetricName):
    return next(item for item in snapshot.metrics if item.name is name)


def _failed_execution(tmp_path: Path, **observation_changes):
    case, store, runtime, service = harness(tmp_path)
    compiled = replace(case.compiled, workflow_generation=0)
    execution_binding = replace(binding(case), workflow_generation=0)
    record = service.plan(compiled, execution_binding, EXECUTABLE)
    changes = {"exit_code": 1, **observation_changes}
    runtime.observation = replace(case.observation, **changes)
    service.execute(record.execution_id, compiled, execution_binding)
    return case, compiled, store, record


def test_persisted_failed_execution_reaches_metrics_health_and_diagnose(
    capsys, tmp_path: Path
) -> None:
    _, compiled, store, _ = _failed_execution(tmp_path)
    root = Path(compiled.repository_root)
    _install_product_context(root, compiled)

    assert store.load().records[0].status is CodexExecutionStatus.FAILED
    metrics_code, metrics = _invoke(capsys, "metrics", root)
    health_code, health = _invoke(capsys, "health", root)
    diagnose_code, diagnose = _invoke(capsys, "diagnose", root)

    failed = next(
        item
        for item in metrics["result"]["metrics"]
        if item["name"] == "codex_executions.failed"
    )
    assert metrics_code == 0
    assert failed["value"] == 1
    assert health_code == 2
    assert health["result"]["global_state"] != "HEALTHY"
    assert diagnose_code == 2
    assert diagnose["status"] == "ATTENTION_REQUIRED"


@pytest.mark.parametrize(
    ("changes", "status", "reason"),
    [
        ({"timed_out": True, "interrupted": True, "exit_code": None}, "INTERRUPTED", "TIMEOUT"),
        ({"interrupted": True, "exit_code": None}, "INTERRUPTED", "INTERRUPTED"),
        ({"tool_failure_observed": True, "exit_code": 0}, "FAILED", "TOOL_FAILURE_EXIT_ZERO"),
    ],
)
def test_terminal_runtime_variants_are_factual_and_non_healthy(
    capsys, tmp_path: Path, changes, status: str, reason: str
) -> None:
    _, compiled, store, _ = _failed_execution(tmp_path, **changes)
    root = Path(compiled.repository_root)
    _install_product_context(root, compiled)

    assert store.load().records[0].status.value == status
    code, health = _invoke(capsys, "health", root)
    events = ExecutionOperationalEventReader(
        OperationalEventStore(root),
        store,
        project_id=PROJECT,
        repository_root=root,
    ).read()

    assert code == 2
    assert health["result"]["global_state"] != "HEALTHY"
    assert events[-1].payload.reason_code == reason


def test_restart_projection_is_stable_exactly_once_and_does_not_write_event_store(
    tmp_path: Path,
) -> None:
    _, compiled, store, _ = _failed_execution(tmp_path)
    root = Path(compiled.repository_root)
    event_store = OperationalEventStore(root)

    first = ExecutionOperationalEventReader(
        event_store, store, project_id=PROJECT, repository_root=root
    ).read()
    restarted = ExecutionOperationalEventReader(
        event_store,
        ExecutionStateStore(root),
        project_id=PROJECT,
        repository_root=root,
    ).read()

    assert first == restarted
    assert len(first) == len({item.event_id for item in first}) == 2
    assert event_store.read() == ()


def test_corrupt_event_store_remains_unavailable_instead_of_faking_zero(
    tmp_path: Path,
) -> None:
    _, compiled, store, _ = _failed_execution(tmp_path)
    root = Path(compiled.repository_root)
    event_dir = root / ".agentic-engineering-os" / "operational-events"
    event_dir.mkdir()
    (event_dir / "segment-000001.jsonl").write_text("{broken\n", encoding="utf-8")
    reader = ExecutionOperationalEventReader(
        OperationalEventStore(root), store, project_id=PROJECT, repository_root=root
    )

    snapshot = MetricsEngine().compute_from_store(reader, MetricsScope(PROJECT))

    assert snapshot.status.value == "UNAVAILABLE"
    assert snapshot.metrics == ()


def test_foreign_repository_execution_is_refused(tmp_path: Path) -> None:
    _, compiled, store, _ = _failed_execution(tmp_path)

    with pytest.raises(ExecutionObservabilityError, match="foreign repository"):
        project_terminal_execution_events(
            store.load(),
            project_id=PROJECT,
            repository_root=Path(compiled.repository_root).parent,
        )


def test_stale_generation_failure_is_historical_but_does_not_poison_current_health(
    capsys, tmp_path: Path
) -> None:
    case, store, runtime, service = harness(tmp_path)
    record = service.plan(case.compiled, binding(case), EXECUTABLE)
    runtime.observation = replace(case.observation, exit_code=1)
    service.execute(record.execution_id, case.compiled, binding(case))
    root = Path(case.compiled.repository_root)
    _install_product_context(root, case.compiled, mission_generation=0)
    projected = project_terminal_execution_events(
        store.load(), project_id=PROJECT, repository_root=root
    )

    historical = MetricsEngine().compute(
        projected, MetricsScope(PROJECT), source_complete=True
    )
    current = MetricsEngine().compute(
        projected,
        MetricsScope(PROJECT, case.compiled.mission_id, 0),
        source_complete=True,
    )
    code, health = _invoke(capsys, "health", root)

    assert _metric(historical, MetricName.CODEX_EXECUTIONS_FAILED).value == 1
    assert _metric(current, MetricName.CODEX_EXECUTIONS_FAILED).value == 0
    assert code == 0
    assert health["result"]["global_state"] == "HEALTHY"


def test_forged_codex_event_without_ledger_record_fails_closed(tmp_path: Path) -> None:
    _, compiled, store, _ = _failed_execution(tmp_path)
    root = Path(compiled.repository_root)
    factual = project_terminal_execution_events(
        store.load(), project_id=PROJECT, repository_root=root
    )[-1]
    forged = replace(
        factual,
        event_id="00000000-0000-4000-8000-000000000777",
        correlation=replace(factual.correlation, execution_id="forged-execution"),
    )
    event_store = OperationalEventStore(root)
    event_store.append(forged)
    reader = ExecutionOperationalEventReader(
        event_store, store, project_id=PROJECT, repository_root=root
    )

    snapshot = MetricsEngine().compute_from_store(reader, MetricsScope(PROJECT))

    assert snapshot.status.value == "UNAVAILABLE"
    assert store.load().records[0].status is CodexExecutionStatus.FAILED


def test_validated_execution_projects_success_without_failure_metric(tmp_path: Path) -> None:
    case, store, _, service, record = observed(harness(tmp_path))
    outcome = service.replay_intake(record.execution_id, case.compiled, case.context)
    root = Path(case.compiled.repository_root)
    reader = ExecutionOperationalEventReader(
        OperationalEventStore(root), store, project_id=PROJECT, repository_root=root
    )

    snapshot = MetricsEngine().compute_from_store(reader, MetricsScope(PROJECT))

    assert outcome.accepted
    assert _metric(snapshot, MetricName.CODEX_EXECUTIONS_COMPLETED).value == 1
    assert _metric(snapshot, MetricName.CODEX_EXECUTIONS_FAILED).value == 0
    assert _metric(snapshot, MetricName.CODEX_EXECUTIONS_INTERRUPTED).value == 0


def test_diagnostics_projection_is_read_only_and_has_no_authority_side_effects(
    capsys, tmp_path: Path
) -> None:
    _, compiled, _, _ = _failed_execution(tmp_path)
    root = Path(compiled.repository_root)
    _install_product_context(root, compiled)
    before = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }

    for command in ("metrics", "health", "diagnose"):
        _invoke(capsys, command, root)

    after = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    assert after == before
    assert not (root / ".agentic-engineering-os" / "evidence").exists()
    assert not (root / ".agentic-engineering-os" / "certifications").exists()
