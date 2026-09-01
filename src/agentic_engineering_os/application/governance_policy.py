"""Deterministic evaluation of non-authoritative operational policies."""

from __future__ import annotations

import hashlib
import json

from agentic_engineering_os.domain import (
    GOVERNANCE_SCHEMA_VERSION,
    HEALTH_MAX_OBSERVATION_AGE,
    GovernedOperation,
    GovernanceCondition,
    GovernanceDecision,
    GovernanceDecisionSet,
    GovernanceEvaluationContext,
    GovernancePolicy,
    GovernancePolicyClass,
    GovernancePolicyResult,
    GovernanceReasonCode,
    HealthDimension,
    HealthState,
    MetricsSnapshotStatus,
)


class GovernancePolicyEvaluationError(ValueError):
    """The caller did not provide a valid typed governance context."""


_DECISION_RANK = {
    GovernanceDecision.ALLOW: 0,
    GovernanceDecision.ALLOW_WITH_WARNING: 1,
    GovernanceDecision.REQUIRE_OPERATOR: 2,
    GovernanceDecision.BLOCK: 3,
}

_CLASS_RANK = {
    GovernancePolicyClass.HARD_SAFETY_POLICY: 0,
    GovernancePolicyClass.OPERATIONAL_POLICY: 1,
    GovernancePolicyClass.OPERATOR_PREFERENCE: 2,
}

_FACT_CONDITIONS = frozenset(
    {
        GovernanceCondition.SANDBOX_NONCOMPLIANT,
        GovernanceCondition.VERIFICATION_INCOMPLETE,
    }
)


class GovernancePolicyEvaluator:
    """Evaluate policy constraints; never enforce or persist their decisions."""

    def evaluate(self, context: GovernanceEvaluationContext) -> GovernanceDecisionSet:
        if not isinstance(context, GovernanceEvaluationContext):
            raise GovernancePolicyEvaluationError(
                "context must be GovernanceEvaluationContext"
            )

        floor, reasons = _input_floor(context)
        results: list[GovernancePolicyResult] = []
        matched_by_class_domain: dict[
            tuple[GovernancePolicyClass, object], set[GovernanceDecision]
        ] = {}

        policies = tuple(
            sorted(
                context.policies,
                key=lambda item: (
                    _CLASS_RANK[item.policy_class],
                    item.domain.value,
                    item.policy_id,
                    item.version,
                ),
            )
        )
        for policy in policies:
            scope_problem = _policy_scope_problem(policy, context)
            if scope_problem is not None:
                floor = GovernanceDecision.BLOCK
                reasons.append(scope_problem)
                results.append(_unmatched(policy, scope_problem))
                continue
            if context.operation not in policy.scope.operations:
                results.append(
                    _unmatched(policy, GovernanceReasonCode.OPERATION_OUT_OF_SCOPE)
                )
                continue
            if not policy.enabled:
                results.append(_unmatched(policy, GovernanceReasonCode.POLICY_DISABLED))
                continue
            matched = _condition_matches(policy.condition, context)
            if matched is None:
                floor = GovernanceDecision.BLOCK
                reasons.append(GovernanceReasonCode.FACT_REQUIRED)
                results.append(_unmatched(policy, GovernanceReasonCode.FACT_REQUIRED))
                continue
            if not matched:
                results.append(_unmatched(policy, GovernanceReasonCode.CONDITION_NOT_MET))
                continue
            result = GovernancePolicyResult(
                policy.policy_id,
                policy.version,
                policy.policy_class,
                policy.domain,
                True,
                policy.action,
                GovernanceReasonCode.POLICY_MATCHED,
            )
            results.append(result)
            matched_by_class_domain.setdefault(
                (policy.policy_class, policy.domain), set()
            ).add(policy.action)

        if any(len(decisions) > 1 for decisions in matched_by_class_domain.values()):
            floor = GovernanceDecision.BLOCK
            reasons.append(GovernanceReasonCode.POLICY_CONFLICT)

        ordered_results = tuple(results)
        decision = max(
            (
                floor,
                *(item.decision for item in ordered_results if item.decision is not None),
            ),
            key=lambda item: _DECISION_RANK[item],
        )
        identities = tuple(
            sorted(
                {
                    context.health.fingerprint,
                    *(
                        _metrics_identity(context)
                        if context.metrics is not None
                        else ()
                    ),
                    *(f"{item.policy_id}:{item.version}" for item in policies),
                }
            )
        )
        unique_reasons = tuple(sorted(set(reasons), key=lambda item: item.value))
        fingerprint = _fingerprint(
            context, floor, decision, ordered_results, unique_reasons, identities
        )
        return GovernanceDecisionSet(
            GOVERNANCE_SCHEMA_VERSION,
            context.scope,
            context.operation,
            context.evaluated_at,
            floor,
            decision,
            ordered_results,
            unique_reasons,
            identities,
            fingerprint,
        )


