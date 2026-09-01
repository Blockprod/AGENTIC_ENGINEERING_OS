from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from agentic_engineering_os.application import (
    GovernancePolicyEvaluationError,
    GovernancePolicyEvaluator,
    HealthEvaluationEngine,
    MetricsEngine,
)
from agentic_engineering_os.domain import (
    Certification,
    Gate,
    GovernedOperation,
    GovernanceCondition,
    GovernanceDecision,
    GovernanceDecisionSet,
    GovernanceEvaluationContext,
    GovernancePolicy,
    GovernancePolicyClass,
    GovernancePolicyDomain,
    GovernancePolicyScope,
    GovernanceRationale,
    GovernanceReasonCode,
    GovernanceScope,
    HealthCondition,
    HealthEvaluationContext,
    HealthObservation,
    HealthScope,
    HealthSource,
    HealthState,
    MetricsHealthInput,
    MetricsScope,
    MissionRole,
)


NOW = datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc)
HEAD = "b" * 40
PROJECT = "project-one"
MISSION = "mission-1"
GENERATION = 3


def _observation(
    source: HealthSource,
    condition: HealthCondition,
    *,
    active: bool,
) -> HealthObservation:
    mission_bound = source in {
        HealthSource.MISSION_STATE_STORE,
        HealthSource.CODEX_RUNTIME,
        HealthSource.EXECUTION_LEDGER,
        HealthSource.REMEDIATION_STORE,
    }
    return HealthObservation(
        source,
        condition,
        PROJECT,
        NOW,
        f"{source.value.lower()}:v1",
        HEAD,
        MISSION if active and mission_bound else None,
        GENERATION if active and mission_bound else None,
    )


def _metrics(*, active: bool, complete: bool = True) -> MetricsHealthInput:
    scope = MetricsScope(
        PROJECT,
        MISSION if active else None,
        GENERATION if active else None,
    )
    return MetricsHealthInput(
        MetricsEngine().compute((), scope, source_complete=complete), NOW, HEAD
    )


def _health(
    state: HealthState = HealthState.HEALTHY, *, active: bool = False
):
    observations = [
        _observation(HealthSource.PROJECT_STATE_STORE, HealthCondition.AVAILABLE, active=active),
        _observation(HealthSource.OPERATIONAL_EVENT_STORE, HealthCondition.AVAILABLE, active=active),
        _observation(HealthSource.PERSISTENCE_DIAGNOSTIC, HealthCondition.AVAILABLE, active=active),
        _observation(HealthSource.PROJECT_CONFIGURATION, HealthCondition.VALID, active=active),
        _observation(HealthSource.CODEX_RUNTIME, HealthCondition.AVAILABLE, active=active),
    ]
    metrics = _metrics(active=active)
    if active:
        observations.extend(
            (
                _observation(HealthSource.MISSION_STATE_STORE, HealthCondition.AVAILABLE, active=True),
                _observation(HealthSource.EXECUTION_LEDGER, HealthCondition.CLEAR, active=True),
                _observation(HealthSource.REMEDIATION_STORE, HealthCondition.CLEAR, active=True),
            )
        )
    if state is HealthState.DEGRADED:
        observations[1] = _observation(
            HealthSource.OPERATIONAL_EVENT_STORE, HealthCondition.DEGRADED, active=active
        )
    elif state is HealthState.BLOCKED:
        observations[2] = _observation(
            HealthSource.PERSISTENCE_DIAGNOSTIC, HealthCondition.FAILED, active=active
        )
    elif state is HealthState.UNKNOWN:
        observations[0] = _observation(
            HealthSource.PROJECT_STATE_STORE, HealthCondition.UNKNOWN, active=active
        )
    scope = HealthScope(
        PROJECT,
        HEAD,
        MISSION if active else None,
        GENERATION if active else None,
    )
    context = HealthEvaluationContext(
        scope,
        NOW,
        active,
        False,
        tuple(observations),
        metrics,
    )
    snapshot = HealthEvaluationEngine().evaluate(context)
    assert snapshot.global_state is state
    return snapshot, metrics


