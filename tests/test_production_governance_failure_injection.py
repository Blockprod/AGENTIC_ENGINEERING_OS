from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    CodexExecutionStatus,
    CodexResultIntake,
    ExecutionStateError,
    GovernancePolicyEvaluator,
    HealthEvaluationEngine,
    IncidentManager,
    MaintenanceGovernanceError,
    MaintenanceGovernanceService,
    MetricsEngine,
    ResourceBudgetEvaluator,
    ResultIntakeRefusalCode,
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
    HealthState,
    IncidentClassification,
    IncidentCorrelation,
    IncidentDiagnostic,
    IncidentDiagnosticCondition,
    IncidentEvaluationContext,
    IncidentResolutionObservation,
    IncidentScope,
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
    MetricsSnapshotStatus,
    MissionRole,
    OperationalCorrelation,
    OperationalEvent,
    OperationalEventPayload,
    OperationalEventType,
    OperationalProvenance,
    OperationalProvenanceKind,
    OperationalSeverity,
    RecoveryObservation,
    RecoveryObservationStatus,
    RecoveryRoute,
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
from agentic_engineering_os.infrastructure import (
    IncidentEventJournal,
    MaintenanceStateStore,
    OperationalEventStore,
)
from test_codex_execution_recovery import (
    EXECUTABLE,
    binding as execution_binding,
    harness as execution_harness,
    planned as planned_execution,
)
from test_operator_diagnostics_cli import (
    _files as repository_files,
    _invoke as invoke_diagnostic,
    _repository as diagnostic_repository,
)


NOW = datetime(2026, 9, 1, 16, 0, tzinfo=timezone.utc)
HEAD = "e" * 40
PROJECT = "production-e2e"
MISSION = "mission-e2e"
GENERATION = 3


@dataclass(frozen=True, slots=True)
class PipelineResult:
    metrics: object
    health: object
    governance: object
    budgets: object
    incidents: object
    admission: object


def _event(index: int, event_type: OperationalEventType, operation: str, *, reason: str | None = None) -> OperationalEvent:
    role = MissionRole.IMPLEMENTER if event_type is OperationalEventType.CODEX_EXECUTION else None
    execution = f"execution-{index}" if role is not None else None
    story = "US-FAILURE" if event_type is OperationalEventType.WORKTREE_LIFECYCLE else None
    assignment = f"assignment-{index}" if story is not None else None
    return OperationalEvent(
        "1.0", f"00000000-0000-4000-8000-{index:012d}", event_type,
        NOW, OperationalSeverity.ERROR, "failure-injection-harness", PROJECT,
        OperationalCorrelation(
            mission_id=MISSION,
            workflow_generation=GENERATION,
            user_story_id=story,
            role=role,
            execution_id=execution,
            assignment_id=assignment,
            repository_commit=HEAD,
        ),
        OperationalEventPayload(operation, reason_code=reason),
        OperationalProvenance(
            OperationalProvenanceKind.DETERMINISTIC_COMPONENT,
            "failure-injection-harness",
        ),
    )


def _health(metrics, *, project=HealthCondition.AVAILABLE, events=HealthCondition.AVAILABLE,
            codex=HealthCondition.AVAILABLE, ledger=HealthCondition.CLEAR,
            persistence=HealthCondition.AVAILABLE, remediation=HealthCondition.CLEAR,
            git_condition: HealthCondition | None = None):
    pairs = [
        (HealthSource.PROJECT_STATE_STORE, project, False),
        (HealthSource.OPERATIONAL_EVENT_STORE, events, False),
        (HealthSource.PERSISTENCE_DIAGNOSTIC, persistence, False),
        (HealthSource.PROJECT_CONFIGURATION, HealthCondition.VALID, False),
        (HealthSource.CODEX_RUNTIME, codex, True),
        (HealthSource.MISSION_STATE_STORE, HealthCondition.AVAILABLE, True),
        (HealthSource.EXECUTION_LEDGER, ledger, True),
        (HealthSource.REMEDIATION_STORE, remediation, True),
    ]
    if git_condition is not None:
        pairs.append((HealthSource.GIT_RECONCILIATION, git_condition, True))
    observations = tuple(
        HealthObservation(
            source, condition, PROJECT, NOW, f"e2e:{source.value.lower()}", HEAD,
            MISSION if bound else None, GENERATION if bound else None,
        )
        for source, condition, bound in pairs
    )
    return HealthEvaluationEngine().evaluate(
        HealthEvaluationContext(
            HealthScope(PROJECT, HEAD, MISSION, GENERATION), NOW, True,
            git_condition is not None, observations, MetricsHealthInput(metrics, NOW, HEAD),
        )
    )


