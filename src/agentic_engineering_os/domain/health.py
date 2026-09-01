"""Immutable, bounded and non-authoritative operational health contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType

from .metrics import MetricsScope, MetricsSnapshot


HEALTH_SCHEMA_VERSION = "1.0"
HEALTH_MAX_OBSERVATION_AGE = timedelta(minutes=5)
MAX_HEALTH_OBSERVATIONS = 16

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SENSITIVE_IDENTITY = re.compile(
    r"(?:^|[._:/-])(?:api[_-]?key|password|secret|token|authorization)(?:[._:/-]|$)",
    re.IGNORECASE,
)
_KNOWN_TOKEN = re.compile(
    r"(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})"
)


class HealthState(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class HealthDimension(str, Enum):
    AUTHORITATIVE_STATE_ACCESS = "AUTHORITATIVE_STATE_ACCESS"
    OBSERVABILITY = "OBSERVABILITY"
    GIT_WORKTREES = "GIT_WORKTREES"
    CODEX_RUNTIME = "CODEX_RUNTIME"
    EXECUTION_RECOVERY = "EXECUTION_RECOVERY"
    PERSISTENCE = "PERSISTENCE"
    REMEDIATION_TRANSACTION = "REMEDIATION_TRANSACTION"
    DEPLOYMENT_CONFIGURATION = "DEPLOYMENT_CONFIGURATION"


class DimensionRequirement(str, Enum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class HealthFreshness(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class HealthSource(str, Enum):
    PROJECT_STATE_STORE = "PROJECT_STATE_STORE"
    MISSION_STATE_STORE = "MISSION_STATE_STORE"
    OPERATIONAL_EVENT_STORE = "OPERATIONAL_EVENT_STORE"
    GIT_RECONCILIATION = "GIT_RECONCILIATION"
    CODEX_RUNTIME = "CODEX_RUNTIME"
    EXECUTION_LEDGER = "EXECUTION_LEDGER"
    PERSISTENCE_DIAGNOSTIC = "PERSISTENCE_DIAGNOSTIC"
    REMEDIATION_STORE = "REMEDIATION_STORE"
    PROJECT_CONFIGURATION = "PROJECT_CONFIGURATION"
    METRICS_SNAPSHOT = "METRICS_SNAPSHOT"


class HealthCondition(str, Enum):
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"
    SATURATED = "SATURATED"
    CORRUPTED = "CORRUPTED"
    RECONCILED = "RECONCILED"
    DRIFT = "DRIFT"
    CLEAR = "CLEAR"
    RECOVERY_PENDING = "RECOVERY_PENDING"
    FAILED = "FAILED"
    PENDING = "PENDING"
    VALID = "VALID"
    INVALID = "INVALID"


_SOURCE_CONDITIONS = MappingProxyType({
    HealthSource.PROJECT_STATE_STORE: frozenset(
        {HealthCondition.AVAILABLE, HealthCondition.UNAVAILABLE, HealthCondition.UNKNOWN}
    ),
    HealthSource.MISSION_STATE_STORE: frozenset(
        {HealthCondition.AVAILABLE, HealthCondition.UNAVAILABLE, HealthCondition.UNKNOWN}
    ),
    HealthSource.OPERATIONAL_EVENT_STORE: frozenset(
        {
            HealthCondition.AVAILABLE,
            HealthCondition.DEGRADED,
            HealthCondition.UNAVAILABLE,
            HealthCondition.UNKNOWN,
            HealthCondition.SATURATED,
            HealthCondition.CORRUPTED,
        }
    ),
    HealthSource.GIT_RECONCILIATION: frozenset(
        {
            HealthCondition.RECONCILED,
            HealthCondition.DRIFT,
            HealthCondition.UNAVAILABLE,
            HealthCondition.UNKNOWN,
        }
    ),
    HealthSource.CODEX_RUNTIME: frozenset(
        {HealthCondition.AVAILABLE, HealthCondition.UNAVAILABLE, HealthCondition.UNKNOWN}
    ),
    HealthSource.EXECUTION_LEDGER: frozenset(
        {
            HealthCondition.CLEAR,
            HealthCondition.RECOVERY_PENDING,
            HealthCondition.UNAVAILABLE,
            HealthCondition.UNKNOWN,
        }
    ),
    HealthSource.PERSISTENCE_DIAGNOSTIC: frozenset(
        {
            HealthCondition.AVAILABLE,
            HealthCondition.DEGRADED,
            HealthCondition.FAILED,
            HealthCondition.UNKNOWN,
        }
    ),
    HealthSource.REMEDIATION_STORE: frozenset(
        {
            HealthCondition.CLEAR,
            HealthCondition.PENDING,
            HealthCondition.FAILED,
            HealthCondition.UNKNOWN,
        }
    ),
    HealthSource.PROJECT_CONFIGURATION: frozenset(
        {HealthCondition.VALID, HealthCondition.INVALID, HealthCondition.UNKNOWN}
    ),
    HealthSource.METRICS_SNAPSHOT: frozenset(
        {
            HealthCondition.AVAILABLE,
            HealthCondition.DEGRADED,
            HealthCondition.UNAVAILABLE,
        }
    ),
})


class HealthReasonCode(str, Enum):
    CONDITION_SATISFIED = "CONDITION_SATISFIED"
    SOURCE_DEGRADED = "SOURCE_DEGRADED"
    SOURCE_BLOCKED = "SOURCE_BLOCKED"
    SOURCE_UNKNOWN = "SOURCE_UNKNOWN"
    MISSING_REQUIRED_OBSERVATION = "MISSING_REQUIRED_OBSERVATION"
    MISSING_OPTIONAL_OBSERVATION = "MISSING_OPTIONAL_OBSERVATION"
    DUPLICATE_SOURCE_OBSERVATION = "DUPLICATE_SOURCE_OBSERVATION"
    WRONG_PROJECT = "WRONG_PROJECT"
    STALE_REPOSITORY_HEAD = "STALE_REPOSITORY_HEAD"
    STALE_GENERATION = "STALE_GENERATION"
    STALE_OBSERVATION = "STALE_OBSERVATION"
    FUTURE_OBSERVATION = "FUTURE_OBSERVATION"
    METRICS_MISSING = "METRICS_MISSING"
    METRICS_INCOMPLETE = "METRICS_INCOMPLETE"
    METRICS_UNAVAILABLE = "METRICS_UNAVAILABLE"
    METRICS_SCOPE_MISMATCH = "METRICS_SCOPE_MISMATCH"
    METRICS_STALE_REPOSITORY_HEAD = "METRICS_STALE_REPOSITORY_HEAD"
    METRICS_STALE = "METRICS_STALE"
    METRICS_FUTURE = "METRICS_FUTURE"
    PERSISTENCE_FAILURES_OBSERVED = "PERSISTENCE_FAILURES_OBSERVED"
    CODEX_FAILURES_OBSERVED = "CODEX_FAILURES_OBSERVED"
    NO_ACTIVE_MISSION = "NO_ACTIVE_MISSION"
    NO_PARALLEL_EXECUTION = "NO_PARALLEL_EXECUTION"
    REQUIRED_DIMENSION_BLOCKED = "REQUIRED_DIMENSION_BLOCKED"
    REQUIRED_DIMENSION_UNKNOWN = "REQUIRED_DIMENSION_UNKNOWN"
    REQUIRED_DIMENSION_DEGRADED = "REQUIRED_DIMENSION_DEGRADED"
    OPTIONAL_DIMENSION_IMPAIRED = "OPTIONAL_DIMENSION_IMPAIRED"
    ALL_REQUIRED_DIMENSIONS_HEALTHY = "ALL_REQUIRED_DIMENSIONS_HEALTHY"


@dataclass(frozen=True, slots=True)
class HealthScope:
    project_id: str
    repository_head: str
    mission_id: str | None = None
    workflow_generation: int | None = None

    def __post_init__(self) -> None:
        MetricsScope(
            project_id=self.project_id,
            mission_id=self.mission_id,
            workflow_generation=self.workflow_generation,
        )
        if not _SHA40.fullmatch(self.repository_head):
            raise ValueError("repository_head must be a lowercase SHA-1")


@dataclass(frozen=True, slots=True)
class HealthObservation:
    source: HealthSource
    condition: HealthCondition
    project_id: str
    observed_at: datetime
    source_identity: str
    repository_head: str
    mission_id: str | None = None
    workflow_generation: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, HealthSource):
            raise ValueError("health observation source is invalid")
        if self.source is HealthSource.METRICS_SNAPSHOT:
            raise ValueError("metrics observations require MetricsHealthInput")
        if not isinstance(self.condition, HealthCondition):
            raise ValueError("health observation condition is invalid")
        allowed = _SOURCE_CONDITIONS.get(self.source)
        if allowed is None or self.condition not in allowed:
            raise ValueError("condition is not valid for this health source")
        MetricsScope(
            project_id=self.project_id,
            mission_id=self.mission_id,
            workflow_generation=self.workflow_generation,
        )
        _validate_utc(self.observed_at, "observed_at")
        if not _valid_source_identity(self.source_identity):
            raise ValueError("source_identity is absent or non-canonical")
        if not _SHA40.fullmatch(self.repository_head):
            raise ValueError("repository_head must be a lowercase SHA-1")


@dataclass(frozen=True, slots=True)
class MetricsHealthInput:
    snapshot: MetricsSnapshot
    observed_at: datetime
    repository_head: str

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, MetricsSnapshot):
            raise ValueError("snapshot must be a MetricsSnapshot")
        _validate_utc(self.observed_at, "observed_at")
        if not _SHA40.fullmatch(self.repository_head):
            raise ValueError("repository_head must be a lowercase SHA-1")


@dataclass(frozen=True, slots=True)
class HealthEvaluationContext:
    scope: HealthScope
    evaluated_at: datetime
    mission_active: bool
    parallel_execution_active: bool
    observations: tuple[HealthObservation, ...]
    metrics: MetricsHealthInput | None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, HealthScope):
            raise ValueError("scope must be a HealthScope")
        _validate_utc(self.evaluated_at, "evaluated_at")
        if not isinstance(self.mission_active, bool):
            raise ValueError("mission_active must be an explicit boolean")
        if not isinstance(self.parallel_execution_active, bool):
            raise ValueError("parallel_execution_active must be an explicit boolean")
        if self.mission_active and (
            self.scope.mission_id is None or self.scope.workflow_generation is None
        ):
            raise ValueError("active mission requires mission and generation scope")
        if self.parallel_execution_active and not self.mission_active:
            raise ValueError("parallel execution requires an active mission")
        if not isinstance(self.observations, tuple):
            raise ValueError("observations must be immutable")
        if len(self.observations) > MAX_HEALTH_OBSERVATIONS:
            raise ValueError("health observation count exceeds policy")
        if any(not isinstance(item, HealthObservation) for item in self.observations):
            raise ValueError("observations must contain HealthObservation values")
        if self.metrics is not None and not isinstance(self.metrics, MetricsHealthInput):
            raise ValueError("metrics must be MetricsHealthInput or None")


@dataclass(frozen=True, slots=True)
class HealthSourceReference:
    source: HealthSource
    source_identity: str
    condition: HealthCondition
    observed_at: datetime
    repository_head: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, HealthSource):
            raise ValueError("source reference is invalid")
        if not isinstance(self.condition, HealthCondition):
            raise ValueError("source reference condition is invalid")
        if self.condition not in _SOURCE_CONDITIONS[self.source]:
            raise ValueError("source reference condition is incompatible")
        if not _valid_source_identity(self.source_identity):
            raise ValueError("source reference identity is invalid")
        _validate_utc(self.observed_at, "observed_at")
        if not _SHA40.fullmatch(self.repository_head):
            raise ValueError("source reference repository_head is invalid")


@dataclass(frozen=True, slots=True)
class HealthDimensionResult:
    dimension: HealthDimension
    requirement: DimensionRequirement
    state: HealthState | None
    scope: HealthScope
    freshness: HealthFreshness
    reasons: tuple[HealthReasonCode, ...]
    sources: tuple[HealthSourceReference, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, HealthDimension):
            raise ValueError("health dimension is invalid")
        if not isinstance(self.requirement, DimensionRequirement):
            raise ValueError("dimension requirement is invalid")
        if not isinstance(self.scope, HealthScope):
            raise ValueError("dimension scope is invalid")
        if not isinstance(self.freshness, HealthFreshness):
            raise ValueError("dimension freshness is invalid")
        if not isinstance(self.reasons, tuple) or not self.reasons:
            raise ValueError("dimension reasons must be a non-empty tuple")
        if any(not isinstance(item, HealthReasonCode) for item in self.reasons):
            raise ValueError("dimension reasons are invalid")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("dimension reasons must be unique")
        if not isinstance(self.sources, tuple) or any(
            not isinstance(item, HealthSourceReference) for item in self.sources
        ):
            raise ValueError("dimension sources must be immutable references")
        if self.requirement is DimensionRequirement.NOT_APPLICABLE:
            if (
                self.state is not None
                or self.freshness is not HealthFreshness.NOT_APPLICABLE
                or self.sources
            ):
                raise ValueError("not-applicable dimension cannot claim health facts")
        elif not isinstance(self.state, HealthState):
            raise ValueError("applicable dimension requires a health state")


@dataclass(frozen=True, slots=True)
class HealthDiagnostic:
    reason: HealthReasonCode
    dimension: HealthDimension | None = None
    source: HealthSource | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reason, HealthReasonCode):
            raise ValueError("health diagnostic reason is invalid")
        if self.dimension is not None and not isinstance(self.dimension, HealthDimension):
            raise ValueError("health diagnostic dimension is invalid")
        if self.source is not None and not isinstance(self.source, HealthSource):
            raise ValueError("health diagnostic source is invalid")


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    schema_version: str
    scope: HealthScope
    evaluated_at: datetime
    global_state: HealthState
    dimensions: tuple[HealthDimensionResult, ...]
    reasons: tuple[HealthReasonCode, ...]
    source_identities: tuple[str, ...]
    diagnostics: tuple[HealthDiagnostic, ...]
    fingerprint: str

    def __post_init__(self) -> None:
        if self.schema_version != HEALTH_SCHEMA_VERSION:
            raise ValueError("unsupported health schema_version")
        if not isinstance(self.scope, HealthScope):
            raise ValueError("health snapshot scope is invalid")
        _validate_utc(self.evaluated_at, "evaluated_at")
        if not isinstance(self.global_state, HealthState):
            raise ValueError("global health state is invalid")
        if not isinstance(self.dimensions, tuple):
            raise ValueError("health dimensions must be immutable")
        if tuple(item.dimension for item in self.dimensions) != tuple(HealthDimension):
            raise ValueError("health snapshot must contain the closed dimension catalog")
        if any(item.scope != self.scope for item in self.dimensions):
            raise ValueError("health dimensions must share the snapshot scope")
        expected_state, expected_reason = _aggregate_dimension_states(self.dimensions)
        if self.global_state is not expected_state:
            raise ValueError("global health state contradicts dimension results")
        if not isinstance(self.reasons, tuple) or not self.reasons:
            raise ValueError("health snapshot requires exact reasons")
        if any(not isinstance(item, HealthReasonCode) for item in self.reasons):
            raise ValueError("health snapshot reasons are invalid")
        if self.reasons != (expected_reason,):
            raise ValueError("health snapshot reason contradicts aggregation")
        if not isinstance(self.source_identities, tuple) or any(
            not _valid_source_identity(item) for item in self.source_identities
        ):
            raise ValueError("health source identities are invalid")
        if tuple(sorted(set(self.source_identities))) != self.source_identities:
            raise ValueError("health source identities must be sorted and unique")
        if not isinstance(self.diagnostics, tuple) or any(
            not isinstance(item, HealthDiagnostic) for item in self.diagnostics
        ):
            raise ValueError("health diagnostics must be immutable")
        if not _SHA256.fullmatch(self.fingerprint):
            raise ValueError("health snapshot fingerprint is invalid")


def _validate_utc(value: datetime, name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
        or value.tzinfo is not timezone.utc
    ):
        raise ValueError(f"{name} must be timezone.utc")


def _valid_source_identity(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and _SOURCE_IDENTITY.fullmatch(value)
        and not _SENSITIVE_IDENTITY.search(value)
        and not _KNOWN_TOKEN.search(value)
    )


def _aggregate_dimension_states(
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
