from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    GovernancePolicyEvaluator,
    HealthEvaluationEngine,
    MaintenanceGovernanceError,
    MaintenanceGovernanceService,
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
    IncidentEscalation,
    IncidentReason,
    IncidentRecord,
    IncidentScope,
    IncidentSeverity,
    IncidentState,
    MaintenanceAdmissionDecision,
    MaintenanceEvaluationContext,
    MaintenanceInitializationRequest,
    MaintenanceOperation,
    MaintenanceScope,
    MaintenanceState,
    MaintenanceTransitionReason,
    MaintenanceTransitionRequest,
    MetricsHealthInput,
    MetricsScope,
    RecoveryObservation,
    RecoveryObservationStatus,
    RecoveryRoute,
    ResourceBudget,
    ResourceBudgetApplicability,
    ResourceBudgetDomain,
    ResourceBudgetEvaluationContext,
    ResourceBudgetRationale,
    ResourceBudgetScope,
    ResourceBudgetUnit,
    ResourceUsageObservation,
    ResourceUsageSource,
    ResourceUsageStatus,
    derive_incident_id,
)
from agentic_engineering_os.infrastructure import MaintenanceStateStore, PersistenceError


NOW = datetime(2026, 9, 1, 15, 0, tzinfo=timezone.utc)
HEAD = "d" * 40
PROJECT = "project-one"
MISSION = "mission-one"
GENERATION = 7


def _metrics(at: datetime = NOW) -> MetricsHealthInput:
    snapshot = MetricsEngine().compute((), MetricsScope(PROJECT, MISSION, GENERATION), source_complete=True)
    return MetricsHealthInput(snapshot, at, HEAD)


def _health(*, at: datetime = NOW, blocked: bool = False):
    pairs = [
        (HealthSource.PROJECT_STATE_STORE, HealthCondition.AVAILABLE),
        (HealthSource.OPERATIONAL_EVENT_STORE, HealthCondition.AVAILABLE),
        (HealthSource.PERSISTENCE_DIAGNOSTIC, HealthCondition.FAILED if blocked else HealthCondition.AVAILABLE),
        (HealthSource.PROJECT_CONFIGURATION, HealthCondition.VALID),
        (HealthSource.CODEX_RUNTIME, HealthCondition.AVAILABLE),
        (HealthSource.MISSION_STATE_STORE, HealthCondition.AVAILABLE),
        (HealthSource.EXECUTION_LEDGER, HealthCondition.CLEAR),
        (HealthSource.REMEDIATION_STORE, HealthCondition.CLEAR),
    ]
    bound = {HealthSource.CODEX_RUNTIME, HealthSource.MISSION_STATE_STORE, HealthSource.EXECUTION_LEDGER, HealthSource.REMEDIATION_STORE}
    observations = tuple(
        HealthObservation(source, condition, PROJECT, at, f"{source.value.lower()}:v1", HEAD,
                          MISSION if source in bound else None, GENERATION if source in bound else None)
        for source, condition in pairs
    )
    return HealthEvaluationEngine().evaluate(
        HealthEvaluationContext(HealthScope(PROJECT, HEAD, MISSION, GENERATION), at, True, False, observations, _metrics(at))
    )


def _governance(health, operation: GovernedOperation, *, at: datetime = NOW, decision: GovernanceDecision = GovernanceDecision.ALLOW):
    condition = GovernanceCondition.ALWAYS if decision is not GovernanceDecision.ALLOW else GovernanceCondition.HEALTH_UNKNOWN
    policy = GovernancePolicy(
        "maintenance-policy", "1.0", GovernancePolicyClass.HARD_SAFETY_POLICY,
        GovernancePolicyDomain.EXECUTION_ADMISSION, True,
        GovernancePolicyScope(PROJECT, (operation,)), condition,
        decision if decision is not GovernanceDecision.ALLOW else GovernanceDecision.BLOCK,
        GovernanceRationale.PROTECT_SAFETY_INVARIANTS,
    )
    return GovernancePolicyEvaluator().evaluate(
        GovernanceEvaluationContext(GovernanceScope(PROJECT, HEAD, MISSION, GENERATION), operation, at, health, _metrics(at), (policy,), True, True)
    )


