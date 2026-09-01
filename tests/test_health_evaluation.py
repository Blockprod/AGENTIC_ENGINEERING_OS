from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    HealthEvaluationEngine,
    HealthEvaluationError,
    MetricsEngine,
)
from agentic_engineering_os.domain import (
    Certification,
    DimensionRequirement,
    Gate,
    HealthCondition,
    HealthDimension,
    HealthEvaluationContext,
    HealthFreshness,
    HealthObservation,
    HealthReasonCode,
    HealthScope,
    HealthSnapshot,
    HealthSource,
    HealthState,
    MetricName,
    MetricsHealthInput,
    MetricsScope,
    MissionRole,
    OperationalCorrelation,
    OperationalEvent,
    OperationalEventPayload,
    OperationalEventType,
    OperationalProvenance,
    OperationalProvenanceKind,
    OperationalSeverity,
)
from agentic_engineering_os.infrastructure import OperationalEventStore


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
HEAD = "a" * 40
PROJECT = "project-one"
MISSION = "mission-1"
GENERATION = 2

_MISSION_SOURCES = {
    HealthSource.MISSION_STATE_STORE,
    HealthSource.GIT_RECONCILIATION,
    HealthSource.CODEX_RUNTIME,
    HealthSource.EXECUTION_LEDGER,
    HealthSource.REMEDIATION_STORE,
}


def _observation(
    source: HealthSource,
    condition: HealthCondition,
    *,
    project_id: str = PROJECT,
    observed_at: datetime = NOW,
    head: str = HEAD,
    mission_id: str | None = None,
    generation: int | None = None,
    bind_mission: bool = True,
) -> HealthObservation:
    if source in _MISSION_SOURCES and mission_id is None and bind_mission:
        mission_id = MISSION
        generation = GENERATION
    return HealthObservation(
        source=source,
        condition=condition,
        project_id=project_id,
        observed_at=observed_at,
        source_identity=f"{source.value.lower()}:v1",
        repository_head=head,
        mission_id=mission_id,
        workflow_generation=generation,
    )


def _healthy_observations(*, active: bool, parallel: bool = False) -> tuple[HealthObservation, ...]:
    observations = [
        _observation(HealthSource.PROJECT_STATE_STORE, HealthCondition.AVAILABLE),
        _observation(HealthSource.OPERATIONAL_EVENT_STORE, HealthCondition.AVAILABLE),
        _observation(HealthSource.PERSISTENCE_DIAGNOSTIC, HealthCondition.AVAILABLE),
        _observation(HealthSource.PROJECT_CONFIGURATION, HealthCondition.VALID),
        _observation(
            HealthSource.CODEX_RUNTIME,
            HealthCondition.AVAILABLE,
            bind_mission=active,
        ),
    ]
    if active:
        observations.extend(
            (
                _observation(HealthSource.MISSION_STATE_STORE, HealthCondition.AVAILABLE),
                _observation(HealthSource.EXECUTION_LEDGER, HealthCondition.CLEAR),
                _observation(HealthSource.REMEDIATION_STORE, HealthCondition.CLEAR),
            )
        )
    if parallel:
        observations.append(
            _observation(HealthSource.GIT_RECONCILIATION, HealthCondition.RECONCILED)
        )
    return tuple(observations)


def _scope(*, active: bool) -> HealthScope:
    return HealthScope(
        PROJECT,
        HEAD,
        MISSION if active else None,
        GENERATION if active else None,
    )


def _metrics(*, active: bool, events: tuple[OperationalEvent, ...] = (), complete: bool = True) -> MetricsHealthInput:
    scope = MetricsScope(
        PROJECT,
        mission_id=MISSION if active else None,
        workflow_generation=GENERATION if active else None,
    )
    snapshot = MetricsEngine().compute(events, scope, source_complete=complete)
    return MetricsHealthInput(snapshot, NOW, HEAD)