def _policy(
    policy_id: str,
    *,
    policy_class: GovernancePolicyClass = GovernancePolicyClass.HARD_SAFETY_POLICY,
    domain: GovernancePolicyDomain = GovernancePolicyDomain.HEALTH_GATING,
    condition: GovernanceCondition = GovernanceCondition.HEALTH_UNKNOWN,
    action: GovernanceDecision = GovernanceDecision.REQUIRE_OPERATOR,
    operations: tuple[GovernedOperation, ...] = (GovernedOperation.EXECUTION,),
    project_id: str = PROJECT,
    mission_id: str | None = None,
    generation: int | None = None,
    role: MissionRole | None = None,
    enabled: bool = True,
) -> GovernancePolicy:
    return GovernancePolicy(
        policy_id,
        "1.0",
        policy_class,
        domain,
        enabled,
        GovernancePolicyScope(project_id, operations, mission_id, generation, role),
        condition,
        action,
        GovernanceRationale.PROTECT_SAFETY_INVARIANTS,
    )


def _hard_floor() -> GovernancePolicy:
    return _policy("hard-health-unknown")


def _context(
    *,
    health_state: HealthState = HealthState.HEALTHY,
    policies: tuple[GovernancePolicy, ...] | None = None,
    active: bool = False,
    operation: GovernedOperation = GovernedOperation.EXECUTION,
    sandbox_compliant: bool | None = True,
    verification_complete: bool | None = True,
) -> GovernanceEvaluationContext:
    health, metrics = _health(health_state, active=active)
    return GovernanceEvaluationContext(
        GovernanceScope(
            PROJECT,
            HEAD,
            MISSION if active else None,
            GENERATION if active else None,
        ),
        operation,
        NOW,
        health,
        metrics,
        policies if policies is not None else (_hard_floor(),),
        sandbox_compliant,
        verification_complete,
    )


def test_hard_safety_policy_blocks_without_mutating() -> None:
    policy = _policy(
        "hard-admission",
        domain=GovernancePolicyDomain.EXECUTION_ADMISSION,
        condition=GovernanceCondition.ALWAYS,
        action=GovernanceDecision.BLOCK,
    )
    result = GovernancePolicyEvaluator().evaluate(_context(policies=(policy,)))
    assert result.decision is GovernanceDecision.BLOCK
    assert result.policy_results[0].matched


def test_operational_policy_can_warn() -> None:
    warning = _policy(
        "operational-warning",
        policy_class=GovernancePolicyClass.OPERATIONAL_POLICY,
        domain=GovernancePolicyDomain.EXECUTION_ADMISSION,
        condition=GovernanceCondition.ALWAYS,
        action=GovernanceDecision.ALLOW_WITH_WARNING,
    )
    result = GovernancePolicyEvaluator().evaluate(
        _context(policies=(_hard_floor(), warning))
    )
    assert result.decision is GovernanceDecision.ALLOW_WITH_WARNING


def test_operator_preference_can_refine_without_authority() -> None:
    preference = _policy(
        "operator-preference",
        policy_class=GovernancePolicyClass.OPERATOR_PREFERENCE,
        domain=GovernancePolicyDomain.EXECUTION_ADMISSION,
        condition=GovernanceCondition.ALWAYS,
        action=GovernanceDecision.ALLOW_WITH_WARNING,
    )
    result = GovernancePolicyEvaluator().evaluate(
        _context(policies=(_hard_floor(), preference))
    )
    assert result.decision is GovernanceDecision.ALLOW_WITH_WARNING


