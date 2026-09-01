"""Closed, persistent contracts for maintenance and recovery admission."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from .governance import GovernanceDecisionSet
from .health import HealthSnapshot
from .identity import is_attributable_human_identity, is_codex_identity
from .incidents import IncidentRecord
from .metrics import MetricsScope
from .resource_budgets import ResourceBudgetDecisionSet


MAINTENANCE_SCHEMA_VERSION = "1.0"
MAINTENANCE_MAX_SOURCE_AGE = timedelta(minutes=5)
MAX_MAINTENANCE_INCIDENTS = 256

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class MaintenanceState(str, Enum):
    NORMAL = "NORMAL"
    DRAINING = "DRAINING"
    MAINTENANCE = "MAINTENANCE"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    FROZEN = "FROZEN"


class MaintenanceOperation(str, Enum):
    READ_DIAGNOSTICS = "READ_DIAGNOSTICS"
    START_MISSION = "START_MISSION"
    START_ROLE_EXECUTION = "START_ROLE_EXECUTION"
    START_PARALLEL_GROUP = "START_PARALLEL_GROUP"
    CREATE_WORKTREE = "CREATE_WORKTREE"
    MERGE = "MERGE"
    COMPLETE_IN_FLIGHT = "COMPLETE_IN_FLIGHT"
    START_REMEDIATION = "START_REMEDIATION"
    RESUME_RECOVERY = "RESUME_RECOVERY"
    APPLY_ADOPTION_MIGRATION = "APPLY_ADOPTION_MIGRATION"


class MaintenanceAdmissionDecision(str, Enum):
    ADMITTED = "ADMITTED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    REFUSED = "REFUSED"


class MaintenanceAdmissionReason(str, Enum):
    READ_ONLY_DIAGNOSTIC = "READ_ONLY_DIAGNOSTIC"
    NORMAL_OPERATION = "NORMAL_OPERATION"
    DRAINING_NEW_WORK_REFUSED = "DRAINING_NEW_WORK_REFUSED"
    DRAINING_SAFE_COMPLETION = "DRAINING_SAFE_COMPLETION"
    MAINTENANCE_NEW_WORK_REFUSED = "MAINTENANCE_NEW_WORK_REFUSED"
    RECOVERY_OPERATION_REQUIRES_HUMAN = "RECOVERY_OPERATION_REQUIRES_HUMAN"
    FROZEN_NEW_WORK_REFUSED = "FROZEN_NEW_WORK_REFUSED"
    HEALTH_BLOCKED_OR_UNKNOWN = "HEALTH_BLOCKED_OR_UNKNOWN"
    GOVERNANCE_BLOCK = "GOVERNANCE_BLOCK"
    GOVERNANCE_REQUIRES_HUMAN = "GOVERNANCE_REQUIRES_HUMAN"
    RESOURCE_BUDGET_REFUSAL = "RESOURCE_BUDGET_REFUSAL"
    CRITICAL_INCIDENT_ACTIVE = "CRITICAL_INCIDENT_ACTIVE"
    SOURCE_SCOPE_MISMATCH = "SOURCE_SCOPE_MISMATCH"
    SOURCE_STALE = "SOURCE_STALE"


class MaintenanceTransitionReason(str, Enum):
    OPERATOR_DRAIN = "OPERATOR_DRAIN"
    OPERATOR_MAINTENANCE = "OPERATOR_MAINTENANCE"
    INCIDENT_ESCALATION = "INCIDENT_ESCALATION"
    RECOVERY_COORDINATION = "RECOVERY_COORDINATION"
    OPERATOR_RETURN_TO_NORMAL = "OPERATOR_RETURN_TO_NORMAL"


class RecoveryRoute(str, Enum):
    P2_SEQUENTIAL_REMEDIATION = "P2_SEQUENTIAL_REMEDIATION"
    P3_PARALLEL_RECOVERY = "P3_PARALLEL_RECOVERY"
    P4_EXECUTION_RECOVERY = "P4_EXECUTION_RECOVERY"
    P5_ADOPTION_MIGRATION_RECOVERY = "P5_ADOPTION_MIGRATION_RECOVERY"


class RecoveryObservationStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class MaintenanceScope:
    project_id: str
    repository_root: str

    def __post_init__(self) -> None:
        MetricsScope(self.project_id)
        root = Path(self.repository_root)
        if not root.is_absolute() or ".." in root.parts:
            raise ValueError("repository_root must be absolute and traversal-free")
        object.__setattr__(self, "repository_root", _path_key(self.repository_root))


@dataclass(frozen=True, slots=True)
class MaintenanceRecord:
    schema_version: str
    scope: MaintenanceScope
    state: MaintenanceState
    revision: int
    updated_at: datetime
    actor_identity: str
    repository_head: str
    mission_id: str | None
    workflow_generation: int | None
    transition_reason: MaintenanceTransitionReason
    recovery_route: RecoveryRoute | None
    previous_fingerprint: str | None
    fingerprint: str

    @classmethod
    def create(
        cls,
        *,
        scope: MaintenanceScope,
        state: MaintenanceState,
        revision: int,
        updated_at: datetime,
        actor_identity: str,
        repository_head: str,
        mission_id: str | None,
        workflow_generation: int | None,
        transition_reason: MaintenanceTransitionReason,
        recovery_route: RecoveryRoute | None,
        previous_fingerprint: str | None,
    ) -> MaintenanceRecord:
        values = {
            "schema_version": MAINTENANCE_SCHEMA_VERSION,
            "scope": {"project_id": scope.project_id, "repository_root": _path_key(scope.repository_root)},
            "state": state.value,
            "revision": revision,
            "updated_at": updated_at.isoformat().replace("+00:00", "Z"),
            "actor_identity": actor_identity,
            "repository_head": repository_head,
            "mission_id": mission_id,
            "workflow_generation": workflow_generation,
            "transition_reason": transition_reason.value,
            "recovery_route": recovery_route.value if recovery_route else None,
            "previous_fingerprint": previous_fingerprint,
        }
        fingerprint = hashlib.sha256(
            json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        return cls(
            MAINTENANCE_SCHEMA_VERSION, scope, state, revision, updated_at,
            actor_identity, repository_head, mission_id, workflow_generation,
            transition_reason, recovery_route, previous_fingerprint, fingerprint,
        )

    def __post_init__(self) -> None:
        if self.schema_version != MAINTENANCE_SCHEMA_VERSION:
            raise ValueError("unsupported maintenance schema_version")
        if not isinstance(self.scope, MaintenanceScope):
            raise ValueError("maintenance scope is invalid")
        if not isinstance(self.state, MaintenanceState):
            raise ValueError("maintenance state is invalid")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 1:
            raise ValueError("maintenance revision must be positive")
        _validate_utc(self.updated_at, "updated_at")
        if not is_attributable_human_identity(self.actor_identity):
            raise ValueError("maintenance actor must be attributable Human")
        if not _SHA40.fullmatch(self.repository_head):
            raise ValueError("repository_head must be lowercase SHA-1")
        MetricsScope(self.scope.project_id, self.mission_id, self.workflow_generation)
        if not isinstance(self.transition_reason, MaintenanceTransitionReason):
            raise ValueError("transition reason is invalid")
        if self.recovery_route is not None and not isinstance(self.recovery_route, RecoveryRoute):
            raise ValueError("recovery route is invalid")
        if self.state is MaintenanceState.RECOVERY_REQUIRED and self.recovery_route is None:
            raise ValueError("RECOVERY_REQUIRED requires an explicit recovery route")
        if self.state is not MaintenanceState.RECOVERY_REQUIRED and self.recovery_route is not None:
            raise ValueError("recovery route is only retained by RECOVERY_REQUIRED")
        if self.revision == 1:
            if self.previous_fingerprint is not None:
                raise ValueError("initial maintenance record has no predecessor")
        elif not isinstance(self.previous_fingerprint, str) or not _SHA256.fullmatch(self.previous_fingerprint):
            raise ValueError("maintenance predecessor fingerprint is invalid")
        if not _SHA256.fullmatch(self.fingerprint):
            raise ValueError("maintenance fingerprint is invalid")
        if self.fingerprint != maintenance_record_fingerprint(self, include_fingerprint=False):
            raise ValueError("maintenance fingerprint is inconsistent")


@dataclass(frozen=True, slots=True)
class RecoveryObservation:
    route: RecoveryRoute
    status: RecoveryObservationStatus
    scope: MaintenanceScope
    repository_head: str
    mission_id: str | None
    workflow_generation: int | None
    observed_at: datetime
    source_identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.route, RecoveryRoute) or not isinstance(self.status, RecoveryObservationStatus):
            raise ValueError("recovery observation classification is invalid")
        if not isinstance(self.scope, MaintenanceScope):
            raise ValueError("recovery observation scope is invalid")
        if not _SHA40.fullmatch(self.repository_head):
            raise ValueError("recovery observation HEAD is invalid")
        MetricsScope(self.scope.project_id, self.mission_id, self.workflow_generation)
        _validate_utc(self.observed_at, "observed_at")
        if not _safe_identity(self.source_identity) or is_codex_identity(self.source_identity):
            raise ValueError("recovery source identity is invalid")


@dataclass(frozen=True, slots=True)
class MaintenanceEvaluationContext:
    scope: MaintenanceScope
    repository_head: str
    mission_id: str | None
    workflow_generation: int | None
    evaluated_at: datetime
    health: HealthSnapshot
    governance: GovernanceDecisionSet
    resource_budgets: ResourceBudgetDecisionSet
    incidents: tuple[IncidentRecord, ...]
    recovery: RecoveryObservation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, MaintenanceScope):
            raise ValueError("maintenance evaluation scope is invalid")
        if not _SHA40.fullmatch(self.repository_head):
            raise ValueError("maintenance evaluation HEAD is invalid")
        MetricsScope(self.scope.project_id, self.mission_id, self.workflow_generation)
        _validate_utc(self.evaluated_at, "evaluated_at")
        if not isinstance(self.health, HealthSnapshot):
            raise ValueError("health source is invalid")
        if not isinstance(self.governance, GovernanceDecisionSet):
            raise ValueError("governance source is invalid")
        if not isinstance(self.resource_budgets, ResourceBudgetDecisionSet):
            raise ValueError("resource budget source is invalid")
        if (
            not isinstance(self.incidents, tuple)
            or len(self.incidents) > MAX_MAINTENANCE_INCIDENTS
            or any(not isinstance(item, IncidentRecord) for item in self.incidents)
        ):
            raise ValueError("incidents must be an immutable bounded tuple")
        if self.recovery is not None and not isinstance(self.recovery, RecoveryObservation):
            raise ValueError("recovery observation is invalid")


@dataclass(frozen=True, slots=True)
class MaintenanceAdmission:
    operation: MaintenanceOperation
    state: MaintenanceState
    decision: MaintenanceAdmissionDecision
    reasons: tuple[MaintenanceAdmissionReason, ...]
    maintenance_fingerprint: str
    evaluated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.operation, MaintenanceOperation):
            raise ValueError("maintenance operation is invalid")
        if not isinstance(self.state, MaintenanceState) or not isinstance(self.decision, MaintenanceAdmissionDecision):
            raise ValueError("maintenance admission classification is invalid")
        if not isinstance(self.reasons, tuple) or not self.reasons:
            raise ValueError("maintenance admission requires reasons")
        if self.reasons != tuple(sorted(set(self.reasons), key=lambda item: item.value)):
            raise ValueError("maintenance admission reasons must be canonical")
        if not _SHA256.fullmatch(self.maintenance_fingerprint):
            raise ValueError("maintenance admission fingerprint is invalid")
        _validate_utc(self.evaluated_at, "evaluated_at")


@dataclass(frozen=True, slots=True)
class MaintenanceInitializationRequest:
    scope: MaintenanceScope
    repository_head: str
    mission_id: str | None
    workflow_generation: int | None
    requested_at: datetime
    operator_identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, MaintenanceScope):
            raise ValueError("initialization scope is invalid")
        if not _SHA40.fullmatch(self.repository_head):
            raise ValueError("initialization HEAD is invalid")
        MetricsScope(self.scope.project_id, self.mission_id, self.workflow_generation)
        _validate_utc(self.requested_at, "requested_at")
        if not is_attributable_human_identity(self.operator_identity):
            raise ValueError("initialization requires attributable Human operator")


@dataclass(frozen=True, slots=True)
class MaintenanceTransitionRequest:
    scope: MaintenanceScope
    repository_head: str
    mission_id: str | None
    workflow_generation: int | None
    expected_revision: int
    expected_fingerprint: str
    target_state: MaintenanceState
    reason: MaintenanceTransitionReason
    requested_at: datetime
    operator_identity: str
    recovery_route: RecoveryRoute | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, MaintenanceScope):
            raise ValueError("transition scope is invalid")
        if not _SHA40.fullmatch(self.repository_head):
            raise ValueError("transition HEAD is invalid")
        MetricsScope(self.scope.project_id, self.mission_id, self.workflow_generation)
        if not isinstance(self.expected_revision, int) or isinstance(self.expected_revision, bool) or self.expected_revision < 1:
            raise ValueError("expected revision is invalid")
        if not _SHA256.fullmatch(self.expected_fingerprint):
            raise ValueError("expected fingerprint is invalid")
        if not isinstance(self.target_state, MaintenanceState) or not isinstance(self.reason, MaintenanceTransitionReason):
            raise ValueError("transition target or reason is invalid")
        _validate_utc(self.requested_at, "requested_at")
        if not is_attributable_human_identity(self.operator_identity):
            raise ValueError("transition requires attributable Human operator")
        if self.recovery_route is not None and not isinstance(self.recovery_route, RecoveryRoute):
            raise ValueError("transition recovery route is invalid")
        if self.target_state is MaintenanceState.RECOVERY_REQUIRED and self.recovery_route is None:
            raise ValueError("RECOVERY_REQUIRED transition requires a route")
        if self.target_state is not MaintenanceState.RECOVERY_REQUIRED and self.recovery_route is not None:
            raise ValueError("recovery route is only valid for RECOVERY_REQUIRED")


@dataclass(frozen=True, slots=True)
class RecoveryDispatchRequest:
    route: RecoveryRoute
    boundary: str
    maintenance_fingerprint: str
    project_id: str
    repository_head: str
    mission_id: str | None
    workflow_generation: int | None
    requested_at: datetime
    operator_identity: str


@dataclass(frozen=True, slots=True)
class MaintenanceTransitionResult:
    previous_state: MaintenanceState
    record: MaintenanceRecord
    recovery_request: RecoveryDispatchRequest | None


def maintenance_record_fingerprint(record: MaintenanceRecord, *, include_fingerprint: bool = True) -> str:
    payload = maintenance_record_to_dict(record)
    if not include_fingerprint:
        payload.pop("fingerprint")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def maintenance_record_to_dict(record: MaintenanceRecord) -> dict[str, object]:
    return {
        "schema_version": record.schema_version,
        "scope": {
            "project_id": record.scope.project_id,
            "repository_root": _path_key(record.scope.repository_root),
        },
        "state": record.state.value,
        "revision": record.revision,
        "updated_at": record.updated_at.isoformat().replace("+00:00", "Z"),
        "actor_identity": record.actor_identity,
        "repository_head": record.repository_head,
        "mission_id": record.mission_id,
        "workflow_generation": record.workflow_generation,
        "transition_reason": record.transition_reason.value,
        "recovery_route": record.recovery_route.value if record.recovery_route else None,
        "previous_fingerprint": record.previous_fingerprint,
        "fingerprint": record.fingerprint,
    }


def maintenance_record_from_dict(value: object) -> MaintenanceRecord:
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "scope", "state", "revision", "updated_at", "actor_identity",
        "repository_head", "mission_id", "workflow_generation", "transition_reason",
        "recovery_route", "previous_fingerprint", "fingerprint",
    }:
        raise ValueError("maintenance record fields are invalid")
    scope = value["scope"]
    if not isinstance(scope, dict) or set(scope) != {"project_id", "repository_root"}:
        raise ValueError("maintenance scope fields are invalid")
    route = value["recovery_route"]
    return MaintenanceRecord(
        _string(value["schema_version"], "schema_version"),
        MaintenanceScope(_string(scope["project_id"], "project_id"), _string(scope["repository_root"], "repository_root")),
        MaintenanceState(_string(value["state"], "state")),
        _integer(value["revision"], "revision"),
        _datetime(value["updated_at"]),
        _string(value["actor_identity"], "actor_identity"),
        _string(value["repository_head"], "repository_head"),
        _optional_string(value["mission_id"], "mission_id"),
        _optional_integer(value["workflow_generation"], "workflow_generation"),
        MaintenanceTransitionReason(_string(value["transition_reason"], "transition_reason")),
        RecoveryRoute(_string(route, "recovery_route")) if route is not None else None,
        _optional_string(value["previous_fingerprint"], "previous_fingerprint"),
        _string(value["fingerprint"], "fingerprint"),
    )


def _datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("maintenance timestamp is invalid")
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError("maintenance timestamp is invalid") from error
    _validate_utc(result, "timestamp")
    return result


def _integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be integer")
    return value


def _optional_integer(value: object, name: str) -> int | None:
    return None if value is None else _integer(value, name)


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be string or null")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be string")
    return value


def _safe_identity(value: object) -> bool:
    return isinstance(value, str) and bool(_IDENTITY.fullmatch(value))


def _validate_utc(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is not timezone.utc or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be explicit UTC")


def _path_key(value: str) -> str:
    return os.path.normcase(str(Path(value).resolve(strict=False)))
