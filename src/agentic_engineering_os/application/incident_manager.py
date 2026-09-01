"""Deterministic incident detection and Human-attributable lifecycle transitions."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from agentic_engineering_os.domain import (
    INCIDENT_MAX_OBSERVATION_AGE,
    INCIDENT_SCHEMA_VERSION,
    GovernanceDecision,
    HealthDimension,
    HealthState,
    IncidentAssessment,
    IncidentClassification,
    IncidentCorrelation,
    IncidentDiagnosticCondition,
    IncidentEscalation,
    IncidentEvaluationContext,
    IncidentOperatorAcknowledgement,
    IncidentReason,
    IncidentRecord,
    IncidentResolutionObservation,
    IncidentSeverity,
    IncidentState,
    MetricsSnapshotStatus,
    OperationalEvent,
    OperationalEventType,
    ResourceBudgetDecision,
    ResourceBudgetDomain,
    derive_incident_id,
    incident_assessment_fingerprint,
    incident_severity,
)


class IncidentManagementError(ValueError):
    """Incident inputs cannot prove a safe deterministic result."""


@dataclass(frozen=True, slots=True)
class _Signal:
    classification: IncidentClassification
    correlation: IncidentCorrelation
    sources: tuple[str, ...]
    reasons: tuple[IncidentReason, ...]


class IncidentManager:
    """Detect and classify incidents without executing remediation or authority."""

    def evaluate(self, context: IncidentEvaluationContext) -> IncidentAssessment:
        if not isinstance(context, IncidentEvaluationContext):
            raise IncidentManagementError("context must be IncidentEvaluationContext")
        prior = {item.incident_id: item for item in context.prior_records}
        for record in prior.values():
            if record.scope != context.scope:
                raise IncidentManagementError("prior incident belongs to another exact scope")
        records: dict[str, IncidentRecord] = {}
        deduplicated: set[str] = set()
        for signal in self._signals(context):
            incident_id = derive_incident_id(
                context.scope.project_id, signal.classification, signal.correlation
            )
            previous = prior.get(incident_id)
            if previous is None:
                records[incident_id] = _new_record(context, signal, incident_id)
            elif previous.state is IncidentState.RESOLVED:
                records[incident_id] = _reopen_record(context, signal, previous)
            else:
                records[incident_id] = previous
                deduplicated.add(incident_id)
        for incident_id, record in prior.items():
            if record.state is not IncidentState.RESOLVED and incident_id not in records:
                records[incident_id] = record
        ordered = tuple(sorted(records.values(), key=lambda item: item.incident_id))
        dedup = tuple(sorted(deduplicated))
        fingerprint = incident_assessment_fingerprint(
            context.scope, context.evaluated_at, ordered, dedup
        )
        return IncidentAssessment(
            INCIDENT_SCHEMA_VERSION,
            context.scope,
            context.evaluated_at,
            ordered,
            dedup,
            fingerprint,
        )

    def acknowledge(
        self, record: IncidentRecord, acknowledgement: IncidentOperatorAcknowledgement
    ) -> IncidentRecord:
        if not isinstance(record, IncidentRecord) or not isinstance(
            acknowledgement, IncidentOperatorAcknowledgement
        ):
            raise IncidentManagementError("typed incident acknowledgement is required")
        if record.state is IncidentState.RESOLVED:
            raise IncidentManagementError("resolved incident cannot be acknowledged")
        if acknowledgement.incident_id != record.incident_id or acknowledgement.scope != record.scope:
            raise IncidentManagementError("acknowledgement is bound to another incident or scope")
        if acknowledgement.occurred_at < record.updated_at:
            raise IncidentManagementError("acknowledgement predates current incident revision")
        escalation = (
            IncidentEscalation.EMERGENCY_BLOCK_RECOMMENDED
            if record.severity is IncidentSeverity.CRITICAL
            else IncidentEscalation.OPERATOR_ACTION_REQUIRED
        )
        return IncidentRecord.create(
            incident_id=record.incident_id,
            revision=record.revision + 1,
            classification=record.classification,
            severity=record.severity,
            state=IncidentState.ESCALATED,
            escalation=escalation,
            scope=record.scope,
            correlation=record.correlation,
            opened_at=record.opened_at,
            updated_at=acknowledgement.occurred_at,
            occurrence_count=record.occurrence_count,
            reopen_count=record.reopen_count,
            reasons=record.reasons,
            source_identities=record.source_identities,
            acknowledged_by=acknowledgement.operator_identity,
            resolution_source_identity=None,
            previous_fingerprint=record.fingerprint,
        )

    def resolve(
        self,
        record: IncidentRecord,
        context: IncidentEvaluationContext,
        resolution: IncidentResolutionObservation,
    ) -> IncidentRecord:
        if not isinstance(record, IncidentRecord) or not isinstance(
            context, IncidentEvaluationContext
        ) or not isinstance(resolution, IncidentResolutionObservation):
            raise IncidentManagementError("typed incident resolution inputs are required")
        if record.state is IncidentState.RESOLVED:
            raise IncidentManagementError("incident is already resolved")
        if record.scope != context.scope or resolution.scope != context.scope or resolution.incident_id != record.incident_id:
            raise IncidentManagementError("resolution is bound to another incident or context")
        if resolution.observed_at < record.updated_at or resolution.observed_at != context.evaluated_at:
            raise IncidentManagementError("resolution timestamp must match current evaluation")
        normalized = tuple(
            item
            for item in context.diagnostics
            if item.classification is record.classification
            and item.correlation == record.correlation
            and item.condition is IncidentDiagnosticCondition.NORMALIZED
            and item.source_identity == resolution.normalized_source_identity
            and _source_current(context, item.scope, item.observed_at)
        )
        if len(normalized) != 1:
            raise IncidentManagementError("exact factual normalization observation is required")
        current_signals = self._signals(context)
        if any(
            item.classification is IncidentClassification.UNKNOWN_CRITICAL_STATE
            for item in current_signals
        ):
            raise IncidentManagementError(
                "critical source uncertainty prevents incident resolution"
            )
        active_ids = {
            derive_incident_id(context.scope.project_id, item.classification, item.correlation)
            for item in current_signals
        }
        if record.incident_id in active_ids:
            raise IncidentManagementError("incident condition remains active")
        sources = _bounded_sources((*record.source_identities, resolution.normalized_source_identity))
        return IncidentRecord.create(
            incident_id=record.incident_id,
            revision=record.revision + 1,
            classification=record.classification,
            severity=record.severity,
            state=IncidentState.RESOLVED,
            escalation=IncidentEscalation.OBSERVE_ONLY,
            scope=record.scope,
            correlation=record.correlation,
            opened_at=record.opened_at,
            updated_at=resolution.observed_at,
            occurrence_count=record.occurrence_count,
            reopen_count=record.reopen_count,
            reasons=record.reasons,
            source_identities=sources,
            acknowledged_by=resolution.operator_identity,
            resolution_source_identity=resolution.normalized_source_identity,
            previous_fingerprint=record.fingerprint,
        )

    def _signals(self, context: IncidentEvaluationContext) -> tuple[_Signal, ...]:
        signals: list[_Signal] = []
        base = IncidentCorrelation(context.scope.mission_id, context.scope.workflow_generation)
        if context.health is None:
            signals.append(_unknown(base, "missing-health", IncidentReason.CRITICAL_SOURCE_MISSING))
        elif not _health_current(context):
            signals.append(_unknown(base, context.health.fingerprint, _scope_or_stale_health(context)))
        else:
            signals.extend(_health_signals(context))
        if context.governance is None:
            signals.append(_unknown(base, "missing-governance", IncidentReason.CRITICAL_SOURCE_MISSING))
        elif not _governance_current(context):
            signals.append(_unknown(base, context.governance.fingerprint, _scope_or_stale_governance(context)))
        elif context.governance.decision is GovernanceDecision.BLOCK:
            signals.append(_Signal(IncidentClassification.POLICY_BLOCK, IncidentCorrelation(context.scope.mission_id, context.scope.workflow_generation, operation=context.governance.operation), (context.governance.fingerprint,), (IncidentReason.GOVERNANCE_BLOCK,)))
        if context.resource_budgets is None:
            signals.append(_unknown(base, "missing-budgets", IncidentReason.CRITICAL_SOURCE_MISSING))
        elif not _budgets_current(context):
            signals.append(_unknown(base, context.resource_budgets.fingerprint, _scope_or_stale_budgets(context)))
        else:
            for decision in context.resource_budgets.decisions:
                correlation = IncidentCorrelation(context.scope.mission_id, context.scope.workflow_generation, operation=context.resource_budgets.operation, resource_domain=decision.domain)
                if decision.decision is ResourceBudgetDecision.UNKNOWN:
                    signals.append(_unknown(correlation, context.resource_budgets.fingerprint, IncidentReason.SOURCE_INCOMPLETE))
                elif decision.decision is ResourceBudgetDecision.LIMIT_EXCEEDED:
                    classification = IncidentClassification.REMEDIATION_LOOP if decision.domain is ResourceBudgetDomain.REMEDIATION_GENERATIONS else IncidentClassification.RESOURCE_EXHAUSTION
                    signals.append(_Signal(classification, correlation, (context.resource_budgets.fingerprint,), (IncidentReason.BUDGET_EXCEEDED,)))
        if context.metrics is not None:
            if not _metrics_current(context) or context.metrics.snapshot.status is not MetricsSnapshotStatus.COMPLETE:
                signals.append(_Signal(IncidentClassification.OBSERVABILITY_LOSS, base, (_metrics_identity(context),), (IncidentReason.METRICS_UNAVAILABLE,)))
        if not context.events_complete:
            signals.append(_unknown(base, "events-incomplete", IncidentReason.SOURCE_INCOMPLETE))
        else:
            signals.extend(_event_signals(context))
        if not context.diagnostics_complete:
            signals.append(_unknown(base, "diagnostics-incomplete", IncidentReason.SOURCE_INCOMPLETE))
        for item in context.diagnostics:
            if not _source_current(context, item.scope, item.observed_at):
                signals.append(_unknown(base, item.source_identity, IncidentReason.SOURCE_SCOPE_MISMATCH if item.scope != context.scope else IncidentReason.SOURCE_STALE))
            elif item.condition is IncidentDiagnosticCondition.ACTIVE:
                signals.append(_Signal(item.classification, item.correlation, (item.source_identity,), (IncidentReason.DIAGNOSTIC_ACTIVE,)))
            elif item.condition is IncidentDiagnosticCondition.UNKNOWN:
                signals.append(_unknown(item.correlation, item.source_identity, IncidentReason.SOURCE_INCOMPLETE))
        return _merge_signals(context, signals)


def _new_record(context: IncidentEvaluationContext, signal: _Signal, incident_id: str) -> IncidentRecord:
    severity = incident_severity(signal.classification)
    state = IncidentState.ESCALATED if severity is IncidentSeverity.CRITICAL else IncidentState.ACKNOWLEDGEMENT_REQUIRED
    escalation = IncidentEscalation.EMERGENCY_BLOCK_RECOMMENDED if severity is IncidentSeverity.CRITICAL else IncidentEscalation.OPERATOR_ACK_REQUIRED
    return IncidentRecord.create(incident_id=incident_id, revision=1, classification=signal.classification, severity=severity, state=state, escalation=escalation, scope=context.scope, correlation=signal.correlation, opened_at=context.evaluated_at, updated_at=context.evaluated_at, occurrence_count=1, reopen_count=0, reasons=signal.reasons, source_identities=signal.sources, acknowledged_by=None, resolution_source_identity=None, previous_fingerprint=None)


def _reopen_record(context: IncidentEvaluationContext, signal: _Signal, previous: IncidentRecord) -> IncidentRecord:
    severity = previous.severity
    state = IncidentState.ESCALATED if severity is IncidentSeverity.CRITICAL else IncidentState.ACKNOWLEDGEMENT_REQUIRED
    escalation = IncidentEscalation.EMERGENCY_BLOCK_RECOMMENDED if severity is IncidentSeverity.CRITICAL else IncidentEscalation.OPERATOR_ACK_REQUIRED
    return IncidentRecord.create(incident_id=previous.incident_id, revision=previous.revision + 1, classification=previous.classification, severity=severity, state=state, escalation=escalation, scope=context.scope, correlation=previous.correlation, opened_at=previous.opened_at, updated_at=context.evaluated_at, occurrence_count=previous.occurrence_count + 1, reopen_count=previous.reopen_count + 1, reasons=signal.reasons, source_identities=_bounded_sources((*previous.source_identities, *signal.sources)), acknowledged_by=None, resolution_source_identity=None, previous_fingerprint=previous.fingerprint)


def _health_signals(context: IncidentEvaluationContext) -> list[_Signal]:
    assert context.health is not None
    mapping = {
        HealthDimension.PERSISTENCE: IncidentClassification.PERSISTENCE_FAILURE,
        HealthDimension.EXECUTION_RECOVERY: IncidentClassification.RECOVERY_STUCK,
        HealthDimension.REMEDIATION_TRANSACTION: IncidentClassification.REMEDIATION_LOOP,
        HealthDimension.GIT_WORKTREES: IncidentClassification.GIT_WORKTREE_DIVERGENCE,
        HealthDimension.CODEX_RUNTIME: IncidentClassification.CODEX_RUNTIME_FAILURE,
        HealthDimension.OBSERVABILITY: IncidentClassification.OBSERVABILITY_LOSS,
    }
    signals: list[_Signal] = []
    correlation = IncidentCorrelation(context.scope.mission_id, context.scope.workflow_generation)
    for dimension in context.health.dimensions:
        if dimension.dimension not in mapping or dimension.state in {None, HealthState.HEALTHY}:
            continue
        if dimension.state is HealthState.UNKNOWN:
            signals.append(_unknown(correlation, context.health.fingerprint, IncidentReason.SOURCE_INCOMPLETE))
        elif dimension.state in {HealthState.BLOCKED, HealthState.DEGRADED}:
            signals.append(_Signal(mapping[dimension.dimension], correlation, (context.health.fingerprint,), (IncidentReason.HEALTH_CONDITION,)))
    return signals


def _event_signals(context: IncidentEvaluationContext) -> list[_Signal]:
    signals: list[_Signal] = []
    seen: set[str] = set()
    for event in context.operational_events:
        correlation = IncidentCorrelation(
            event.correlation.mission_id,
            event.correlation.workflow_generation,
            event.correlation.user_story_id,
            event.correlation.role,
            event.correlation.execution_id,
            event.correlation.assignment_id,
        )
        if event.event_id in seen or not _event_current(context, event):
            reason = IncidentReason.SOURCE_INCOMPLETE if event.event_id in seen else (IncidentReason.SOURCE_SCOPE_MISMATCH if event.project_id != context.scope.project_id else IncidentReason.SOURCE_STALE)
            signals.append(_unknown(IncidentCorrelation(context.scope.mission_id, context.scope.workflow_generation), event.event_id, reason))
            seen.add(event.event_id)
            continue
        seen.add(event.event_id)
        classification = None
        if event.event_type is OperationalEventType.PERSISTENCE_FAILURE:
            classification = IncidentClassification.PERSISTENCE_FAILURE
        elif event.event_type is OperationalEventType.WORKTREE_LIFECYCLE and event.payload.operation == "DIVERGENCE_OBSERVED":
            classification = IncidentClassification.GIT_WORKTREE_DIVERGENCE
        elif event.event_type is OperationalEventType.CODEX_EXECUTION and event.payload.operation in {"FAILED", "INTERRUPTED"}:
            classification = IncidentClassification.CODEX_RUNTIME_FAILURE
        elif event.event_type is OperationalEventType.REMEDIATION_RECOVERY and event.payload.operation in {"FAILED", "RECOVERY_REQUIRED"}:
            classification = IncidentClassification.RECOVERY_STUCK
        elif event.event_type is OperationalEventType.OPERATIONAL_ANOMALY and event.payload.operation == "DETECTED" and not any(item.name == "incident_record" for item in event.payload.attributes):
            classification = IncidentClassification.UNKNOWN_CRITICAL_STATE
        if classification is not None:
            signals.append(_Signal(classification, correlation, (event.event_id,), (IncidentReason.OPERATIONAL_EVENT,)))
    return signals


def _merge_signals(context: IncidentEvaluationContext, signals: list[_Signal]) -> tuple[_Signal, ...]:
    merged: dict[str, _Signal] = {}
    for signal in signals:
        key = derive_incident_id(context.scope.project_id, signal.classification, signal.correlation)
        previous = merged.get(key)
        if previous is None:
            merged[key] = signal
        else:
            merged[key] = _Signal(signal.classification, signal.correlation, _bounded_sources((*previous.sources, *signal.sources)), tuple(sorted(set((*previous.reasons, *signal.reasons)), key=lambda item: item.value)))
    return tuple(merged[key] for key in sorted(merged))


def _unknown(correlation: IncidentCorrelation, source: str, reason: IncidentReason) -> _Signal:
    return _Signal(IncidentClassification.UNKNOWN_CRITICAL_STATE, correlation, (source,), (reason,))


def _bounded_sources(values: tuple[str, ...]) -> tuple[str, ...]:
    ordered = tuple(sorted(set(values)))
    if len(ordered) <= 8:
        return ordered
    digest = hashlib.sha256("\n".join(ordered).encode("utf-8")).hexdigest()
    return (*ordered[:7], f"aggregate-{digest[:32]}")


def _source_current(context: IncidentEvaluationContext, scope, observed_at) -> bool:
    age = context.evaluated_at - observed_at
    return scope == context.scope and age.total_seconds() >= 0 and age <= INCIDENT_MAX_OBSERVATION_AGE


def _health_current(context: IncidentEvaluationContext) -> bool:
    assert context.health is not None
    scope = context.health.scope
    return _base_scope_matches(context, scope.project_id, scope.repository_head, scope.mission_id, scope.workflow_generation) and _fresh(context, context.health.evaluated_at)


def _governance_current(context: IncidentEvaluationContext) -> bool:
    assert context.governance is not None
    scope = context.governance.scope
    return _base_scope_matches(context, scope.project_id, scope.repository_head, scope.mission_id, scope.workflow_generation) and _fresh(context, context.governance.evaluated_at)


def _budgets_current(context: IncidentEvaluationContext) -> bool:
    assert context.resource_budgets is not None
    scope = context.resource_budgets.scope
    return _base_scope_matches(context, scope.project_id, scope.repository_head, scope.mission_id, scope.workflow_generation) and _fresh(context, context.resource_budgets.evaluated_at)


def _metrics_current(context: IncidentEvaluationContext) -> bool:
    assert context.metrics is not None
    scope = context.metrics.snapshot.scope
    return _base_scope_matches(context, scope.project_id, context.metrics.repository_head, scope.mission_id, scope.workflow_generation) and _fresh(context, context.metrics.observed_at)


def _event_current(context: IncidentEvaluationContext, event: OperationalEvent) -> bool:
    corr = event.correlation
    return _base_scope_matches(context, event.project_id, corr.repository_commit or "", corr.mission_id, corr.workflow_generation) and _fresh(context, event.occurred_at)


def _base_scope_matches(context: IncidentEvaluationContext, project_id: str, head: str, mission_id: str | None, generation: int | None) -> bool:
    return (project_id, head, mission_id, generation) == (context.scope.project_id, context.scope.repository_head, context.scope.mission_id, context.scope.workflow_generation)


def _fresh(context: IncidentEvaluationContext, observed_at) -> bool:
    age = context.evaluated_at - observed_at
    return age.total_seconds() >= 0 and age <= INCIDENT_MAX_OBSERVATION_AGE


def _scope_or_stale_health(context):
    assert context.health is not None
    return IncidentReason.SOURCE_STALE if _fresh(context, context.health.evaluated_at) is False else IncidentReason.SOURCE_SCOPE_MISMATCH


def _scope_or_stale_governance(context):
    assert context.governance is not None
    return IncidentReason.SOURCE_STALE if _fresh(context, context.governance.evaluated_at) is False else IncidentReason.SOURCE_SCOPE_MISMATCH


def _scope_or_stale_budgets(context):
    assert context.resource_budgets is not None
    return IncidentReason.SOURCE_STALE if _fresh(context, context.resource_budgets.evaluated_at) is False else IncidentReason.SOURCE_SCOPE_MISMATCH


def _metrics_identity(context: IncidentEvaluationContext) -> str:
    assert context.metrics is not None
    return context.metrics.snapshot.source_fingerprint or hashlib.sha256(repr(context.metrics.snapshot).encode("utf-8")).hexdigest()
