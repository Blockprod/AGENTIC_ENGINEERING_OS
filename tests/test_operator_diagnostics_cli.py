from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_engineering_os import cli
from agentic_engineering_os.domain import (
    MissionStateGitPolicy,
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    OperationalCorrelation,
    OperationalEvent,
    OperationalEventPayload,
    OperationalEventType,
    OperationalProvenance,
    OperationalProvenanceKind,
    OperationalSeverity,
)
from agentic_engineering_os.infrastructure import (
    ExecutionStateStore,
    MissionStateStore,
    OperationalEventStore,
    ProjectConfigurationValidator,
    ProjectStateStore,
    WorktreeRegistryStore,
)


PROJECT = "diagnostic-project"


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Diagnostic Test")
    _git(root, "config", "user.email", "diagnostic@example.invalid")
    (root / "README.md").write_text("# Diagnostic fixture\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "fixture")
    state = root / ".agentic-engineering-os"
    state.mkdir()
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
    ExecutionStateStore(root).initialize()
    WorktreeRegistryStore(root).initialize()
    return root


def _invoke(capsys, command: str, root: Path, *arguments: str):
    code = cli.main([command, "--repository", str(root), *arguments, "--json"])
    captured = capsys.readouterr()
    raw = captured.out if captured.out else captured.err
    return code, json.loads(raw), raw


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def _activate_mission(root: Path, *, observed_commit: str | None = None) -> None:
    MissionStateStore(root).initialize(
        MissionState(
            "1.0",
            "mission-1",
            0,
            MissionStatus.ACTIVE,
            MissionRole.ORCHESTRATOR,
            "Observe diagnostics",
            "repository",
            OperatingStep.ACT,
            "inspect",
            observed_commit or _git(root, "rev-parse", "HEAD"),
            datetime.now(timezone.utc),
        )
    )


def _event(
    root: Path,
    *,
    age: timedelta = timedelta(),
    project_id: str = PROJECT,
    mission_scoped: bool = False,
):
    head = _git(root, "rev-parse", "HEAD")
    event = OperationalEvent(
        "1.0",
        "00000000-0000-4000-8000-000000000901",
        OperationalEventType.PERSISTENCE_FAILURE,
        datetime.now(timezone.utc) - age,
        OperationalSeverity.ERROR,
        "diagnostic-fixture",
        project_id,
        OperationalCorrelation(
            mission_id="mission-1" if mission_scoped else None,
            workflow_generation=0 if mission_scoped else None,
            repository_commit=head,
        ),
        OperationalEventPayload("WRITE_FAILED", reason_code="WRITE_FAILED"),
        OperationalProvenance(
            OperationalProvenanceKind.DETERMINISTIC_COMPONENT,
            "diagnostic-fixture",
        ),
    )
    OperationalEventStore(root).append(event)


def test_healthy_repository_and_health_contract(capsys, tmp_path: Path) -> None:
    code, payload, _ = _invoke(capsys, "health", _repository(tmp_path))
    assert code == 0
    assert payload["status"] == "HEALTHY"
    result = payload["result"]
    assert result["global_state"] == "HEALTHY"
    assert result["dimensions"]
    assert result["reasons"]
    assert result["scope"]["project_id"] == PROJECT
    assert all("freshness" in item for item in result["dimensions"])
    assert "diagnostics" in result


def test_degraded_and_unknown_health_are_attention(capsys, tmp_path: Path) -> None:
    degraded = _repository(tmp_path / "degraded")
    _activate_mission(degraded)
    _event(degraded, mission_scoped=True)
    code, payload, _ = _invoke(capsys, "health", degraded)
    assert code == 2
    assert payload["status"] == "DEGRADED"

    unknown = _repository(tmp_path / "unknown")
    event_dir = unknown / ".agentic-engineering-os" / "operational-events"
    event_dir.mkdir()
    (event_dir / "segment-000001.jsonl").write_text("{broken\n", encoding="utf-8")
    code, payload, _ = _invoke(capsys, "health", unknown)
    assert code == 2
    assert payload["status"] == "UNKNOWN"


def test_metrics_complete_incomplete_and_unavailable(capsys, tmp_path: Path) -> None:
    complete = _repository(tmp_path / "complete")
    code, payload, _ = _invoke(capsys, "metrics", complete)
    assert code == 0
    assert payload["result"]["status"] == "COMPLETE"

    incomplete = _repository(tmp_path / "incomplete")
    directory = incomplete / ".agentic-engineering-os" / "operational-events"
    directory.mkdir()
    (directory / ".retention-exhausted").write_bytes(b"1.0\n")
    code, payload, _ = _invoke(capsys, "metrics", incomplete)
    assert code == 2
    assert payload["result"]["status"] == "INCOMPLETE"

    unavailable = _repository(tmp_path / "unavailable")
    directory = unavailable / ".agentic-engineering-os" / "operational-events"
    directory.mkdir()
    (directory / "segment-000001.jsonl").write_text("not-json\n", encoding="utf-8")
    code, payload, _ = _invoke(capsys, "metrics", unavailable)
    assert code == 2
    assert payload["result"]["status"] == "UNAVAILABLE"


def test_metrics_closed_scopes_and_malformed_scope_fail_closed(capsys, tmp_path: Path) -> None:
    root = _repository(tmp_path)
    code, payload, _ = _invoke(capsys, "metrics", root, "--story", "US-1")
    assert code == 1
    assert payload["status"] == "ERROR"
    assert "US-1" not in payload["result"]["detail"]

    code, payload, _ = _invoke(capsys, "metrics", root, "--mission", "bad;scope", "--generation", "1")
    assert code in {1, 2}
    assert payload["status"] in {"ERROR", "BLOCKED"}


def test_active_incident_listing_and_exact_inspection(capsys, tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _event(root)
    code, payload, _ = _invoke(capsys, "incidents", root)
    assert code == 2
    records = payload["result"]["records"]
    assert records
    incident_id = records[0]["incident_id"]
    code, exact, _ = _invoke(capsys, "incidents", root, "--incident", incident_id)
    assert code == 2
    assert exact["result"]["incident_id"] == incident_id
    assert exact["result"]["classification"]
    assert exact["result"]["state"]
    assert exact["result"]["escalation"]
    assert exact["result"]["correlation"] is not None


def test_incident_actions_are_not_a_cli_surface(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    for action in ("--resolve", "--ack", "--remediate"):
        result = subprocess.run(
            [sys.executable, "-m", "agentic_engineering_os", "incidents", "--repository", str(root), action, "inc-fake"],
            shell=False,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2


def test_diagnose_aggregates_engines_without_synthetic_score(capsys, tmp_path: Path) -> None:
    code, payload, raw = _invoke(capsys, "diagnose", _repository(tmp_path))
    assert code == 0
    result = payload["result"]
    assert result["authority_notice"] == "DIAGNOSTIC_ONLY_NOT_AUTHORIZATION"
    assert result["governance"]["meaning"] == "NO_ADDITIONAL_GOVERNANCE_BLOCK"
    assert result["budgets"]["meaning"] == "ONE_EXECUTION_DIAGNOSTIC_PROBE_NOT_AUTHORIZATION"
    assert result["budgets"]["decision_set"]["decisions"][0]["current_value"] == 0
    assert result["budgets"]["decision_set"]["decisions"][0]["requested_value"] == 1
    assert result["store_diagnostics"]["operational_event_store"] == "AVAILABLE"
    assert "confidence" not in raw.casefold()
    assert "operation authorized" not in raw.casefold()


def test_json_is_canonical_and_module_entrypoint_matches(capsys, tmp_path: Path) -> None:
    root = _repository(tmp_path)
    code, payload, raw = _invoke(capsys, "metrics", root)
    assert code == 0
    assert raw == json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    module = subprocess.run(
        [sys.executable, "-m", "agentic_engineering_os", "metrics", "--repository", str(root), "--json"],
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    assert module.returncode == code
    assert json.loads(module.stdout) == payload
    executable = Path(sys.executable).with_name(
        "agentic-os.exe" if os.name == "nt" else "agentic-os"
    )
    if not executable.is_file():
        return
    try:
        console = subprocess.run(
            [str(executable), "metrics", "--repository", str(root), "--json"],
            shell=False,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        if getattr(error, "winerror", None) == 4551:
            return
        raise
    assert console.returncode == code
    assert json.loads(console.stdout) == payload


def test_commands_do_not_mutate_repository_or_authority(capsys, tmp_path: Path) -> None:
    root = _repository(tmp_path)
    before = _files(root)
    for command in ("health", "metrics", "incidents", "diagnose"):
        _invoke(capsys, command, root)
    assert _files(root) == before
    assert not any(
        name in vars(cli)
        for name in ("StateTransitionService", "EvidenceRecorder", "CertificationService")
    )


def test_diagnose_degrades_an_absent_lazy_execution_ledger(
    capsys, tmp_path: Path
) -> None:
    root = _repository(tmp_path)
    ExecutionStateStore(root).ledger_path.unlink()

    code, payload, raw = _invoke(capsys, "diagnose", root)

    assert code == 2
    assert payload["status"] == "ATTENTION_REQUIRED"
    assert payload["result"]["store_diagnostics"]["operational_event_store"] == "UNAVAILABLE"
    assert "Traceback" not in raw


def test_foreign_project_data_fails_closed_without_cross_project_aggregation(capsys, tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _event(root, project_id="foreign-project")
    code, payload, raw = _invoke(capsys, "metrics", root)
    assert code == 2
    assert payload["result"]["status"] == "INCOMPLETE"
    assert "foreign-project" not in raw


def test_fake_project_and_stale_event_fail_closed(capsys, tmp_path: Path) -> None:
    root = _repository(tmp_path)
    code, payload, _ = _invoke(capsys, "health", root, "--project-id", "fake-project")
    assert code == 2
    assert payload["result"]["code"] == "PROJECT_SCOPE_MISMATCH"

    _event(root, age=timedelta(minutes=10))
    code, payload, _ = _invoke(capsys, "incidents", root)
    assert code == 2
    assert any(
        item["classification"] == "UNKNOWN_CRITICAL_STATE"
        for item in payload["result"]["records"]
    )


def test_missing_authoritative_state_and_wrong_head_fail_closed(capsys, tmp_path: Path) -> None:
    missing = _repository(tmp_path / "missing")
    (missing / ".agentic-engineering-os" / "state.json").unlink()
    code, payload, _ = _invoke(capsys, "health", missing)
    assert code == 2
    assert payload["result"]["global_state"] == "BLOCKED"

    stale = _repository(tmp_path / "stale")
    _activate_mission(stale, observed_commit="f" * 40)
    code, payload, _ = _invoke(capsys, "health", stale)
    assert code == 2
    assert payload["result"]["global_state"] == "UNKNOWN"
    assert any(
        item["freshness"] == "STALE" for item in payload["result"]["dimensions"]
    )


def test_corrupt_secret_source_does_not_leak(capsys, tmp_path: Path) -> None:
    root = _repository(tmp_path)
    secret = "ghp_" + "abcdefghijklmnopqrstuvwxyz123456"
    directory = root / ".agentic-engineering-os" / "operational-events"
    directory.mkdir()
    (directory / "segment-000001.jsonl").write_text(
        json.dumps({"payload": secret}) + "\n", encoding="utf-8"
    )
    code, payload, raw = _invoke(capsys, "incidents", root)
    assert code == 2
    assert payload["result"]["code"] == "INCIDENT_SOURCE_UNAVAILABLE"
    assert secret not in raw


def test_traversal_and_unknown_query_surface_are_refused(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    traversal = subprocess.run(
        [sys.executable, "-m", "agentic_engineering_os", "health", "--repository", str(root / ".." / "repository")],
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    assert traversal.returncode == 2

    unknown = subprocess.run(
        [sys.executable, "-m", "agentic_engineering_os", "metrics", "--repository", str(root), "--metric", "arbitrary", "--policy", "allow-all"],
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    assert unknown.returncode == 2

    forged = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentic_engineering_os",
            "diagnose",
            "--repository",
            str(root),
            "--health-state",
            "HEALTHY",
            "--governance-decision",
            "ALLOW",
        ],
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    assert forged.returncode == 2



def test_symlink_repository_is_refused_without_platform_privilege(
    monkeypatch, tmp_path: Path
) -> None:
    root = _repository(tmp_path)
    monkeypatch.setattr(cli, "_has_symlink_component", lambda path: True)
    with pytest.raises(cli.CliError, match="UNSAFE_REPOSITORY_PATH"):
        cli._repository_root(str(root))


def test_output_is_bounded(capsys) -> None:
    with pytest.raises(cli.CliError, match="OUTPUT_LIMIT_EXCEEDED"):
        cli._emit("diagnose", "OK", {"value": "x" * 1_000_001}, True)
    assert capsys.readouterr().out == ""