def test_operator_preference_cannot_override_hard_block() -> None:
    hard = _policy(
        "hard-block",
        domain=GovernancePolicyDomain.EXECUTION_ADMISSION,
        condition=GovernanceCondition.ALWAYS,
        action=GovernanceDecision.BLOCK,
    )
    preference = _policy(
        "preference-allow",
        policy_class=GovernancePolicyClass.OPERATOR_PREFERENCE,
        domain=GovernancePolicyDomain.EXECUTION_ADMISSION,
        condition=GovernanceCondition.ALWAYS,
        action=GovernanceDecision.ALLOW,
    )
    result = GovernancePolicyEvaluator().evaluate(
        _context(policies=(preference, hard))
    )
    assert result.decision is GovernanceDecision.BLOCK


def test_policy_order_does_not_change_results_or_fingerprint() -> None:
    warning = _policy(
        "warning",
        policy_class=GovernancePolicyClass.OPERATIONAL_POLICY,
        domain=GovernancePolicyDomain.EXECUTION_ADMISSION,
        condition=GovernanceCondition.ALWAYS,
        action=GovernanceDecision.ALLOW_WITH_WARNING,
    )
    first = GovernancePolicyEvaluator().evaluate(
        _context(policies=(_hard_floor(), warning))
    )
    second = GovernancePolicyEvaluator().evaluate(
        _context(policies=(warning, _hard_floor()))
    )
    assert first == second


@pytest.mark.parametrize(
    ("health_state", "decision"),
    [
        (HealthState.HEALTHY, GovernanceDecision.ALLOW),
        (HealthState.DEGRADED, GovernanceDecision.ALLOW_WITH_WARNING),
        (HealthState.BLOCKED, GovernanceDecision.BLOCK),
        (HealthState.UNKNOWN, GovernanceDecision.REQUIRE_OPERATOR),
    ],
)
def test_health_floor_is_explicit(
    health_state: HealthState, decision: GovernanceDecision
) -> None:
    result = GovernancePolicyEvaluator().evaluate(_context(health_state=health_state))
    assert result.decision is decision


def test_project_mission_generation_scoped_policy_matches_exactly() -> None:
    scoped = _policy(
        "mission-warning",
        policy_class=GovernancePolicyClass.OPERATIONAL_POLICY,
        domain=GovernancePolicyDomain.EXECUTION_ADMISSION,
        condition=GovernanceCondition.ALWAYS,
        action=GovernanceDecision.ALLOW_WITH_WARNING,
        mission_id=MISSION,
        generation=GENERATION,
    )
    result = GovernancePolicyEvaluator().evaluate(
        _context(active=True, policies=(_hard_floor(), scoped))
    )
    assert result.decision is GovernanceDecision.ALLOW_WITH_WARNING


def test_conflicting_same_class_and_domain_blocks_as_invalid_policy_set() -> None:
    first = _policy(
        "op-allow",
        policy_class=GovernancePolicyClass.OPERATIONAL_POLICY,
        domain=GovernancePolicyDomain.EXECUTION_ADMISSION,
        condition=GovernanceCondition.ALWAYS,
        action=GovernanceDecision.ALLOW,
    )
    second = replace(first, policy_id="op-block", action=GovernanceDecision.BLOCK)
    result = GovernancePolicyEvaluator().evaluate(
        _context(policies=(_hard_floor(), first, second))
    )
    assert result.decision is GovernanceDecision.BLOCK
    assert GovernanceReasonCode.POLICY_CONFLICT in result.reasons


def test_same_class_same_decision_is_not_a_conflict() -> None:
    first = _policy(
        "op-one",
        policy_class=GovernancePolicyClass.OPERATIONAL_POLICY,
        domain=GovernancePolicyDomain.EXECUTION_ADMISSION,
        condition=GovernanceCondition.ALWAYS,
        action=GovernanceDecision.ALLOW_WITH_WARNING,
    )
    second = replace(first, policy_id="op-two")
    result = GovernancePolicyEvaluator().evaluate(
        _context(policies=(_hard_floor(), first, second))
    )
    assert result.decision is GovernanceDecision.ALLOW_WITH_WARNING
    assert GovernanceReasonCode.POLICY_CONFLICT not in result.reasons