def _governance(health, metrics, operation: GovernedOperation = GovernedOperation.EXECUTION):
    policy = GovernancePolicy(
        "production-health-floor", "1.0",
        GovernancePolicyClass.HARD_SAFETY_POLICY,
        GovernancePolicyDomain.HEALTH_GATING, True,
        GovernancePolicyScope(PROJECT, (operation,), MISSION, GENERATION),
        GovernanceCondition.HEALTH_UNKNOWN, GovernanceDecision.BLOCK,
        GovernanceRationale.PROTECT_SAFETY_INVARIANTS,
    )
    return GovernancePolicyEvaluator().evaluate(
        GovernanceEvaluationContext(
            GovernanceScope(PROJECT, HEAD, MISSION, GENERATION), operation, NOW,
            health, MetricsHealthInput(metrics, NOW, HEAD),
            (policy,), True, True,
        )
    )


def _budget(
    root: Path,
    domain: ResourceBudgetDomain = ResourceBudgetDomain.CODEX_CONCURRENCY,
    *,
    current: int | None = 0,
    requested: int = 1,
    limit: int = 4,
    status: ResourceUsageStatus = ResourceUsageStatus.COMPLETE,
    operation: GovernedOperation = GovernedOperation.EXECUTION,
):
    units = {
        ResourceBudgetDomain.CODEX_CONCURRENCY: ResourceBudgetUnit.EXECUTIONS,
        ResourceBudgetDomain.WORKTREE_CONCURRENCY: ResourceBudgetUnit.WORKTREES,
        ResourceBudgetDomain.EXECUTION_TIME: ResourceBudgetUnit.SECONDS,
        ResourceBudgetDomain.REMEDIATION_GENERATIONS: ResourceBudgetUnit.GENERATIONS,
        ResourceBudgetDomain.RUNTIME_STORAGE: ResourceBudgetUnit.BYTES,
        ResourceBudgetDomain.OBSERVABILITY_STORAGE: ResourceBudgetUnit.BYTES,
    }
    sources = {
        ResourceBudgetDomain.CODEX_CONCURRENCY: ResourceUsageSource.EXECUTION_STATE_STORE,
        ResourceBudgetDomain.WORKTREE_CONCURRENCY: ResourceUsageSource.WORKTREE_REGISTRY,
        ResourceBudgetDomain.EXECUTION_TIME: ResourceUsageSource.CODEX_RUNTIME,
        ResourceBudgetDomain.REMEDIATION_GENERATIONS: ResourceUsageSource.MISSION_STATE,
        ResourceBudgetDomain.RUNTIME_STORAGE: ResourceUsageSource.FILESYSTEM,
        ResourceBudgetDomain.OBSERVABILITY_STORAGE: ResourceUsageSource.EVENT_STORE_RETENTION,
    }
    scope = ResourceBudgetScope(PROJECT, HEAD, str(root.resolve()), MISSION, GENERATION)
    budget = ResourceBudget(
        f"{domain.value.lower()}-ceiling", "1.0", domain, scope, limit,
        units[domain], GovernancePolicyClass.HARD_SAFETY_POLICY,
        "failure-injection-policy", ResourceBudgetApplicability.APPLICABLE,
        ResourceBudgetRationale.SAFETY_CEILING,
    )
    count = current or 0
    identities = (
        tuple(f"active-{index}" for index in range(count))
        if domain in {ResourceBudgetDomain.CODEX_CONCURRENCY, ResourceBudgetDomain.WORKTREE_CONCURRENCY}
        else ()
    )
    roots = tuple(str(root.resolve()) for _ in identities)
    usage = ResourceUsageObservation(
        domain, units[domain], status, sources[domain], "failure-injection-source",
        scope, NOW, current if status is ResourceUsageStatus.COMPLETE else None,
        requested, identities if status is ResourceUsageStatus.COMPLETE else (),
        roots if status is ResourceUsageStatus.COMPLETE else (),
    )
    return ResourceBudgetEvaluator().evaluate(
        ResourceBudgetEvaluationContext(scope, operation, NOW, (budget,), (usage,))
    )


