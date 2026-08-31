from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_engineering_os.application import MetricsComputationError, MetricsEngine
from agentic_engineering_os.domain import (
    METRIC_CATALOG,
    DurationSummary,
    Evidence,
    MetricAvailability,
    MetricName,
    MetricType,
    MetricUnit,
    MetricsDiagnosticCode,
    MetricsScope,
    MetricsSnapshot,
    MetricsSnapshotStatus,
    MissionRole,
    OperationalCorrelation,
    OperationalEvent,
    OperationalEventPayload,
    OperationalEventType,
    OperationalProvenance,
    OperationalProvenanceKind,
    OperationalSeverity,
)
from agentic_engineering_os.infrastructure import (
    OperationalEventStore,
    OperationalEventStoreError,
)


BASE_TIME = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def _event(
    index: int,
    event_type: OperationalEventType,
    operation: str,
    *,
    seconds: int | None = None,
    project_id: str = "project-one",
    correlation: OperationalCorrelation | None = None,
    outcome: str | None = None,
    reason_code: str | None = None,
) -> OperationalEvent:
    return OperationalEvent(
        schema_version="1.0",
        event_id=f"00000000-0000-4000-8000-{index:012d}",
        event_type=event_type,
        occurred_at=BASE_TIME + timedelta(seconds=index if seconds is None else seconds),
        severity=OperationalSeverity.INFO,
        source_component="MetricsFixture",
        project_id=project_id,
        correlation=correlation or OperationalCorrelation(),
        payload=OperationalEventPayload(
            operation=operation,
            outcome=outcome,
            reason_code=reason_code,
        ),
        provenance=OperationalProvenance(
            OperationalProvenanceKind.DETERMINISTIC_COMPONENT,
            "MetricsFixture",
        ),
    )


def _mission(
    *,
    generation: int = 1,
    story: str | None = None,
    role: MissionRole | None = None,
    execution: str | None = None,
    assignment: str | None = None,
    wave: bool = False,
) -> OperationalCorrelation:
    return OperationalCorrelation(
        mission_id="mission-1",
        workflow_generation=generation,
        user_story_id=story,
        role=role,
        execution_id=execution,
        assignment_id=assignment,
        wave_index=0 if wave else None,
        group_index=0 if wave else None,
    )


def _metric(snapshot: MetricsSnapshot, name: MetricName):
    return next(item for item in snapshot.metrics if item.name is name)


def test_closed_catalog_has_stable_types_units_and_derivations() -> None:
    assert tuple(METRIC_CATALOG) == tuple(MetricName)
    assert all(definition.derivation for definition in METRIC_CATALOG.values())
    assert {item.metric_type for item in METRIC_CATALOG.values()} == {
        MetricType.COUNTER,
        MetricType.GAUGE,
        MetricType.DURATION_SUMMARY,
        MetricType.DERIVED_METRIC,
    }
    assert {item.unit for item in METRIC_CATALOG.values()} == {
        MetricUnit.COUNT,
        MetricUnit.RATIO,
        MetricUnit.MICROSECONDS,
    }
    with pytest.raises(ValueError):
        MetricName("caller.controlled.metric")


def test_counters_are_exact_for_complete_fixed_corpus() -> None:
    events = (
        _event(1, OperationalEventType.MISSION_LIFECYCLE, "STARTED", correlation=_mission()),
        _event(2, OperationalEventType.MISSION_LIFECYCLE, "BLOCKED", correlation=_mission()),
        _event(3, OperationalEventType.MISSION_LIFECYCLE, "FINISHED", correlation=_mission()),
        _event(4, OperationalEventType.PERSISTENCE_FAILURE, "WRITE_FAILED"),
        _event(5, OperationalEventType.ADOPTION_MIGRATION, "REFUSED"),
    )
    snapshot = MetricsEngine().compute(
        events, MetricsScope("project-one"), source_complete=True
    )

    assert snapshot.status is MetricsSnapshotStatus.COMPLETE
    assert _metric(snapshot, MetricName.MISSIONS_STARTED).value == 1
    assert _metric(snapshot, MetricName.MISSIONS_COMPLETED).value == 1
    assert _metric(snapshot, MetricName.MISSIONS_BLOCKED).value == 1
    assert _metric(snapshot, MetricName.PERSISTENCE_FAILURES).value == 1
    assert _metric(snapshot, MetricName.ADOPTIONS_REFUSED).value == 1
    assert _metric(snapshot, MetricName.MERGES_FAILED).value == 0


