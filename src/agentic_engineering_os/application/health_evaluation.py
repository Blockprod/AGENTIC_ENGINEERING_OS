"""Deterministic, read-only evaluation of operational health."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict

from agentic_engineering_os.domain import (
    HEALTH_MAX_OBSERVATION_AGE,
    HEALTH_SCHEMA_VERSION,
    DimensionRequirement,
    HealthCondition,
    HealthDiagnostic,
    HealthDimension,
    HealthDimensionResult,
    HealthEvaluationContext,
    HealthFreshness,
    HealthObservation,
    HealthReasonCode,
    HealthSnapshot,
    HealthSource,
    HealthSourceReference,
    HealthState,
    MetricAvailability,
    MetricName,
    MetricsHealthInput,
    MetricsSnapshotStatus,
)


class HealthEvaluationError(ValueError):
    """The caller did not provide the closed factual health context."""


_SOURCE_DIMENSION = {
    HealthSource.PROJECT_STATE_STORE: HealthDimension.AUTHORITATIVE_STATE_ACCESS,
    HealthSource.MISSION_STATE_STORE: HealthDimension.AUTHORITATIVE_STATE_ACCESS,
    HealthSource.OPERATIONAL_EVENT_STORE: HealthDimension.OBSERVABILITY,
    HealthSource.GIT_RECONCILIATION: HealthDimension.GIT_WORKTREES,
    HealthSource.CODEX_RUNTIME: HealthDimension.CODEX_RUNTIME,
    HealthSource.EXECUTION_LEDGER: HealthDimension.EXECUTION_RECOVERY,
    HealthSource.PERSISTENCE_DIAGNOSTIC: HealthDimension.PERSISTENCE,
    HealthSource.REMEDIATION_STORE: HealthDimension.REMEDIATION_TRANSACTION,
    HealthSource.PROJECT_CONFIGURATION: HealthDimension.DEPLOYMENT_CONFIGURATION,
}

_CONDITION_STATE = {
    HealthCondition.AVAILABLE: HealthState.HEALTHY,
    HealthCondition.RECONCILED: HealthState.HEALTHY,
    HealthCondition.CLEAR: HealthState.HEALTHY,
    HealthCondition.VALID: HealthState.HEALTHY,
    HealthCondition.DEGRADED: HealthState.DEGRADED,
    HealthCondition.SATURATED: HealthState.DEGRADED,
    HealthCondition.UNAVAILABLE: HealthState.BLOCKED,
    HealthCondition.DRIFT: HealthState.BLOCKED,
    HealthCondition.RECOVERY_PENDING: HealthState.BLOCKED,
    HealthCondition.FAILED: HealthState.BLOCKED,
    HealthCondition.PENDING: HealthState.BLOCKED,
    HealthCondition.INVALID: HealthState.BLOCKED,
    HealthCondition.UNKNOWN: HealthState.UNKNOWN,
    HealthCondition.CORRUPTED: HealthState.UNKNOWN,
}

_MISSION_BOUND_SOURCES = frozenset(
    {
        HealthSource.MISSION_STATE_STORE,
        HealthSource.GIT_RECONCILIATION,
        HealthSource.CODEX_RUNTIME,
        HealthSource.EXECUTION_LEDGER,
        HealthSource.REMEDIATION_STORE,
    }
)

_STATE_ORDER = {
    HealthState.HEALTHY: 0,
    HealthState.DEGRADED: 1,
    HealthState.UNKNOWN: 2,
    HealthState.BLOCKED: 3,
}


class HealthEvaluationEngine:
    """Evaluate operational capability without mutating or authorizing anything."""

    def evaluate(self, context: HealthEvaluationContext) -> HealthSnapshot:
        if not isinstance(context, HealthEvaluationContext):
            raise HealthEvaluationError("context must be HealthEvaluationContext")

        grouped: dict[HealthDimension, list[HealthObservation]] = defaultdict(list)
        for observation in context.observations:
            grouped[_SOURCE_DIMENSION[observation.source]].append(observation)

        results = {
            dimension: self._evaluate_dimension(dimension, grouped[dimension], context)
            for dimension in HealthDimension
        }
        metrics_state, metrics_reasons, metrics_reference, metrics_usable = (
            self._evaluate_metrics(context.metrics, context)
        )
        results[HealthDimension.OBSERVABILITY] = _merge_result(
            results[HealthDimension.OBSERVABILITY],
            metrics_state,
            metrics_reasons,
            metrics_reference,
        )
        foreign_observations = sorted(
            (
                item
                for item in context.observations
                if item.project_id != context.scope.project_id
            ),
            key=lambda item: (item.source.value, item.source_identity),
        )
        for observation in foreign_observations:
            results[HealthDimension.OBSERVABILITY] = _merge_result(
                results[HealthDimension.OBSERVABILITY],
                HealthState.UNKNOWN,
                (HealthReasonCode.WRONG_PROJECT,),
                _reference(observation),
            )
        if metrics_usable and context.mission_active and context.metrics is not None:
            self._apply_metric_signals(results, context.metrics, metrics_reference)

        dimensions = tuple(results[item] for item in HealthDimension)
        global_state, global_reason = _aggregate(dimensions)
        diagnostics = tuple(
            HealthDiagnostic(reason, result.dimension, _diagnostic_source(result, reason))
            for result in dimensions
            for reason in result.reasons
            if reason
            not in {
                HealthReasonCode.CONDITION_SATISFIED,
                HealthReasonCode.NO_ACTIVE_MISSION,
                HealthReasonCode.NO_PARALLEL_EXECUTION,
            }
        )
        identities = tuple(
            sorted(
                {
                    source.source_identity
                    for result in dimensions
                    for source in result.sources
                }
            )
        )
        fingerprint = _snapshot_fingerprint(
            context, global_state, dimensions, diagnostics, identities
        )
        return HealthSnapshot(
            schema_version=HEALTH_SCHEMA_VERSION,
            scope=context.scope,
            evaluated_at=context.evaluated_at,
            global_state=global_state,
            dimensions=dimensions,
            reasons=(global_reason,),
            source_identities=identities,
            diagnostics=diagnostics,
            fingerprint=fingerprint,
        )

    def _evaluate_dimension(
        self,
        dimension: HealthDimension,
        observations: list[HealthObservation],
        context: HealthEvaluationContext,
    ) -> HealthDimensionResult:
        requirement = _requirement(dimension, context)
        if requirement is DimensionRequirement.NOT_APPLICABLE:
            reason = (
                HealthReasonCode.NO_PARALLEL_EXECUTION
                if dimension is HealthDimension.GIT_WORKTREES
                else HealthReasonCode.NO_ACTIVE_MISSION
            )
            return HealthDimensionResult(
                dimension,
                requirement,
                None,
                context.scope,
                HealthFreshness.NOT_APPLICABLE,
                (reason,),
                (),
            )

        expected = _expected_sources(dimension, context)
        counts = Counter(item.source for item in observations)
        references = tuple(
            _reference(item)
            for item in sorted(
                observations,
                key=lambda item: (
                    item.source.value,
                    item.source_identity,
                    item.observed_at,
                    item.condition.value,
                ),
            )
        )
        reasons: list[HealthReasonCode] = []
        states: list[HealthState] = []
        freshness = HealthFreshness.FRESH

        for source in expected:
            matching = [item for item in observations if item.source is source]
            if not matching:
                states.append(HealthState.UNKNOWN)
                reasons.append(
                    HealthReasonCode.MISSING_REQUIRED_OBSERVATION
                    if requirement is DimensionRequirement.REQUIRED
                    else HealthReasonCode.MISSING_OPTIONAL_OBSERVATION
                )
                freshness = HealthFreshness.UNKNOWN
                continue
            if counts[source] > 1:
                states.append(HealthState.UNKNOWN)
                reasons.append(HealthReasonCode.DUPLICATE_SOURCE_OBSERVATION)
                freshness = HealthFreshness.UNKNOWN
                continue
            observation = matching[0]
            problem = _binding_or_freshness_problem(observation, context)
            if problem is not None:
                states.append(HealthState.UNKNOWN)
                reasons.append(problem)
                freshness = (
                    HealthFreshness.STALE
                    if problem
                    in {
                        HealthReasonCode.STALE_REPOSITORY_HEAD,
                        HealthReasonCode.STALE_GENERATION,
                        HealthReasonCode.STALE_OBSERVATION,
                        HealthReasonCode.FUTURE_OBSERVATION,
                    }
                    else HealthFreshness.UNKNOWN
                )
                continue
            state = _condition_state(source, observation.condition)
            states.append(state)
            reasons.append(_state_reason(state))

        state = max(states, key=lambda item: _STATE_ORDER[item])
        return HealthDimensionResult(
            dimension,
            requirement,
            state,
            context.scope,
            freshness,
            _unique_reasons(reasons),
            references,
        )

    def _evaluate_metrics(
        self,
        metrics: MetricsHealthInput | None,
        context: HealthEvaluationContext,
    ) -> tuple[
        HealthState,
        tuple[HealthReasonCode, ...],
        HealthSourceReference | None,
        bool,
    ]:
        if metrics is None:
            return HealthState.UNKNOWN, (HealthReasonCode.METRICS_MISSING,), None, False
        reference = HealthSourceReference(
            HealthSource.METRICS_SNAPSHOT,
            _metrics_identity(metrics),
            _metrics_condition(metrics),
            metrics.observed_at,
            metrics.repository_head,
        )
        freshness_problem = _metrics_binding_or_freshness_problem(metrics, context)
        if freshness_problem is not None:
            return HealthState.UNKNOWN, (freshness_problem,), reference, False
        if metrics.snapshot.status is MetricsSnapshotStatus.UNAVAILABLE:
            return (
                HealthState.UNKNOWN,
                (HealthReasonCode.METRICS_UNAVAILABLE,),
                reference,
                False,
            )
        if metrics.snapshot.status is MetricsSnapshotStatus.INCOMPLETE:
            return (
                HealthState.UNKNOWN,
                (HealthReasonCode.METRICS_INCOMPLETE,),
                reference,
                False,
            )
        return (
            HealthState.HEALTHY,
            (HealthReasonCode.CONDITION_SATISFIED,),
            reference,
            True,
        )

    def _apply_metric_signals(
        self,
        results: dict[HealthDimension, HealthDimensionResult],
        metrics: MetricsHealthInput,
        reference: HealthSourceReference | None,
    ) -> None:
        if reference is None:
            return
        persistence_failures = _metric_integer(
            metrics, MetricName.PERSISTENCE_FAILURES
        )
        codex_failures = sum(
            _metric_integer(metrics, name)
            for name in (
                MetricName.CODEX_EXECUTIONS_FAILED,
                MetricName.CODEX_EXECUTIONS_INTERRUPTED,
                MetricName.CODEX_TIMEOUTS,
            )
        )
        if persistence_failures > 0:
            results[HealthDimension.PERSISTENCE] = _degrade_with_metric(
                results[HealthDimension.PERSISTENCE],
                HealthReasonCode.PERSISTENCE_FAILURES_OBSERVED,
                reference,
            )
        if codex_failures > 0:
            results[HealthDimension.CODEX_RUNTIME] = _degrade_with_metric(
                results[HealthDimension.CODEX_RUNTIME],
                HealthReasonCode.CODEX_FAILURES_OBSERVED,
                reference,
            )


def _requirement(
    dimension: HealthDimension, context: HealthEvaluationContext
) -> DimensionRequirement:
    if dimension in {
        HealthDimension.AUTHORITATIVE_STATE_ACCESS,
        HealthDimension.OBSERVABILITY,
        HealthDimension.PERSISTENCE,
        HealthDimension.DEPLOYMENT_CONFIGURATION,
    }:
        return DimensionRequirement.REQUIRED
    if dimension is HealthDimension.CODEX_RUNTIME:
        return (
            DimensionRequirement.REQUIRED
            if context.mission_active
            else DimensionRequirement.OPTIONAL
        )
    if dimension is HealthDimension.GIT_WORKTREES:
        return (
            DimensionRequirement.REQUIRED
            if context.parallel_execution_active
            else DimensionRequirement.NOT_APPLICABLE
        )
    return (
        DimensionRequirement.REQUIRED
        if context.mission_active
        else DimensionRequirement.NOT_APPLICABLE
    )


def _expected_sources(
    dimension: HealthDimension, context: HealthEvaluationContext
) -> tuple[HealthSource, ...]:
    if dimension is HealthDimension.AUTHORITATIVE_STATE_ACCESS:
        return (
            (HealthSource.PROJECT_STATE_STORE, HealthSource.MISSION_STATE_STORE)
            if context.mission_active
            else (HealthSource.PROJECT_STATE_STORE,)
        )
    return {
        HealthDimension.OBSERVABILITY: (HealthSource.OPERATIONAL_EVENT_STORE,),
        HealthDimension.GIT_WORKTREES: (HealthSource.GIT_RECONCILIATION,),
        HealthDimension.CODEX_RUNTIME: (HealthSource.CODEX_RUNTIME,),
        HealthDimension.EXECUTION_RECOVERY: (HealthSource.EXECUTION_LEDGER,),
        HealthDimension.PERSISTENCE: (HealthSource.PERSISTENCE_DIAGNOSTIC,),
        HealthDimension.REMEDIATION_TRANSACTION: (HealthSource.REMEDIATION_STORE,),
        HealthDimension.DEPLOYMENT_CONFIGURATION: (HealthSource.PROJECT_CONFIGURATION,),
    }[dimension]


def _binding_or_freshness_problem(
    observation: HealthObservation, context: HealthEvaluationContext
) -> HealthReasonCode | None:
    if observation.project_id != context.scope.project_id:
        return HealthReasonCode.WRONG_PROJECT
    if observation.repository_head != context.scope.repository_head:
        return HealthReasonCode.STALE_REPOSITORY_HEAD
    if observation.source in _MISSION_BOUND_SOURCES and context.mission_active:
        if (
            observation.mission_id != context.scope.mission_id
            or observation.workflow_generation != context.scope.workflow_generation
        ):
            return HealthReasonCode.STALE_GENERATION
    elif observation.mission_id is not None and (
        observation.mission_id != context.scope.mission_id
        or observation.workflow_generation != context.scope.workflow_generation
    ):
        return HealthReasonCode.STALE_GENERATION
    age = context.evaluated_at - observation.observed_at
    if age.total_seconds() < 0:
        return HealthReasonCode.FUTURE_OBSERVATION
    if age > HEALTH_MAX_OBSERVATION_AGE:
        return HealthReasonCode.STALE_OBSERVATION
    return None


def _metrics_binding_or_freshness_problem(
    metrics: MetricsHealthInput, context: HealthEvaluationContext
) -> HealthReasonCode | None:
    scope = metrics.snapshot.scope
    if scope.project_id != context.scope.project_id:
        return HealthReasonCode.METRICS_SCOPE_MISMATCH
    if metrics.repository_head != context.scope.repository_head:
        return HealthReasonCode.METRICS_STALE_REPOSITORY_HEAD
    expected_mission = context.scope.mission_id if context.mission_active else None
    expected_generation = (
        context.scope.workflow_generation if context.mission_active else None
    )
    if (
        scope.mission_id != expected_mission
        or scope.workflow_generation != expected_generation
        or scope.user_story_id is not None
        or scope.role is not None
        or scope.execution_id is not None
    ):
        return HealthReasonCode.METRICS_SCOPE_MISMATCH
    age = context.evaluated_at - metrics.observed_at
    if age.total_seconds() < 0:
        return HealthReasonCode.METRICS_FUTURE
    if age > HEALTH_MAX_OBSERVATION_AGE:
        return HealthReasonCode.METRICS_STALE
    return None


def _condition_state(source: HealthSource, condition: HealthCondition) -> HealthState:
    state = _CONDITION_STATE[condition]
    if source is HealthSource.OPERATIONAL_EVENT_STORE and condition in {
        HealthCondition.UNAVAILABLE,
        HealthCondition.SATURATED,
    }:
        return HealthState.UNKNOWN
    if source is HealthSource.EXECUTION_LEDGER and condition is HealthCondition.UNAVAILABLE:
        return HealthState.UNKNOWN
    return state


def _state_reason(state: HealthState) -> HealthReasonCode:
    return {
        HealthState.HEALTHY: HealthReasonCode.CONDITION_SATISFIED,
        HealthState.DEGRADED: HealthReasonCode.SOURCE_DEGRADED,
        HealthState.BLOCKED: HealthReasonCode.SOURCE_BLOCKED,
        HealthState.UNKNOWN: HealthReasonCode.SOURCE_UNKNOWN,
    }[state]


def _reference(observation: HealthObservation) -> HealthSourceReference:
    return HealthSourceReference(
        observation.source,
        observation.source_identity,
        observation.condition,
        observation.observed_at,
        observation.repository_head,
    )


def _metrics_identity(metrics: MetricsHealthInput) -> str:
    if metrics.snapshot.source_fingerprint is not None:
        return metrics.snapshot.source_fingerprint
    payload = {
        "schema_version": metrics.snapshot.schema_version,
        "catalog_version": metrics.snapshot.catalog_version,
        "status": metrics.snapshot.status.value,
        "scope": repr(metrics.snapshot.scope),
        "diagnostics": [repr(item) for item in metrics.snapshot.diagnostics],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _metrics_condition(metrics: MetricsHealthInput) -> HealthCondition:
    return {
        MetricsSnapshotStatus.COMPLETE: HealthCondition.AVAILABLE,
        MetricsSnapshotStatus.INCOMPLETE: HealthCondition.DEGRADED,
        MetricsSnapshotStatus.UNAVAILABLE: HealthCondition.UNAVAILABLE,
    }[metrics.snapshot.status]


def _metric_integer(metrics: MetricsHealthInput, name: MetricName) -> int:
    sample = next(item for item in metrics.snapshot.metrics if item.name is name)
    if (
        sample.availability is not MetricAvailability.AVAILABLE
        or not isinstance(sample.value, int)
        or isinstance(sample.value, bool)
    ):
        raise HealthEvaluationError(f"required metric is unavailable: {name.value}")
    return sample.value


def _merge_result(
    result: HealthDimensionResult,
    state: HealthState,
    reasons: tuple[HealthReasonCode, ...],
    reference: HealthSourceReference | None,
) -> HealthDimensionResult:
    merged_state = max((result.state, state), key=lambda item: _STATE_ORDER[item])
    freshness = result.freshness
    if any(
        reason
        in {
            HealthReasonCode.STALE_OBSERVATION,
            HealthReasonCode.FUTURE_OBSERVATION,
            HealthReasonCode.STALE_REPOSITORY_HEAD,
            HealthReasonCode.METRICS_STALE,
            HealthReasonCode.METRICS_FUTURE,
            HealthReasonCode.METRICS_STALE_REPOSITORY_HEAD,
        }
        for reason in reasons
    ):
        freshness = HealthFreshness.STALE
    elif HealthReasonCode.METRICS_MISSING in reasons:
        freshness = HealthFreshness.UNKNOWN
    sources = result.sources + ((reference,) if reference is not None else ())
    return HealthDimensionResult(
        result.dimension,
        result.requirement,
        merged_state,
        result.scope,
        freshness,
        _unique_reasons((*result.reasons, *reasons)),
        sources,
    )


def _degrade_with_metric(
    result: HealthDimensionResult,
    reason: HealthReasonCode,
    reference: HealthSourceReference,
) -> HealthDimensionResult:
    if result.requirement is DimensionRequirement.NOT_APPLICABLE:
        return result
    state = result.state
    if state is HealthState.HEALTHY:
        state = HealthState.DEGRADED
    return HealthDimensionResult(
        result.dimension,
        result.requirement,
        state,
        result.scope,
        result.freshness,
        _unique_reasons((*result.reasons, reason)),
        result.sources + (reference,),
    )


def _aggregate(
    dimensions: tuple[HealthDimensionResult, ...],
) -> tuple[HealthState, HealthReasonCode]:
    required = tuple(
        item.state
        for item in dimensions
        if item.requirement is DimensionRequirement.REQUIRED
    )
    optional = tuple(
        item.state
        for item in dimensions
        if item.requirement is DimensionRequirement.OPTIONAL
    )
    if HealthState.BLOCKED in required:
        return HealthState.BLOCKED, HealthReasonCode.REQUIRED_DIMENSION_BLOCKED
    if HealthState.UNKNOWN in required:
        return HealthState.UNKNOWN, HealthReasonCode.REQUIRED_DIMENSION_UNKNOWN
    if HealthState.DEGRADED in required:
        return HealthState.DEGRADED, HealthReasonCode.REQUIRED_DIMENSION_DEGRADED
    if any(item in {HealthState.BLOCKED, HealthState.DEGRADED} for item in optional):
        return HealthState.DEGRADED, HealthReasonCode.OPTIONAL_DIMENSION_IMPAIRED
    return HealthState.HEALTHY, HealthReasonCode.ALL_REQUIRED_DIMENSIONS_HEALTHY


def _diagnostic_source(
    result: HealthDimensionResult, reason: HealthReasonCode
) -> HealthSource | None:
    if reason in {
        HealthReasonCode.METRICS_MISSING,
        HealthReasonCode.METRICS_INCOMPLETE,
        HealthReasonCode.METRICS_UNAVAILABLE,
        HealthReasonCode.METRICS_SCOPE_MISMATCH,
        HealthReasonCode.METRICS_STALE_REPOSITORY_HEAD,
        HealthReasonCode.METRICS_STALE,
        HealthReasonCode.METRICS_FUTURE,
        HealthReasonCode.PERSISTENCE_FAILURES_OBSERVED,
        HealthReasonCode.CODEX_FAILURES_OBSERVED,
    }:
        return HealthSource.METRICS_SNAPSHOT
    return result.sources[0].source if len(result.sources) == 1 else None


def _unique_reasons(reasons: tuple[HealthReasonCode, ...] | list[HealthReasonCode]) -> tuple[HealthReasonCode, ...]:
    return tuple(sorted(set(reasons), key=lambda item: item.value))


def _snapshot_fingerprint(
    context: HealthEvaluationContext,
    state: HealthState,
    dimensions: tuple[HealthDimensionResult, ...],
    diagnostics: tuple[HealthDiagnostic, ...],
    identities: tuple[str, ...],
) -> str:
    payload = {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "scope": {
            "project_id": context.scope.project_id,
            "repository_head": context.scope.repository_head,
            "mission_id": context.scope.mission_id,
            "workflow_generation": context.scope.workflow_generation,
        },
        "evaluated_at": context.evaluated_at.isoformat().replace("+00:00", "Z"),
        "global_state": state.value,
        "dimensions": [
            {
                "dimension": item.dimension.value,
                "requirement": item.requirement.value,
                "state": item.state.value if item.state is not None else None,
                "freshness": item.freshness.value,
                "reasons": [reason.value for reason in item.reasons],
                "sources": [
                    {
                        "source": source.source.value,
                        "source_identity": source.source_identity,
                        "condition": source.condition.value,
                        "observed_at": source.observed_at.isoformat().replace(
                            "+00:00", "Z"
                        ),
                        "repository_head": source.repository_head,
                    }
                    for source in item.sources
                ],
            }
            for item in dimensions
        ],
        "diagnostics": [
            {
                "reason": item.reason.value,
                "dimension": item.dimension.value if item.dimension is not None else None,
                "source": item.source.value if item.source is not None else None,
            }
            for item in diagnostics
        ],
        "source_identities": identities,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
