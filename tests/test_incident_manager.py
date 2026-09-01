from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    GovernancePolicyEvaluator,
    HealthEvaluationEngine,
    IncidentManagementError,
    IncidentManager,
    MetricsEngine,
    ResourceBudgetEvaluator,
)
from agentic_engineering_os.domain import (
    GovernedOperation,
    GovernanceCondition,
    GovernanceDecision,
    GovernanceEvaluationContext,
    GovernancePolicy,
    GovernancePolicyClass,
    GovernancePolicyDomain,
    GovernancePolicyScope,
    GovernanceRationale,
    GovernanceScope,
    HealthCondition,
    HealthEvaluationContext,
    HealthObservation,
    HealthScope,
    HealthSource,
    IncidentClassification,
    IncidentCorrelation,
    IncidentDiagnostic,
    IncidentDiagnosticCondition,
    IncidentEscalation,
    IncidentEvaluationContext,
    IncidentOperatorAcknowledgement,
    IncidentResolutionObservation,
    IncidentScope,
    IncidentSeverity,
    IncidentState,
    MetricsHealthInput,
    MetricsScope,
    OperationalCorrelation,
    OperationalEvent,
    OperationalEventPayload,
    OperationalEventType,
    OperationalProvenance,
    OperationalProvenanceKind,
    OperationalSeverity,
    ResourceBudget,
    ResourceBudgetApplicability,
    ResourceBudgetDecision,
    ResourceBudgetDomain,
    ResourceBudgetEvaluationContext,
    ResourceBudgetRationale,
    ResourceBudgetScope,
    ResourceBudgetUnit,
    ResourceUsageObservation,
    ResourceUsageSource,
    ResourceUsageStatus,
)
from agentic_engineering_os.infrastructure import IncidentEventJournal, OperationalEventStore


NOW = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)
HEAD = "c" * 40
PROJECT = "project-one"
MISSION = "mission-one"
GENERATION = 4


def _scope() -> IncidentScope:
    return IncidentScope(PROJECT, HEAD, MISSION, GENERATION)


def _metrics() -> MetricsHealthInput:
    snapshot = MetricsEngine().compute(
        (), MetricsScope(PROJECT, MISSION, GENERATION), source_complete=True
    )
    return MetricsHealthInput(snapshot, NOW, HEAD)


def _health(*, source: HealthSource | None = None, condition: HealthCondition | None = None):
    pairs = [
        (HealthSource.PROJECT_STATE_STORE, HealthCondition.AVAILABLE),
        (HealthSource.OPERATIONAL_EVENT_STORE, HealthCondition.AVAILABLE),
        (HealthSource.PERSISTENCE_DIAGNOSTIC, HealthCondition.AVAILABLE),
        (HealthSource.PROJECT_CONFIGURATION, HealthCondition.VALID),
        (HealthSource.CODEX_RUNTIME, HealthCondition.AVAILABLE),
        (HealthSource.MISSION_STATE_STORE, HealthCondition.AVAILABLE),
        (HealthSource.EXECUTION_LEDGER, HealthCondition.CLEAR),
        (HealthSource.REMEDIATION_STORE, HealthCondition.CLEAR),
    ]
    observations = []
    mission_bound = {
        HealthSource.MISSION_STATE_STORE,
        HealthSource.CODEX_RUNTIME,
        HealthSource.EXECUTION_LEDGER,
        HealthSource.REMEDIATION_STORE,
    }
    for item_source, item_condition in pairs:
        if item_source is source:
            item_condition = condition
        assert item_condition is not None
        observations.append(
            HealthObservation(
                item_source,
                item_condition,
                PROJECT,
                NOW,
                f"{item_source.value.lower()}:v1",
                HEAD,
                MISSION if item_source in mission_bound else None,
                GENERATION if item_source in mission_bound else None,
            )
        )
    return HealthEvaluationEngine().evaluate(
        HealthEvaluationContext(
            HealthScope(PROJECT, HEAD, MISSION, GENERATION),
            NOW,
            True,
            False,
            tuple(observations),
            _metrics(),
        )
    )