def _context(
    *,
    active: bool,
    parallel: bool = False,
    observations: tuple[HealthObservation, ...] | None = None,
    metrics: MetricsHealthInput | None | object = ...,
) -> HealthEvaluationContext:
    selected_metrics = _metrics(active=active) if metrics is ... else metrics
    return HealthEvaluationContext(
        scope=_scope(active=active),
        evaluated_at=NOW,
        mission_active=active,
        parallel_execution_active=parallel,
        observations=(
            _healthy_observations(active=active, parallel=parallel)
            if observations is None
            else observations
        ),
        metrics=selected_metrics,  # type: ignore[arg-type]
    )


def _dimension(snapshot: HealthSnapshot, dimension: HealthDimension):
    return next(item for item in snapshot.dimensions if item.dimension is dimension)


def _replace_source(
    observations: tuple[HealthObservation, ...],
    source: HealthSource,
    replacement: HealthObservation,
) -> tuple[HealthObservation, ...]:
    return tuple(replacement if item.source is source else item for item in observations)


def _event(
    index: int,
    event_type: OperationalEventType,
    operation: str,
    *,
    reason_code: str | None = None,
) -> OperationalEvent:
    correlation = OperationalCorrelation(
        mission_id=MISSION,
        workflow_generation=GENERATION,
        role=MissionRole.IMPLEMENTER if event_type is OperationalEventType.CODEX_EXECUTION else None,
        execution_id="exec-1" if event_type is OperationalEventType.CODEX_EXECUTION else None,
    )
    return OperationalEvent(
        schema_version="1.0",
        event_id=f"00000000-0000-4000-8000-{index:012d}",
        event_type=event_type,
        occurred_at=NOW - timedelta(seconds=10 - index),
        severity=OperationalSeverity.ERROR,
        source_component="HealthFixture",
        project_id=PROJECT,
        correlation=correlation,
        payload=OperationalEventPayload(operation=operation, reason_code=reason_code),
        provenance=OperationalProvenance(
            OperationalProvenanceKind.DETERMINISTIC_COMPONENT, "HealthFixture"
        ),
    )


def test_fully_healthy_idle_repository() -> None:
    snapshot = HealthEvaluationEngine().evaluate(_context(active=False))
    assert snapshot.global_state is HealthState.HEALTHY
    assert snapshot.reasons == (HealthReasonCode.ALL_REQUIRED_DIMENSIONS_HEALTHY,)
    assert len(snapshot.dimensions) == len(HealthDimension)


def test_healthy_active_mission() -> None:
    snapshot = HealthEvaluationEngine().evaluate(_context(active=True))
    assert snapshot.global_state is HealthState.HEALTHY
    assert _dimension(snapshot, HealthDimension.CODEX_RUNTIME).requirement is DimensionRequirement.REQUIRED
    assert _dimension(snapshot, HealthDimension.AUTHORITATIVE_STATE_ACCESS).state is HealthState.HEALTHY


def test_degraded_observability_is_explicit() -> None:
    observations = _replace_source(
        _healthy_observations(active=False),
        HealthSource.OPERATIONAL_EVENT_STORE,
        _observation(HealthSource.OPERATIONAL_EVENT_STORE, HealthCondition.DEGRADED),
    )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=False, observations=observations)
    )
    assert snapshot.global_state is HealthState.DEGRADED
    assert _dimension(snapshot, HealthDimension.OBSERVABILITY).state is HealthState.DEGRADED


def test_blocked_persistence_wins_over_complete_zero_metrics() -> None:
    observations = _replace_source(
        _healthy_observations(active=False),
        HealthSource.PERSISTENCE_DIAGNOSTIC,
        _observation(HealthSource.PERSISTENCE_DIAGNOSTIC, HealthCondition.FAILED),
    )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=False, observations=observations)
    )
    assert snapshot.global_state is HealthState.BLOCKED
    assert _dimension(snapshot, HealthDimension.PERSISTENCE).state is HealthState.BLOCKED


def test_unknown_codex_runtime_blocks_healthy_for_active_mission() -> None:
    observations = _replace_source(
        _healthy_observations(active=True),
        HealthSource.CODEX_RUNTIME,
        _observation(HealthSource.CODEX_RUNTIME, HealthCondition.UNKNOWN),
    )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=True, observations=observations)
    )
    assert snapshot.global_state is HealthState.UNKNOWN