def _input_floor(
    context: GovernanceEvaluationContext,
) -> tuple[GovernanceDecision, list[GovernanceReasonCode]]:
    reasons: list[GovernanceReasonCode] = []
    if not any(
        item.policy_class is GovernancePolicyClass.HARD_SAFETY_POLICY
        for item in context.policies
    ):
        return GovernanceDecision.BLOCK, [
            GovernanceReasonCode.MISSING_HARD_SAFETY_POLICY
        ]
    health_problem = _health_problem(context)
    if health_problem is not None:
        return GovernanceDecision.BLOCK, [health_problem]
    floor, reason = {
        HealthState.BLOCKED: (
            GovernanceDecision.BLOCK,
            GovernanceReasonCode.HEALTH_BLOCKED_FLOOR,
        ),
        HealthState.UNKNOWN: (
            GovernanceDecision.REQUIRE_OPERATOR,
            GovernanceReasonCode.HEALTH_UNKNOWN_FLOOR,
        ),
        HealthState.DEGRADED: (
            GovernanceDecision.ALLOW_WITH_WARNING,
            GovernanceReasonCode.HEALTH_DEGRADED_FLOOR,
        ),
        HealthState.HEALTHY: (
            GovernanceDecision.ALLOW,
            GovernanceReasonCode.NO_ADDITIONAL_CONSTRAINT,
        ),
    }[context.health.global_state]
    reasons.append(reason)

    if context.metrics is not None:
        provided_metrics_problem = _metrics_problem(context)
        if provided_metrics_problem is not None:
            return GovernanceDecision.BLOCK, [*reasons, provided_metrics_problem]

    applicable = tuple(
        item
        for item in context.policies
        if item.enabled and context.operation in item.scope.operations
    )
    if any(item.condition is GovernanceCondition.METRICS_NOT_COMPLETE for item in applicable):
        metrics_problem = _metrics_problem(context)
        if metrics_problem is not None:
            return GovernanceDecision.BLOCK, [*reasons, metrics_problem]
    if any(item.condition in _FACT_CONDITIONS for item in applicable):
        if any(
            item.condition is GovernanceCondition.SANDBOX_NONCOMPLIANT
            and context.sandbox_compliant is None
            or item.condition is GovernanceCondition.VERIFICATION_INCOMPLETE
            and context.verification_complete is None
            for item in applicable
        ):
            return GovernanceDecision.BLOCK, [
                *reasons,
                GovernanceReasonCode.FACT_REQUIRED,
            ]
    return floor, reasons


def _health_problem(
    context: GovernanceEvaluationContext,
) -> GovernanceReasonCode | None:
    health = context.health
    if (
        health.scope.project_id != context.scope.project_id
        or health.scope.repository_head != context.scope.repository_head
        or health.scope.mission_id != context.scope.mission_id
        or health.scope.workflow_generation != context.scope.workflow_generation
    ):
        return GovernanceReasonCode.HEALTH_SCOPE_MISMATCH
    age = context.evaluated_at - health.evaluated_at
    if age.total_seconds() < 0 or age > HEALTH_MAX_OBSERVATION_AGE:
        return GovernanceReasonCode.HEALTH_STALE
    return None


def _metrics_problem(
    context: GovernanceEvaluationContext,
) -> GovernanceReasonCode | None:
    metrics = context.metrics
    if metrics is None:
        return GovernanceReasonCode.METRICS_REQUIRED
    scope = metrics.snapshot.scope
    if (
        scope.project_id != context.scope.project_id
        or scope.mission_id != context.scope.mission_id
        or scope.workflow_generation != context.scope.workflow_generation
        or scope.user_story_id is not None
        or scope.role is not None
        or scope.execution_id is not None
        or metrics.repository_head != context.scope.repository_head
    ):
        return GovernanceReasonCode.METRICS_SCOPE_MISMATCH
    age = context.evaluated_at - metrics.observed_at
    if age.total_seconds() < 0 or age > HEALTH_MAX_OBSERVATION_AGE:
        return GovernanceReasonCode.METRICS_STALE
    if metrics.snapshot.status is not MetricsSnapshotStatus.COMPLETE:
        return GovernanceReasonCode.METRICS_NOT_COMPLETE
    return None


def _policy_scope_problem(
    policy: GovernancePolicy, context: GovernanceEvaluationContext
) -> GovernanceReasonCode | None:
    scope = policy.scope
    if scope.project_id != context.scope.project_id:
        return GovernanceReasonCode.POLICY_SCOPE_MISMATCH
    if scope.mission_id is not None and (
        scope.mission_id != context.scope.mission_id
        or scope.workflow_generation != context.scope.workflow_generation
    ):
        return GovernanceReasonCode.POLICY_SCOPE_MISMATCH
    if scope.role is not None and scope.role is not context.scope.role:
        return GovernanceReasonCode.POLICY_SCOPE_MISMATCH
    return None


