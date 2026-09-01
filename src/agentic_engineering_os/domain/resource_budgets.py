"""Strict, immutable and non-authoritative resource budget contracts."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from .governance import GovernedOperation, GovernancePolicyClass
from .metrics import MetricsScope


RESOURCE_BUDGET_SCHEMA_VERSION = "1.0"
RESOURCE_BUDGET_MAX_OBSERVATION_AGE = timedelta(minutes=5)
MAX_RESOURCE_BUDGETS = 32
MAX_RESOURCE_USAGE_OBSERVATIONS = 6
MAX_RESOURCE_VALUE = (1 << 63) - 1

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_VERSION = re.compile(r"^[1-9][0-9]*\.[0-9]+$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE = re.compile(
    r"(?:^|[._:/-])(?:api[_-]?key|password|secret|token|authorization)(?:[._:/-]|$)",
    re.IGNORECASE,
)
_KNOWN_TOKEN = re.compile(
    r"(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})"
)


class ResourceBudgetDomain(str, Enum):
    CODEX_CONCURRENCY = "CODEX_CONCURRENCY"
    WORKTREE_CONCURRENCY = "WORKTREE_CONCURRENCY"
    EXECUTION_TIME = "EXECUTION_TIME"
    REMEDIATION_GENERATIONS = "REMEDIATION_GENERATIONS"
    RUNTIME_STORAGE = "RUNTIME_STORAGE"
    OBSERVABILITY_STORAGE = "OBSERVABILITY_STORAGE"


class ResourceBudgetUnit(str, Enum):
    EXECUTIONS = "EXECUTIONS"
    WORKTREES = "WORKTREES"
    SECONDS = "SECONDS"
    GENERATIONS = "GENERATIONS"
    BYTES = "BYTES"


class ResourceBudgetApplicability(str, Enum):
    APPLICABLE = "APPLICABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ResourceUsageStatus(str, Enum):
    COMPLETE = "COMPLETE"
    UNKNOWN = "UNKNOWN"
    UNAVAILABLE = "UNAVAILABLE"


class ResourceUsageSource(str, Enum):
    METRICS_SNAPSHOT = "METRICS_SNAPSHOT"
    WORKTREE_REGISTRY = "WORKTREE_REGISTRY"
    EXECUTION_STATE_STORE = "EXECUTION_STATE_STORE"
    CODEX_RUNTIME = "CODEX_RUNTIME"
    MISSION_STATE = "MISSION_STATE"
    FILESYSTEM = "FILESYSTEM"
    EVENT_STORE_RETENTION = "EVENT_STORE_RETENTION"


class ResourceBudgetRationale(str, Enum):
    SAFETY_CEILING = "SAFETY_CEILING"
    PROJECT_CAPACITY = "PROJECT_CAPACITY"
    MACHINE_CAPACITY = "MACHINE_CAPACITY"
    OPERATOR_CONSERVATION = "OPERATOR_CONSERVATION"
    RUNTIME_TIMEOUT = "RUNTIME_TIMEOUT"
    REMEDIATION_BOUND = "REMEDIATION_BOUND"
    RETENTION_CAPACITY = "RETENTION_CAPACITY"


class ResourceBudgetDecision(str, Enum):
    WITHIN_BUDGET = "WITHIN_BUDGET"
    NEAR_LIMIT = "NEAR_LIMIT"
    LIMIT_REACHED = "LIMIT_REACHED"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    UNKNOWN = "UNKNOWN"


class ResourceBudgetReason(str, Enum):
    USAGE_WITHIN_BUDGET = "USAGE_WITHIN_BUDGET"
    USAGE_NEAR_LIMIT = "USAGE_NEAR_LIMIT"
    USAGE_REACHES_LIMIT = "USAGE_REACHES_LIMIT"
    USAGE_EXCEEDS_LIMIT = "USAGE_EXCEEDS_LIMIT"
    USAGE_UNKNOWN = "USAGE_UNKNOWN"
    USAGE_UNAVAILABLE = "USAGE_UNAVAILABLE"
    USAGE_STALE = "USAGE_STALE"
    USAGE_SCOPE_MISMATCH = "USAGE_SCOPE_MISMATCH"
    USAGE_SOURCE_INCOMPATIBLE = "USAGE_SOURCE_INCOMPATIBLE"
    USAGE_UNIT_MISMATCH = "USAGE_UNIT_MISMATCH"
    DUPLICATE_ACTIVE_IDENTITY = "DUPLICATE_ACTIVE_IDENTITY"
    ACTIVE_IDENTITY_COUNT_MISMATCH = "ACTIVE_IDENTITY_COUNT_MISMATCH"
    CROSS_REPOSITORY_RESOURCE = "CROSS_REPOSITORY_RESOURCE"
    INVALID_REQUEST = "INVALID_REQUEST"
    ARITHMETIC_OVERFLOW = "ARITHMETIC_OVERFLOW"
    MISSING_USAGE = "MISSING_USAGE"
    MISSING_NON_PREFERENCE_CEILING = "MISSING_NON_PREFERENCE_CEILING"
    BUDGET_SCOPE_MISMATCH = "BUDGET_SCOPE_MISMATCH"
    PREFERENCE_ABOVE_CEILING_IGNORED = "PREFERENCE_ABOVE_CEILING_IGNORED"
    HIGHER_LIMIT_IGNORED = "HIGHER_LIMIT_IGNORED"


_DOMAIN_UNIT = {
    ResourceBudgetDomain.CODEX_CONCURRENCY: ResourceBudgetUnit.EXECUTIONS,
    ResourceBudgetDomain.WORKTREE_CONCURRENCY: ResourceBudgetUnit.WORKTREES,
    ResourceBudgetDomain.EXECUTION_TIME: ResourceBudgetUnit.SECONDS,
    ResourceBudgetDomain.REMEDIATION_GENERATIONS: ResourceBudgetUnit.GENERATIONS,
    ResourceBudgetDomain.RUNTIME_STORAGE: ResourceBudgetUnit.BYTES,
    ResourceBudgetDomain.OBSERVABILITY_STORAGE: ResourceBudgetUnit.BYTES,
}

_DOMAIN_SOURCES = {
    ResourceBudgetDomain.CODEX_CONCURRENCY: frozenset(
        {ResourceUsageSource.EXECUTION_STATE_STORE}
    ),
    ResourceBudgetDomain.WORKTREE_CONCURRENCY: frozenset(
        {ResourceUsageSource.WORKTREE_REGISTRY, ResourceUsageSource.METRICS_SNAPSHOT}
    ),
    ResourceBudgetDomain.EXECUTION_TIME: frozenset(
        {ResourceUsageSource.CODEX_RUNTIME, ResourceUsageSource.EXECUTION_STATE_STORE}
    ),
    ResourceBudgetDomain.REMEDIATION_GENERATIONS: frozenset(
        {ResourceUsageSource.MISSION_STATE}
    ),
    ResourceBudgetDomain.RUNTIME_STORAGE: frozenset(
        {ResourceUsageSource.FILESYSTEM}
    ),
    ResourceBudgetDomain.OBSERVABILITY_STORAGE: frozenset(
        {ResourceUsageSource.FILESYSTEM, ResourceUsageSource.EVENT_STORE_RETENTION}
    ),
}


@dataclass(frozen=True, slots=True)
class ResourceBudgetScope:
    project_id: str
    repository_head: str
    repository_root: str
    mission_id: str | None = None
    workflow_generation: int | None = None

    def __post_init__(self) -> None:
        MetricsScope(self.project_id, self.mission_id, self.workflow_generation)
        if not _SHA40.fullmatch(self.repository_head):
            raise ValueError("repository_head must be a lowercase SHA-1")
        _validate_absolute_path(self.repository_root, "repository_root")


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    budget_id: str
    version: str
    domain: ResourceBudgetDomain
    scope: ResourceBudgetScope
    limit: int
    unit: ResourceBudgetUnit
    policy_class: GovernancePolicyClass
    source_identity: str
    applicability: ResourceBudgetApplicability
    rationale: ResourceBudgetRationale

    def __post_init__(self) -> None:
        if not _safe_identity(self.budget_id):
            raise ValueError("budget_id is absent, unsafe, or non-canonical")
        if not isinstance(self.version, str) or not _VERSION.fullmatch(self.version):
            raise ValueError("budget version is invalid")
        if not isinstance(self.domain, ResourceBudgetDomain):
            raise ValueError("budget domain is invalid")
        if not isinstance(self.scope, ResourceBudgetScope):
            raise ValueError("budget scope is invalid")
        _validate_resource_value(self.limit, "limit")
        if not isinstance(self.unit, ResourceBudgetUnit) or self.unit is not _DOMAIN_UNIT[self.domain]:
            raise ValueError("budget unit is incompatible with its domain")
        if not isinstance(self.policy_class, GovernancePolicyClass):
            raise ValueError("budget policy_class is invalid")
        if not _safe_identity(self.source_identity):
            raise ValueError("budget source_identity is invalid")
        if not isinstance(self.applicability, ResourceBudgetApplicability):
            raise ValueError("budget applicability is invalid")
        if not isinstance(self.rationale, ResourceBudgetRationale):
            raise ValueError("budget rationale is invalid")


@dataclass(frozen=True, slots=True)
class ResourceUsageObservation:
    domain: ResourceBudgetDomain
    unit: ResourceBudgetUnit
    status: ResourceUsageStatus
    source: ResourceUsageSource
    source_identity: str
    scope: ResourceBudgetScope
    observed_at: datetime
    current_value: int | None
    requested_value: int
    active_identities: tuple[str, ...] = ()
    repository_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.domain, ResourceBudgetDomain):
            raise ValueError("usage domain is invalid")
        if not isinstance(self.unit, ResourceBudgetUnit):
            raise ValueError("usage unit is invalid")
        if not isinstance(self.status, ResourceUsageStatus):
            raise ValueError("usage status is invalid")
        if not isinstance(self.source, ResourceUsageSource):
            raise ValueError("usage source is invalid")
        if not _safe_identity(self.source_identity):
            raise ValueError("usage source_identity is invalid")
        if not isinstance(self.scope, ResourceBudgetScope):
            raise ValueError("usage scope is invalid")
        _validate_utc(self.observed_at, "observed_at")
        if self.status is ResourceUsageStatus.COMPLETE:
            if self.current_value is None:
                raise ValueError("complete usage requires a factual current_value")
            _validate_resource_value(self.current_value, "current_value")
        elif self.current_value is not None:
            raise ValueError("unknown or unavailable usage cannot claim current_value")
        _validate_resource_value(self.requested_value, "requested_value")
        if not isinstance(self.active_identities, tuple) or any(
            not _safe_identity(item) for item in self.active_identities
        ):
            raise ValueError("active identities must be an immutable canonical tuple")
        if not isinstance(self.repository_roots, tuple):
            raise ValueError("repository_roots must be immutable")
        for root in self.repository_roots:
            _validate_absolute_path(root, "resource repository_root")


@dataclass(frozen=True, slots=True)
class ResourceBudgetEvaluationContext:
    scope: ResourceBudgetScope
    operation: GovernedOperation
    evaluated_at: datetime
    budgets: tuple[ResourceBudget, ...]
    usage: tuple[ResourceUsageObservation, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ResourceBudgetScope):
            raise ValueError("resource budget scope is invalid")
        if not isinstance(self.operation, GovernedOperation):
            raise ValueError("operation is invalid")
        _validate_utc(self.evaluated_at, "evaluated_at")
        if not isinstance(self.budgets, tuple) or len(self.budgets) > MAX_RESOURCE_BUDGETS:
            raise ValueError("budgets must be an immutable bounded tuple")
        if any(not isinstance(item, ResourceBudget) for item in self.budgets):
            raise ValueError("budgets must contain ResourceBudget values")
        budget_ids = [item.budget_id for item in self.budgets]
        if len(budget_ids) != len(set(budget_ids)):
            raise ValueError("budget_id must be unique")
        if not isinstance(self.usage, tuple) or len(self.usage) > MAX_RESOURCE_USAGE_OBSERVATIONS:
            raise ValueError("usage must be an immutable bounded tuple")
        if any(not isinstance(item, ResourceUsageObservation) for item in self.usage):
            raise ValueError("usage must contain ResourceUsageObservation values")
        domains = [item.domain for item in self.usage]
        if len(domains) != len(set(domains)):
            raise ValueError("one current usage observation is allowed per domain")


@dataclass(frozen=True, slots=True)
class ResourceBudgetDomainDecision:
    domain: ResourceBudgetDomain
    unit: ResourceBudgetUnit
    decision: ResourceBudgetDecision
    effective_limit: int | None
    current_value: int | None
    requested_value: int | None
    effective_future_value: int | None
    budget_identities: tuple[str, ...]
    source_identities: tuple[str, ...]
    reasons: tuple[ResourceBudgetReason, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.domain, ResourceBudgetDomain) or self.unit is not _DOMAIN_UNIT[self.domain]:
            raise ValueError("domain decision binding is invalid")
        if not isinstance(self.decision, ResourceBudgetDecision):
            raise ValueError("resource budget decision is invalid")
        for value in (self.effective_limit, self.current_value, self.requested_value, self.effective_future_value):
            if value is not None:
                _validate_resource_value(value, "decision value")
        if not _canonical_identities(self.budget_identities) or not _canonical_identities(self.source_identities):
            raise ValueError("decision identities must be sorted, unique, and non-empty")
        if not isinstance(self.reasons, tuple) or not self.reasons:
            raise ValueError("decision reasons must be non-empty")
        if self.reasons != tuple(sorted(set(self.reasons), key=lambda item: item.value)):
            raise ValueError("decision reasons must be sorted and unique")
        expected = _expected_decision(
            self.effective_limit,
            self.current_value,
            self.requested_value,
            self.effective_future_value,
        )
        if self.decision is ResourceBudgetDecision.UNKNOWN:
            if all(value is not None for value in (self.effective_limit, self.current_value, self.requested_value, self.effective_future_value)):
                raise ValueError("UNKNOWN decision must preserve an explicit uncertainty")
        elif expected is not self.decision:
            raise ValueError("resource budget decision contradicts its values")


@dataclass(frozen=True, slots=True)
class ResourceBudgetDecisionSet:
    schema_version: str
    scope: ResourceBudgetScope
    operation: GovernedOperation
    evaluated_at: datetime
    decisions: tuple[ResourceBudgetDomainDecision, ...]
    fingerprint: str

    def __post_init__(self) -> None:
        if self.schema_version != RESOURCE_BUDGET_SCHEMA_VERSION:
            raise ValueError("unsupported resource budget schema_version")
        if not isinstance(self.scope, ResourceBudgetScope) or not isinstance(self.operation, GovernedOperation):
            raise ValueError("resource budget result binding is invalid")
        _validate_utc(self.evaluated_at, "evaluated_at")
        if not isinstance(self.decisions, tuple) or not self.decisions:
            raise ValueError("resource budget result requires decisions")
        if any(not isinstance(item, ResourceBudgetDomainDecision) for item in self.decisions):
            raise ValueError("resource budget decisions are invalid")
        if self.decisions != tuple(sorted(self.decisions, key=lambda item: item.domain.value)):
            raise ValueError("resource budget decisions must be canonically ordered")
        if len({item.domain for item in self.decisions}) != len(self.decisions):
            raise ValueError("resource budget decisions must have unique domains")
        if not _SHA256.fullmatch(self.fingerprint):
            raise ValueError("resource budget fingerprint is invalid")
        if self.fingerprint != resource_budget_decision_fingerprint(
            self.scope, self.operation, self.evaluated_at, self.decisions
        ):
            raise ValueError("resource budget fingerprint is inconsistent")


def resource_budget_decision_fingerprint(
    scope: ResourceBudgetScope,
    operation: GovernedOperation,
    evaluated_at: datetime,
    decisions: tuple[ResourceBudgetDomainDecision, ...],
) -> str:
    payload = {
        "schema_version": RESOURCE_BUDGET_SCHEMA_VERSION,
        "scope": {
            "project_id": scope.project_id,
            "repository_head": scope.repository_head,
            "repository_root": _path_key(scope.repository_root),
            "mission_id": scope.mission_id,
            "workflow_generation": scope.workflow_generation,
        },
        "operation": operation.value,
        "evaluated_at": evaluated_at.isoformat().replace("+00:00", "Z"),
        "decisions": [
            {
                "domain": item.domain.value,
                "unit": item.unit.value,
                "decision": item.decision.value,
                "effective_limit": item.effective_limit,
                "current_value": item.current_value,
                "requested_value": item.requested_value,
                "effective_future_value": item.effective_future_value,
                "budget_identities": item.budget_identities,
                "source_identities": item.source_identities,
                "reasons": tuple(reason.value for reason in item.reasons),
            }
            for item in decisions
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def expected_unit(domain: ResourceBudgetDomain) -> ResourceBudgetUnit:
    return _DOMAIN_UNIT[domain]


def allowed_usage_sources(domain: ResourceBudgetDomain) -> frozenset[ResourceUsageSource]:
    return _DOMAIN_SOURCES[domain]


def _expected_decision(
    limit: int | None,
    current: int | None,
    requested: int | None,
    future: int | None,
) -> ResourceBudgetDecision:
    if None in (limit, current, requested, future):
        return ResourceBudgetDecision.UNKNOWN
    assert limit is not None and future is not None
    if future > limit:
        return ResourceBudgetDecision.LIMIT_EXCEEDED
    if future == limit:
        return ResourceBudgetDecision.LIMIT_REACHED
    if limit > 0 and future * 5 >= limit * 4:
        return ResourceBudgetDecision.NEAR_LIMIT
    return ResourceBudgetDecision.WITHIN_BUDGET


def _validate_resource_value(value: object, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_RESOURCE_VALUE:
        raise ValueError(f"{name} must be a non-negative 64-bit integer")


def _safe_identity(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and _ID.fullmatch(value)
        and not _SENSITIVE.search(value)
        and not _KNOWN_TOKEN.search(value)
    )


def _canonical_identities(values: object) -> bool:
    return bool(
        isinstance(values, tuple)
        and values
        and all(_safe_identity(item) for item in values)
        and values == tuple(sorted(set(values)))
    )


def _validate_utc(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is not timezone.utc or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone.utc")


def _validate_absolute_path(value: object, name: str) -> None:
    if not isinstance(value, str) or not value or not Path(value).is_absolute() or ".." in Path(value).parts:
        raise ValueError(f"{name} must be an absolute traversal-free path")


def _path_key(value: str) -> str:
    return os.path.normcase(str(Path(value).resolve(strict=False))).casefold()