def _pipeline(
    root: Path,
    *,
    project_condition=HealthCondition.AVAILABLE,
    event_condition: HealthCondition | None = None,
    codex_condition=HealthCondition.AVAILABLE,
    ledger_condition=HealthCondition.CLEAR,
    persistence_condition=HealthCondition.AVAILABLE,
    remediation_condition=HealthCondition.CLEAR,
    git_condition: HealthCondition | None = None,
    budget_domain=ResourceBudgetDomain.CODEX_CONCURRENCY,
    budget_current: int | None = 0,
    budget_requested=1,
    budget_limit=4,
    budget_status=ResourceUsageStatus.COMPLETE,
) -> PipelineResult:
    store = OperationalEventStore(root)
    metrics = MetricsEngine().compute_from_store(store, MetricsScope(PROJECT, MISSION, GENERATION))
    inferred_event = {
        MetricsSnapshotStatus.COMPLETE: HealthCondition.AVAILABLE,
        MetricsSnapshotStatus.INCOMPLETE: HealthCondition.SATURATED,
        MetricsSnapshotStatus.UNAVAILABLE: HealthCondition.CORRUPTED,
    }[metrics.status]
    health = _health(
        metrics, project=project_condition, events=event_condition or inferred_event,
        codex=codex_condition, ledger=ledger_condition,
        persistence=persistence_condition, remediation=remediation_condition,
        git_condition=git_condition,
    )
    governance = _governance(health, metrics)
    budgets = _budget(
        root, budget_domain, current=budget_current, requested=budget_requested,
        limit=budget_limit, status=budget_status,
    )
    try:
        events = store.read()
        complete = not store.retention_exhausted()
    except Exception:
        events, complete = (), False
    incidents = IncidentManager().evaluate(
        IncidentEvaluationContext(
            IncidentScope(PROJECT, HEAD, MISSION, GENERATION), NOW, health,
            governance, budgets, MetricsHealthInput(metrics, NOW, HEAD), events,
            complete, (), True, (),
        )
    )
    maintenance_store = MaintenanceStateStore(root)
    service = MaintenanceGovernanceService(maintenance_store)
    service.initialize(
        MaintenanceInitializationRequest(
            MaintenanceScope(PROJECT, str(root.resolve())), HEAD, MISSION,
            GENERATION, NOW - timedelta(seconds=1), "Alice/Operator",
        )
    )
    admission = service.evaluate(
        MaintenanceEvaluationContext(
            MaintenanceScope(PROJECT, str(root.resolve())), HEAD, MISSION,
            GENERATION, NOW, health, governance, budgets, incidents.records,
        ),
        MaintenanceOperation.START_ROLE_EXECUTION,
    )
    return PipelineResult(metrics, health, governance, budgets, incidents, admission)


def _assert_failed_closed(result: PipelineResult) -> None:
    assert result.metrics.status is not MetricsSnapshotStatus.COMPLETE or result.health.global_state is not HealthState.HEALTHY
    assert result.health.global_state is not HealthState.HEALTHY
    assert result.governance.decision is not GovernanceDecision.ALLOW
    assert result.incidents.records
    assert result.admission.decision is MaintenanceAdmissionDecision.REFUSED


def test_A_healthy_nominal_pipeline_is_non_authoritative(tmp_path: Path) -> None:
    result = _pipeline(tmp_path)
    assert result.metrics.status is MetricsSnapshotStatus.COMPLETE
    assert result.health.global_state is HealthState.HEALTHY
    assert result.governance.decision is GovernanceDecision.ALLOW
    assert all(item.decision is ResourceBudgetDecision.WITHIN_BUDGET for item in result.budgets.decisions)
    assert result.incidents.records == ()
    assert result.admission.decision is MaintenanceAdmissionDecision.ADMITTED
    for value in (result.metrics, result.health, result.governance, result.admission):
        for forbidden in ("certify", "authorize", "pass_gate", "merge", "approve"):
            assert not hasattr(value, forbidden)