def _condition_matches(
    condition: GovernanceCondition, context: GovernanceEvaluationContext
) -> bool | None:
    dimensions = {item.dimension: item.state for item in context.health.dimensions}
    if condition is GovernanceCondition.ALWAYS:
        return True
    if condition is GovernanceCondition.HEALTH_BLOCKED:
        return context.health.global_state is HealthState.BLOCKED
    if condition is GovernanceCondition.HEALTH_UNKNOWN:
        return context.health.global_state is HealthState.UNKNOWN
    if condition is GovernanceCondition.HEALTH_DEGRADED:
        return context.health.global_state is HealthState.DEGRADED
    if condition is GovernanceCondition.OBSERVABILITY_NOT_HEALTHY:
        return dimensions[HealthDimension.OBSERVABILITY] is not HealthState.HEALTHY
    if condition is GovernanceCondition.RECOVERY_NOT_HEALTHY:
        state = dimensions[HealthDimension.EXECUTION_RECOVERY]
        return None if state is None else state is not HealthState.HEALTHY
    if condition is GovernanceCondition.CODEX_RUNTIME_NOT_HEALTHY:
        state = dimensions[HealthDimension.CODEX_RUNTIME]
        return None if state is None else state is not HealthState.HEALTHY
    if condition is GovernanceCondition.DEPLOYMENT_NOT_HEALTHY:
        return (
            dimensions[HealthDimension.DEPLOYMENT_CONFIGURATION]
            is not HealthState.HEALTHY
        )
    if condition is GovernanceCondition.METRICS_NOT_COMPLETE:
        return (
            None
            if context.metrics is None
            else context.metrics.snapshot.status is not MetricsSnapshotStatus.COMPLETE
        )
    if condition is GovernanceCondition.SANDBOX_NONCOMPLIANT:
        return (
            None
            if context.sandbox_compliant is None
            else not context.sandbox_compliant
        )
    if condition is GovernanceCondition.VERIFICATION_INCOMPLETE:
        return (
            None
            if context.verification_complete is None
            else not context.verification_complete
        )
    if condition is GovernanceCondition.MAINTENANCE_REQUESTED:
        return context.maintenance_requested
    return context.operator_intervention_requested


def _unmatched(
    policy: GovernancePolicy, reason: GovernanceReasonCode
) -> GovernancePolicyResult:
    return GovernancePolicyResult(
        policy.policy_id,
        policy.version,
        policy.policy_class,
        policy.domain,
        False,
        None,
        reason,
    )


def _metrics_identity(context: GovernanceEvaluationContext) -> tuple[str, ...]:
    assert context.metrics is not None
    fingerprint = context.metrics.snapshot.source_fingerprint
    return (
        fingerprint
        if fingerprint is not None
        else hashlib.sha256(
            (
                context.metrics.snapshot.schema_version
                + context.metrics.snapshot.status.value
                + repr(context.metrics.snapshot.scope)
            ).encode("utf-8")
        ).hexdigest(),
    )


def _fingerprint(
    context: GovernanceEvaluationContext,
    floor: GovernanceDecision,
    decision: GovernanceDecision,
    results: tuple[GovernancePolicyResult, ...],
    reasons: tuple[GovernanceReasonCode, ...],
    identities: tuple[str, ...],
) -> str:
    payload = {
        "schema_version": GOVERNANCE_SCHEMA_VERSION,
        "scope": {
            "project_id": context.scope.project_id,
            "repository_head": context.scope.repository_head,
            "mission_id": context.scope.mission_id,
            "workflow_generation": context.scope.workflow_generation,
            "role": context.scope.role.value if context.scope.role is not None else None,
            "execution_id": context.scope.execution_id,
            "worktree_id": context.scope.worktree_id,
        },
        "operation": context.operation.value,
        "evaluated_at": context.evaluated_at.isoformat().replace("+00:00", "Z"),
        "facts": {
            "sandbox_compliant": context.sandbox_compliant,
            "verification_complete": context.verification_complete,
            "maintenance_requested": context.maintenance_requested,
            "operator_intervention_requested": context.operator_intervention_requested,
        },
        "input_floor": floor.value,
        "decision": decision.value,
        "results": [
            {
                "policy_id": item.policy_id,
                "version": item.version,
                "class": item.policy_class.value,
                "domain": item.domain.value,
                "matched": item.matched,
                "decision": item.decision.value if item.decision is not None else None,
                "reason": item.reason.value,
            }
            for item in results
        ],
        "reasons": [item.value for item in reasons],
        "source_identities": identities,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
