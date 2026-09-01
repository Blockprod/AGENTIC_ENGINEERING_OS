"""Immutable, strict and non-authoritative operational incident contracts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import cast

from .governance import GovernedOperation
from .governance import GovernanceDecisionSet
from .health import HealthSnapshot, MetricsHealthInput
from .identity import is_attributable_human_identity
from .enums import MissionRole
from .metrics import MetricsScope
from .operational_events import OperationalEvent
from .resource_budgets import ResourceBudgetDecisionSet, ResourceBudgetDomain


INCIDENT_SCHEMA_VERSION = "1.0"
INCIDENT_MAX_OBSERVATION_AGE = timedelta(minutes=5)
MAX_INCIDENT_DIAGNOSTICS = 32
MAX_INCIDENT_RECORDS = 256
MAX_INCIDENT_SOURCES = 8
MAX_INCIDENT_RECORD_BYTES = 2_048
MAX_INCIDENT_SOURCE_EVENTS = 1_024

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_INCIDENT_ID = re.compile(r"^inc-[0-9a-f]{32}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE = re.compile(
    r"(?:^|[._:/-])(?:api[_-]?key|password|secret|token|authorization)(?:[._:/-]|$)",
    re.IGNORECASE,
)


class IncidentState(str, Enum):
    OPEN = "OPEN"
    ACKNOWLEDGEMENT_REQUIRED = "ACKNOWLEDGEMENT_REQUIRED"
    ESCALATED = "ESCALATED"
    RESOLVED = "RESOLVED"


class IncidentClassification(str, Enum):
    PERSISTENCE_FAILURE = "PERSISTENCE_FAILURE"
    RECOVERY_STUCK = "RECOVERY_STUCK"
    REMEDIATION_LOOP = "REMEDIATION_LOOP"
    GIT_WORKTREE_DIVERGENCE = "GIT_WORKTREE_DIVERGENCE"
    CODEX_RUNTIME_FAILURE = "CODEX_RUNTIME_FAILURE"
    RESOURCE_EXHAUSTION = "RESOURCE_EXHAUSTION"
    OBSERVABILITY_LOSS = "OBSERVABILITY_LOSS"
    POLICY_BLOCK = "POLICY_BLOCK"
    UNKNOWN_CRITICAL_STATE = "UNKNOWN_CRITICAL_STATE"


class IncidentSeverity(str, Enum):
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class IncidentEscalation(str, Enum):
    OBSERVE_ONLY = "OBSERVE_ONLY"
    OPERATOR_ACK_REQUIRED = "OPERATOR_ACK_REQUIRED"
    OPERATOR_ACTION_REQUIRED = "OPERATOR_ACTION_REQUIRED"
    EMERGENCY_BLOCK_RECOMMENDED = "EMERGENCY_BLOCK_RECOMMENDED"


class IncidentDiagnosticCondition(str, Enum):
    ACTIVE = "ACTIVE"
    NORMALIZED = "NORMALIZED"
    UNKNOWN = "UNKNOWN"


class IncidentReason(str, Enum):
    HEALTH_CONDITION = "HEALTH_CONDITION"
    GOVERNANCE_BLOCK = "GOVERNANCE_BLOCK"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    METRICS_UNAVAILABLE = "METRICS_UNAVAILABLE"
    OPERATIONAL_EVENT = "OPERATIONAL_EVENT"
    DIAGNOSTIC_ACTIVE = "DIAGNOSTIC_ACTIVE"
    CRITICAL_SOURCE_MISSING = "CRITICAL_SOURCE_MISSING"
    SOURCE_STALE = "SOURCE_STALE"
    SOURCE_SCOPE_MISMATCH = "SOURCE_SCOPE_MISMATCH"
    SOURCE_INCOMPLETE = "SOURCE_INCOMPLETE"


_SEVERITY = {
    IncidentClassification.PERSISTENCE_FAILURE: IncidentSeverity.CRITICAL,
    IncidentClassification.RECOVERY_STUCK: IncidentSeverity.ERROR,
    IncidentClassification.REMEDIATION_LOOP: IncidentSeverity.ERROR,
    IncidentClassification.GIT_WORKTREE_DIVERGENCE: IncidentSeverity.ERROR,
    IncidentClassification.CODEX_RUNTIME_FAILURE: IncidentSeverity.ERROR,
    IncidentClassification.RESOURCE_EXHAUSTION: IncidentSeverity.CRITICAL,
    IncidentClassification.OBSERVABILITY_LOSS: IncidentSeverity.CRITICAL,
    IncidentClassification.POLICY_BLOCK: IncidentSeverity.ERROR,
    IncidentClassification.UNKNOWN_CRITICAL_STATE: IncidentSeverity.CRITICAL,
}


@dataclass(frozen=True, slots=True)
class IncidentScope:
    project_id: str
    repository_head: str
    mission_id: str | None = None
    workflow_generation: int | None = None

    def __post_init__(self) -> None:
        MetricsScope(self.project_id, self.mission_id, self.workflow_generation)
        if not _SHA40.fullmatch(self.repository_head):
            raise ValueError("repository_head must be a lowercase SHA-1")


@dataclass(frozen=True, slots=True)
class IncidentCorrelation:
    mission_id: str | None = None
    workflow_generation: int | None = None
    user_story_id: str | None = None
    role: MissionRole | None = None
    execution_id: str | None = None
    assignment_id: str | None = None
    operation: GovernedOperation | None = None
    resource_domain: ResourceBudgetDomain | None = None

    def __post_init__(self) -> None:
        MetricsScope(
            "incident-correlation",
            self.mission_id,
            self.workflow_generation,
            user_story_id=self.user_story_id,
        )
        for name in ("execution_id", "assignment_id"):
            value = getattr(self, name)
            if value is not None and not _safe_identity(value):
                raise ValueError(f"{name} is unsafe or non-canonical")
        if self.assignment_id is not None and self.user_story_id is None:
            raise ValueError("assignment_id requires user_story_id")
        if self.role is not None and not isinstance(self.role, MissionRole):
            raise ValueError("incident role is invalid")
        if self.execution_id is not None and self.role is None:
            raise ValueError("execution_id requires role")
        if self.operation is not None and not isinstance(self.operation, GovernedOperation):
            raise ValueError("incident operation is invalid")
        if self.resource_domain is not None and not isinstance(
            self.resource_domain, ResourceBudgetDomain
        ):
            raise ValueError("incident resource_domain is invalid")


@dataclass(frozen=True, slots=True)
class IncidentDiagnostic:
    classification: IncidentClassification
    scope: IncidentScope
    correlation: IncidentCorrelation
    condition: IncidentDiagnosticCondition
    observed_at: datetime
    source_identity: str

    def __post_init__(self) -> None:
        if not isinstance(self.classification, IncidentClassification):
            raise ValueError("diagnostic classification is invalid")
        if not isinstance(self.scope, IncidentScope) or not isinstance(
            self.correlation, IncidentCorrelation
        ):
            raise ValueError("diagnostic binding is invalid")
        _require_correlation_scope(self.scope, self.correlation)
        if not isinstance(self.condition, IncidentDiagnosticCondition):
            raise ValueError("diagnostic condition is invalid")
        _validate_utc(self.observed_at, "observed_at")
        if not _safe_identity(self.source_identity):
            raise ValueError("diagnostic source_identity is invalid")


@dataclass(frozen=True, slots=True)
class IncidentOperatorAcknowledgement:
    incident_id: str
    scope: IncidentScope
    occurred_at: datetime
    operator_identity: str

    def __post_init__(self) -> None:
        if not _INCIDENT_ID.fullmatch(self.incident_id):
            raise ValueError("acknowledgement incident_id is invalid")
        if not isinstance(self.scope, IncidentScope):
            raise ValueError("acknowledgement scope is invalid")
        _validate_utc(self.occurred_at, "occurred_at")
        if not is_attributable_human_identity(self.operator_identity):
            raise ValueError("acknowledgement requires attributable non-Codex operator")


@dataclass(frozen=True, slots=True)
class IncidentResolutionObservation:
    incident_id: str
    scope: IncidentScope
    observed_at: datetime
    normalized_source_identity: str
    operator_identity: str

    def __post_init__(self) -> None:
        if not _INCIDENT_ID.fullmatch(self.incident_id):
            raise ValueError("resolution incident_id is invalid")
        if not isinstance(self.scope, IncidentScope):
            raise ValueError("resolution scope is invalid")
        _validate_utc(self.observed_at, "observed_at")
        if not _safe_identity(self.normalized_source_identity):
            raise ValueError("resolution source identity is invalid")
        if not is_attributable_human_identity(self.operator_identity):
            raise ValueError("resolution requires attributable non-Codex operator")


@dataclass(frozen=True, slots=True)
class IncidentEvaluationContext:
    scope: IncidentScope
    evaluated_at: datetime
    health: HealthSnapshot | None
    governance: GovernanceDecisionSet | None
    resource_budgets: ResourceBudgetDecisionSet | None
    metrics: MetricsHealthInput | None
    operational_events: tuple[OperationalEvent, ...]
    events_complete: bool
    diagnostics: tuple[IncidentDiagnostic, ...]
    diagnostics_complete: bool
    prior_records: tuple[IncidentRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.scope, IncidentScope):
            raise ValueError("incident evaluation scope is invalid")
        _validate_utc(self.evaluated_at, "evaluated_at")
        if self.health is not None and not isinstance(self.health, HealthSnapshot):
            raise ValueError("health source is invalid")
        if self.governance is not None and not isinstance(self.governance, GovernanceDecisionSet):
            raise ValueError("governance source is invalid")
        if self.resource_budgets is not None and not isinstance(self.resource_budgets, ResourceBudgetDecisionSet):
            raise ValueError("resource budget source is invalid")
        if self.metrics is not None and not isinstance(self.metrics, MetricsHealthInput):
            raise ValueError("metrics source is invalid")
        if not isinstance(self.operational_events, tuple) or len(self.operational_events) > MAX_INCIDENT_SOURCE_EVENTS or any(not isinstance(item, OperationalEvent) for item in self.operational_events):
            raise ValueError("operational events must be immutable and bounded")
        if not isinstance(self.events_complete, bool) or not isinstance(self.diagnostics_complete, bool):
            raise ValueError("source completeness must be explicit")
        if not isinstance(self.diagnostics, tuple) or len(self.diagnostics) > MAX_INCIDENT_DIAGNOSTICS or any(not isinstance(item, IncidentDiagnostic) for item in self.diagnostics):
            raise ValueError("incident diagnostics must be immutable and bounded")
        if not isinstance(self.prior_records, tuple) or len(self.prior_records) > MAX_INCIDENT_RECORDS or any(not isinstance(item, IncidentRecord) for item in self.prior_records):
            raise ValueError("prior incident records must be immutable and bounded")
        identities = [item.incident_id for item in self.prior_records]
        if len(identities) != len(set(identities)):
            raise ValueError("prior records must contain one latest revision per incident")


@dataclass(frozen=True, slots=True)
class IncidentRecord:
    schema_version: str
    incident_id: str
    revision: int
    classification: IncidentClassification
    severity: IncidentSeverity
    state: IncidentState
    escalation: IncidentEscalation
    scope: IncidentScope
    correlation: IncidentCorrelation
    opened_at: datetime
    updated_at: datetime
    occurrence_count: int
    reopen_count: int
    reasons: tuple[IncidentReason, ...]
    source_identities: tuple[str, ...]
    acknowledged_by: str | None
    resolution_source_identity: str | None
    previous_fingerprint: str | None
    fingerprint: str

    @classmethod
    def create(
        cls,
        *,
        incident_id: str,
        revision: int,
        classification: IncidentClassification,
        severity: IncidentSeverity,
        state: IncidentState,
        escalation: IncidentEscalation,
        scope: IncidentScope,
        correlation: IncidentCorrelation,
        opened_at: datetime,
        updated_at: datetime,
        occurrence_count: int,
        reopen_count: int,
        reasons: tuple[IncidentReason, ...],
        source_identities: tuple[str, ...],
        acknowledged_by: str | None,
        resolution_source_identity: str | None,
        previous_fingerprint: str | None,
    ) -> IncidentRecord:
        values = {
            "schema_version": INCIDENT_SCHEMA_VERSION,
            "incident_id": incident_id,
            "revision": revision,
            "classification": classification.value,
            "severity": severity.value,
            "state": state.value,
            "escalation": escalation.value,
            "scope": _scope_data(scope),
            "correlation": _correlation_data(correlation),
            "opened_at": _utc_text(opened_at),
            "updated_at": _utc_text(updated_at),
            "occurrence_count": occurrence_count,
            "reopen_count": reopen_count,
            "reasons": [item.value for item in reasons],
            "source_identities": list(source_identities),
            "acknowledged_by": acknowledged_by,
            "resolution_source_identity": resolution_source_identity,
            "previous_fingerprint": previous_fingerprint,
            "fingerprint": "",
        }
        fingerprint = hashlib.sha256(_canonical(values).encode("utf-8")).hexdigest()
        return cls(
            INCIDENT_SCHEMA_VERSION, incident_id, revision, classification, severity,
            state, escalation, scope, correlation, opened_at, updated_at,
            occurrence_count, reopen_count, reasons, source_identities, acknowledged_by,
            resolution_source_identity, previous_fingerprint, fingerprint,
        )

    def __post_init__(self) -> None:
        if self.schema_version != INCIDENT_SCHEMA_VERSION:
            raise ValueError("unsupported incident schema_version")
        if not isinstance(self.classification, IncidentClassification):
            raise ValueError("incident classification is invalid")
        if self.severity is not _SEVERITY[self.classification]:
            raise ValueError("incident severity contradicts classification")
        if not isinstance(self.state, IncidentState) or not isinstance(
            self.escalation, IncidentEscalation
        ):
            raise ValueError("incident lifecycle is invalid")
        if not isinstance(self.scope, IncidentScope) or not isinstance(
            self.correlation, IncidentCorrelation
        ):
            raise ValueError("incident scope/correlation is invalid")
        _require_correlation_scope(self.scope, self.correlation)
        if self.incident_id != derive_incident_id(
            self.scope.project_id, self.classification, self.correlation
        ):
            raise ValueError("incident identity is inconsistent")
        for name in ("revision", "occurrence_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.reopen_count, int) or isinstance(self.reopen_count, bool) or self.reopen_count < 0:
            raise ValueError("reopen_count must be non-negative")
        if self.reopen_count >= self.occurrence_count:
            raise ValueError("reopen_count must be lower than occurrence_count")
        if not isinstance(self.reasons, tuple) or not self.reasons or self.reasons != tuple(sorted(set(self.reasons), key=lambda item: item.value)):
            raise ValueError("incident reasons must be a canonical non-empty tuple")
        _validate_utc(self.opened_at, "opened_at")
        _validate_utc(self.updated_at, "updated_at")
        if self.updated_at < self.opened_at:
            raise ValueError("incident timestamps are inverted")
        if not isinstance(self.source_identities, tuple) or not 1 <= len(self.source_identities) <= MAX_INCIDENT_SOURCES:
            raise ValueError("incident sources must be a non-empty bounded tuple")
        if self.source_identities != tuple(sorted(set(self.source_identities))) or any(
            not _safe_identity(item) for item in self.source_identities
        ):
            raise ValueError("incident sources must be canonical and unique")
        if self.acknowledged_by is not None and not is_attributable_human_identity(
            self.acknowledged_by
        ):
            raise ValueError("incident acknowledgement identity is not Human-attributable")
        if self.resolution_source_identity is not None and not _safe_identity(
            self.resolution_source_identity
        ):
            raise ValueError("incident resolution source is invalid")
        if self.previous_fingerprint is not None and not _SHA256.fullmatch(
            self.previous_fingerprint
        ):
            raise ValueError("previous incident fingerprint is invalid")
        if self.revision == 1 and self.previous_fingerprint is not None or self.revision > 1 and self.previous_fingerprint is None:
            raise ValueError("incident revision chain is invalid")
        _validate_lifecycle(self)
        if not _SHA256.fullmatch(self.fingerprint) or self.fingerprint != incident_record_fingerprint(self):
            raise ValueError("incident fingerprint is invalid or inconsistent")
        if len(canonical_incident_record_json(self).encode("utf-8")) > MAX_INCIDENT_RECORD_BYTES:
            raise ValueError("incident record exceeds size policy")


@dataclass(frozen=True, slots=True)
class IncidentAssessment:
    schema_version: str
    scope: IncidentScope
    evaluated_at: datetime
    records: tuple[IncidentRecord, ...]
    deduplicated_incident_ids: tuple[str, ...]
    fingerprint: str

    def __post_init__(self) -> None:
        if self.schema_version != INCIDENT_SCHEMA_VERSION or not isinstance(self.scope, IncidentScope):
            raise ValueError("incident assessment binding is invalid")
        _validate_utc(self.evaluated_at, "evaluated_at")
        if not isinstance(self.records, tuple) or len(self.records) > MAX_INCIDENT_RECORDS:
            raise ValueError("assessment records must be immutable and bounded")
        if self.records != tuple(sorted(self.records, key=lambda item: item.incident_id)):
            raise ValueError("assessment records must be canonically ordered")
        if len({item.incident_id for item in self.records}) != len(self.records):
            raise ValueError("assessment records must have unique logical incidents")
        if any(item.scope != self.scope for item in self.records):
            raise ValueError("assessment records must share exact scope")
        if self.deduplicated_incident_ids != tuple(sorted(set(self.deduplicated_incident_ids))):
            raise ValueError("deduplicated identities must be canonical")
        if not _SHA256.fullmatch(self.fingerprint) or self.fingerprint != incident_assessment_fingerprint(
            self.scope, self.evaluated_at, self.records, self.deduplicated_incident_ids
        ):
            raise ValueError("incident assessment fingerprint is inconsistent")


def derive_incident_id(
    project_id: str,
    classification: IncidentClassification,
    correlation: IncidentCorrelation,
) -> str:
    payload = {"project_id": project_id, "classification": classification.value, "correlation": _correlation_data(correlation)}
    return "inc-" + hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:32]


def canonical_incident_record_json(record: IncidentRecord) -> str:
    return _canonical(incident_record_to_dict(record))


def incident_record_to_dict(record: IncidentRecord) -> dict[str, object]:
    return {
        "schema_version": record.schema_version,
        "incident_id": record.incident_id,
        "revision": record.revision,
        "classification": record.classification.value,
        "severity": record.severity.value,
        "state": record.state.value,
        "escalation": record.escalation.value,
        "scope": _scope_data(record.scope),
        "correlation": _correlation_data(record.correlation),
        "opened_at": _utc_text(record.opened_at),
        "updated_at": _utc_text(record.updated_at),
        "occurrence_count": record.occurrence_count,
        "reopen_count": record.reopen_count,
        "reasons": [item.value for item in record.reasons],
        "source_identities": list(record.source_identities),
        "acknowledged_by": record.acknowledged_by,
        "resolution_source_identity": record.resolution_source_identity,
        "previous_fingerprint": record.previous_fingerprint,
        "fingerprint": record.fingerprint,
    }


def incident_record_from_json(text: str) -> IncidentRecord:
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("incident record JSON is invalid") from error
    fields = {
        "schema_version", "incident_id", "revision", "classification", "severity",
        "state", "escalation", "scope", "correlation", "opened_at", "updated_at",
        "occurrence_count", "reopen_count", "reasons", "source_identities", "acknowledged_by",
        "resolution_source_identity", "previous_fingerprint", "fingerprint",
    }
    if not isinstance(data, dict) or set(data) != fields:
        raise ValueError("incident record has unknown or missing fields")
    scope = _exact_dict(data["scope"], {"project_id", "repository_head", "mission_id", "workflow_generation"})
    corr = _exact_dict(data["correlation"], {"mission_id", "workflow_generation", "user_story_id", "role", "execution_id", "assignment_id", "operation", "resource_domain"})
    sources = data["source_identities"]
    reasons = data["reasons"]
    if not isinstance(sources, list) or any(not isinstance(item, str) for item in sources):
        raise ValueError("incident record sources are invalid")
    if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
        raise ValueError("incident record reasons are invalid")
    return IncidentRecord(
        cast(str, data["schema_version"]), cast(str, data["incident_id"]), _strict_int(data["revision"]),
        IncidentClassification(cast(str, data["classification"])), IncidentSeverity(cast(str, data["severity"])),
        IncidentState(cast(str, data["state"])), IncidentEscalation(cast(str, data["escalation"])),
        IncidentScope(cast(str, scope["project_id"]), cast(str, scope["repository_head"]), cast(str | None, scope["mission_id"]), cast(int | None, scope["workflow_generation"])),
        IncidentCorrelation(cast(str | None, corr["mission_id"]), cast(int | None, corr["workflow_generation"]), cast(str | None, corr["user_story_id"]), MissionRole(corr["role"]) if corr["role"] is not None else None, cast(str | None, corr["execution_id"]), cast(str | None, corr["assignment_id"]), GovernedOperation(corr["operation"]) if corr["operation"] is not None else None, ResourceBudgetDomain(corr["resource_domain"]) if corr["resource_domain"] is not None else None),
        _parse_utc(data["opened_at"]), _parse_utc(data["updated_at"]), _strict_int(data["occurrence_count"]), _strict_int(data["reopen_count"]), tuple(IncidentReason(item) for item in reasons), tuple(sources),
        cast(str | None, data["acknowledged_by"]), cast(str | None, data["resolution_source_identity"]), cast(str | None, data["previous_fingerprint"]), cast(str, data["fingerprint"]),
    )


def incident_record_fingerprint(record: IncidentRecord) -> str:
    data = incident_record_to_dict(record)
    data["fingerprint"] = ""
    return hashlib.sha256(_canonical(data).encode("utf-8")).hexdigest()


def incident_assessment_fingerprint(scope: IncidentScope, evaluated_at: datetime, records: tuple[IncidentRecord, ...], deduplicated: tuple[str, ...]) -> str:
    data = {"schema_version": INCIDENT_SCHEMA_VERSION, "scope": _scope_data(scope), "evaluated_at": _utc_text(evaluated_at), "records": [item.fingerprint for item in records], "deduplicated": deduplicated}
    return hashlib.sha256(_canonical(data).encode("utf-8")).hexdigest()


def incident_severity(classification: IncidentClassification) -> IncidentSeverity:
    return _SEVERITY[classification]


def _validate_lifecycle(record: IncidentRecord) -> None:
    if record.state is IncidentState.RESOLVED:
        if record.escalation is not IncidentEscalation.OBSERVE_ONLY or record.resolution_source_identity is None or record.acknowledged_by is None:
            raise ValueError("resolved incident requires factual resolution and Human operator")
        return
    if record.resolution_source_identity is not None:
        raise ValueError("unresolved incident cannot claim resolution")
    if record.severity is IncidentSeverity.CRITICAL:
        if record.state is not IncidentState.ESCALATED or record.escalation is not IncidentEscalation.EMERGENCY_BLOCK_RECOMMENDED:
            raise ValueError("critical incident must recommend emergency block")
    elif record.acknowledged_by is None:
        if record.state is not IncidentState.ACKNOWLEDGEMENT_REQUIRED or record.escalation is not IncidentEscalation.OPERATOR_ACK_REQUIRED:
            raise ValueError("error incident must require acknowledgement")
    elif record.state is not IncidentState.ESCALATED or record.escalation is not IncidentEscalation.OPERATOR_ACTION_REQUIRED:
        raise ValueError("acknowledged error incident must require operator action")


def _scope_data(scope: IncidentScope) -> dict[str, object]:
    return {"project_id": scope.project_id, "repository_head": scope.repository_head, "mission_id": scope.mission_id, "workflow_generation": scope.workflow_generation}


def _correlation_data(correlation: IncidentCorrelation) -> dict[str, object]:
    return {"mission_id": correlation.mission_id, "workflow_generation": correlation.workflow_generation, "user_story_id": correlation.user_story_id, "role": correlation.role.value if correlation.role else None, "execution_id": correlation.execution_id, "assignment_id": correlation.assignment_id, "operation": correlation.operation.value if correlation.operation else None, "resource_domain": correlation.resource_domain.value if correlation.resource_domain else None}


def _require_correlation_scope(scope: IncidentScope, correlation: IncidentCorrelation) -> None:
    if correlation.mission_id != scope.mission_id or correlation.workflow_generation != scope.workflow_generation:
        raise ValueError("incident correlation must match exact mission scope")


def _safe_identity(value: object) -> bool:
    return bool(isinstance(value, str) and _ID.fullmatch(value) and not _SENSITIVE.search(value))


def _validate_utc(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is not timezone.utc or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone.utc")


def _utc_text(value: datetime) -> str:
    _validate_utc(value, "timestamp")
    return value.isoformat().replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("incident timestamp is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _validate_utc(parsed, "timestamp")
    return parsed


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate incident JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-JSON constant: {value}")


def _exact_dict(value: object, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("incident nested object has unknown or missing fields")
    return cast(dict[str, object], value)


def _strict_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("incident integer is invalid")
    return value