@pytest.mark.parametrize("scenario", ["corrupt", "truncated", "saturated", "writer-lock", "io-unavailable"])
def test_B_C_event_store_failures_propagate_without_silent_recovery(tmp_path: Path, monkeypatch, scenario: str) -> None:
    directory = tmp_path / ".agentic-engineering-os" / "operational-events"
    directory.mkdir(parents=True)
    if scenario == "corrupt":
        (directory / "segment-000001.jsonl").write_text("not-json\n", encoding="utf-8")
    elif scenario == "truncated":
        (directory / "segment-000001.jsonl").write_bytes(b'{"record_version":"1.0"')
    elif scenario == "saturated":
        (directory / ".retention-exhausted").write_bytes(b"1.0\n")
    elif scenario == "writer-lock":
        (directory / ".writer.lock").write_text("pid=stale\n", encoding="ascii")
    else:
        monkeypatch.setattr(OperationalEventStore, "read", lambda self: (_ for _ in ()).throw(OSError("injected I/O unavailable")))
    result = _pipeline(tmp_path)
    assert result.metrics.status is (MetricsSnapshotStatus.INCOMPLETE if scenario == "saturated" else MetricsSnapshotStatus.UNAVAILABLE)
    _assert_failed_closed(result)
    assert any(item.classification in {IncidentClassification.OBSERVABILITY_LOSS, IncidentClassification.PERSISTENCE_FAILURE, IncidentClassification.UNKNOWN_CRITICAL_STATE} for item in result.incidents.records)


def test_D_authoritative_store_unavailable_is_not_masked_by_complete_metrics(tmp_path: Path) -> None:
    result = _pipeline(
        tmp_path, project_condition=HealthCondition.UNAVAILABLE,
        persistence_condition=HealthCondition.FAILED,
    )
    assert result.metrics.status is MetricsSnapshotStatus.COMPLETE
    _assert_failed_closed(result)
    assert any(item.classification is IncidentClassification.PERSISTENCE_FAILURE for item in result.incidents.records)


@pytest.mark.parametrize(
    ("event", "codex", "ledger", "classification"),
    [
        (_event(101, OperationalEventType.CODEX_EXECUTION, "INTERRUPTED", reason="TIMEOUT"), HealthCondition.UNAVAILABLE, HealthCondition.CLEAR, IncidentClassification.CODEX_RUNTIME_FAILURE),
        (_event(102, OperationalEventType.CODEX_EXECUTION, "FAILED", reason="TOOL_FAILURE_EXIT_ZERO"), HealthCondition.UNAVAILABLE, HealthCondition.CLEAR, IncidentClassification.CODEX_RUNTIME_FAILURE),
        (_event(103, OperationalEventType.REMEDIATION_RECOVERY, "RECOVERY_REQUIRED", reason="UNCERTAIN_EXECUTION"), HealthCondition.AVAILABLE, HealthCondition.RECOVERY_PENDING, IncidentClassification.RECOVERY_STUCK),
    ],
)
def test_E_F_G_codex_execution_failures_flow_to_incident_and_admission(tmp_path: Path, event, codex, ledger, classification) -> None:
    OperationalEventStore(tmp_path).append(event)
    result = _pipeline(tmp_path, codex_condition=codex, ledger_condition=ledger)
    _assert_failed_closed(result)
    assert any(item.classification is classification for item in result.incidents.records)


def test_G_actual_P4_restart_requires_recovery_and_never_blind_retries(tmp_path: Path) -> None:
    case, _, runtime, service, record = planned_execution(execution_harness(tmp_path))
    runtime.error = RuntimeError("injected interruption")
    with pytest.raises(ExecutionStateError, match="RUNTIME_OUTCOME_UNCERTAIN"):
        service.execute(record.execution_id, case.compiled, execution_binding(case))
    root = Path(case.compiled.repository_root)
    (root / "dirty-side-effect.txt").write_text("observed", encoding="utf-8")
    inspection = service.inspect_restart(record.execution_id, case.compiled, execution_binding(case), EXECUTABLE)
    assert inspection.disposition.value == "RECOVERY_REQUIRED"
    assert inspection.operator_intervention_required
    assert not inspection.blind_retry_allowed


