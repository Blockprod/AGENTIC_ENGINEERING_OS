from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from agentic_engineering_os.application import (
    IntegrationGateClassification,
    MissionIntegrationResult,
    MissionIntegrationStatus,
    MissionPhase,
    MissionRequest,
    MissionRoleLaunchResult,
    MissionRunResult,
    MissionRunStatus,
    ORCHESTRATION_RECORD_VERSION,
    OrchestrationRecord,
    request_fingerprint,
)
from agentic_engineering_os.application.mission_composition import (
    _OperationalMissionEventSink,
)
from agentic_engineering_os.domain import (
    Evidence,
    EvidenceType,
    MissionRole,
    OperationalEventType,
    ProjectState,
)
from agentic_engineering_os.infrastructure import OperationalEventStore


NOW = datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc)
HEAD = "a" * 40


class _Store:
    def __init__(self, value) -> None:
        self.value = value

    def load(self):
        return self.value


class _Worktrees:
    def __init__(self, assignments=()) -> None:
        self.registry_store = _Store(SimpleNamespace(assignments=assignments))


def test_operational_sink_emits_correlated_boundary_families_once(
    tmp_path: Path,
) -> None:
    request = MissionRequest("Observe mission boundaries", str(tmp_path))
    record = OrchestrationRecord(
        ORCHESTRATION_RECORD_VERSION,
        "mission-1",
        request,
        request_fingerprint(request),
        HEAD,
        0,
    )
    sink = _OperationalMissionEventSink(
        tmp_path,
        "project",
        _Store(SimpleNamespace(records=())),  # type: ignore[arg-type]
        _Store(ProjectState("1.0", project_id="project")),  # type: ignore[arg-type]
        _Store(record),  # type: ignore[arg-type]
        _Worktrees(),  # type: ignore[arg-type]
    )
    mission = SimpleNamespace(
        mission_id="mission-1", workflow_generation=0, observed_commit=HEAD
    )
    sink.mission_started("mission-1", occurred_at=NOW)
    sink.mission_resumed(mission, occurred_at=NOW)
    sink.human_approved(
        Evidence(
            "EV-HUMAN-1",
            EvidenceType.HUMAN_APPROVAL,
            "US-0001",
            True,
            "Human",
            None,
            None,
            None,
            HEAD,
            NOW,
            "Human/Alice",
        ),
        occurred_at=NOW,
    )
    dossier = SimpleNamespace(
        mission_id="mission-1",
        workflow_generation=0,
        user_story_id="US-0001",
        integration_commit=HEAD,
    )
    sink.role_execution(
        dossier,
        MissionRole.TESTER,
        MissionRoleLaunchResult(True),
        occurred_at=NOW,
    )
    gate = SimpleNamespace(
        mission_id="mission-1",
        workflow_generation=0,
        wave_index=0,
        group_index=0,
        baseline_commit=HEAD,
        result=IntegrationGateClassification.PASS,
        findings=(),
    )
    merge = SimpleNamespace(
        mission_id="mission-1",
        workflow_generation=0,
        wave_index=0,
        group_index=0,
        result=SimpleNamespace(value="MERGED"),
        integration_commit=HEAD,
        primary_after=HEAD,
        findings=(),
    )
    attempt = SimpleNamespace(gate_result=gate, merge_result=merge)
    sink.integration(
        MissionIntegrationResult(
            MissionIntegrationStatus.READY_FOR_TESTER,
            "mission-1",
            0,
            ("US-0001",),
            HEAD,
            (),
            MissionRole.TESTER,
            remediation_attempt=attempt,  # type: ignore[arg-type]
        ),
        occurred_at=NOW,
    )
    sink.remediation(
        SimpleNamespace(
            mission_id="mission-1",
            new_generation=1,
            baseline_commit=HEAD,
            triggering_stage=SimpleNamespace(value="TESTER"),
        ),
        occurred_at=NOW,
    )
    terminal = MissionRunResult(
        "mission-1",
        MissionRunStatus.COMPLETED,
        MissionPhase.REPORT,
        0,
        (),
        ("US-0001",),
        (),
        "complete",
        HEAD,
    )
    sink.record(terminal, occurred_at=NOW)
    sink.record(terminal, occurred_at=NOW)

    events = OperationalEventStore(tmp_path).read()
    families = {item.event_type for item in events}
    assert {
        OperationalEventType.MISSION_LIFECYCLE,
        OperationalEventType.ROLE_EXECUTION,
        OperationalEventType.INTEGRATION_GATE,
        OperationalEventType.MERGE_OPERATION,
        OperationalEventType.CONTROL_PLANE_DECISION,
        OperationalEventType.HUMAN_WAITING,
        OperationalEventType.REMEDIATION_RECOVERY,
    }.issubset(families)
    assert len({item.event_id for item in events}) == len(events)


def test_execution_event_projection_uses_each_ledger_repository_root(
    tmp_path: Path, monkeypatch
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    assignment = SimpleNamespace(worktree_path=str(worktree))
    record = OrchestrationRecord(
        ORCHESTRATION_RECORD_VERSION,
        "mission-1",
        MissionRequest("Observe worktree execution", str(tmp_path)),
        request_fingerprint(MissionRequest("Observe worktree execution", str(tmp_path))),
        HEAD,
        0,
    )
    sink = _OperationalMissionEventSink(
        tmp_path,
        "project",
        _Store(SimpleNamespace(records=())),  # type: ignore[arg-type]
        _Store(ProjectState("1.0", project_id="project")),  # type: ignore[arg-type]
        _Store(record),  # type: ignore[arg-type]
        _Worktrees((assignment,)),  # type: ignore[arg-type]
    )
    observed_roots: list[Path] = []

    monkeypatch.setattr(
        "agentic_engineering_os.application.mission_composition.ExecutionStateStore",
        lambda path: _Store(SimpleNamespace(records=())),
    )

    def project(ledger, *, project_id, repository_root):
        del ledger, project_id
        observed_roots.append(Path(repository_root).resolve())
        return ()

    monkeypatch.setattr(
        "agentic_engineering_os.application.mission_composition.project_terminal_execution_events",
        project,
    )

    sink._project_execution_events()

    assert observed_roots == [tmp_path.resolve(), worktree.resolve()]
