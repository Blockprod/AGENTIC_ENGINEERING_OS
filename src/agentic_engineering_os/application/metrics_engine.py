"""Deterministic derivation of bounded metrics from OperationalEvents."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from agentic_engineering_os.domain import (
    MAX_METRIC_DIMENSION_CARDINALITY,
    MAX_METRIC_SOURCE_EVENTS,
    METRIC_CATALOG,
    METRIC_CATALOG_VERSION,
    METRICS_SCHEMA_VERSION,
    DurationSummary,
    MetricAvailability,
    MetricName,
    MetricSample,
    MetricsDiagnostic,
    MetricsDiagnosticCode,
    MetricsScope,
    MetricsSnapshot,
    MetricsSnapshotStatus,
    OperationalEvent,
    OperationalEventType,
    operational_event_fingerprint,
)


class OperationalEventReader(Protocol):
    def read(self) -> tuple[OperationalEvent, ...]: ...


class MetricsComputationError(ValueError):
    """The caller supplied an invalid metrics request rather than observations."""


@dataclass(frozen=True, slots=True)
class _LifecycleResult:
    durations: tuple[int, ...]
    active_count: int
    source_event_count: int
    diagnostics: tuple[MetricsDiagnostic, ...]


_COUNTER_RULES = {
    MetricName.MISSIONS_STARTED: (OperationalEventType.MISSION_LIFECYCLE, {"STARTED"}),
    MetricName.MISSIONS_COMPLETED: (OperationalEventType.MISSION_LIFECYCLE, {"FINISHED"}),
    MetricName.MISSIONS_BLOCKED: (OperationalEventType.MISSION_LIFECYCLE, {"BLOCKED"}),
    MetricName.ROLE_EXECUTIONS_STARTED: (OperationalEventType.ROLE_EXECUTION, {"STARTED"}),
    MetricName.ROLE_EXECUTIONS_COMPLETED: (OperationalEventType.ROLE_EXECUTION, {"FINISHED"}),
    MetricName.ROLE_EXECUTIONS_FAILED: (OperationalEventType.ROLE_EXECUTION, {"FAILED"}),
    MetricName.CODEX_EXECUTIONS_STARTED: (OperationalEventType.CODEX_EXECUTION, {"STARTED"}),
    MetricName.CODEX_EXECUTIONS_COMPLETED: (OperationalEventType.CODEX_EXECUTION, {"FINISHED"}),
    MetricName.CODEX_EXECUTIONS_FAILED: (OperationalEventType.CODEX_EXECUTION, {"FAILED"}),
    MetricName.CODEX_EXECUTIONS_INTERRUPTED: (
        OperationalEventType.CODEX_EXECUTION,
        {"INTERRUPTED"},
    ),
    MetricName.REMEDIATIONS_REQUESTED: (
        OperationalEventType.REMEDIATION_RECOVERY,
        {"REQUESTED"},
    ),
    MetricName.HUMAN_WAITS_STARTED: (
        OperationalEventType.HUMAN_WAITING,
        {"WAITING_STARTED"},
    ),
    MetricName.WORKTREES_CREATED: (OperationalEventType.WORKTREE_LIFECYCLE, {"CREATED"}),
    MetricName.WORKTREES_COMPLETED: (
        OperationalEventType.WORKTREE_LIFECYCLE,
        {"COMPLETED"},
    ),
    MetricName.WORKTREES_FAILED: (OperationalEventType.WORKTREE_LIFECYCLE, {"FAILED"}),
    MetricName.MERGES_SUCCEEDED: (OperationalEventType.MERGE_OPERATION, {"FINISHED"}),
    MetricName.MERGES_FAILED: (
        OperationalEventType.MERGE_OPERATION,
        {"FAILED", "CONFLICT_OBSERVED"},
    ),
    MetricName.PERSISTENCE_FAILURES: (
        OperationalEventType.PERSISTENCE_FAILURE,
        {"READ_FAILED", "WRITE_FAILED", "CORRUPTION_OBSERVED"},
    ),
    MetricName.ADOPTIONS_SUCCEEDED: (OperationalEventType.ADOPTION_MIGRATION, {"FINISHED"}),
    MetricName.ADOPTIONS_FAILED: (OperationalEventType.ADOPTION_MIGRATION, {"FAILED"}),
    MetricName.ADOPTIONS_REFUSED: (OperationalEventType.ADOPTION_MIGRATION, {"REFUSED"}),
}


class MetricsEngine:
    """Pure metrics computation; it owns no persistence or control authority."""

    def compute(
        self,
        events: Sequence[OperationalEvent],
        scope: MetricsScope,
        *,
        source_complete: bool,
    ) -> MetricsSnapshot:
        if not isinstance(scope, MetricsScope):
            raise MetricsComputationError("scope must be a MetricsScope")
        if not isinstance(source_complete, bool):
            raise MetricsComputationError("source_complete must be explicit boolean")
        if not isinstance(events, (tuple, list)):
            raise MetricsComputationError("events must be a bounded tuple or list")
        if not source_complete:
            return _empty_snapshot(
                scope,
                MetricsSnapshotStatus.INCOMPLETE,
                MetricsDiagnosticCode.INCOMPLETE_SOURCE,
            )
        validation = _validate_corpus(events, scope)
        if validation is not None:
            return _empty_snapshot(
                scope,
                MetricsSnapshotStatus.INCOMPLETE,
                validation.code,
                validation.event_ids,
            )
        ordered = tuple(sorted(events, key=lambda item: (item.occurred_at, item.event_id)))
        selected = tuple(item for item in ordered if _matches_scope(item, scope))
        return _compute_snapshot(selected, scope)

    def compute_from_store(
        self,
        event_store: OperationalEventReader,
        scope: MetricsScope,
    ) -> MetricsSnapshot:
        if not isinstance(scope, MetricsScope):
            raise MetricsComputationError("scope must be a MetricsScope")
        reader = getattr(event_store, "read", None)
        if not callable(reader):
            raise MetricsComputationError("event_store must expose read()")
        try:
            events = reader()
        except Exception:
            return _empty_snapshot(
                scope,
                MetricsSnapshotStatus.UNAVAILABLE,
                MetricsDiagnosticCode.EVENT_SOURCE_UNAVAILABLE,
            )
        if not isinstance(events, tuple):
            return _empty_snapshot(
                scope,
                MetricsSnapshotStatus.UNAVAILABLE,
                MetricsDiagnosticCode.EVENT_SOURCE_UNAVAILABLE,
            )
        retention_probe = getattr(event_store, "retention_exhausted", None)
        if callable(retention_probe):
            try:
                if retention_probe():
                    return _empty_snapshot(
                        scope,
                        MetricsSnapshotStatus.INCOMPLETE,
                        MetricsDiagnosticCode.EVENT_SOURCE_SATURATED,
                    )
            except Exception:
                return _empty_snapshot(
                    scope,
                    MetricsSnapshotStatus.UNAVAILABLE,
                    MetricsDiagnosticCode.EVENT_SOURCE_UNAVAILABLE,
                )
        try:
            return self.compute(events, scope, source_complete=True)
        except MetricsComputationError:
            return _empty_snapshot(
                scope,
                MetricsSnapshotStatus.UNAVAILABLE,
                MetricsDiagnosticCode.EVENT_SOURCE_UNAVAILABLE,
            )


def _compute_snapshot(
    events: tuple[OperationalEvent, ...], scope: MetricsScope
) -> MetricsSnapshot:
    samples: dict[MetricName, MetricSample] = {}
    diagnostics: list[MetricsDiagnostic] = []

    for name, (event_type, operations) in _COUNTER_RULES.items():
        matches = tuple(
            item
            for item in events
            if item.event_type is event_type and item.payload.operation in operations
        )
        samples[name] = _sample(name, scope, len(matches), len(matches))

    timeout_events = tuple(
        item
        for item in events
        if item.event_type is OperationalEventType.CODEX_EXECUTION
        and item.payload.operation in {"FAILED", "INTERRUPTED"}
        and item.payload.reason_code == "TIMEOUT"
    )
    samples[MetricName.CODEX_TIMEOUTS] = _sample(
        MetricName.CODEX_TIMEOUTS, scope, len(timeout_events), len(timeout_events)
    )

    recovery_events = tuple(
        item
        for item in events
        if (
            item.event_type is OperationalEventType.REMEDIATION_RECOVERY
            and item.payload.operation == "RECOVERY_REQUIRED"
        )
        or (
            item.event_type is OperationalEventType.CODEX_EXECUTION
            and item.payload.operation == "RECOVERY_INSPECTED"
        )
    )
    samples[MetricName.RECOVERIES_OBSERVED] = _sample(
        MetricName.RECOVERIES_OBSERVED,
        scope,
        len(recovery_events),
        len(recovery_events),
    )

    gate_events = tuple(
        item for item in events if item.event_type is OperationalEventType.INTEGRATION_GATE
    )
    invalid_gates = tuple(
        item for item in gate_events if item.payload.outcome not in {"PASS", "FAIL", "UNKNOWN"}
    )
    for outcome, name in (
        ("PASS", MetricName.INTEGRATION_GATES_PASS),
        ("FAIL", MetricName.INTEGRATION_GATES_FAIL),
        ("UNKNOWN", MetricName.INTEGRATION_GATES_UNKNOWN),
    ):
        matches = tuple(item for item in gate_events if item.payload.outcome == outcome)
        if invalid_gates:
            samples[name] = _unavailable_sample(
                name,
                scope,
                len(gate_events),
                (MetricsDiagnosticCode.UNCLASSIFIED_OUTCOME,),
            )
        else:
            samples[name] = _sample(name, scope, len(matches), len(matches))
    if invalid_gates:
        diagnostics.append(
            MetricsDiagnostic(
                MetricsDiagnosticCode.UNCLASSIFIED_OUTCOME,
                MetricName.INTEGRATION_GATES_UNKNOWN,
                tuple(item.event_id for item in invalid_gates[:4]),
            )
        )

    role = _lifecycle(
        events,
        scope,
        MetricName.ROLE_EXECUTION_DURATION,
        OperationalEventType.ROLE_EXECUTION,
        "STARTED",
        {"FINISHED", "FAILED"},
        _role_key,
    )
    codex = _lifecycle(
        events,
        scope,
        MetricName.CODEX_EXECUTION_DURATION,
        OperationalEventType.CODEX_EXECUTION,
        "STARTED",
        {"FINISHED", "FAILED", "INTERRUPTED"},
        _codex_key,
    )
    human = _lifecycle(
        events,
        scope,
        MetricName.HUMAN_WAIT_DURATION,
        OperationalEventType.HUMAN_WAITING,
        "WAITING_STARTED",
        {"WAITING_FINISHED"},
        _human_wait_key,
    )
    worktree = _lifecycle(
        events,
        scope,
        MetricName.ACTIVE_WORKTREES,
        OperationalEventType.WORKTREE_LIFECYCLE,
        "CREATED",
        {"COMPLETED", "FAILED", "CLEANED"},
        _worktree_key,
        open_is_diagnostic=False,
    )
    diagnostics.extend((*role.diagnostics, *codex.diagnostics, *human.diagnostics, *worktree.diagnostics))

    samples[MetricName.ROLE_EXECUTION_DURATION] = _duration_sample(
        MetricName.ROLE_EXECUTION_DURATION, scope, role
    )
    samples[MetricName.CODEX_EXECUTION_DURATION] = _duration_sample(
        MetricName.CODEX_EXECUTION_DURATION, scope, codex
    )
    samples[MetricName.HUMAN_WAIT_DURATION] = _duration_sample(
        MetricName.HUMAN_WAIT_DURATION, scope, human
    )
    samples[MetricName.ACTIVE_HUMAN_WAITS] = _gauge_sample(
        MetricName.ACTIVE_HUMAN_WAITS, scope, human
    )
    samples[MetricName.ACTIVE_WORKTREES] = _gauge_sample(
        MetricName.ACTIVE_WORKTREES, scope, worktree
    )

    samples[MetricName.ROLE_FAILURE_RATE] = _rate_sample(
        MetricName.ROLE_FAILURE_RATE,
        scope,
        samples[MetricName.ROLE_EXECUTIONS_FAILED].value,
        (
            samples[MetricName.ROLE_EXECUTIONS_COMPLETED].value
            + samples[MetricName.ROLE_EXECUTIONS_FAILED].value
        ),
    )
    codex_failures = (
        samples[MetricName.CODEX_EXECUTIONS_FAILED].value
        + samples[MetricName.CODEX_EXECUTIONS_INTERRUPTED].value
    )
    samples[MetricName.CODEX_FAILURE_RATE] = _rate_sample(
        MetricName.CODEX_FAILURE_RATE,
        scope,
        codex_failures,
        codex_failures + samples[MetricName.CODEX_EXECUTIONS_COMPLETED].value,
    )

    ordered_samples = tuple(samples[name] for name in MetricName)
    ordered_diagnostics = tuple(
        sorted(
            diagnostics,
            key=lambda item: (
                item.code.value,
                item.metric_name.value if item.metric_name is not None else "",
                item.event_ids,
            ),
        )
    )
    return MetricsSnapshot(
        schema_version=METRICS_SCHEMA_VERSION,
        catalog_version=METRIC_CATALOG_VERSION,
        status=(
            MetricsSnapshotStatus.INCOMPLETE
            if ordered_diagnostics
            else MetricsSnapshotStatus.COMPLETE
        ),
        scope=scope,
        source_event_count=len(events),
        source_first_event_id=events[0].event_id if events else None,
        source_last_event_id=events[-1].event_id if events else None,
        source_fingerprint=_source_fingerprint(events, scope),
        metrics=ordered_samples,
        diagnostics=ordered_diagnostics,
    )


def _validate_corpus(
    events: Sequence[OperationalEvent], scope: MetricsScope
) -> MetricsDiagnostic | None:
    if len(events) > MAX_METRIC_SOURCE_EVENTS:
        return MetricsDiagnostic(
            MetricsDiagnosticCode.CARDINALITY_LIMIT_EXCEEDED, None
        )
    event_ids: set[str] = set()
    dimensions: dict[str, set[object]] = defaultdict(set)
    for item in events:
        if not isinstance(item, OperationalEvent):
            raise MetricsComputationError("every source item must be OperationalEvent")
        if item.event_id in event_ids:
            return MetricsDiagnostic(
                MetricsDiagnosticCode.DUPLICATE_EVENT_ID, None, (item.event_id,)
            )
        event_ids.add(item.event_id)
        if item.project_id != scope.project_id:
            return MetricsDiagnostic(
                MetricsDiagnosticCode.CROSS_PROJECT_EVENT, None, (item.event_id,)
            )
        for name, value in (
            ("mission", item.correlation.mission_id),
            ("story", item.correlation.user_story_id),
            ("execution", item.correlation.execution_id),
            ("assignment", item.correlation.assignment_id),
        ):
            if value is not None:
                dimensions[name].add(value)
                if len(dimensions[name]) > MAX_METRIC_DIMENSION_CARDINALITY:
                    return MetricsDiagnostic(
                        MetricsDiagnosticCode.CARDINALITY_LIMIT_EXCEEDED,
                        None,
                        (item.event_id,),
                    )
    return None


def _matches_scope(event: OperationalEvent, scope: MetricsScope) -> bool:
    correlation = event.correlation
    return (
        event.project_id == scope.project_id
        and (scope.mission_id is None or correlation.mission_id == scope.mission_id)
        and (
            scope.workflow_generation is None
            or correlation.workflow_generation == scope.workflow_generation
        )
        and (
            scope.user_story_id is None
            or correlation.user_story_id == scope.user_story_id
        )
        and (scope.role is None or correlation.role is scope.role)
        and (
            scope.execution_id is None
            or correlation.execution_id == scope.execution_id
        )
    )


def _sample(
    name: MetricName,
    scope: MetricsScope,
    value: int,
    source_event_count: int,
) -> MetricSample:
    definition = METRIC_CATALOG[name]
    return MetricSample(
        name,
        definition.metric_type,
        MetricAvailability.AVAILABLE,
        value,
        definition.unit,
        scope,
        source_event_count,
        definition.derivation,
    )


def _unavailable_sample(
    name: MetricName,
    scope: MetricsScope,
    source_event_count: int,
    diagnostic_codes: tuple[MetricsDiagnosticCode, ...] = (),
) -> MetricSample:
    definition = METRIC_CATALOG[name]
    return MetricSample(
        name,
        definition.metric_type,
        MetricAvailability.UNAVAILABLE,
        None,
        definition.unit,
        scope,
        source_event_count,
        definition.derivation,
        diagnostic_codes,
    )


def _duration_sample(
    name: MetricName, scope: MetricsScope, result: _LifecycleResult
) -> MetricSample:
    codes = tuple(sorted({item.code for item in result.diagnostics}, key=lambda item: item.value))
    if not result.durations or codes:
        return _unavailable_sample(name, scope, result.source_event_count, codes)
    summary = DurationSummary(
        count=len(result.durations),
        total_microseconds=sum(result.durations),
        minimum_microseconds=min(result.durations),
        maximum_microseconds=max(result.durations),
    )
    definition = METRIC_CATALOG[name]
    return MetricSample(
        name,
        definition.metric_type,
        MetricAvailability.AVAILABLE,
        summary,
        definition.unit,
        scope,
        result.source_event_count,
        definition.derivation,
    )


def _gauge_sample(
    name: MetricName, scope: MetricsScope, result: _LifecycleResult
) -> MetricSample:
    blocking = tuple(
        sorted(
            {
                item.code
                for item in result.diagnostics
                if item.code is not MetricsDiagnosticCode.OPEN_LIFECYCLE
            },
            key=lambda item: item.value,
        )
    )
    if blocking:
        return _unavailable_sample(name, scope, result.source_event_count, blocking)
    return _sample(name, scope, result.active_count, result.source_event_count)


def _rate_sample(
    name: MetricName,
    scope: MetricsScope,
    numerator: int,
    denominator: int,
) -> MetricSample:
    if denominator == 0:
        return _unavailable_sample(name, scope, 0)
    definition = METRIC_CATALOG[name]
    return MetricSample(
        name,
        definition.metric_type,
        MetricAvailability.AVAILABLE,
        float(numerator / denominator),
        definition.unit,
        scope,
        denominator,
        definition.derivation,
    )


def _lifecycle(
    events: tuple[OperationalEvent, ...],
    scope: MetricsScope,
    metric_name: MetricName,
    event_type: OperationalEventType,
    start_operation: str,
    terminal_operations: set[str],
    key_builder: Callable[[OperationalEvent], tuple[object, ...]],
    *,
    open_is_diagnostic: bool = True,
) -> _LifecycleResult:
    relevant = tuple(
        item
        for item in events
        if item.event_type is event_type
        and item.payload.operation in ({start_operation} | terminal_operations)
    )
    groups: dict[tuple[object, ...], list[OperationalEvent]] = defaultdict(list)
    for item in relevant:
        groups[key_builder(item)].append(item)
    durations: list[int] = []
    diagnostics: list[MetricsDiagnostic] = []
    active = 0
    for group in groups.values():
        starts = [item for item in group if item.payload.operation == start_operation]
        terminals = [item for item in group if item.payload.operation in terminal_operations]
        ids = tuple(item.event_id for item in group[:4])
        if not starts and terminals:
            diagnostics.append(
                MetricsDiagnostic(
                    MetricsDiagnosticCode.TERMINAL_WITHOUT_START, metric_name, ids
                )
            )
        elif len(starts) > 1 or len(terminals) > 1:
            diagnostics.append(
                MetricsDiagnostic(
                    MetricsDiagnosticCode.AMBIGUOUS_LIFECYCLE, metric_name, ids
                )
            )
        elif len(starts) == 1 and not terminals:
            active += 1
            if open_is_diagnostic:
                diagnostics.append(
                    MetricsDiagnostic(
                        MetricsDiagnosticCode.OPEN_LIFECYCLE, metric_name, ids
                    )
                )
        elif len(starts) == 1 and len(terminals) == 1:
            start, terminal = starts[0], terminals[0]
            if terminal.occurred_at < start.occurred_at:
                diagnostics.append(
                    MetricsDiagnostic(
                        MetricsDiagnosticCode.END_BEFORE_START, metric_name, ids
                    )
                )
            else:
                delta = terminal.occurred_at - start.occurred_at
                durations.append(
                    delta.days * 86_400_000_000
                    + delta.seconds * 1_000_000
                    + delta.microseconds
                )
    return _LifecycleResult(
        tuple(durations), active, len(relevant), tuple(diagnostics)
    )


def _role_key(event: OperationalEvent) -> tuple[object, ...]:
    correlation = event.correlation
    return (
        correlation.mission_id,
        correlation.workflow_generation,
        correlation.user_story_id,
        correlation.role,
        correlation.execution_id,
    )


def _codex_key(event: OperationalEvent) -> tuple[object, ...]:
    return _role_key(event)


def _human_wait_key(event: OperationalEvent) -> tuple[object, ...]:
    correlation = event.correlation
    return (
        correlation.mission_id,
        correlation.workflow_generation,
        correlation.user_story_id,
    )


def _worktree_key(event: OperationalEvent) -> tuple[object, ...]:
    correlation = event.correlation
    return (
        correlation.mission_id,
        correlation.workflow_generation,
        correlation.user_story_id,
        correlation.assignment_id,
    )


def _source_fingerprint(
    events: tuple[OperationalEvent, ...], scope: MetricsScope
) -> str:
    payload = {
        "scope": {
            "project_id": scope.project_id,
            "mission_id": scope.mission_id,
            "workflow_generation": scope.workflow_generation,
            "user_story_id": scope.user_story_id,
            "role": scope.role.value if scope.role is not None else None,
            "execution_id": scope.execution_id,
        },
        "events": [operational_event_fingerprint(item) for item in events],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _empty_snapshot(
    scope: MetricsScope,
    status: MetricsSnapshotStatus,
    code: MetricsDiagnosticCode,
    event_ids: tuple[str, ...] = (),
) -> MetricsSnapshot:
    return MetricsSnapshot(
        schema_version=METRICS_SCHEMA_VERSION,
        catalog_version=METRIC_CATALOG_VERSION,
        status=status,
        scope=scope,
        source_event_count=0,
        source_first_event_id=None,
        source_last_event_id=None,
        source_fingerprint=None,
        metrics=(),
        diagnostics=(MetricsDiagnostic(code, None, event_ids),),
    )