def test_contextual_not_applicable_dimensions_claim_no_success() -> None:
    snapshot = HealthEvaluationEngine().evaluate(_context(active=False))
    for dimension in (
        HealthDimension.GIT_WORKTREES,
        HealthDimension.EXECUTION_RECOVERY,
        HealthDimension.REMEDIATION_TRANSACTION,
    ):
        result = _dimension(snapshot, dimension)
        assert result.requirement is DimensionRequirement.NOT_APPLICABLE
        assert result.state is None
        assert result.freshness is HealthFreshness.NOT_APPLICABLE
        assert result.sources == ()


def test_repeated_evaluation_is_deterministic() -> None:
    context = _context(active=True)
    first = HealthEvaluationEngine().evaluate(context)
    second = HealthEvaluationEngine().evaluate(context)
    assert first == second
    assert first.fingerprint == second.fingerprint


def test_observation_input_order_does_not_change_snapshot() -> None:
    context = _context(active=True, parallel=True)
    reordered = replace(context, observations=tuple(reversed(context.observations)))
    assert HealthEvaluationEngine().evaluate(context) == HealthEvaluationEngine().evaluate(
        reordered
    )


def test_corrupted_event_store_and_unavailable_metrics_never_look_healthy(
    tmp_path: Path,
) -> None:
    segment = tmp_path / ".agentic-engineering-os/operational-events/segment-000001.jsonl"
    segment.parent.mkdir(parents=True)
    segment.write_bytes(b"{broken}\n")
    metrics_snapshot = MetricsEngine().compute_from_store(
        OperationalEventStore(tmp_path), MetricsScope(PROJECT)
    )
    observations = _replace_source(
        _healthy_observations(active=False),
        HealthSource.OPERATIONAL_EVENT_STORE,
        _observation(HealthSource.OPERATIONAL_EVENT_STORE, HealthCondition.CORRUPTED),
    )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(
            active=False,
            observations=observations,
            metrics=MetricsHealthInput(metrics_snapshot, NOW, HEAD),
        )
    )
    assert snapshot.global_state is HealthState.UNKNOWN
    assert HealthReasonCode.METRICS_UNAVAILABLE in _dimension(
        snapshot, HealthDimension.OBSERVABILITY
    ).reasons


def test_saturated_event_store_wins_over_apparently_complete_metrics() -> None:
    observations = _replace_source(
        _healthy_observations(active=False),
        HealthSource.OPERATIONAL_EVENT_STORE,
        _observation(HealthSource.OPERATIONAL_EVENT_STORE, HealthCondition.SATURATED),
    )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=False, observations=observations)
    )
    assert snapshot.global_state is HealthState.UNKNOWN
    assert _dimension(snapshot, HealthDimension.OBSERVABILITY).state is HealthState.UNKNOWN


def test_stale_metrics_make_observability_unknown() -> None:
    metrics = replace(_metrics(active=False), observed_at=NOW - timedelta(minutes=6))
    snapshot = HealthEvaluationEngine().evaluate(_context(active=False, metrics=metrics))
    assert snapshot.global_state is HealthState.UNKNOWN
    assert _dimension(snapshot, HealthDimension.OBSERVABILITY).freshness is HealthFreshness.STALE
    assert HealthReasonCode.METRICS_STALE in _dimension(
        snapshot, HealthDimension.OBSERVABILITY
    ).reasons


def test_stale_generation_is_unknown() -> None:
    observations = _replace_source(
        _healthy_observations(active=True),
        HealthSource.CODEX_RUNTIME,
        _observation(
            HealthSource.CODEX_RUNTIME,
            HealthCondition.AVAILABLE,
            mission_id=MISSION,
            generation=GENERATION - 1,
        ),
    )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=True, observations=observations)
    )
    assert snapshot.global_state is HealthState.UNKNOWN
    assert HealthReasonCode.STALE_GENERATION in _dimension(
        snapshot, HealthDimension.CODEX_RUNTIME
    ).reasons


@pytest.mark.parametrize("wrong_sources", [(HealthSource.PROJECT_STATE_STORE,), (HealthSource.PROJECT_STATE_STORE, HealthSource.PERSISTENCE_DIAGNOSTIC)])
def test_wrong_or_mixed_project_inputs_never_yield_healthy(
    wrong_sources: tuple[HealthSource, ...],
) -> None:
    observations = _healthy_observations(active=False)
    for source in wrong_sources:
        original = next(item for item in observations if item.source is source)
        observations = _replace_source(
            observations, source, replace(original, project_id="project-two")
        )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=False, observations=observations)
    )
    assert snapshot.global_state is not HealthState.HEALTHY