def test_F_actual_P4_zero_exit_tool_failure_and_malformed_result_never_succeed(tmp_path: Path) -> None:
    case, store, runtime, service, record = planned_execution(execution_harness(tmp_path))
    runtime.observation = replace(
        case.observation,
        exit_code=0,
        tool_failure_observed=True,
    )
    service.execute(record.execution_id, case.compiled, execution_binding(case))
    assert store.load().records[0].status is CodexExecutionStatus.FAILED
    assert not service.inspect_restart(
        record.execution_id, case.compiled, execution_binding(case), EXECUTABLE
    ).blind_retry_allowed

    malformed_event = replace(case.observation.events[0], payload_json="not-json")
    malformed = replace(case.observation, events=(malformed_event,))
    intake = CodexResultIntake().process(case.compiled, malformed, case.context)
    assert not intake.accepted
    assert any(
        item.code is ResultIntakeRefusalCode.PAYLOAD_MALFORMED
        for item in intake.refusal_reasons
    )


def test_H_git_worktree_divergence_is_observed_without_cleanup(tmp_path: Path) -> None:
    marker = tmp_path / "dirty-worktree.txt"
    marker.write_text("must remain", encoding="utf-8")
    OperationalEventStore(tmp_path).append(
        _event(201, OperationalEventType.WORKTREE_LIFECYCLE, "DIVERGENCE_OBSERVED", reason="BRANCH_DRIFT")
    )
    result = _pipeline(tmp_path, git_condition=HealthCondition.DRIFT)
    _assert_failed_closed(result)
    assert marker.read_text(encoding="utf-8") == "must remain"
    assert any(item.classification is IncidentClassification.GIT_WORKTREE_DIVERGENCE for item in result.incidents.records)


@pytest.mark.parametrize(
    ("domain", "current", "requested", "limit", "status", "expected"),
    [
        (ResourceBudgetDomain.CODEX_CONCURRENCY, 3, 2, 4, ResourceUsageStatus.COMPLETE, ResourceBudgetDecision.LIMIT_EXCEEDED),
        (ResourceBudgetDomain.WORKTREE_CONCURRENCY, 4, 1, 4, ResourceUsageStatus.COMPLETE, ResourceBudgetDecision.LIMIT_EXCEEDED),
        (ResourceBudgetDomain.EXECUTION_TIME, 0, 61, 60, ResourceUsageStatus.COMPLETE, ResourceBudgetDecision.LIMIT_EXCEEDED),
        (ResourceBudgetDomain.REMEDIATION_GENERATIONS, 4, 1, 4, ResourceUsageStatus.COMPLETE, ResourceBudgetDecision.LIMIT_EXCEEDED),
        (ResourceBudgetDomain.RUNTIME_STORAGE, None, 1, 100, ResourceUsageStatus.UNKNOWN, ResourceBudgetDecision.UNKNOWN),
        (ResourceBudgetDomain.OBSERVABILITY_STORAGE, 100, 1, 100, ResourceUsageStatus.COMPLETE, ResourceBudgetDecision.LIMIT_EXCEEDED),
    ],
)
def test_I_J_K_resource_exhaustion_is_never_admitted(tmp_path: Path, domain, current, requested, limit, status, expected) -> None:
    result = _pipeline(
        tmp_path, budget_domain=domain, budget_current=current,
        budget_requested=requested, budget_limit=limit, budget_status=status,
    )
    assert result.budgets.decisions[0].decision is expected
    assert result.admission.decision is MaintenanceAdmissionDecision.REFUSED
    assert any(item.classification in {IncidentClassification.RESOURCE_EXHAUSTION, IncidentClassification.UNKNOWN_CRITICAL_STATE, IncidentClassification.REMEDIATION_LOOP} for item in result.incidents.records)


def _maintenance_context(root: Path, pipeline: PipelineResult, *, at: datetime, recovery=None):
    governance = _governance_for(pipeline.health, pipeline.metrics, GovernedOperation.MAINTENANCE, at)
    budgets = _budget(root, operation=GovernedOperation.MAINTENANCE)
    return MaintenanceEvaluationContext(
        MaintenanceScope(PROJECT, str(root.resolve())), HEAD, MISSION, GENERATION,
        at, pipeline.health, governance, budgets, pipeline.incidents.records, recovery,
    )