def test_stale_health_snapshot_blocks() -> None:
    context = _context()
    stale = replace(context.health, evaluated_at=NOW - timedelta(minutes=6))
    result = GovernancePolicyEvaluator().evaluate(replace(context, health=stale))
    assert result.decision is GovernanceDecision.BLOCK
    assert GovernanceReasonCode.HEALTH_STALE in result.reasons


def test_wrong_project_or_generation_health_blocks() -> None:
    idle = _context()
    wrong_project = replace(idle.scope, project_id="project-two")
    first = GovernancePolicyEvaluator().evaluate(replace(idle, scope=wrong_project))
    active = _context(active=True)
    wrong_generation = replace(active.scope, workflow_generation=GENERATION + 1)
    second = GovernancePolicyEvaluator().evaluate(
        replace(active, scope=wrong_generation)
    )
    assert first.decision is GovernanceDecision.BLOCK
    assert second.decision is GovernanceDecision.BLOCK


def test_policy_from_wrong_project_or_generation_is_refused() -> None:
    wrong_project = replace(_hard_floor(), scope=replace(_hard_floor().scope, project_id="project-two"))
    first = GovernancePolicyEvaluator().evaluate(_context(policies=(wrong_project,)))
    wrong_generation = _policy(
        "wrong-generation",
        mission_id=MISSION,
        generation=GENERATION + 1,
    )
    second = GovernancePolicyEvaluator().evaluate(
        _context(active=True, policies=(_hard_floor(), wrong_generation))
    )
    assert first.decision is GovernanceDecision.BLOCK
    assert second.decision is GovernanceDecision.BLOCK
    assert GovernanceReasonCode.POLICY_SCOPE_MISMATCH in second.reasons


def test_missing_hard_safety_policy_blocks() -> None:
    operational = _policy(
        "only-operational",
        policy_class=GovernancePolicyClass.OPERATIONAL_POLICY,
        domain=GovernancePolicyDomain.EXECUTION_ADMISSION,
        condition=GovernanceCondition.ALWAYS,
        action=GovernanceDecision.ALLOW,
    )
    result = GovernancePolicyEvaluator().evaluate(_context(policies=(operational,)))
    assert result.decision is GovernanceDecision.BLOCK
    assert GovernanceReasonCode.MISSING_HARD_SAFETY_POLICY in result.reasons


def test_metrics_dependent_policy_requires_metrics() -> None:
    metric_policy = _policy(
        "metrics-required",
        domain=GovernancePolicyDomain.OBSERVABILITY_REQUIRED,
        condition=GovernanceCondition.METRICS_NOT_COMPLETE,
        action=GovernanceDecision.BLOCK,
    )
    context = replace(_context(policies=(metric_policy,)), metrics=None)
    result = GovernancePolicyEvaluator().evaluate(context)
    assert result.decision is GovernanceDecision.BLOCK
    assert GovernanceReasonCode.METRICS_REQUIRED in result.reasons


def test_stale_or_wrong_scope_metrics_block_when_required() -> None:
    policy = _policy(
        "metrics-required",
        domain=GovernancePolicyDomain.OBSERVABILITY_REQUIRED,
        condition=GovernanceCondition.METRICS_NOT_COMPLETE,
        action=GovernanceDecision.BLOCK,
    )
    context = _context(policies=(policy,))
    stale = replace(context.metrics, observed_at=NOW - timedelta(minutes=6))
    first = GovernancePolicyEvaluator().evaluate(replace(context, metrics=stale))
    wrong_snapshot = MetricsEngine().compute(
        (), MetricsScope("project-two"), source_complete=True
    )
    wrong = MetricsHealthInput(wrong_snapshot, NOW, HEAD)
    second = GovernancePolicyEvaluator().evaluate(replace(context, metrics=wrong))
    assert GovernanceReasonCode.METRICS_STALE in first.reasons
    assert GovernanceReasonCode.METRICS_SCOPE_MISMATCH in second.reasons