def _governance(health, *, block: bool = False):
    policy = GovernancePolicy(
        "hard-policy",
        "1.0",
        GovernancePolicyClass.HARD_SAFETY_POLICY,
        GovernancePolicyDomain.EXECUTION_ADMISSION,
        True,
        GovernancePolicyScope(PROJECT, (GovernedOperation.EXECUTION,)),
        GovernanceCondition.ALWAYS if block else GovernanceCondition.HEALTH_UNKNOWN,
        GovernanceDecision.BLOCK if block else GovernanceDecision.REQUIRE_OPERATOR,
        GovernanceRationale.PROTECT_SAFETY_INVARIANTS,
    )
    return GovernancePolicyEvaluator().evaluate(
        GovernanceEvaluationContext(
            GovernanceScope(PROJECT, HEAD, MISSION, GENERATION),
            GovernedOperation.EXECUTION,
            NOW,
            health,
            _metrics(),
            (policy,),
            True,
            True,
        )
    )


def _budgets(root: Path, *, domain=ResourceBudgetDomain.CODEX_CONCURRENCY, limit=4, current=0, requested=1):
    scope = ResourceBudgetScope(PROJECT, HEAD, str(root), MISSION, GENERATION)
    unit = {
        ResourceBudgetDomain.CODEX_CONCURRENCY: ResourceBudgetUnit.EXECUTIONS,
        ResourceBudgetDomain.REMEDIATION_GENERATIONS: ResourceBudgetUnit.GENERATIONS,
    }[domain]
    source = {
        ResourceBudgetDomain.CODEX_CONCURRENCY: ResourceUsageSource.EXECUTION_STATE_STORE,
        ResourceBudgetDomain.REMEDIATION_GENERATIONS: ResourceUsageSource.MISSION_STATE,
    }[domain]
    budget = ResourceBudget(
        "hard-budget", "1.0", domain, scope, limit, unit,
        GovernancePolicyClass.HARD_SAFETY_POLICY, "project-policy",
        ResourceBudgetApplicability.APPLICABLE, ResourceBudgetRationale.SAFETY_CEILING,
    )
    identities = tuple(f"execution-{index}" for index in range(current)) if domain is ResourceBudgetDomain.CODEX_CONCURRENCY else ()
    roots = (str(root),) * current if domain is ResourceBudgetDomain.CODEX_CONCURRENCY else ()
    usage = ResourceUsageObservation(
        domain, unit, ResourceUsageStatus.COMPLETE, source, "factual-usage", scope,
        NOW, current, requested, identities, roots,
    )
    return ResourceBudgetEvaluator().evaluate(
        ResourceBudgetEvaluationContext(scope, GovernedOperation.EXECUTION, NOW, (budget,), (usage,))
    )


def _context(root: Path, *, health=None, governance=None, budgets=None, events=(), diagnostics=(), prior=(), events_complete=True, diagnostics_complete=True):
    health = health or _health()
    governance = governance or _governance(health)
    budgets = budgets or _budgets(root)
    return IncidentEvaluationContext(
        _scope(), NOW, health, governance, budgets, _metrics(), tuple(events),
        events_complete, tuple(diagnostics), diagnostics_complete, tuple(prior),
    )


def _diagnostic(classification: IncidentClassification, condition: IncidentDiagnosticCondition, *, source="diagnostic-one", correlation=None, observed_at=NOW, scope=None):
    return IncidentDiagnostic(
        classification, scope or _scope(), correlation or IncidentCorrelation(MISSION, GENERATION),
        condition, observed_at, source,
    )


def _event(event_type: OperationalEventType, operation: str, *, event_id="00000000-0000-4000-8000-000000000001", occurred_at=NOW, project=PROJECT, generation=GENERATION):
    return OperationalEvent(
        "1.0", event_id, event_type, occurred_at, OperationalSeverity.ERROR,
        "test-observer", project,
        OperationalCorrelation(mission_id=MISSION, workflow_generation=generation, repository_commit=HEAD),
        OperationalEventPayload(operation, reason_code="FACTUAL_FAILURE"),
        OperationalProvenance(OperationalProvenanceKind.DETERMINISTIC_COMPONENT, "test-observer"),
    )