def _governance_for(health, metrics, operation, at):
    policy = GovernancePolicy(
        "maintenance-health-floor", "1.0", GovernancePolicyClass.HARD_SAFETY_POLICY,
        GovernancePolicyDomain.HEALTH_GATING, True,
        GovernancePolicyScope(PROJECT, (operation,), MISSION, GENERATION),
        GovernanceCondition.HEALTH_UNKNOWN, GovernanceDecision.BLOCK,
        GovernanceRationale.PROTECT_SAFETY_INVARIANTS,
    )
    return GovernancePolicyEvaluator().evaluate(
        GovernanceEvaluationContext(
            GovernanceScope(PROJECT, HEAD, MISSION, GENERATION), operation, at,
            health, MetricsHealthInput(metrics, at, HEAD), (policy,), True, True,
        )
    )


def test_L_M_N_O_critical_freeze_restart_attacks_and_controlled_recovery(tmp_path: Path) -> None:
    failure = _pipeline(tmp_path, persistence_condition=HealthCondition.FAILED)
    store = MaintenanceStateStore(tmp_path)
    service = MaintenanceGovernanceService(store)
    normal = store.load()
    freeze_at = NOW + timedelta(seconds=1)
    freeze_context = _maintenance_context(tmp_path, failure, at=freeze_at)
    freeze = service.request_transition(
        freeze_context,
        MaintenanceTransitionRequest(
            freeze_context.scope, HEAD, MISSION, GENERATION, normal.revision,
            normal.fingerprint, MaintenanceState.FROZEN,
            MaintenanceTransitionReason.INCIDENT_ESCALATION, freeze_at,
            "Alice/Operator",
        ),
    ).record
    assert freeze.state is MaintenanceState.FROZEN

    restarted = MaintenanceGovernanceService(MaintenanceStateStore(tmp_path))
    assert MaintenanceStateStore(tmp_path).load() == freeze
    assert restarted.evaluate(
        MaintenanceEvaluationContext(
            freeze_context.scope, HEAD, MISSION, GENERATION, freeze_at,
            failure.health, failure.governance, failure.budgets,
            failure.incidents.records,
        ), MaintenanceOperation.READ_DIAGNOSTICS,
    ).decision is MaintenanceAdmissionDecision.ADMITTED
    assert restarted.evaluate(
        MaintenanceEvaluationContext(
            freeze_context.scope, HEAD, MISSION, GENERATION, freeze_at,
            failure.health, failure.governance, failure.budgets,
            failure.incidents.records,
        ), MaintenanceOperation.START_ROLE_EXECUTION,
    ).decision is MaintenanceAdmissionDecision.REFUSED
    with pytest.raises(ValueError, match="Human"):
        MaintenanceTransitionRequest(
            freeze_context.scope, HEAD, MISSION, GENERATION, freeze.revision,
            freeze.fingerprint, MaintenanceState.MAINTENANCE,
            MaintenanceTransitionReason.OPERATOR_MAINTENANCE,
            freeze_at + timedelta(seconds=1), "CoDeX/FakeHuman",
        )
    wrong_human = MaintenanceTransitionRequest(
        freeze_context.scope, HEAD, MISSION, GENERATION, freeze.revision,
        freeze.fingerprint, MaintenanceState.MAINTENANCE,
        MaintenanceTransitionReason.OPERATOR_MAINTENANCE,
        freeze_at + timedelta(seconds=1), "Mallory/Operator",
    )
    with pytest.raises(MaintenanceGovernanceError, match="OPERATOR_MISMATCH"):
        restarted.request_transition(
            _maintenance_context(tmp_path, failure, at=freeze_at + timedelta(seconds=1)),
            wrong_human,
        )

    recovery_at = freeze_at + timedelta(seconds=1)
    recovery_context = _maintenance_context(tmp_path, failure, at=recovery_at)
    recovery_required = restarted.request_transition(
        recovery_context,
        MaintenanceTransitionRequest(
            recovery_context.scope, HEAD, MISSION, GENERATION, freeze.revision,
            freeze.fingerprint, MaintenanceState.RECOVERY_REQUIRED,
            MaintenanceTransitionReason.RECOVERY_COORDINATION, recovery_at,
            "Alice/Operator", RecoveryRoute.P4_EXECUTION_RECOVERY,
        ),
    )
    assert recovery_required.recovery_request is not None
    assert "RestartSafeCodexExecutionService.inspect_restart" in recovery_required.recovery_request.boundary
    with pytest.raises(MaintenanceGovernanceError, match="STALE_TRANSITION"):
        restarted.request_transition(
            recovery_context,
            MaintenanceTransitionRequest(
                recovery_context.scope, HEAD, MISSION, GENERATION,
                freeze.revision, freeze.fingerprint,
                MaintenanceState.RECOVERY_REQUIRED,
                MaintenanceTransitionReason.RECOVERY_COORDINATION, recovery_at,
                "Alice/Operator", RecoveryRoute.P4_EXECUTION_RECOVERY,
            ),
        )

    healthy_root = tmp_path / "healthy-observations"
    healthy_root.mkdir()
    healthy = _pipeline(healthy_root)
    resolved_at = recovery_at + timedelta(seconds=1)
    observation = RecoveryObservation(
        RecoveryRoute.P4_EXECUTION_RECOVERY, RecoveryObservationStatus.SUCCEEDED,
        MaintenanceScope(PROJECT, str(tmp_path.resolve())), HEAD, MISSION,
        GENERATION, resolved_at, "execution-recovery-service",
    )
    critical = next(
        item for item in failure.incidents.records
        if item.classification is IncidentClassification.PERSISTENCE_FAILURE
    )
    normalized = IncidentDiagnostic(
        IncidentClassification.PERSISTENCE_FAILURE,
        IncidentScope(PROJECT, HEAD, MISSION, GENERATION),
        IncidentCorrelation(MISSION, GENERATION),
        IncidentDiagnosticCondition.NORMALIZED,
        resolved_at,
        "execution-recovery-service",
    )
    incident_context = IncidentEvaluationContext(
        IncidentScope(PROJECT, HEAD, MISSION, GENERATION), resolved_at,
        healthy.health,
        _governance_for(healthy.health, healthy.metrics, GovernedOperation.MAINTENANCE, resolved_at),
        _budget(tmp_path, operation=GovernedOperation.MAINTENANCE),
        MetricsHealthInput(healthy.metrics, resolved_at, HEAD), (), True,
        (normalized,), True, (critical,),
    )
    resolved = IncidentManager().resolve(
        critical,
        incident_context,
        IncidentResolutionObservation(
            critical.incident_id,
            IncidentScope(PROJECT, HEAD, MISSION, GENERATION),
            resolved_at,
            "execution-recovery-service",
            "Alice/Operator",
        ),
    )
    assert resolved.state is IncidentState.RESOLVED

    unresolved_exit = MaintenanceEvaluationContext(
        MaintenanceScope(PROJECT, str(tmp_path.resolve())), HEAD, MISSION,
        GENERATION, resolved_at, healthy.health,
        _governance_for(healthy.health, healthy.metrics, GovernedOperation.MAINTENANCE, resolved_at),
        _budget(tmp_path, operation=GovernedOperation.MAINTENANCE),
        (critical,), observation,
    )
    with pytest.raises(MaintenanceGovernanceError, match="CRITICAL_INCIDENT_ACTIVE"):
        restarted.request_transition(
            unresolved_exit,
            MaintenanceTransitionRequest(
                unresolved_exit.scope, HEAD, MISSION, GENERATION,
                recovery_required.record.revision,
                recovery_required.record.fingerprint, MaintenanceState.NORMAL,
                MaintenanceTransitionReason.OPERATOR_RETURN_TO_NORMAL,
                resolved_at, "Alice/Operator",
            ),
        )
    exit_context = MaintenanceEvaluationContext(
        MaintenanceScope(PROJECT, str(tmp_path.resolve())), HEAD, MISSION,
        GENERATION, resolved_at, healthy.health,
        _governance_for(healthy.health, healthy.metrics, GovernedOperation.MAINTENANCE, resolved_at),
        _budget(tmp_path, operation=GovernedOperation.MAINTENANCE), (resolved,), observation,
    )
    recovered = restarted.request_transition(
        exit_context,
        MaintenanceTransitionRequest(
            exit_context.scope, HEAD, MISSION, GENERATION,
            recovery_required.record.revision, recovery_required.record.fingerprint,
            MaintenanceState.NORMAL,
            MaintenanceTransitionReason.OPERATOR_RETURN_TO_NORMAL, resolved_at,
            "Alice/Operator",
        ),
    )
    assert recovered.record.state is MaintenanceState.NORMAL
    foreign = replace(
        exit_context,
        scope=MaintenanceScope("foreign-project", str(tmp_path.resolve())),
    )
    with pytest.raises(MaintenanceGovernanceError, match="STATE_SCOPE_MISMATCH"):
        restarted.evaluate(foreign, MaintenanceOperation.START_ROLE_EXECUTION)