def _budgets(root: Path, operation: GovernedOperation, *, at: datetime = NOW, limit: int = 4, current: int = 0):
    scope = ResourceBudgetScope(PROJECT, HEAD, str(root), MISSION, GENERATION)
    budget = ResourceBudget(
        "execution-budget", "1.0", ResourceBudgetDomain.CODEX_CONCURRENCY, scope,
        limit, ResourceBudgetUnit.EXECUTIONS, GovernancePolicyClass.HARD_SAFETY_POLICY,
        "project-policy", ResourceBudgetApplicability.APPLICABLE,
        ResourceBudgetRationale.SAFETY_CEILING,
    )
    usage = ResourceUsageObservation(
        ResourceBudgetDomain.CODEX_CONCURRENCY, ResourceBudgetUnit.EXECUTIONS,
        ResourceUsageStatus.COMPLETE, ResourceUsageSource.EXECUTION_STATE_STORE,
        "execution-store", scope, at, current, 1,
        tuple(f"execution-{index}" for index in range(current)), tuple(str(root) for _ in range(current)),
    )
    return ResourceBudgetEvaluator().evaluate(ResourceBudgetEvaluationContext(scope, operation, at, (budget,), (usage,)))


def _context(root: Path, operation: GovernedOperation = GovernedOperation.EXECUTION, *, at: datetime = NOW, health=None, budget_limit: int = 4, budget_current: int = 0, incidents=(), recovery=None):
    health = health or _health(at=at)
    return MaintenanceEvaluationContext(
        MaintenanceScope(PROJECT, str(root.resolve())), HEAD, MISSION, GENERATION, at,
        health, _governance(health, operation, at=at),
        _budgets(root, operation, at=at, limit=budget_limit, current=budget_current),
        tuple(incidents), recovery,
    )


def _service(root: Path):
    store = MaintenanceStateStore(root)
    service = MaintenanceGovernanceService(store)
    record = service.initialize(MaintenanceInitializationRequest(MaintenanceScope(PROJECT, str(root.resolve())), HEAD, MISSION, GENERATION, NOW, "Alice/Operator"))
    return store, service, record


def _transition(service, record, root: Path, target: MaintenanceState, *, at=NOW + timedelta(seconds=1), route=None):
    reasons = {
        MaintenanceState.NORMAL: MaintenanceTransitionReason.OPERATOR_RETURN_TO_NORMAL,
        MaintenanceState.DRAINING: MaintenanceTransitionReason.OPERATOR_DRAIN,
        MaintenanceState.MAINTENANCE: MaintenanceTransitionReason.OPERATOR_MAINTENANCE,
        MaintenanceState.RECOVERY_REQUIRED: MaintenanceTransitionReason.RECOVERY_COORDINATION,
        MaintenanceState.FROZEN: MaintenanceTransitionReason.INCIDENT_ESCALATION,
    }
    request = MaintenanceTransitionRequest(
        MaintenanceScope(PROJECT, str(root.resolve())), HEAD, MISSION, GENERATION,
        record.revision, record.fingerprint, target, reasons[target], at,
        "Alice/Operator", route,
    )
    return service.request_transition(_context(root, GovernedOperation.MAINTENANCE, at=at), request)


def _critical() -> IncidentRecord:
    scope = IncidentScope(PROJECT, HEAD, MISSION, GENERATION)
    return IncidentRecord.create(
        incident_id=derive_incident_id(
            PROJECT,
            IncidentClassification.PERSISTENCE_FAILURE,
            IncidentCorrelation(MISSION, GENERATION),
        ),
        revision=1, classification=IncidentClassification.PERSISTENCE_FAILURE,
        severity=IncidentSeverity.CRITICAL, state=IncidentState.ESCALATED,
        escalation=IncidentEscalation.EMERGENCY_BLOCK_RECOMMENDED, scope=scope,
        correlation=IncidentCorrelation(MISSION, GENERATION), opened_at=NOW,
        updated_at=NOW, occurrence_count=1, reopen_count=0,
        reasons=(IncidentReason.HEALTH_CONDITION,), source_identities=("health-engine",),
        acknowledged_by=None, resolution_source_identity=None, previous_fingerprint=None,
    )