def test_codex_timeout_recovery_merge_and_gate_counters() -> None:
    codex = _mission(role=MissionRole.IMPLEMENTER, execution="exec-1")
    events = (
        _event(1, OperationalEventType.CODEX_EXECUTION, "INTERRUPTED", correlation=codex, reason_code="TIMEOUT"),
        _event(2, OperationalEventType.CODEX_EXECUTION, "RECOVERY_INSPECTED", correlation=codex),
        _event(3, OperationalEventType.REMEDIATION_RECOVERY, "REQUESTED", correlation=_mission()),
        _event(4, OperationalEventType.REMEDIATION_RECOVERY, "RECOVERY_REQUIRED", correlation=_mission()),
        _event(5, OperationalEventType.INTEGRATION_GATE, "EVALUATED", correlation=_mission(wave=True), outcome="PASS"),
        _event(6, OperationalEventType.INTEGRATION_GATE, "EVALUATED", correlation=_mission(wave=True), outcome="FAIL"),
        _event(7, OperationalEventType.INTEGRATION_GATE, "EVALUATED", correlation=_mission(wave=True), outcome="UNKNOWN"),
        _event(8, OperationalEventType.MERGE_OPERATION, "FINISHED", correlation=_mission(wave=True)),
    )
    snapshot = MetricsEngine().compute(events, MetricsScope("project-one"), source_complete=True)

    assert _metric(snapshot, MetricName.CODEX_TIMEOUTS).value == 1
    assert _metric(snapshot, MetricName.REMEDIATIONS_REQUESTED).value == 1
    assert _metric(snapshot, MetricName.RECOVERIES_OBSERVED).value == 2
    assert _metric(snapshot, MetricName.INTEGRATION_GATES_PASS).value == 1
    assert _metric(snapshot, MetricName.INTEGRATION_GATES_FAIL).value == 1
    assert _metric(snapshot, MetricName.INTEGRATION_GATES_UNKNOWN).value == 1
    assert _metric(snapshot, MetricName.MERGES_SUCCEEDED).value == 1


def test_role_and_codex_durations_use_exact_correlated_pairs() -> None:
    role = _mission(role=MissionRole.TESTER, execution="role-exec")
    codex = _mission(role=MissionRole.IMPLEMENTER, execution="codex-exec")
    events = (
        _event(1, OperationalEventType.ROLE_EXECUTION, "STARTED", seconds=1, correlation=role),
        _event(2, OperationalEventType.ROLE_EXECUTION, "FINISHED", seconds=4, correlation=role),
        _event(3, OperationalEventType.CODEX_EXECUTION, "STARTED", seconds=5, correlation=codex),
        _event(4, OperationalEventType.CODEX_EXECUTION, "FINISHED", seconds=7, correlation=codex),
    )
    snapshot = MetricsEngine().compute(events, MetricsScope("project-one"), source_complete=True)
    role_duration = _metric(snapshot, MetricName.ROLE_EXECUTION_DURATION)
    codex_duration = _metric(snapshot, MetricName.CODEX_EXECUTION_DURATION)

    assert role_duration.value == DurationSummary(1, 3_000_000, 3_000_000, 3_000_000)
    assert codex_duration.value == DurationSummary(1, 2_000_000, 2_000_000, 2_000_000)
    assert role_duration.source_event_count == 2
    assert role_duration.value.mean_microseconds == 3_000_000


def test_failure_rates_require_observed_terminals() -> None:
    role = _mission(role=MissionRole.TESTER, execution="role-exec")
    events = (
        _event(1, OperationalEventType.ROLE_EXECUTION, "STARTED", correlation=role),
        _event(2, OperationalEventType.ROLE_EXECUTION, "FAILED", correlation=role),
    )
    snapshot = MetricsEngine().compute(events, MetricsScope("project-one"), source_complete=True)
    assert _metric(snapshot, MetricName.ROLE_FAILURE_RATE).value == 1.0
    assert _metric(snapshot, MetricName.CODEX_FAILURE_RATE).availability is MetricAvailability.UNAVAILABLE


def test_active_worktree_gauge_is_derived_from_unclosed_lifecycle() -> None:
    correlation = _mission(story="US-1", assignment="assignment-1")
    snapshot = MetricsEngine().compute(
        (_event(1, OperationalEventType.WORKTREE_LIFECYCLE, "CREATED", correlation=correlation),),
        MetricsScope("project-one"),
        source_complete=True,
    )
    gauge = _metric(snapshot, MetricName.ACTIVE_WORKTREES)
    assert gauge.availability is MetricAvailability.AVAILABLE
    assert gauge.value == 1