def test_restart_incident_identity_deduplicates_through_event_journal(tmp_path: Path) -> None:
    OperationalEventStore(tmp_path).append(
        _event(301, OperationalEventType.PERSISTENCE_FAILURE, "WRITE_FAILED", reason="INJECTED")
    )
    first = _pipeline(tmp_path, persistence_condition=HealthCondition.FAILED)
    record = next(item for item in first.incidents.records if item.classification is IncidentClassification.PERSISTENCE_FAILURE)
    journal = IncidentEventJournal(OperationalEventStore(tmp_path))
    journal.append(record)
    prior = IncidentEventJournal(OperationalEventStore(tmp_path)).latest(
        IncidentScope(PROJECT, HEAD, MISSION, GENERATION)
    )
    assert prior == (record,)
    restarted = IncidentManager().evaluate(
        IncidentEvaluationContext(
            IncidentScope(PROJECT, HEAD, MISSION, GENERATION), NOW,
            first.health, first.governance, first.budgets,
            MetricsHealthInput(first.metrics, NOW, HEAD),
            tuple(item for item in OperationalEventStore(tmp_path).read() if item.source_component != "incident-manager"),
            True, (), True, prior,
        )
    )
    assert any(item.incident_id == record.incident_id for item in restarted.records)
    assert next(item for item in restarted.records if item.incident_id == record.incident_id) == record


