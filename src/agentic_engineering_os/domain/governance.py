"""Strict, immutable and non-authoritative governance policy contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from .enums import MissionRole
from .health import HealthSnapshot, MetricsHealthInput
from .metrics import MetricsScope


GOVERNANCE_SCHEMA_VERSION = "1.0"
MAX_GOVERNANCE_POLICIES = 32

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_VERSION = re.compile(r"^[1-9][0-9]*\.[0-9]+$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE = re.compile(
    r"(?:^|[._:-])(?:api[_-]?key|password|secret|token|authorization)(?:[._:-]|$)",
    re.IGNORECASE,
)
_DYNAMIC_CODE = re.compile(
    r"(?:^|[._:-])(?:eval|exec|import|script|shell|command)(?:[._:-]|$)",
    re.IGNORECASE,
)
_KNOWN_TOKEN = re.compile(
    r"(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})"
)


class GovernancePolicyClass(str, Enum):
    HARD_SAFETY_POLICY = "HARD_SAFETY_POLICY"
    OPERATIONAL_POLICY = "OPERATIONAL_POLICY"
    OPERATOR_PREFERENCE = "OPERATOR_PREFERENCE"


class GovernancePolicyDomain(str, Enum):
    EXECUTION_ADMISSION = "EXECUTION_ADMISSION"
    SANDBOX_SAFETY = "SANDBOX_SAFETY"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    MAINTENANCE_MODE = "MAINTENANCE_MODE"
    OBSERVABILITY_REQUIRED = "OBSERVABILITY_REQUIRED"
    HEALTH_GATING = "HEALTH_GATING"
    VERIFICATION_TIER = "VERIFICATION_TIER"
    OPERATOR_INTERVENTION = "OPERATOR_INTERVENTION"


class GovernedOperation(str, Enum):
    EXECUTION = "EXECUTION"
    MERGE = "MERGE"
    RECOVERY = "RECOVERY"
    MAINTENANCE = "MAINTENANCE"
    VERIFICATION = "VERIFICATION"
    DEPLOYMENT = "DEPLOYMENT"


class GovernanceCondition(str, Enum):
    ALWAYS = "ALWAYS"
    HEALTH_BLOCKED = "HEALTH_BLOCKED"
    HEALTH_UNKNOWN = "HEALTH_UNKNOWN"
    HEALTH_DEGRADED = "HEALTH_DEGRADED"
    OBSERVABILITY_NOT_HEALTHY = "OBSERVABILITY_NOT_HEALTHY"
    RECOVERY_NOT_HEALTHY = "RECOVERY_NOT_HEALTHY"
    CODEX_RUNTIME_NOT_HEALTHY = "CODEX_RUNTIME_NOT_HEALTHY"
    DEPLOYMENT_NOT_HEALTHY = "DEPLOYMENT_NOT_HEALTHY"
    METRICS_NOT_COMPLETE = "METRICS_NOT_COMPLETE"
    SANDBOX_NONCOMPLIANT = "SANDBOX_NONCOMPLIANT"
    VERIFICATION_INCOMPLETE = "VERIFICATION_INCOMPLETE"
    MAINTENANCE_REQUESTED = "MAINTENANCE_REQUESTED"
    OPERATOR_INTERVENTION_REQUESTED = "OPERATOR_INTERVENTION_REQUESTED"


class GovernanceDecision(str, Enum):
    ALLOW = "ALLOW"
    ALLOW_WITH_WARNING = "ALLOW_WITH_WARNING"
    REQUIRE_OPERATOR = "REQUIRE_OPERATOR"
    BLOCK = "BLOCK"


class GovernanceRationale(str, Enum):
    PROTECT_SAFETY_INVARIANTS = "PROTECT_SAFETY_INVARIANTS"
    REQUIRE_RELIABLE_OBSERVABILITY = "REQUIRE_RELIABLE_OBSERVABILITY"
    REQUIRE_CLEAR_RECOVERY = "REQUIRE_CLEAR_RECOVERY"
    ENFORCE_SANDBOX = "ENFORCE_SANDBOX"
    REQUIRE_VERIFICATION = "REQUIRE_VERIFICATION"
    RESPECT_OPERATOR_REQUEST = "RESPECT_OPERATOR_REQUEST"
    MAINTENANCE_REQUEST = "MAINTENANCE_REQUEST"
    OPERATIONAL_CAUTION = "OPERATIONAL_CAUTION"


class GovernanceReasonCode(str, Enum):
    POLICY_MATCHED = "POLICY_MATCHED"
    CONDITION_NOT_MET = "CONDITION_NOT_MET"
    POLICY_DISABLED = "POLICY_DISABLED"
    OPERATION_OUT_OF_SCOPE = "OPERATION_OUT_OF_SCOPE"
    POLICY_SCOPE_MISMATCH = "POLICY_SCOPE_MISMATCH"
    POLICY_CONFLICT = "POLICY_CONFLICT"
    MISSING_HARD_SAFETY_POLICY = "MISSING_HARD_SAFETY_POLICY"
    HEALTH_BLOCKED_FLOOR = "HEALTH_BLOCKED_FLOOR"
    HEALTH_UNKNOWN_FLOOR = "HEALTH_UNKNOWN_FLOOR"
    HEALTH_DEGRADED_FLOOR = "HEALTH_DEGRADED_FLOOR"
    HEALTH_STALE = "HEALTH_STALE"
    HEALTH_SCOPE_MISMATCH = "HEALTH_SCOPE_MISMATCH"
    METRICS_REQUIRED = "METRICS_REQUIRED"
    METRICS_NOT_COMPLETE = "METRICS_NOT_COMPLETE"
    METRICS_STALE = "METRICS_STALE"
    METRICS_SCOPE_MISMATCH = "METRICS_SCOPE_MISMATCH"
    FACT_REQUIRED = "FACT_REQUIRED"
    NO_ADDITIONAL_CONSTRAINT = "NO_ADDITIONAL_CONSTRAINT"


_DOMAIN_CONDITIONS = {
    GovernancePolicyDomain.EXECUTION_ADMISSION: frozenset(
        {
            GovernanceCondition.ALWAYS,
            GovernanceCondition.HEALTH_BLOCKED,
            GovernanceCondition.HEALTH_UNKNOWN,
            GovernanceCondition.HEALTH_DEGRADED,
        }
    ),
    GovernancePolicyDomain.SANDBOX_SAFETY: frozenset(
        {GovernanceCondition.SANDBOX_NONCOMPLIANT}
    ),
    GovernancePolicyDomain.RECOVERY_REQUIRED: frozenset(
        {GovernanceCondition.RECOVERY_NOT_HEALTHY}
    ),
    GovernancePolicyDomain.MAINTENANCE_MODE: frozenset(
        {GovernanceCondition.MAINTENANCE_REQUESTED}
    ),
    GovernancePolicyDomain.OBSERVABILITY_REQUIRED: frozenset(
        {
            GovernanceCondition.OBSERVABILITY_NOT_HEALTHY,
            GovernanceCondition.METRICS_NOT_COMPLETE,
        }
    ),
    GovernancePolicyDomain.HEALTH_GATING: frozenset(
        {
            GovernanceCondition.HEALTH_BLOCKED,
            GovernanceCondition.HEALTH_UNKNOWN,
            GovernanceCondition.HEALTH_DEGRADED,
        }
    ),
    GovernancePolicyDomain.VERIFICATION_TIER: frozenset(
        {GovernanceCondition.VERIFICATION_INCOMPLETE}
    ),
    GovernancePolicyDomain.OPERATOR_INTERVENTION: frozenset(
        {GovernanceCondition.OPERATOR_INTERVENTION_REQUESTED}
    ),
}


@dataclass(frozen=True, slots=True)
class GovernanceScope:
    project_id: str
    repository_head: str
    mission_id: str | None = None
    workflow_generation: int | None = None
    role: MissionRole | None = None
    execution_id: str | None = None
    worktree_id: str | None = None

    def __post_init__(self) -> None:
        MetricsScope(
            self.project_id,
            self.mission_id,
            self.workflow_generation,
            role=self.role,
            execution_id=self.execution_id,
        )
        if not _SHA40.fullmatch(self.repository_head):
            raise ValueError("repository_head must be a lowercase SHA-1")
        if self.worktree_id is not None and (
            self.mission_id is None or not _safe_identifier(self.worktree_id)
        ):
            raise ValueError("worktree_id requires mission scope and canonical identity")


@dataclass(frozen=True, slots=True)
class GovernancePolicyScope:
    project_id: str
    operations: tuple[GovernedOperation, ...]
    mission_id: str | None = None
    workflow_generation: int | None = None
    role: MissionRole | None = None

    def __post_init__(self) -> None:
        MetricsScope(
            self.project_id,
            self.mission_id,
            self.workflow_generation,
            role=self.role,
        )
        if not isinstance(self.operations, tuple) or not self.operations:
            raise ValueError("policy operations must be a non-empty immutable tuple")
        if any(not isinstance(item, GovernedOperation) for item in self.operations):
            raise ValueError("policy operation is invalid")
        order = tuple(item for item in GovernedOperation if item in self.operations)
        if self.operations != order:
            raise ValueError("policy operations must be unique and canonically ordered")


@dataclass(frozen=True, slots=True)
class GovernancePolicy:
    policy_id: str
    version: str
    policy_class: GovernancePolicyClass
    domain: GovernancePolicyDomain
    enabled: bool
    scope: GovernancePolicyScope
    condition: GovernanceCondition
    action: GovernanceDecision
    rationale: GovernanceRationale

    def __post_init__(self) -> None:
        if not _safe_identifier(self.policy_id):
            raise ValueError("policy_id is absent, unsafe, or non-canonical")
        if not isinstance(self.version, str) or not _VERSION.fullmatch(self.version):
            raise ValueError("policy version is invalid")
        if not isinstance(self.policy_class, GovernancePolicyClass):
            raise ValueError("policy class is invalid")
        if not isinstance(self.domain, GovernancePolicyDomain):
            raise ValueError("policy domain is invalid")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be an explicit boolean")
        if not isinstance(self.scope, GovernancePolicyScope):
            raise ValueError("policy scope is invalid")
        if not isinstance(self.condition, GovernanceCondition):
            raise ValueError("policy condition is invalid")
        if self.condition not in _DOMAIN_CONDITIONS[self.domain]:
            raise ValueError("policy condition is incompatible with its domain")
        if not isinstance(self.action, GovernanceDecision):
            raise ValueError("policy action is invalid")
        if not isinstance(self.rationale, GovernanceRationale):
            raise ValueError("policy rationale is invalid")
        if self.policy_class is GovernancePolicyClass.HARD_SAFETY_POLICY:
            if not self.enabled:
                raise ValueError("hard safety policy cannot be disabled")
            if self.action not in {
                GovernanceDecision.BLOCK,
                GovernanceDecision.REQUIRE_OPERATOR,
            }:
                raise ValueError("hard safety policy cannot weaken the safety floor")
        if self.policy_class is GovernancePolicyClass.OPERATOR_PREFERENCE and self.action not in {
            GovernanceDecision.ALLOW,
            GovernanceDecision.ALLOW_WITH_WARNING,
        }:
            raise ValueError("operator preference cannot impose or bypass authority")


@dataclass(frozen=True, slots=True)
class GovernanceEvaluationContext:
    scope: GovernanceScope
    operation: GovernedOperation
    evaluated_at: datetime
    health: HealthSnapshot
    metrics: MetricsHealthInput | None
    policies: tuple[GovernancePolicy, ...]
    sandbox_compliant: bool | None = None
    verification_complete: bool | None = None
    maintenance_requested: bool = False
    operator_intervention_requested: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.scope, GovernanceScope):
            raise ValueError("governance scope is invalid")
        if not isinstance(self.operation, GovernedOperation):
            raise ValueError("governed operation is invalid")
        _validate_utc(self.evaluated_at, "evaluated_at")
        if not isinstance(self.health, HealthSnapshot):
            raise ValueError("health must be HealthSnapshot")
        if self.metrics is not None and not isinstance(self.metrics, MetricsHealthInput):
            raise ValueError("metrics must be MetricsHealthInput or None")
        if not isinstance(self.policies, tuple):
            raise ValueError("policies must be immutable")
        if len(self.policies) > MAX_GOVERNANCE_POLICIES:
            raise ValueError("policy count exceeds policy")
        if any(not isinstance(item, GovernancePolicy) for item in self.policies):
            raise ValueError("policies must contain GovernancePolicy values")
        identities = [item.policy_id for item in self.policies]
        if len(set(identities)) != len(identities):
            raise ValueError(
                "policy_id must be unique; only one version may be evaluated"
            )
        for name in ("sandbox_compliant", "verification_complete"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{name} must be boolean or None")
        if not isinstance(self.maintenance_requested, bool) or not isinstance(
            self.operator_intervention_requested, bool
        ):
            raise ValueError("request facts must be explicit booleans")


@dataclass(frozen=True, slots=True)
class GovernancePolicyResult:
    policy_id: str
    version: str
    policy_class: GovernancePolicyClass
    domain: GovernancePolicyDomain
    matched: bool
    decision: GovernanceDecision | None
    reason: GovernanceReasonCode

    def __post_init__(self) -> None:
        if not _safe_identifier(self.policy_id) or not _VERSION.fullmatch(self.version):
            raise ValueError("policy result identity is invalid")
        if not isinstance(self.policy_class, GovernancePolicyClass) or not isinstance(
            self.domain, GovernancePolicyDomain
        ):
            raise ValueError("policy result classification is invalid")
        if not isinstance(self.matched, bool) or not isinstance(
            self.reason, GovernanceReasonCode
        ):
            raise ValueError("policy result is invalid")
        if self.matched != (self.decision is not None):
            raise ValueError("matched policy result must carry exactly one decision")
        if self.matched != (self.reason is GovernanceReasonCode.POLICY_MATCHED):
            raise ValueError("policy result reason contradicts match status")
        if (
            self.policy_class is GovernancePolicyClass.HARD_SAFETY_POLICY
            and self.decision
            not in {None, GovernanceDecision.BLOCK, GovernanceDecision.REQUIRE_OPERATOR}
        ):
            raise ValueError("hard safety result cannot weaken the safety floor")
        if (
            self.policy_class is GovernancePolicyClass.OPERATOR_PREFERENCE
            and self.decision
            not in {
                None,
                GovernanceDecision.ALLOW,
                GovernanceDecision.ALLOW_WITH_WARNING,
            }
        ):
            raise ValueError("operator preference result cannot impose authority")


@dataclass(frozen=True, slots=True)
class GovernanceDecisionSet:
    schema_version: str
    scope: GovernanceScope
    operation: GovernedOperation
    evaluated_at: datetime
    input_floor: GovernanceDecision
    decision: GovernanceDecision
    policy_results: tuple[GovernancePolicyResult, ...]
    reasons: tuple[GovernanceReasonCode, ...]
    source_identities: tuple[str, ...]
    fingerprint: str

    def __post_init__(self) -> None:
        if self.schema_version != GOVERNANCE_SCHEMA_VERSION:
            raise ValueError("unsupported governance schema_version")
        if not isinstance(self.scope, GovernanceScope) or not isinstance(
            self.operation, GovernedOperation
        ):
            raise ValueError("governance decision binding is invalid")
        _validate_utc(self.evaluated_at, "evaluated_at")
        if not isinstance(self.input_floor, GovernanceDecision) or not isinstance(
            self.decision, GovernanceDecision
        ):
            raise ValueError("governance decisions are invalid")
        if not isinstance(self.policy_results, tuple) or any(
            not isinstance(item, GovernancePolicyResult) for item in self.policy_results
        ):
            raise ValueError("policy results must be immutable")
        if self.policy_results != tuple(sorted(self.policy_results, key=_policy_result_key)):
            raise ValueError("policy results must be canonically ordered")
        expected = max(
            (
                self.input_floor,
                *(item.decision for item in self.policy_results if item.decision is not None),
            ),
            key=_decision_rank,
        )
        if self.decision is not expected:
            raise ValueError("aggregate governance decision is inconsistent")
        if not isinstance(self.reasons, tuple) or not self.reasons or any(
            not isinstance(item, GovernanceReasonCode) for item in self.reasons
        ):
            raise ValueError("governance reasons are invalid")
        if self.reasons != tuple(sorted(set(self.reasons), key=lambda item: item.value)):
            raise ValueError("governance reasons must be sorted and unique")
        if (
            not isinstance(self.source_identities, tuple)
            or not self.source_identities
            or tuple(sorted(set(self.source_identities))) != self.source_identities
        ):
            raise ValueError("source identities must be sorted and unique")
        if any(not _safe_identifier(item) for item in self.source_identities):
            raise ValueError("source identity is unsafe")
        if not _SHA256.fullmatch(self.fingerprint):
            raise ValueError("governance fingerprint is invalid")


def _decision_rank(decision: GovernanceDecision) -> int:
    return {
        GovernanceDecision.ALLOW: 0,
        GovernanceDecision.ALLOW_WITH_WARNING: 1,
        GovernanceDecision.REQUIRE_OPERATOR: 2,
        GovernanceDecision.BLOCK: 3,
    }[decision]


def _policy_result_key(result: GovernancePolicyResult) -> tuple[int, str, str, str]:
    class_rank = {
        GovernancePolicyClass.HARD_SAFETY_POLICY: 0,
        GovernancePolicyClass.OPERATIONAL_POLICY: 1,
        GovernancePolicyClass.OPERATOR_PREFERENCE: 2,
    }[result.policy_class]
    return class_rank, result.domain.value, result.policy_id, result.version


def _safe_identifier(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and _ID.fullmatch(value)
        and not _SENSITIVE.search(value)
        and not _DYNAMIC_CODE.search(value)
        and not _KNOWN_TOKEN.search(value)
    )


def _validate_utc(value: datetime, name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is not timezone.utc
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"{name} must be timezone.utc")