def test_human_wait_duration_and_active_gauge_are_separate() -> None:
    correlation = _mission(story="US-1")
    events = (
        _event(1, OperationalEventType.HUMAN_WAITING, "WAITING_STARTED", seconds=1, correlation=correlation),
        _event(2, OperationalEventType.HUMAN_WAITING, "WAITING_FINISHED", seconds=6, correlation=correlation),
    )
    snapshot = MetricsEngine().compute(events, MetricsScope("project-one"), source_complete=True)
    assert _metric(snapshot, MetricName.HUMAN_WAIT_DURATION).value == DurationSummary(
        1, 5_000_000, 5_000_000, 5_000_000
    )
    assert _metric(snapshot, MetricName.ACTIVE_HUMAN_WAITS).value == 0


def test_project_mission_generation_story_role_and_execution_scopes_are_exact() -> None:
    first = _mission(story="US-1", role=MissionRole.IMPLEMENTER, execution="exec-1")
    second = OperationalCorrelation(
        mission_id="mission-2",
        workflow_generation=1,
        user_story_id="US-2",
        role=MissionRole.TESTER,
        execution_id="exec-2",
    )
    events = (
        _event(1, OperationalEventType.CODEX_EXECUTION, "STARTED", correlation=first),
        _event(2, OperationalEventType.CODEX_EXECUTION, "STARTED", correlation=second),
    )
    scope = MetricsScope(
        "project-one",
        mission_id="mission-1",
        workflow_generation=1,
        user_story_id="US-1",
        role=MissionRole.IMPLEMENTER,
        execution_id="exec-1",
    )
    snapshot = MetricsEngine().compute(events, scope, source_complete=True)
    assert snapshot.source_event_count == 1
    assert _metric(snapshot, MetricName.CODEX_EXECUTIONS_STARTED).value == 1


def test_repeated_computation_is_deterministic_independent_of_input_order() -> None:
    events = (
        _event(2, OperationalEventType.PERSISTENCE_FAILURE, "READ_FAILED"),
        _event(1, OperationalEventType.ADOPTION_MIGRATION, "FINISHED"),
    )
    engine = MetricsEngine()
    first = engine.compute(events, MetricsScope("project-one"), source_complete=True)
    second = engine.compute(tuple(reversed(events)), MetricsScope("project-one"), source_complete=True)
    assert first == second
    assert first.source_fingerprint == second.source_fingerprint


def test_unicode_project_binding_round_trips() -> None:
    scope = MetricsScope("projet-équipe")
    event = _event(1, OperationalEventType.PERSISTENCE_FAILURE, "READ_FAILED", project_id="projet-équipe")
    snapshot = MetricsEngine().compute((event,), scope, source_complete=True)
    assert snapshot.scope.project_id == "projet-équipe"
    assert snapshot.status is MetricsSnapshotStatus.COMPLETE


def test_restart_and_store_read_produce_same_snapshot(tmp_path: Path) -> None:
    store = OperationalEventStore(tmp_path)
    store.append(_event(1, OperationalEventType.PERSISTENCE_FAILURE, "READ_FAILED"))
    engine = MetricsEngine()
    first = engine.compute_from_store(store, MetricsScope("project-one"))
    second = engine.compute_from_store(OperationalEventStore(tmp_path), MetricsScope("project-one"))
    assert first == second


def test_cross_project_mix_fails_closed_without_metrics() -> None:
    events = (
        _event(1, OperationalEventType.PERSISTENCE_FAILURE, "READ_FAILED"),
        _event(2, OperationalEventType.PERSISTENCE_FAILURE, "READ_FAILED", project_id="project-two"),
    )
    snapshot = MetricsEngine().compute(events, MetricsScope("project-one"), source_complete=True)
    assert snapshot.status is MetricsSnapshotStatus.INCOMPLETE
    assert snapshot.metrics == ()
    assert snapshot.diagnostics[0].code is MetricsDiagnosticCode.CROSS_PROJECT_EVENT


def test_duplicate_event_id_fails_closed() -> None:
    event = _event(1, OperationalEventType.PERSISTENCE_FAILURE, "READ_FAILED")
    snapshot = MetricsEngine().compute((event, event), MetricsScope("project-one"), source_complete=True)
    assert snapshot.metrics == ()
    assert snapshot.diagnostics[0].code is MetricsDiagnosticCode.DUPLICATE_EVENT_ID