def test_old_healthy_snapshot_is_not_reused_after_restart(tmp_path: Path) -> None:
    healthy = _pipeline(tmp_path)
    stale_health = replace(healthy.health, evaluated_at=NOW - timedelta(minutes=6))
    with pytest.raises(MaintenanceGovernanceError, match="SOURCE_INVALID|STATE_SCOPE_MISMATCH"):
        service = MaintenanceGovernanceService(MaintenanceStateStore(tmp_path))
        context = MaintenanceEvaluationContext(
            MaintenanceScope(PROJECT, str(tmp_path.resolve())), HEAD, MISSION,
            GENERATION, NOW, stale_health, healthy.governance, healthy.budgets, (),
        )
        current = MaintenanceStateStore(tmp_path).load()
        service.request_transition(
            context,
            MaintenanceTransitionRequest(
                context.scope, HEAD, MISSION, GENERATION, current.revision,
                current.fingerprint, MaintenanceState.MAINTENANCE,
                MaintenanceTransitionReason.OPERATOR_MAINTENANCE, NOW,
                "Alice/Operator",
            ),
        )


def test_P_diagnostics_are_truthful_read_only_and_secret_free(capsys, tmp_path: Path) -> None:
    root = diagnostic_repository(tmp_path)
    directory = root / ".agentic-engineering-os" / "operational-events"
    directory.mkdir()
    (directory / ".retention-exhausted").write_bytes(b"1.0\n")
    before = repository_files(root)
    code, payload, raw = invoke_diagnostic(capsys, "diagnose", root)
    assert code == 2
    assert payload["status"] == "ATTENTION_REQUIRED"
    assert payload["result"]["health"]["global_state"] != "HEALTHY"
    assert payload["result"]["metrics"]["status"] == "INCOMPLETE"
    assert payload["result"]["incidents"]["active_count"] > 0
    assert payload["result"]["governance"]["meaning"] != "NO_ADDITIONAL_GOVERNANCE_BLOCK"
    assert payload["result"]["authority_notice"] == "DIAGNOSTIC_ONLY_NOT_AUTHORIZATION"
    assert "secret" not in raw.casefold()
    assert repository_files(root) == before
