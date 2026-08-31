"""Immutable, bounded and non-authoritative runtime metric contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from unicodedata import normalize

from .enums import MissionRole


METRICS_SCHEMA_VERSION = "1.0"
METRIC_CATALOG_VERSION = "1.0"
MAX_METRIC_SOURCE_EVENTS = 10_000
MAX_METRIC_DIMENSION_CARDINALITY = 1_024

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_PROJECT_PATTERN = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,128}$")
_SECRET_PATTERN = re.compile(
    r"(?:api[_-]?key|password|secret|token|authorization)\s*[:=]",
    re.IGNORECASE,
)
_TOKEN_PATTERNS = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/-]{12,}", re.IGNORECASE),
)
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class MetricType(str, Enum):
    COUNTER = "COUNTER"
    GAUGE = "GAUGE"
    DURATION_SUMMARY = "DURATION_SUMMARY"
    DERIVED_METRIC = "DERIVED_METRIC"


class MetricUnit(str, Enum):
    COUNT = "COUNT"
    RATIO = "RATIO"
    MICROSECONDS = "MICROSECONDS"


class MetricAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class MetricsSnapshotStatus(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


class MetricName(str, Enum):
    MISSIONS_STARTED = "missions.started"
    MISSIONS_COMPLETED = "missions.completed"
    MISSIONS_BLOCKED = "missions.blocked"
    ROLE_EXECUTIONS_STARTED = "role_executions.started"
    ROLE_EXECUTIONS_COMPLETED = "role_executions.completed"
    ROLE_EXECUTIONS_FAILED = "role_executions.failed"
    CODEX_EXECUTIONS_STARTED = "codex_executions.started"
    CODEX_EXECUTIONS_COMPLETED = "codex_executions.completed"
    CODEX_EXECUTIONS_FAILED = "codex_executions.failed"
    CODEX_EXECUTIONS_INTERRUPTED = "codex_executions.interrupted"
    CODEX_TIMEOUTS = "codex_executions.timeouts"
    REMEDIATIONS_REQUESTED = "remediations.requested"
    RECOVERIES_OBSERVED = "recoveries.observed"
    HUMAN_WAITS_STARTED = "human_waits.started"
    WORKTREES_CREATED = "worktrees.created"
    WORKTREES_COMPLETED = "worktrees.completed"
    WORKTREES_FAILED = "worktrees.failed"
    INTEGRATION_GATES_PASS = "integration_gates.pass"
    INTEGRATION_GATES_FAIL = "integration_gates.fail"
    INTEGRATION_GATES_UNKNOWN = "integration_gates.unknown"
    MERGES_SUCCEEDED = "merges.succeeded"
    MERGES_FAILED = "merges.failed"
    PERSISTENCE_FAILURES = "persistence.failures"
    ADOPTIONS_SUCCEEDED = "adoptions.succeeded"
    ADOPTIONS_FAILED = "adoptions.failed"
    ADOPTIONS_REFUSED = "adoptions.refused"
    ACTIVE_WORKTREES = "worktrees.active"
    ACTIVE_HUMAN_WAITS = "human_waits.active"
    ROLE_EXECUTION_DURATION = "role_executions.duration"
    CODEX_EXECUTION_DURATION = "codex_executions.duration"
    HUMAN_WAIT_DURATION = "human_waits.duration"
    ROLE_FAILURE_RATE = "role_executions.failure_rate"
    CODEX_FAILURE_RATE = "codex_executions.failure_rate"


class MetricsDiagnosticCode(str, Enum):
    INCOMPLETE_SOURCE = "INCOMPLETE_SOURCE"
    EVENT_SOURCE_UNAVAILABLE = "EVENT_SOURCE_UNAVAILABLE"
    EVENT_SOURCE_SATURATED = "EVENT_SOURCE_SATURATED"
    DUPLICATE_EVENT_ID = "DUPLICATE_EVENT_ID"
    CROSS_PROJECT_EVENT = "CROSS_PROJECT_EVENT"
    CARDINALITY_LIMIT_EXCEEDED = "CARDINALITY_LIMIT_EXCEEDED"
    AMBIGUOUS_LIFECYCLE = "AMBIGUOUS_LIFECYCLE"
    TERMINAL_WITHOUT_START = "TERMINAL_WITHOUT_START"
    END_BEFORE_START = "END_BEFORE_START"
    OPEN_LIFECYCLE = "OPEN_LIFECYCLE"
    UNCLASSIFIED_OUTCOME = "UNCLASSIFIED_OUTCOME"


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    name: MetricName
    metric_type: MetricType
    unit: MetricUnit
    derivation: str


_DEFINITIONS = (
    *(
        MetricDefinition(name, MetricType.COUNTER, MetricUnit.COUNT, derivation)
        for name, derivation in (
            (MetricName.MISSIONS_STARTED, "MISSION_LIFECYCLE:STARTED"),
            (MetricName.MISSIONS_COMPLETED, "MISSION_LIFECYCLE:FINISHED"),
            (MetricName.MISSIONS_BLOCKED, "MISSION_LIFECYCLE:BLOCKED"),
            (MetricName.ROLE_EXECUTIONS_STARTED, "ROLE_EXECUTION:STARTED"),
            (MetricName.ROLE_EXECUTIONS_COMPLETED, "ROLE_EXECUTION:FINISHED"),
            (MetricName.ROLE_EXECUTIONS_FAILED, "ROLE_EXECUTION:FAILED"),
            (MetricName.CODEX_EXECUTIONS_STARTED, "CODEX_EXECUTION:STARTED"),
            (MetricName.CODEX_EXECUTIONS_COMPLETED, "CODEX_EXECUTION:FINISHED"),
            (MetricName.CODEX_EXECUTIONS_FAILED, "CODEX_EXECUTION:FAILED"),
            (MetricName.CODEX_EXECUTIONS_INTERRUPTED, "CODEX_EXECUTION:INTERRUPTED"),
            (MetricName.CODEX_TIMEOUTS, "CODEX_EXECUTION reason_code=TIMEOUT"),
            (MetricName.REMEDIATIONS_REQUESTED, "REMEDIATION_RECOVERY:REQUESTED"),
            (MetricName.RECOVERIES_OBSERVED, "RECOVERY_REQUIRED|RECOVERY_INSPECTED"),
            (MetricName.HUMAN_WAITS_STARTED, "HUMAN_WAITING:WAITING_STARTED"),
            (MetricName.WORKTREES_CREATED, "WORKTREE_LIFECYCLE:CREATED"),
            (MetricName.WORKTREES_COMPLETED, "WORKTREE_LIFECYCLE:COMPLETED"),
            (MetricName.WORKTREES_FAILED, "WORKTREE_LIFECYCLE:FAILED"),
            (MetricName.INTEGRATION_GATES_PASS, "INTEGRATION_GATE outcome=PASS"),
            (MetricName.INTEGRATION_GATES_FAIL, "INTEGRATION_GATE outcome=FAIL"),
            (MetricName.INTEGRATION_GATES_UNKNOWN, "INTEGRATION_GATE outcome=UNKNOWN"),
            (MetricName.MERGES_SUCCEEDED, "MERGE_OPERATION:FINISHED"),
            (MetricName.MERGES_FAILED, "MERGE_OPERATION:FAILED|CONFLICT_OBSERVED"),
            (MetricName.PERSISTENCE_FAILURES, "PERSISTENCE_FAILURE:*"),
            (MetricName.ADOPTIONS_SUCCEEDED, "ADOPTION_MIGRATION:FINISHED"),
            (MetricName.ADOPTIONS_FAILED, "ADOPTION_MIGRATION:FAILED"),
            (MetricName.ADOPTIONS_REFUSED, "ADOPTION_MIGRATION:REFUSED"),
        )
    ),
    MetricDefinition(
        MetricName.ACTIVE_WORKTREES,
        MetricType.GAUGE,
        MetricUnit.COUNT,
        "created worktree lifecycles without a terminal observation",
    ),
    MetricDefinition(
        MetricName.ACTIVE_HUMAN_WAITS,
        MetricType.GAUGE,
        MetricUnit.COUNT,
        "waiting lifecycles without a terminal observation",
    ),
    MetricDefinition(
        MetricName.ROLE_EXECUTION_DURATION,
        MetricType.DURATION_SUMMARY,
        MetricUnit.MICROSECONDS,
        "paired ROLE_EXECUTION STARTED→FINISHED|FAILED",
    ),
    MetricDefinition(
        MetricName.CODEX_EXECUTION_DURATION,
        MetricType.DURATION_SUMMARY,
        MetricUnit.MICROSECONDS,
        "paired CODEX_EXECUTION STARTED→FINISHED|FAILED|INTERRUPTED",
    ),
    MetricDefinition(
        MetricName.HUMAN_WAIT_DURATION,
        MetricType.DURATION_SUMMARY,
        MetricUnit.MICROSECONDS,
        "paired HUMAN_WAITING WAITING_STARTED→WAITING_FINISHED",
    ),
    MetricDefinition(
        MetricName.ROLE_FAILURE_RATE,
        MetricType.DERIVED_METRIC,
        MetricUnit.RATIO,
        "role failed terminals / all role terminals",
    ),
    MetricDefinition(
        MetricName.CODEX_FAILURE_RATE,
        MetricType.DERIVED_METRIC,
        MetricUnit.RATIO,
        "Codex failed|interrupted terminals / all Codex terminals",
    ),
)

METRIC_CATALOG = MappingProxyType({item.name: item for item in _DEFINITIONS})


@dataclass(frozen=True, slots=True)
class MetricsScope:
    project_id: str
    mission_id: str | None = None
    workflow_generation: int | None = None
    user_story_id: str | None = None
    role: MissionRole | None = None
    execution_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.project_id, str)
            or self.project_id != self.project_id.strip()
            or normalize("NFC", self.project_id) != self.project_id
            or not _PROJECT_PATTERN.fullmatch(self.project_id)
            or _unsafe_label(self.project_id)
        ):
            raise ValueError("project_id is absent, unsafe, or non-canonical")
        for name in ("mission_id", "user_story_id", "execution_id"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str)
                or not _ID_PATTERN.fullmatch(value)
                or _unsafe_label(value)
            ):
                raise ValueError(f"{name} is unsafe or non-canonical")
        if self.workflow_generation is not None and (
            isinstance(self.workflow_generation, bool)
            or not isinstance(self.workflow_generation, int)
            or self.workflow_generation < 0
        ):
            raise ValueError("workflow_generation must be non-negative")
        if self.role is not None and not isinstance(self.role, MissionRole):
            raise ValueError("role must be a MissionRole")
        contextual = (
            self.workflow_generation,
            self.user_story_id,
            self.role,
            self.execution_id,
        )
        if self.mission_id is None and any(value is not None for value in contextual):
            raise ValueError("mission-scoped dimensions require mission_id")
        if self.execution_id is not None and self.role is None:
            raise ValueError("execution_id requires role")


@dataclass(frozen=True, slots=True)
class DurationSummary:
    count: int
    total_microseconds: int
    minimum_microseconds: int
    maximum_microseconds: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.count, int)
            or isinstance(self.count, bool)
            or self.count <= 0
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in (
                    self.total_microseconds,
                    self.minimum_microseconds,
                    self.maximum_microseconds,
                )
            )
            or self.minimum_microseconds > self.maximum_microseconds
            or self.total_microseconds < self.minimum_microseconds * self.count
            or self.total_microseconds > self.maximum_microseconds * self.count
        ):
            raise ValueError("duration summary is inconsistent")

    @property
    def mean_microseconds(self) -> float:
        return self.total_microseconds / self.count


MetricValue = int | float | DurationSummary | None


@dataclass(frozen=True, slots=True)
class MetricSample:
    name: MetricName
    metric_type: MetricType
    availability: MetricAvailability
    value: MetricValue
    unit: MetricUnit
    scope: MetricsScope
    source_event_count: int
    derivation: str
    diagnostic_codes: tuple[MetricsDiagnosticCode, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.availability, MetricAvailability):
            raise ValueError("metric availability is invalid")
        if not isinstance(self.diagnostic_codes, tuple):
            raise ValueError("metric diagnostic codes must be immutable")
        definition = METRIC_CATALOG.get(self.name)
        if (
            definition is None
            or self.metric_type is not definition.metric_type
            or self.unit is not definition.unit
            or self.derivation != definition.derivation
        ):
            raise ValueError("metric sample does not match the closed catalog")
        if (
            not isinstance(self.source_event_count, int)
            or isinstance(self.source_event_count, bool)
            or self.source_event_count < 0
        ):
            raise ValueError("source_event_count must be non-negative")
        if any(not isinstance(code, MetricsDiagnosticCode) for code in self.diagnostic_codes):
            raise ValueError("metric diagnostic codes are invalid")
        if self.availability is MetricAvailability.UNAVAILABLE:
            if self.value is not None:
                raise ValueError("unavailable metric cannot carry a value")
            return
        if self.metric_type in {MetricType.COUNTER, MetricType.GAUGE}:
            if not isinstance(self.value, int) or isinstance(self.value, bool) or self.value < 0:
                raise ValueError("counter/gauge value must be a non-negative integer")
        elif self.metric_type is MetricType.DERIVED_METRIC:
            if not isinstance(self.value, float) or not 0.0 <= self.value <= 1.0:
                raise ValueError("derived ratio must be a float in [0, 1]")
        elif not isinstance(self.value, DurationSummary):
            raise ValueError("duration metric requires DurationSummary")


@dataclass(frozen=True, slots=True)
class MetricsDiagnostic:
    code: MetricsDiagnosticCode
    metric_name: MetricName | None
    event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.code, MetricsDiagnosticCode):
            raise ValueError("diagnostic code is invalid")
        if self.metric_name is not None and not isinstance(self.metric_name, MetricName):
            raise ValueError("diagnostic metric name is invalid")
        if not isinstance(self.event_ids, tuple):
            raise ValueError("diagnostic event IDs must be immutable")
        if len(self.event_ids) > 4 or any(
            not isinstance(item, str) or not _ID_PATTERN.fullmatch(item)
            for item in self.event_ids
        ):
            raise ValueError("diagnostic event IDs are invalid or unbounded")


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    schema_version: str
    catalog_version: str
    status: MetricsSnapshotStatus
    scope: MetricsScope
    source_event_count: int
    source_first_event_id: str | None
    source_last_event_id: str | None
    source_fingerprint: str | None
    metrics: tuple[MetricSample, ...]
    diagnostics: tuple[MetricsDiagnostic, ...]

    def __post_init__(self) -> None:
        if self.schema_version != METRICS_SCHEMA_VERSION:
            raise ValueError("unsupported metrics schema_version")
        if self.catalog_version != METRIC_CATALOG_VERSION:
            raise ValueError("unsupported metric catalog_version")
        if not isinstance(self.status, MetricsSnapshotStatus):
            raise ValueError("snapshot status is invalid")
        if not isinstance(self.scope, MetricsScope):
            raise ValueError("snapshot scope is invalid")
        if not isinstance(self.metrics, tuple) or not isinstance(self.diagnostics, tuple):
            raise ValueError("snapshot collections must be immutable")
        if (
            not isinstance(self.source_event_count, int)
            or isinstance(self.source_event_count, bool)
            or self.source_event_count < 0
        ):
            raise ValueError("source_event_count must be non-negative")
        if any(not isinstance(item, MetricSample) or item.scope != self.scope for item in self.metrics):
            raise ValueError("snapshot metrics must be bound to its scope")
        names = tuple(item.name for item in self.metrics)
        if len(set(names)) != len(names):
            raise ValueError("snapshot metrics must not contain duplicate names")
        if names != tuple(
            sorted(names, key=lambda item: list(MetricName).index(item))
        ):
            raise ValueError("snapshot metrics must use catalog order")
        if any(not isinstance(item, MetricsDiagnostic) for item in self.diagnostics):
            raise ValueError("snapshot diagnostics are invalid")
        if (self.source_first_event_id is None) != (self.source_last_event_id is None):
            raise ValueError("source event range must have both boundaries")
        if any(
            item is not None and not _ID_PATTERN.fullmatch(item)
            for item in (self.source_first_event_id, self.source_last_event_id)
        ):
            raise ValueError("source event boundaries are invalid")
        if self.source_event_count == 0 and (
            self.source_first_event_id is not None or self.source_last_event_id is not None
        ):
            raise ValueError("empty source cannot claim event boundaries")
        if self.source_event_count > 0 and (
            self.source_first_event_id is None or self.source_last_event_id is None
        ):
            raise ValueError("non-empty source requires event boundaries")
        if self.source_fingerprint is not None and not _FINGERPRINT_PATTERN.fullmatch(
            self.source_fingerprint
        ):
            raise ValueError("source fingerprint is invalid")
        if self.status is MetricsSnapshotStatus.COMPLETE and self.diagnostics:
            raise ValueError("complete snapshot cannot carry anomalies")
        if self.status is MetricsSnapshotStatus.COMPLETE and names != tuple(MetricName):
            raise ValueError("complete snapshot must contain the full metric catalog")
        if self.status is MetricsSnapshotStatus.COMPLETE and self.source_fingerprint is None:
            raise ValueError("complete snapshot requires a source fingerprint")
        if self.metrics and self.source_fingerprint is None:
            raise ValueError("computed metrics require a source fingerprint")
        if self.status is not MetricsSnapshotStatus.COMPLETE and not self.diagnostics:
            raise ValueError("non-complete snapshot requires diagnostics")
        if self.status is MetricsSnapshotStatus.UNAVAILABLE and (
            self.metrics or self.source_event_count != 0 or self.source_fingerprint is not None
        ):
            raise ValueError("unavailable snapshot cannot claim source metrics")


def _unsafe_label(value: str) -> bool:
    return bool(_SECRET_PATTERN.search(value)) or any(
        pattern.search(value) for pattern in _TOKEN_PATTERNS
    )