def test_normal_admission_and_exact_enforcement(tmp_path: Path) -> None:
    _, service, _ = _service(tmp_path)
    admission = service.evaluate(_context(tmp_path), MaintenanceOperation.START_ROLE_EXECUTION)
    assert admission.decision is MaintenanceAdmissionDecision.ADMITTED
    service.enforce(admission, MaintenanceOperation.START_ROLE_EXECUTION)


def test_enter_maintenance_and_restart_restores_exact_state(tmp_path: Path) -> None:
    _, service, record = _service(tmp_path)
    result = _transition(service, record, tmp_path, MaintenanceState.MAINTENANCE)
    restarted = MaintenanceStateStore(tmp_path).load()
    assert restarted == result.record
    assert restarted.state is MaintenanceState.MAINTENANCE


def test_valid_human_can_exit_maintenance_after_healthy_reevaluation(tmp_path: Path) -> None:
    _, service, record = _service(tmp_path)
    maintenance = _transition(service, record, tmp_path, MaintenanceState.MAINTENANCE).record
    result = _transition(
        service,
        maintenance,
        tmp_path,
        MaintenanceState.NORMAL,
        at=NOW + timedelta(seconds=2),
    )
    assert result.record.state is MaintenanceState.NORMAL


def test_frozen_blocks_execution_but_keeps_diagnostics(tmp_path: Path) -> None:
    _, service, record = _service(tmp_path)
    frozen = _transition(service, record, tmp_path, MaintenanceState.FROZEN).record
    service = MaintenanceGovernanceService(MaintenanceStateStore(tmp_path))
    execution = service.evaluate(_context(tmp_path, at=NOW + timedelta(seconds=2)), MaintenanceOperation.START_ROLE_EXECUTION)
    diagnostic = service.evaluate(_context(tmp_path, at=NOW + timedelta(seconds=2)), MaintenanceOperation.READ_DIAGNOSTICS)
    assert frozen.state is MaintenanceState.FROZEN
    assert execution.decision is MaintenanceAdmissionDecision.REFUSED
    assert diagnostic.decision is MaintenanceAdmissionDecision.ADMITTED


def test_draining_only_admits_safe_completion(tmp_path: Path) -> None:
    _, service, record = _service(tmp_path)
    _transition(service, record, tmp_path, MaintenanceState.DRAINING)
    at = NOW + timedelta(seconds=2)
    assert service.evaluate(_context(tmp_path, GovernedOperation.VERIFICATION, at=at), MaintenanceOperation.COMPLETE_IN_FLIGHT).decision is MaintenanceAdmissionDecision.ADMITTED
    assert service.evaluate(_context(tmp_path, at=at), MaintenanceOperation.START_PARALLEL_GROUP).decision is MaintenanceAdmissionDecision.REFUSED


def test_recovery_route_is_declarative_existing_boundary(tmp_path: Path) -> None:
    _, service, record = _service(tmp_path)
    result = _transition(service, record, tmp_path, MaintenanceState.RECOVERY_REQUIRED, route=RecoveryRoute.P4_EXECUTION_RECOVERY)
    assert result.recovery_request is not None
    assert result.recovery_request.boundary == "RestartSafeCodexExecutionService.inspect_restart"
    assert not hasattr(result.recovery_request, "execute")


def test_successful_recovery_then_human_reevaluation_returns_normal(tmp_path: Path) -> None:
    _, service, record = _service(tmp_path)
    recovery_required = _transition(service, record, tmp_path, MaintenanceState.RECOVERY_REQUIRED, route=RecoveryRoute.P3_PARALLEL_RECOVERY).record
    at = NOW + timedelta(seconds=2)
    observation = RecoveryObservation(RecoveryRoute.P3_PARALLEL_RECOVERY, RecoveryObservationStatus.SUCCEEDED, MaintenanceScope(PROJECT, str(tmp_path.resolve())), HEAD, MISSION, GENERATION, at, "parallel-recovery")
    context = _context(tmp_path, GovernedOperation.MAINTENANCE, at=at, recovery=observation)
    request = MaintenanceTransitionRequest(context.scope, HEAD, MISSION, GENERATION, recovery_required.revision, recovery_required.fingerprint, MaintenanceState.NORMAL, MaintenanceTransitionReason.OPERATOR_RETURN_TO_NORMAL, at, "Alice/Operator")
    assert service.request_transition(context, request).record.state is MaintenanceState.NORMAL