def test_explicit_untrusted_metrics_cannot_be_ignored_by_unrelated_policy() -> None:
    context = _context()
    wrong_snapshot = MetricsEngine().compute(
        (), MetricsScope("project-two"), source_complete=True
    )
    result = GovernancePolicyEvaluator().evaluate(
        replace(context, metrics=MetricsHealthInput(wrong_snapshot, NOW, HEAD))
    )
    assert result.decision is GovernanceDecision.BLOCK
    assert GovernanceReasonCode.METRICS_SCOPE_MISMATCH in result.reasons


def test_explicit_incomplete_metrics_block() -> None:
    context = _context()
    result = GovernancePolicyEvaluator().evaluate(
        replace(context, metrics=_metrics(active=False, complete=False))
    )
    assert result.decision is GovernanceDecision.BLOCK
    assert GovernanceReasonCode.METRICS_NOT_COMPLETE in result.reasons


def test_missing_sandbox_fact_blocks_instead_of_assuming_safe() -> None:
    sandbox = _policy(
        "sandbox",
        domain=GovernancePolicyDomain.SANDBOX_SAFETY,
        condition=GovernanceCondition.SANDBOX_NONCOMPLIANT,
        action=GovernanceDecision.BLOCK,
    )
    result = GovernancePolicyEvaluator().evaluate(
        _context(policies=(sandbox,), sandbox_compliant=None)
    )
    assert result.decision is GovernanceDecision.BLOCK
    assert GovernanceReasonCode.FACT_REQUIRED in result.reasons


def test_sandbox_noncompliance_blocks_and_compliance_does_not_match() -> None:
    sandbox = _policy(
        "sandbox",
        domain=GovernancePolicyDomain.SANDBOX_SAFETY,
        condition=GovernanceCondition.SANDBOX_NONCOMPLIANT,
        action=GovernanceDecision.BLOCK,
    )
    blocked = GovernancePolicyEvaluator().evaluate(
        _context(policies=(sandbox,), sandbox_compliant=False)
    )
    allowed = GovernancePolicyEvaluator().evaluate(
        _context(policies=(sandbox,), sandbox_compliant=True)
    )
    assert blocked.decision is GovernanceDecision.BLOCK
    assert allowed.decision is GovernanceDecision.ALLOW


def test_verification_and_operator_request_conditions_are_closed() -> None:
    verification = _policy(
        "verification",
        domain=GovernancePolicyDomain.VERIFICATION_TIER,
        condition=GovernanceCondition.VERIFICATION_INCOMPLETE,
        action=GovernanceDecision.REQUIRE_OPERATOR,
    )
    result = GovernancePolicyEvaluator().evaluate(
        _context(policies=(verification,), verification_complete=False)
    )
    assert result.decision is GovernanceDecision.REQUIRE_OPERATOR


def test_disabled_operational_policy_and_operation_mismatch_do_not_apply() -> None:
    disabled = _policy(
        "disabled-warning",
        policy_class=GovernancePolicyClass.OPERATIONAL_POLICY,
        domain=GovernancePolicyDomain.EXECUTION_ADMISSION,
        condition=GovernanceCondition.ALWAYS,
        action=GovernanceDecision.ALLOW_WITH_WARNING,
        enabled=False,
    )
    merge_only = replace(
        disabled,
        policy_id="merge-warning",
        enabled=True,
        scope=replace(disabled.scope, operations=(GovernedOperation.MERGE,)),
    )
    result = GovernancePolicyEvaluator().evaluate(
        _context(policies=(_hard_floor(), disabled, merge_only))
    )
    assert result.decision is GovernanceDecision.ALLOW


def test_hard_policy_cannot_be_disabled_or_allow() -> None:
    with pytest.raises(ValueError, match="cannot be disabled"):
        replace(_hard_floor(), enabled=False)
    with pytest.raises(ValueError, match="cannot weaken"):
        replace(_hard_floor(), action=GovernanceDecision.ALLOW)