@pytest.mark.parametrize(
    ("events", "code"),
    [
        (
            (
                _event(1, OperationalEventType.ROLE_EXECUTION, "STARTED", seconds=1, correlation=_mission(role=MissionRole.TESTER, execution="exec")),
                _event(2, OperationalEventType.ROLE_EXECUTION, "STARTED", seconds=2, correlation=_mission(role=MissionRole.TESTER, execution="exec")),
                _event(3, OperationalEventType.ROLE_EXECUTION, "FINISHED", seconds=3, correlation=_mission(role=MissionRole.TESTER, execution="exec")),
            ),
            MetricsDiagnosticCode.AMBIGUOUS_LIFECYCLE,
        ),
        (
            (_event(1, OperationalEventType.ROLE_EXECUTION, "FINISHED", correlation=_mission(role=MissionRole.TESTER, execution="exec")),),
            MetricsDiagnosticCode.TERMINAL_WITHOUT_START,
        ),
        (
            (
                _event(1, OperationalEventType.ROLE_EXECUTION, "STARTED", seconds=5, correlation=_mission(role=MissionRole.TESTER, execution="exec")),
                _event(2, OperationalEventType.ROLE_EXECUTION, "FINISHED", seconds=2, correlation=_mission(role=MissionRole.TESTER, execution="exec")),
            ),
            MetricsDiagnosticCode.END_BEFORE_START,
        ),
        (
            (_event(1, OperationalEventType.ROLE_EXECUTION, "STARTED", correlation=_mission(role=MissionRole.TESTER, execution="exec")),),
            MetricsDiagnosticCode.OPEN_LIFECYCLE,
        ),
    ],
)
def test_ambiguous_incomplete_or_inverted_duration_is_unavailable(
    events: tuple[OperationalEvent, ...], code: MetricsDiagnosticCode
) -> None:
    snapshot = MetricsEngine().compute(events, MetricsScope("project-one"), source_complete=True)
    duration = _metric(snapshot, MetricName.ROLE_EXECUTION_DURATION)
    assert snapshot.status is MetricsSnapshotStatus.INCOMPLETE
    assert duration.availability is MetricAvailability.UNAVAILABLE
    assert code in duration.diagnostic_codes


def test_mixed_generation_never_forms_duration_pair() -> None:
    events = (
        _event(1, OperationalEventType.ROLE_EXECUTION, "STARTED", correlation=_mission(generation=1, role=MissionRole.TESTER, execution="exec")),
        _event(2, OperationalEventType.ROLE_EXECUTION, "FINISHED", correlation=_mission(generation=2, role=MissionRole.TESTER, execution="exec")),
    )
    snapshot = MetricsEngine().compute(events, MetricsScope("project-one"), source_complete=True)
    assert _metric(snapshot, MetricName.ROLE_EXECUTION_DURATION).availability is MetricAvailability.UNAVAILABLE
    assert {item.code for item in snapshot.diagnostics} == {
        MetricsDiagnosticCode.OPEN_LIFECYCLE,
        MetricsDiagnosticCode.TERMINAL_WITHOUT_START,
    }


def test_unclassified_gate_outcome_never_becomes_unknown_count() -> None:
    event = _event(
        1,
        OperationalEventType.INTEGRATION_GATE,
        "EVALUATED",
        correlation=_mission(wave=True),
        outcome="CERTIFIED",
    )
    snapshot = MetricsEngine().compute((event,), MetricsScope("project-one"), source_complete=True)
    assert _metric(snapshot, MetricName.INTEGRATION_GATES_UNKNOWN).availability is MetricAvailability.UNAVAILABLE
    assert snapshot.diagnostics[0].code is MetricsDiagnosticCode.UNCLASSIFIED_OUTCOME


def test_incomplete_declared_source_returns_no_fake_zero() -> None:
    snapshot = MetricsEngine().compute((), MetricsScope("project-one"), source_complete=False)
    assert snapshot.status is MetricsSnapshotStatus.INCOMPLETE
    assert snapshot.metrics == ()
    assert snapshot.source_fingerprint is None
    assert snapshot.diagnostics[0].code is MetricsDiagnosticCode.INCOMPLETE_SOURCE


def test_empty_complete_corpus_has_exact_zero_counters_but_no_derived_ratio() -> None:
    snapshot = MetricsEngine().compute((), MetricsScope("project-one"), source_complete=True)
    assert _metric(snapshot, MetricName.MISSIONS_STARTED).value == 0
    assert _metric(snapshot, MetricName.ROLE_FAILURE_RATE).availability is MetricAvailability.UNAVAILABLE