def test_codex_cannot_declare_recovery_succeeded(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="recovery source"):
        RecoveryObservation(
            RecoveryRoute.P3_PARALLEL_RECOVERY,
            RecoveryObservationStatus.SUCCEEDED,
            MaintenanceScope(PROJECT, str(tmp_path.resolve())),
            HEAD, MISSION, GENERATION, NOW, "CoDeX/Recovery",
        )


@pytest.mark.parametrize("identity", ["Codex/Fake", "codex/Fake", "CODEX/Fake", "CoDeX/Fake"])
def test_fake_codex_operator_is_refused(tmp_path: Path, identity: str) -> None:
    with pytest.raises(ValueError, match="Human"):
        MaintenanceInitializationRequest(MaintenanceScope(PROJECT, str(tmp_path.resolve())), HEAD, MISSION, GENERATION, NOW, identity)


def test_stale_duplicate_and_foreign_transitions_are_refused(tmp_path: Path) -> None:
    _, service, record = _service(tmp_path)
    result = _transition(service, record, tmp_path, MaintenanceState.MAINTENANCE)
    stale = MaintenanceTransitionRequest(result.record.scope, HEAD, MISSION, GENERATION, record.revision, record.fingerprint, MaintenanceState.FROZEN, MaintenanceTransitionReason.INCIDENT_ESCALATION, NOW + timedelta(seconds=2), "Alice")
    with pytest.raises(MaintenanceGovernanceError, match="STALE_TRANSITION"):
        service.request_transition(_context(tmp_path, GovernedOperation.MAINTENANCE, at=NOW + timedelta(seconds=2)), stale)
    foreign_scope = MaintenanceScope("project-two", str(tmp_path.resolve()))
    foreign = replace(stale, scope=foreign_scope, expected_revision=result.record.revision, expected_fingerprint=result.record.fingerprint)
    with pytest.raises(MaintenanceGovernanceError, match="FOREIGN_TRANSITION"):
        service.request_transition(_context(tmp_path, GovernedOperation.MAINTENANCE, at=NOW + timedelta(seconds=2)), foreign)


def test_current_revision_with_stale_timestamp_is_refused(tmp_path: Path) -> None:
    _, service, record = _service(tmp_path)
    maintenance = _transition(service, record, tmp_path, MaintenanceState.MAINTENANCE).record
    stale = MaintenanceTransitionRequest(
        maintenance.scope, HEAD, MISSION, GENERATION, maintenance.revision,
        maintenance.fingerprint, MaintenanceState.FROZEN,
        MaintenanceTransitionReason.INCIDENT_ESCALATION, maintenance.updated_at,
        "Alice/Operator",
    )
    with pytest.raises(MaintenanceGovernanceError, match="STALE_TRANSITION"):
        service.request_transition(
            _context(tmp_path, GovernedOperation.MAINTENANCE, at=maintenance.updated_at),
            stale,
        )


def test_wrong_generation_source_is_refused(tmp_path: Path) -> None:
    _, service, _ = _service(tmp_path)
    context = _context(tmp_path)
    wrong = replace(context, workflow_generation=GENERATION + 1)
    with pytest.raises(MaintenanceGovernanceError, match="STATE_SCOPE_MISMATCH"):
        service.evaluate(wrong, MaintenanceOperation.START_ROLE_EXECUTION)


def test_corruption_and_missing_expected_state_fail_closed(tmp_path: Path) -> None:
    store = MaintenanceStateStore(tmp_path)
    with pytest.raises(PersistenceError, match="MAINTENANCE_STATE_ABSENT"):
        store.load()
    state_dir = tmp_path / ".agentic-engineering-os"
    state_dir.mkdir()
    store.maintenance_path.write_text('{"schema_version":"1.0","schema_version":"1.0"}', encoding="utf-8")
    with pytest.raises(PersistenceError, match="INVALID_MAINTENANCE_STATE"):
        MaintenanceStateStore(tmp_path).load()