def _single(context: IncidentEvaluationContext):
    result = IncidentManager().evaluate(context)
    assert len(result.records) == 1
    return result.records[0]


def test_persistence_failure_incident_from_health(tmp_path: Path) -> None:
    health = _health(source=HealthSource.PERSISTENCE_DIAGNOSTIC, condition=HealthCondition.FAILED)
    records = IncidentManager().evaluate(
        _context(tmp_path, health=health, governance=_governance(health))
    ).records
    record = next(
        item
        for item in records
        if item.classification is IncidentClassification.PERSISTENCE_FAILURE
    )
    assert record.classification is IncidentClassification.PERSISTENCE_FAILURE
    assert record.severity is IncidentSeverity.CRITICAL
    assert record.escalation is IncidentEscalation.EMERGENCY_BLOCK_RECOMMENDED


def test_repeated_same_condition_is_deduplicated(tmp_path: Path) -> None:
    diagnostic = _diagnostic(IncidentClassification.RECOVERY_STUCK, IncidentDiagnosticCondition.ACTIVE)
    first = _single(_context(tmp_path, diagnostics=(diagnostic,)))
    second = IncidentManager().evaluate(_context(tmp_path, diagnostics=(diagnostic,), prior=(first,)))
    assert second.records == (first,)
    assert second.deduplicated_incident_ids == (first.incident_id,)


def test_recovery_stuck_from_health(tmp_path: Path) -> None:
    health = _health(source=HealthSource.EXECUTION_LEDGER, condition=HealthCondition.RECOVERY_PENDING)
    records = IncidentManager().evaluate(_context(tmp_path, health=health, governance=_governance(health))).records
    assert any(item.classification is IncidentClassification.RECOVERY_STUCK for item in records)


def test_budget_exceeded_creates_resource_exhaustion(tmp_path: Path) -> None:
    record = _single(_context(tmp_path, budgets=_budgets(tmp_path, limit=1, current=1, requested=1)))
    assert record.classification is IncidentClassification.RESOURCE_EXHAUSTION


def test_remediation_generation_exceeded_is_loop(tmp_path: Path) -> None:
    budgets = _budgets(tmp_path, domain=ResourceBudgetDomain.REMEDIATION_GENERATIONS, limit=4, current=4, requested=1)
    record = _single(_context(tmp_path, budgets=budgets))
    assert record.classification is IncidentClassification.REMEDIATION_LOOP


def test_policy_block_incident(tmp_path: Path) -> None:
    health = _health()
    record = _single(_context(tmp_path, health=health, governance=_governance(health, block=True)))
    assert record.classification is IncidentClassification.POLICY_BLOCK


def test_operator_acknowledgement_is_attributable(tmp_path: Path) -> None:
    record = _single(_context(tmp_path, diagnostics=(_diagnostic(IncidentClassification.RECOVERY_STUCK, IncidentDiagnosticCondition.ACTIVE),)))
    acknowledged = IncidentManager().acknowledge(
        record, IncidentOperatorAcknowledgement(record.incident_id, _scope(), NOW + timedelta(seconds=1), "Alice Operator")
    )
    assert acknowledged.state is IncidentState.ESCALATED
    assert acknowledged.acknowledged_by == "Alice Operator"
    assert acknowledged.escalation is IncidentEscalation.OPERATOR_ACTION_REQUIRED


@pytest.mark.parametrize("producer", ["Codex/FakeOperator", "codex/FakeOperator", "CODEX/FakeOperator", "CoDeX/FakeOperator"])
def test_fake_codex_operator_is_refused(producer: str) -> None:
    with pytest.raises(ValueError, match="non-Codex"):
        IncidentOperatorAcknowledgement("inc-" + "a" * 32, _scope(), NOW, producer)