def test_operator_preference_cannot_block_or_require_operator() -> None:
    preference = _policy(
        "preference",
        policy_class=GovernancePolicyClass.OPERATOR_PREFERENCE,
        domain=GovernancePolicyDomain.EXECUTION_ADMISSION,
        condition=GovernanceCondition.ALWAYS,
        action=GovernanceDecision.ALLOW,
    )
    with pytest.raises(ValueError, match="cannot impose"):
        replace(preference, action=GovernanceDecision.BLOCK)


def test_arbitrary_domain_action_condition_and_dynamic_code_are_refused() -> None:
    with pytest.raises(ValueError):
        GovernancePolicyDomain("RESOURCE_BUDGET")
    with pytest.raises(ValueError):
        GovernanceDecision("CERTIFY")
    with pytest.raises(ValueError):
        GovernanceCondition("eval(payload)")
    with pytest.raises(ValueError, match="policy_id"):
        replace(_hard_floor(), policy_id="eval:os.system")


def test_secret_like_policy_identity_is_refused() -> None:
    with pytest.raises(ValueError, match="policy_id"):
        replace(_hard_floor(), policy_id="token:synthetic")
    with pytest.raises(ValueError, match="policy_id"):
        replace(_hard_floor(), policy_id="ghp_abcdefghijklmnopqrstuvwxyz123456")


def test_incompatible_domain_condition_is_refused() -> None:
    with pytest.raises(ValueError, match="incompatible"):
        replace(
            _hard_floor(),
            domain=GovernancePolicyDomain.SANDBOX_SAFETY,
            condition=GovernanceCondition.HEALTH_UNKNOWN,
        )


def test_forged_allow_decision_is_rejected() -> None:
    blocked = GovernancePolicyEvaluator().evaluate(
        _context(health_state=HealthState.BLOCKED)
    )
    with pytest.raises(ValueError, match="inconsistent"):
        replace(blocked, decision=GovernanceDecision.ALLOW)


def test_decision_set_is_immutable_and_has_no_business_authority() -> None:
    result = GovernancePolicyEvaluator().evaluate(_context())
    with pytest.raises(FrozenInstanceError):
        result.decision = GovernanceDecision.BLOCK  # type: ignore[misc]
    assert not isinstance(result, (Gate, Certification))
    for forbidden in (
        "to_gate",
        "to_certification",
        "to_evidence",
        "approve",
        "merge",
        "save",
        "mutate",
        "enforce",
    ):
        assert not hasattr(result, forbidden)


def test_wrong_evaluator_input_fails_closed() -> None:
    with pytest.raises(GovernancePolicyEvaluationError):
        GovernancePolicyEvaluator().evaluate(GovernanceDecision.ALLOW)  # type: ignore[arg-type]


def test_policy_set_is_immutable_unique_and_bounded() -> None:
    context = _context()
    with pytest.raises(ValueError, match="immutable"):
        replace(context, policies=list(context.policies))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unique"):
        replace(context, policies=context.policies * 2)
    with pytest.raises(ValueError, match="one version"):
        replace(
            context,
            policies=(context.policies[0], replace(context.policies[0], version="2.0")),
        )
    many = tuple(replace(_hard_floor(), policy_id=f"hard-{index}") for index in range(33))
    with pytest.raises(ValueError, match="exceeds"):
        replace(context, policies=many)


def test_decision_is_only_governance_constraint_not_control_plane_allowance() -> None:
    result = GovernancePolicyEvaluator().evaluate(_context())
    assert result.decision is GovernanceDecision.ALLOW
    assert result.input_floor is GovernanceDecision.ALLOW
    assert not hasattr(result, "authorized")
    assert len(result.fingerprint) == 64
    assert result.source_identities == tuple(sorted(result.source_identities))