def test_blocked_health_cannot_transition_to_normal(tmp_path: Path) -> None:
    _, service, record = _service(tmp_path)
    maintenance = _transition(service, record, tmp_path, MaintenanceState.MAINTENANCE).record
    at = NOW + timedelta(seconds=2)
    health = _health(at=at, blocked=True)
    context = _context(tmp_path, GovernedOperation.MAINTENANCE, at=at, health=health)
    request = MaintenanceTransitionRequest(context.scope, HEAD, MISSION, GENERATION, maintenance.revision, maintenance.fingerprint, MaintenanceState.NORMAL, MaintenanceTransitionReason.OPERATOR_RETURN_TO_NORMAL, at, "Alice")
    with pytest.raises(MaintenanceGovernanceError, match="NORMAL_EXIT_REFUSED"):
        service.request_transition(context, request)


def test_unresolved_critical_incident_prevents_exit(tmp_path: Path) -> None:
    _, service, record = _service(tmp_path)
    maintenance = _transition(service, record, tmp_path, MaintenanceState.MAINTENANCE).record
    at = NOW + timedelta(seconds=2)
    context = _context(tmp_path, GovernedOperation.MAINTENANCE, at=at, incidents=(_critical(),))
    request = MaintenanceTransitionRequest(context.scope, HEAD, MISSION, GENERATION, maintenance.revision, maintenance.fingerprint, MaintenanceState.NORMAL, MaintenanceTransitionReason.OPERATOR_RETURN_TO_NORMAL, at, "Alice")
    with pytest.raises(MaintenanceGovernanceError, match="CRITICAL_INCIDENT_ACTIVE"):
        service.request_transition(context, request)


def test_budget_exceeded_refuses_new_execution(tmp_path: Path) -> None:
    _, service, _ = _service(tmp_path)
    admission = service.evaluate(_context(tmp_path, budget_limit=1, budget_current=1), MaintenanceOperation.START_ROLE_EXECUTION)
    assert admission.decision is MaintenanceAdmissionDecision.REFUSED


def test_direct_workflow_guard_cannot_reuse_admission_to_bypass_freeze(tmp_path: Path) -> None:
    _, service, record = _service(tmp_path)
    admission = service.evaluate(_context(tmp_path), MaintenanceOperation.START_ROLE_EXECUTION)
    _transition(service, record, tmp_path, MaintenanceState.FROZEN)
    with pytest.raises(MaintenanceGovernanceError, match="ADMISSION_REQUIRED"):
        service.enforce(admission, MaintenanceOperation.START_ROLE_EXECUTION)


def test_store_has_no_public_save_and_models_have_no_business_authority(tmp_path: Path) -> None:
    store, service, _ = _service(tmp_path)
    assert not hasattr(store, "save")
    admission = service.evaluate(_context(tmp_path), MaintenanceOperation.START_ROLE_EXECUTION)
    for forbidden in ("certify", "approve", "pass_gate", "merge", "remediate", "recover"):
        assert not hasattr(admission, forbidden)
    with pytest.raises(PersistenceError, match="WRITE_NOT_AUTHORIZED"):
        store._replace_authorized(store.load(), authorization=object())


def test_existing_write_lock_refuses_last_write_wins(tmp_path: Path) -> None:
    store = MaintenanceStateStore(tmp_path)
    state_dir = tmp_path / ".agentic-engineering-os"
    state_dir.mkdir()
    (state_dir / ".maintenance.lock").write_text("held", encoding="utf-8")
    service = MaintenanceGovernanceService(store)
    request = MaintenanceInitializationRequest(
        MaintenanceScope(PROJECT, str(tmp_path.resolve())), HEAD, MISSION,
        GENERATION, NOW, "Alice/Operator",
    )
    with pytest.raises(PersistenceError, match="CONCURRENT_WRITE"):
        service.initialize(request)


def test_incident_evaluation_does_not_auto_mutate_maintenance(tmp_path: Path) -> None:
    store, service, record = _service(tmp_path)
    admission = service.evaluate(_context(tmp_path, incidents=(_critical(),)), MaintenanceOperation.START_ROLE_EXECUTION)
    assert admission.decision is MaintenanceAdmissionDecision.REFUSED
    assert store.load() == record