@pytest.mark.parametrize("corruption", [b"{broken}\n", b'{"record_version":"1.0"}'])
def test_corrupted_or_incomplete_store_returns_unavailable_snapshot(
    tmp_path: Path, corruption: bytes
) -> None:
    segment = tmp_path / ".agentic-engineering-os/operational-events/segment-000001.jsonl"
    segment.parent.mkdir(parents=True)
    segment.write_bytes(corruption)
    snapshot = MetricsEngine().compute_from_store(
        OperationalEventStore(tmp_path), MetricsScope("project-one")
    )
    assert snapshot.status is MetricsSnapshotStatus.UNAVAILABLE
    assert snapshot.metrics == ()
    assert snapshot.diagnostics[0].code is MetricsDiagnosticCode.EVENT_SOURCE_UNAVAILABLE


def test_lock_conflicted_store_is_unavailable(tmp_path: Path) -> None:
    lock = tmp_path / ".agentic-engineering-os/operational-events/.writer.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("pid=other\n", encoding="ascii")
    snapshot = MetricsEngine().compute_from_store(
        OperationalEventStore(tmp_path), MetricsScope("project-one")
    )
    assert snapshot.status is MetricsSnapshotStatus.UNAVAILABLE
    assert snapshot.metrics == ()


def test_store_returning_non_events_is_unavailable() -> None:
    class InvalidStore:
        def read(self) -> tuple[object, ...]:
            return (object(),)

    snapshot = MetricsEngine().compute_from_store(  # type: ignore[arg-type]
        InvalidStore(), MetricsScope("project-one")
    )
    assert snapshot.status is MetricsSnapshotStatus.UNAVAILABLE
    assert snapshot.metrics == ()
    assert snapshot.diagnostics[0].code is MetricsDiagnosticCode.EVENT_SOURCE_UNAVAILABLE


def test_saturated_store_returns_incomplete_without_metrics_after_restart(
    tmp_path: Path,
) -> None:
    store = OperationalEventStore(tmp_path, max_segment_bytes=1_024, max_segments=1)
    store.append(_event(1, OperationalEventType.PERSISTENCE_FAILURE, "READ_FAILED"))
    with pytest.raises(OperationalEventStoreError, match="RETENTION_LIMIT_REACHED"):
        store.append(_event(2, OperationalEventType.PERSISTENCE_FAILURE, "WRITE_FAILED"))
    restarted = OperationalEventStore(tmp_path, max_segment_bytes=1_024, max_segments=1)
    snapshot = MetricsEngine().compute_from_store(
        restarted, MetricsScope("project-one")
    )
    assert snapshot.status is MetricsSnapshotStatus.INCOMPLETE
    assert snapshot.metrics == ()
    assert snapshot.diagnostics[0].code is MetricsDiagnosticCode.EVENT_SOURCE_SATURATED


def test_secret_like_scope_and_arbitrary_source_are_refused() -> None:
    with pytest.raises(ValueError):
        MetricsScope("token=synthetic-secret")
    with pytest.raises(MetricsComputationError):
        MetricsEngine().compute("events", MetricsScope("project-one"), source_complete=True)  # type: ignore[arg-type]


def test_high_cardinality_story_dimensions_fail_closed() -> None:
    events = tuple(
        _event(
            index,
            OperationalEventType.OPERATIONAL_ANOMALY,
            "DETECTED",
            correlation=_mission(story=f"US-{index:04d}"),
        )
        for index in range(1, 1_026)
    )
    snapshot = MetricsEngine().compute(events, MetricsScope("project-one"), source_complete=True)
    assert snapshot.metrics == ()
    assert snapshot.diagnostics[0].code is MetricsDiagnosticCode.CARDINALITY_LIMIT_EXCEEDED


def test_snapshot_is_immutable_and_has_no_authority_api() -> None:
    snapshot = MetricsEngine().compute((), MetricsScope("project-one"), source_complete=True)
    with pytest.raises(FrozenInstanceError):
        snapshot.status = MetricsSnapshotStatus.UNAVAILABLE  # type: ignore[misc]
    assert not isinstance(snapshot, Evidence)
    for forbidden in (
        "to_evidence",
        "to_gate",
        "to_certification",
        "certify",
        "authorize_merge",
        "save_project_state",
    ):
        assert not hasattr(snapshot, forbidden)