def test_foreign_observation_for_not_applicable_dimension_still_contaminates_input() -> None:
    observations = _healthy_observations(active=False) + (
        _observation(
            HealthSource.GIT_RECONCILIATION,
            HealthCondition.RECONCILED,
            project_id="project-two",
        ),
    )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=False, observations=observations)
    )
    assert snapshot.global_state is HealthState.UNKNOWN
    assert HealthReasonCode.WRONG_PROJECT in _dimension(
        snapshot, HealthDimension.OBSERVABILITY
    ).reasons


def test_inaccessible_project_state_is_blocked() -> None:
    observations = _replace_source(
        _healthy_observations(active=False),
        HealthSource.PROJECT_STATE_STORE,
        _observation(HealthSource.PROJECT_STATE_STORE, HealthCondition.UNAVAILABLE),
    )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=False, observations=observations)
    )
    assert snapshot.global_state is HealthState.BLOCKED


def test_git_worktree_drift_blocks_parallel_execution() -> None:
    observations = _replace_source(
        _healthy_observations(active=True, parallel=True),
        HealthSource.GIT_RECONCILIATION,
        _observation(HealthSource.GIT_RECONCILIATION, HealthCondition.DRIFT),
    )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=True, parallel=True, observations=observations)
    )
    assert snapshot.global_state is HealthState.BLOCKED


def test_pending_recovery_blocks_active_mission_health() -> None:
    observations = _replace_source(
        _healthy_observations(active=True),
        HealthSource.EXECUTION_LEDGER,
        _observation(HealthSource.EXECUTION_LEDGER, HealthCondition.RECOVERY_PENDING),
    )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=True, observations=observations)
    )
    assert snapshot.global_state is HealthState.BLOCKED


def test_pending_remediation_blocks_active_mission_health() -> None:
    observations = _replace_source(
        _healthy_observations(active=True),
        HealthSource.REMEDIATION_STORE,
        _observation(HealthSource.REMEDIATION_STORE, HealthCondition.PENDING),
    )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=True, observations=observations)
    )
    assert snapshot.global_state is HealthState.BLOCKED


def test_blocked_precedes_unknown_without_discarding_diagnostics() -> None:
    observations = _replace_source(
        _healthy_observations(active=True),
        HealthSource.PERSISTENCE_DIAGNOSTIC,
        _observation(HealthSource.PERSISTENCE_DIAGNOSTIC, HealthCondition.FAILED),
    )
    observations = _replace_source(
        observations,
        HealthSource.CODEX_RUNTIME,
        _observation(HealthSource.CODEX_RUNTIME, HealthCondition.UNKNOWN),
    )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=True, observations=observations)
    )
    assert snapshot.global_state is HealthState.BLOCKED
    assert _dimension(snapshot, HealthDimension.CODEX_RUNTIME).state is HealthState.UNKNOWN
    assert _dimension(snapshot, HealthDimension.PERSISTENCE).state is HealthState.BLOCKED


def test_incomplete_metrics_never_yield_healthy() -> None:
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=False, metrics=_metrics(active=False, complete=False))
    )
    assert snapshot.global_state is HealthState.UNKNOWN
    observability = _dimension(snapshot, HealthDimension.OBSERVABILITY)
    assert HealthReasonCode.METRICS_INCOMPLETE in observability.reasons
    assert observability.freshness is HealthFreshness.FRESH


def test_missing_required_dimension_never_yields_healthy() -> None:
    observations = tuple(
        item
        for item in _healthy_observations(active=False)
        if item.source is not HealthSource.PROJECT_STATE_STORE
    )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=False, observations=observations)
    )
    assert snapshot.global_state is HealthState.UNKNOWN
    assert HealthReasonCode.MISSING_REQUIRED_OBSERVATION in _dimension(
        snapshot, HealthDimension.AUTHORITATIVE_STATE_ACCESS
    ).reasons