def test_resolved_then_reopened_preserves_logical_identity(tmp_path: Path) -> None:
    correlation = IncidentCorrelation(MISSION, GENERATION)
    active = _diagnostic(IncidentClassification.RECOVERY_STUCK, IncidentDiagnosticCondition.ACTIVE, correlation=correlation)
    first = _single(_context(tmp_path, diagnostics=(active,)))
    normalized = _diagnostic(IncidentClassification.RECOVERY_STUCK, IncidentDiagnosticCondition.NORMALIZED, source="recovery-check", correlation=correlation)
    resolve_context = _context(tmp_path, diagnostics=(normalized,), prior=(first,))
    resolved = IncidentManager().resolve(
        first,
        resolve_context,
        IncidentResolutionObservation(first.incident_id, _scope(), NOW, "recovery-check", "Alice Operator"),
    )
    reopened_context = replace(_context(tmp_path, diagnostics=(active,)), evaluated_at=NOW + timedelta(seconds=1), prior_records=(resolved,))
    reopened = _single(reopened_context)
    assert resolved.state is IncidentState.RESOLVED
    assert reopened.incident_id == first.incident_id
    assert reopened.reopen_count == 1
    assert reopened.occurrence_count == 2


def test_stale_health_snapshot_is_unknown_critical(tmp_path: Path) -> None:
    stale = replace(_health(), evaluated_at=NOW - timedelta(minutes=6))
    records = IncidentManager().evaluate(
        _context(tmp_path, health=stale, governance=None)
    ).records
    assert any(
        item.classification is IncidentClassification.UNKNOWN_CRITICAL_STATE
        for item in records
    )


def test_foreign_project_or_wrong_generation_source_is_unknown(tmp_path: Path) -> None:
    health = _health()
    foreign_scope = HealthScope("project-two", HEAD, MISSION, GENERATION)
    foreign = replace(
        health,
        scope=foreign_scope,
        dimensions=tuple(replace(item, scope=foreign_scope) for item in health.dimensions),
    )
    first = IncidentManager().evaluate(_context(tmp_path, health=foreign)).records
    generation_scope = HealthScope(PROJECT, HEAD, MISSION, GENERATION + 1)
    wrong_generation = replace(
        health,
        scope=generation_scope,
        dimensions=tuple(
            replace(item, scope=generation_scope) for item in health.dimensions
        ),
    )
    second = IncidentManager().evaluate(
        _context(tmp_path, health=wrong_generation)
    ).records
    assert any(item.classification is IncidentClassification.UNKNOWN_CRITICAL_STATE for item in first)
    assert any(item.classification is IncidentClassification.UNKNOWN_CRITICAL_STATE for item in second)


def test_forged_incident_severity_is_refused(tmp_path: Path) -> None:
    record = _single(_context(tmp_path, diagnostics=(_diagnostic(IncidentClassification.RECOVERY_STUCK, IncidentDiagnosticCondition.ACTIVE),)))
    with pytest.raises(ValueError, match="severity"):
        replace(record, severity=IncidentSeverity.CRITICAL)


def test_resolving_wrong_incident_is_refused(tmp_path: Path) -> None:
    record = _single(_context(tmp_path, diagnostics=(_diagnostic(IncidentClassification.RECOVERY_STUCK, IncidentDiagnosticCondition.ACTIVE),)))
    resolution = IncidentResolutionObservation("inc-" + "f" * 32, _scope(), NOW, "recovery-check", "Alice")
    with pytest.raises(IncidentManagementError, match="another incident"):
        IncidentManager().resolve(record, _context(tmp_path), resolution)


def test_close_without_factual_recovery_is_refused(tmp_path: Path) -> None:
    record = _single(_context(tmp_path, diagnostics=(_diagnostic(IncidentClassification.RECOVERY_STUCK, IncidentDiagnosticCondition.ACTIVE),)))
    resolution = IncidentResolutionObservation(record.incident_id, _scope(), NOW, "missing-recovery", "Alice")
    with pytest.raises(IncidentManagementError, match="normalization"):
        IncidentManager().resolve(record, _context(tmp_path), resolution)


def test_resolution_is_blocked_when_a_critical_source_is_unknown(tmp_path: Path) -> None:
    correlation = IncidentCorrelation(MISSION, GENERATION)
    active = _diagnostic(
        IncidentClassification.RECOVERY_STUCK,
        IncidentDiagnosticCondition.ACTIVE,
        correlation=correlation,
    )
    record = _single(_context(tmp_path, diagnostics=(active,)))
    normalized = _diagnostic(
        IncidentClassification.RECOVERY_STUCK,
        IncidentDiagnosticCondition.NORMALIZED,
        source="recovery-check",
        correlation=correlation,
    )
    context = replace(
        _context(tmp_path, diagnostics=(normalized,)), events_complete=False
    )
    resolution = IncidentResolutionObservation(
        record.incident_id, _scope(), NOW, "recovery-check", "Alice"
    )
    with pytest.raises(IncidentManagementError, match="uncertainty"):
        IncidentManager().resolve(record, context, resolution)


def test_duplicate_event_flood_creates_one_unknown_incident(tmp_path: Path) -> None:
    event = _event(OperationalEventType.PERSISTENCE_FAILURE, "WRITE_FAILED")
    result = IncidentManager().evaluate(_context(tmp_path, events=(event, event)))
    identities = [item.incident_id for item in result.records]
    assert len(identities) == len(set(identities))
    assert len(result.records) == 2


def test_missing_critical_source_fails_closed(tmp_path: Path) -> None:
    context = replace(_context(tmp_path), health=None, governance=None, resource_budgets=None)
    record = _single(context)
    assert record.classification is IncidentClassification.UNKNOWN_CRITICAL_STATE


def test_stale_operational_event_is_not_treated_as_current(tmp_path: Path) -> None:
    event = _event(OperationalEventType.PERSISTENCE_FAILURE, "READ_FAILED", occurred_at=NOW - timedelta(minutes=6))
    record = _single(_context(tmp_path, events=(event,)))
    assert record.classification is IncidentClassification.UNKNOWN_CRITICAL_STATE


def test_manager_does_not_resolve_on_disappearance(tmp_path: Path) -> None:
    active = _diagnostic(IncidentClassification.RECOVERY_STUCK, IncidentDiagnosticCondition.ACTIVE)
    record = _single(_context(tmp_path, diagnostics=(active,)))
    assessment = IncidentManager().evaluate(_context(tmp_path, prior=(record,)))
    assert assessment.records == (record,)
    assert record.state is not IncidentState.RESOLVED


def test_manager_is_immutable_non_authoritative_and_executes_no_remediation(tmp_path: Path) -> None:
    assessment = IncidentManager().evaluate(_context(tmp_path))
    with pytest.raises(FrozenInstanceError):
        assessment.evaluated_at = NOW  # type: ignore[misc]
    for forbidden in ("remediate", "recover", "save", "mutate", "to_evidence", "to_gate", "to_certification", "approve", "merge", "schedule"):
        assert not hasattr(assessment, forbidden)


def test_incident_journal_reuses_event_store_and_reconstructs_latest(tmp_path: Path) -> None:
    store = OperationalEventStore(tmp_path)
    journal = IncidentEventJournal(store)
    record = _single(_context(tmp_path, diagnostics=(_diagnostic(IncidentClassification.RECOVERY_STUCK, IncidentDiagnosticCondition.ACTIVE),)))
    receipt = journal.append(record)
    acknowledged = IncidentManager().acknowledge(record, IncidentOperatorAcknowledgement(record.incident_id, _scope(), NOW + timedelta(seconds=1), "Alice"))
    journal.append(acknowledged)
    assert receipt.event_id
    assert journal.latest(_scope()) == (acknowledged,)
    assert len(store.read()) == 2


def test_journal_append_is_explicit_and_duplicate_snapshot_is_refused(tmp_path: Path) -> None:
    journal = IncidentEventJournal(OperationalEventStore(tmp_path))
    record = _single(_context(tmp_path, diagnostics=(_diagnostic(IncidentClassification.RECOVERY_STUCK, IncidentDiagnosticCondition.ACTIVE),)))
    journal.append(record)
    with pytest.raises(RuntimeError, match="DUPLICATE_EVENT_ID"):
        journal.append(record)