def test_missing_metrics_never_yield_healthy() -> None:
    snapshot = HealthEvaluationEngine().evaluate(_context(active=False, metrics=None))
    assert snapshot.global_state is HealthState.UNKNOWN


def test_stale_git_head_is_unknown_not_reconciled() -> None:
    observations = _replace_source(
        _healthy_observations(active=True, parallel=True),
        HealthSource.GIT_RECONCILIATION,
        _observation(
            HealthSource.GIT_RECONCILIATION,
            HealthCondition.RECONCILED,
            head="b" * 40,
        ),
    )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=True, parallel=True, observations=observations)
    )
    assert snapshot.global_state is HealthState.UNKNOWN
    assert HealthReasonCode.STALE_REPOSITORY_HEAD in _dimension(
        snapshot, HealthDimension.GIT_WORKTREES
    ).reasons


def test_store_diagnostic_from_an_old_head_is_unknown() -> None:
    observations = _replace_source(
        _healthy_observations(active=False),
        HealthSource.OPERATIONAL_EVENT_STORE,
        _observation(
            HealthSource.OPERATIONAL_EVENT_STORE,
            HealthCondition.AVAILABLE,
            head="b" * 40,
        ),
    )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=False, observations=observations)
    )
    assert snapshot.global_state is HealthState.UNKNOWN
    assert HealthReasonCode.STALE_REPOSITORY_HEAD in _dimension(
        snapshot, HealthDimension.OBSERVABILITY
    ).reasons


def test_future_observation_is_unknown() -> None:
    observations = _replace_source(
        _healthy_observations(active=False),
        HealthSource.PROJECT_STATE_STORE,
        _observation(
            HealthSource.PROJECT_STATE_STORE,
            HealthCondition.AVAILABLE,
            observed_at=NOW + timedelta(seconds=1),
        ),
    )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=False, observations=observations)
    )
    assert snapshot.global_state is HealthState.UNKNOWN


def test_metrics_wrong_project_or_generation_are_unknown() -> None:
    wrong_project_snapshot = MetricsEngine().compute(
        (), MetricsScope("project-two"), source_complete=True
    )
    wrong_project = MetricsHealthInput(wrong_project_snapshot, NOW, HEAD)
    first = HealthEvaluationEngine().evaluate(
        _context(active=False, metrics=wrong_project)
    )
    wrong_generation_snapshot = MetricsEngine().compute(
        (),
        MetricsScope(PROJECT, mission_id=MISSION, workflow_generation=GENERATION - 1),
        source_complete=True,
    )
    second = HealthEvaluationEngine().evaluate(
        _context(
            active=True,
            metrics=MetricsHealthInput(wrong_generation_snapshot, NOW, HEAD),
        )
    )
    assert first.global_state is HealthState.UNKNOWN
    assert second.global_state is HealthState.UNKNOWN


def test_duplicate_source_observation_is_unknown() -> None:
    observations = _healthy_observations(active=False) + (
        _observation(HealthSource.PROJECT_STATE_STORE, HealthCondition.AVAILABLE),
    )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=False, observations=observations)
    )
    assert snapshot.global_state is HealthState.UNKNOWN


def test_current_generation_persistence_failure_metric_degrades_health() -> None:
    event = _event(1, OperationalEventType.PERSISTENCE_FAILURE, "READ_FAILED")
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=True, metrics=_metrics(active=True, events=(event,)))
    )
    assert snapshot.global_state is HealthState.DEGRADED
    assert HealthReasonCode.PERSISTENCE_FAILURES_OBSERVED in _dimension(
        snapshot, HealthDimension.PERSISTENCE
    ).reasons


def test_current_generation_codex_failure_metric_degrades_health() -> None:
    events = (
        _event(1, OperationalEventType.CODEX_EXECUTION, "STARTED"),
        _event(
            2,
            OperationalEventType.CODEX_EXECUTION,
            "INTERRUPTED",
            reason_code="TIMEOUT",
        ),
    )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=True, metrics=_metrics(active=True, events=events))
    )
    assert snapshot.global_state is HealthState.DEGRADED
    assert HealthReasonCode.CODEX_FAILURES_OBSERVED in _dimension(
        snapshot, HealthDimension.CODEX_RUNTIME
    ).reasons


def test_optional_codex_failure_degrades_idle_repository() -> None:
    observations = _replace_source(
        _healthy_observations(active=False),
        HealthSource.CODEX_RUNTIME,
        _observation(
            HealthSource.CODEX_RUNTIME,
            HealthCondition.UNAVAILABLE,
            bind_mission=False,
        ),
    )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=False, observations=observations)
    )
    assert snapshot.global_state is HealthState.DEGRADED
    assert snapshot.reasons == (HealthReasonCode.OPTIONAL_DIMENSION_IMPAIRED,)


def test_missing_optional_codex_fact_does_not_become_critical() -> None:
    observations = tuple(
        item
        for item in _healthy_observations(active=False)
        if item.source is not HealthSource.CODEX_RUNTIME
    )
    snapshot = HealthEvaluationEngine().evaluate(
        _context(active=False, observations=observations)
    )
    assert snapshot.global_state is HealthState.HEALTHY
    assert _dimension(snapshot, HealthDimension.CODEX_RUNTIME).state is HealthState.UNKNOWN


def test_forged_healthy_snapshot_is_rejected_by_model() -> None:
    blocked_observations = _replace_source(
        _healthy_observations(active=False),
        HealthSource.PERSISTENCE_DIAGNOSTIC,
        _observation(HealthSource.PERSISTENCE_DIAGNOSTIC, HealthCondition.FAILED),
    )
    blocked = HealthEvaluationEngine().evaluate(
        _context(active=False, observations=blocked_observations)
    )
    with pytest.raises(ValueError, match="contradicts"):
        replace(
            blocked,
            global_state=HealthState.HEALTHY,
            reasons=(HealthReasonCode.ALL_REQUIRED_DIMENSIONS_HEALTHY,),
        )


def test_health_snapshot_is_immutable_and_has_no_authority_api() -> None:
    snapshot = HealthEvaluationEngine().evaluate(_context(active=False))
    with pytest.raises(FrozenInstanceError):
        snapshot.global_state = HealthState.BLOCKED  # type: ignore[misc]
    assert not isinstance(snapshot, (Gate, Certification))
    for forbidden in (
        "to_gate",
        "to_certification",
        "to_evidence",
        "authorize",
        "approve",
        "save",
        "block_story",
    ):
        assert not hasattr(snapshot, forbidden)


def test_wrong_api_input_and_invalid_source_condition_fail_closed() -> None:
    with pytest.raises(HealthEvaluationError):
        HealthEvaluationEngine().evaluate(HealthState.HEALTHY)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not valid"):
        _observation(HealthSource.PROJECT_CONFIGURATION, HealthCondition.AVAILABLE)


def test_context_and_observations_are_immutable_and_bounded() -> None:
    context = _context(active=False)
    with pytest.raises(FrozenInstanceError):
        context.mission_active = True  # type: ignore[misc]
    with pytest.raises(ValueError, match="exceeds policy"):
        replace(context, observations=context.observations * 4)
    with pytest.raises(ValueError, match="source_identity"):
        replace(context.observations[0], source_identity="token:synthetic")


def test_every_applicable_dimension_explains_state_freshness_scope_and_sources() -> None:
    snapshot = HealthEvaluationEngine().evaluate(_context(active=True, parallel=True))
    for result in snapshot.dimensions:
        assert result.reasons
        assert result.scope == snapshot.scope
        if result.requirement is not DimensionRequirement.NOT_APPLICABLE:
            assert result.state is not None
            assert result.freshness is HealthFreshness.FRESH
            assert result.sources
    assert snapshot.source_identities
    assert len(snapshot.fingerprint) == 64


def test_closed_catalogs_refuse_arbitrary_values() -> None:
    with pytest.raises(ValueError):
        HealthState("CERTIFIED")
    with pytest.raises(ValueError):
        HealthDimension("CALLER_CONTROLLED")
    assert _dimension(
        HealthEvaluationEngine().evaluate(_context(active=True)),
        HealthDimension.PERSISTENCE,
    ).sources[0].source is HealthSource.PERSISTENCE_DIAGNOSTIC
