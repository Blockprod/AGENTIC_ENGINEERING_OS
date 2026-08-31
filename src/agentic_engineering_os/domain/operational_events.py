"""Strict, immutable and non-authoritative operational observations."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias, cast
from unicodedata import normalize
from uuid import UUID

from .enums import MissionRole
from .identity import has_attributable_codex_role, is_attributable_human_identity


OPERATIONAL_EVENT_SCHEMA_VERSION = "1.0"
MAX_OPERATIONAL_EVENT_BYTES = 16_384
MAX_OPERATIONAL_ATTRIBUTES = 32
MAX_OPERATIONAL_STRING_LENGTH = 2_048

OperationalScalar: TypeAlias = str | int | float | bool | None
ErrorPathPart: TypeAlias = str | int

_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_UTC_TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z$"
)
_PROJECT_ID_PATTERN = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,128}$")
_CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_ATTRIBUTE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_OPERATION_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_REASON_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SHA40_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(
        r"(?:api[_-]?key|password|secret|token|authorization)\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\bbearer\s+[A-Za-z0-9._~+/-]{12,}", re.IGNORECASE),
)
_SENSITIVE_ATTRIBUTE_NAMES = frozenset(
    {
        "api_key",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "env",
        "environment",
        "password",
        "secret",
        "secrets",
        "stderr",
        "stdout",
        "token",
    }
)


class OperationalEventType(str, Enum):
    MISSION_LIFECYCLE = "MISSION_LIFECYCLE"
    ROLE_EXECUTION = "ROLE_EXECUTION"
    CODEX_EXECUTION = "CODEX_EXECUTION"
    WORKTREE_LIFECYCLE = "WORKTREE_LIFECYCLE"
    INTEGRATION_GATE = "INTEGRATION_GATE"
    MERGE_OPERATION = "MERGE_OPERATION"
    CONTROL_PLANE_DECISION = "CONTROL_PLANE_DECISION"
    REMEDIATION_RECOVERY = "REMEDIATION_RECOVERY"
    HUMAN_WAITING = "HUMAN_WAITING"
    PERSISTENCE_FAILURE = "PERSISTENCE_FAILURE"
    ADOPTION_MIGRATION = "ADOPTION_MIGRATION"
    OPERATIONAL_ANOMALY = "OPERATIONAL_ANOMALY"


class OperationalSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class OperationalProvenanceKind(str, Enum):
    DETERMINISTIC_COMPONENT = "DETERMINISTIC_COMPONENT"
    GIT_OBSERVATION = "GIT_OBSERVATION"
    PROCESS_RUNTIME = "PROCESS_RUNTIME"
    CODEX_RUNTIME = "CODEX_RUNTIME"
    OPERATOR_HUMAN = "OPERATOR_HUMAN"


_OPERATIONS = MappingProxyType(
    {
        OperationalEventType.MISSION_LIFECYCLE: frozenset(
            {"STARTED", "STATUS_OBSERVED", "BLOCKED", "FINISHED"}
        ),
        OperationalEventType.ROLE_EXECUTION: frozenset(
            {"STARTED", "FINISHED", "FAILED"}
        ),
        OperationalEventType.CODEX_EXECUTION: frozenset(
            {
                "PLANNED",
                "STARTED",
                "FINISHED",
                "INTERRUPTED",
                "FAILED",
                "RECOVERY_INSPECTED",
            }
        ),
        OperationalEventType.WORKTREE_LIFECYCLE: frozenset(
            {
                "PLANNED",
                "CREATED",
                "COMPLETED",
                "FAILED",
                "CLEANED",
                "DIVERGENCE_OBSERVED",
            }
        ),
        OperationalEventType.INTEGRATION_GATE: frozenset({"EVALUATED"}),
        OperationalEventType.MERGE_OPERATION: frozenset(
            {"STARTED", "FINISHED", "FAILED", "CONFLICT_OBSERVED"}
        ),
        OperationalEventType.CONTROL_PLANE_DECISION: frozenset(
            {
                "EVIDENCE_RECORDED",
                "GATE_EVALUATED",
                "CERTIFICATION_RECORDED",
                "HUMAN_APPROVAL_RECORDED",
            }
        ),
        OperationalEventType.REMEDIATION_RECOVERY: frozenset(
            {"REQUESTED", "STARTED", "FINISHED", "FAILED", "RECOVERY_REQUIRED"}
        ),
        OperationalEventType.HUMAN_WAITING: frozenset(
            {"WAITING_STARTED", "WAITING_FINISHED"}
        ),
        OperationalEventType.PERSISTENCE_FAILURE: frozenset(
            {"READ_FAILED", "WRITE_FAILED", "CORRUPTION_OBSERVED"}
        ),
        OperationalEventType.ADOPTION_MIGRATION: frozenset(
            {"PLANNED", "STARTED", "FINISHED", "FAILED", "REFUSED"}
        ),
        OperationalEventType.OPERATIONAL_ANOMALY: frozenset({"DETECTED"}),
    }
)


class OperationalEventError(ValueError):
    """An operational observation is malformed or unsafe to retain."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: tuple[ErrorPathPart, ...] = (),
    ) -> None:
        self.code = code
        self.message = message
        self.path = path
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class OperationalAttribute:
    name: str
    value: OperationalScalar

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _ATTRIBUTE_NAME_PATTERN.fullmatch(
            self.name
        ):
            _fail("INVALID_ATTRIBUTE_NAME", "attribute name is not canonical", "name")
        if _is_sensitive_attribute_name(self.name):
            _fail("SENSITIVE_FIELD", "sensitive attribute names are forbidden", "name")
        _validate_scalar(self.value, ("value",))


@dataclass(frozen=True, slots=True)
class OperationalEventPayload:
    operation: str
    outcome: str | None = None
    reason_code: str | None = None
    duration_ms: int | None = None
    attempt: int | None = None
    attributes: tuple[OperationalAttribute, ...] = ()

    def __post_init__(self) -> None:
        _canonical_token(self.operation, _OPERATION_PATTERN, "operation")
        _optional_text(self.outcome, "outcome", maximum=128)
        if self.reason_code is not None:
            _canonical_token(self.reason_code, _REASON_PATTERN, "reason_code")
        _optional_integer(self.duration_ms, "duration_ms", minimum=0, maximum=604_800_000)
        _optional_integer(self.attempt, "attempt", minimum=1, maximum=1_000_000)
        if not isinstance(self.attributes, tuple):
            _fail("INVALID_ATTRIBUTES", "attributes must be an immutable tuple", "attributes")
        if len(self.attributes) > MAX_OPERATIONAL_ATTRIBUTES:
            _fail("PAYLOAD_TOO_LARGE", "too many operational attributes", "attributes")
        if any(not isinstance(item, OperationalAttribute) for item in self.attributes):
            _fail("INVALID_ATTRIBUTES", "attribute type is invalid", "attributes")
        names = [item.name for item in self.attributes]
        if names != sorted(names, key=lambda item: (item.casefold(), item)):
            _fail("NON_CANONICAL_PAYLOAD", "attributes must use canonical order", "attributes")
        if len(names) != len(set(names)):
            _fail("DUPLICATE_ATTRIBUTE", "attribute names must be unique", "attributes")


@dataclass(frozen=True, slots=True)
class OperationalProvenance:
    kind: OperationalProvenanceKind
    producer: str
    source_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, OperationalProvenanceKind):
            _fail("INVALID_PROVENANCE", "provenance kind is invalid", "kind")
        _required_text(self.producer, "producer", maximum=128)
        _optional_text(self.source_ref, "source_ref", maximum=256)
        if (
            self.kind is OperationalProvenanceKind.OPERATOR_HUMAN
            and not is_attributable_human_identity(self.producer)
        ):
            _fail(
                "INVALID_PROVENANCE",
                "Human observation requires an attributable non-Codex producer",
                "producer",
            )
        if (
            self.kind is OperationalProvenanceKind.CODEX_RUNTIME
            and not has_attributable_codex_role(self.producer)
        ):
            _fail(
                "INVALID_PROVENANCE",
                "Codex observation requires an attributable Codex role",
                "producer",
            )


@dataclass(frozen=True, slots=True)
class OperationalCorrelation:
    mission_id: str | None = None
    workflow_generation: int | None = None
    user_story_id: str | None = None
    role: MissionRole | None = None
    execution_id: str | None = None
    assignment_id: str | None = None
    wave_index: int | None = None
    group_index: int | None = None
    gate_id: str | None = None
    certification_id: str | None = None
    repository_commit: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "mission_id",
            "user_story_id",
            "execution_id",
            "assignment_id",
            "gate_id",
            "certification_id",
        ):
            _optional_identifier(cast(str | None, getattr(self, field_name)), field_name)
        if self.role is not None and not isinstance(self.role, MissionRole):
            _fail("INVALID_CORRELATION", "role is invalid", "role")
        _optional_integer(
            self.workflow_generation,
            "workflow_generation",
            minimum=0,
            maximum=2_147_483_647,
        )
        _optional_integer(self.wave_index, "wave_index", minimum=0, maximum=2_147_483_647)
        _optional_integer(self.group_index, "group_index", minimum=0, maximum=2_147_483_647)
        if self.repository_commit is not None and (
            not isinstance(self.repository_commit, str)
            or not _SHA40_PATTERN.fullmatch(self.repository_commit)
        ):
            _fail(
                "INVALID_CORRELATION",
                "repository_commit must be a lowercase Git SHA-1",
                "repository_commit",
            )
        contextual = (
            self.workflow_generation,
            self.user_story_id,
            self.role,
            self.execution_id,
            self.assignment_id,
            self.wave_index,
            self.group_index,
            self.gate_id,
            self.certification_id,
        )
        if self.mission_id is None and any(item is not None for item in contextual):
            _fail(
                "INVALID_CORRELATION",
                "mission-scoped correlation requires mission_id",
                "mission_id",
            )
        if self.workflow_generation is not None and self.mission_id is None:
            _fail(
                "INVALID_CORRELATION",
                "workflow_generation requires mission_id",
                "workflow_generation",
            )
        if self.execution_id is not None and self.role is None:
            _fail(
                "INVALID_CORRELATION",
                "execution_id requires an attributable role",
                "execution_id",
            )
        if self.assignment_id is not None and self.user_story_id is None:
            _fail(
                "INVALID_CORRELATION",
                "assignment_id requires user_story_id",
                "assignment_id",
            )
        if (self.wave_index is None) != (self.group_index is None):
            _fail(
                "INVALID_CORRELATION",
                "wave_index and group_index must be provided together",
                "wave_index",
            )


@dataclass(frozen=True, slots=True)
class OperationalEvent:
    """One factual observation with no Control Plane or Human authority."""

    schema_version: str
    event_id: str
    event_type: OperationalEventType
    occurred_at: datetime
    severity: OperationalSeverity
    source_component: str
    project_id: str
    correlation: OperationalCorrelation
    payload: OperationalEventPayload
    provenance: OperationalProvenance

    def __post_init__(self) -> None:
        if self.schema_version != OPERATIONAL_EVENT_SCHEMA_VERSION:
            _fail("UNKNOWN_SCHEMA_VERSION", "schema_version is not supported", "schema_version")
        _validate_event_id(self.event_id)
        if not isinstance(self.event_type, OperationalEventType):
            _fail("INVALID_EVENT_TYPE", "event_type is invalid", "event_type")
        if not isinstance(self.severity, OperationalSeverity):
            _fail("INVALID_SEVERITY", "severity is invalid", "severity")
        if not isinstance(self.occurred_at, datetime):
            _fail("INVALID_TIMESTAMP", "occurred_at must be datetime", "occurred_at")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() != timedelta(0):
            _fail("INVALID_TIMESTAMP", "occurred_at must use UTC", "occurred_at")
        _required_text(self.source_component, "source_component", maximum=128)
        if (
            not isinstance(self.project_id, str)
            or normalize("NFC", self.project_id) != self.project_id
            or not _PROJECT_ID_PATTERN.fullmatch(self.project_id)
        ):
            _fail("INVALID_PROJECT_ID", "project_id is absent or non-canonical", "project_id")
        _reject_secret(self.project_id, ("project_id",))
        if not isinstance(self.correlation, OperationalCorrelation):
            _fail("INVALID_CORRELATION", "correlation type is invalid", "correlation")
        if not isinstance(self.payload, OperationalEventPayload):
            _fail("INVALID_PAYLOAD", "payload type is invalid", "payload")
        if not isinstance(self.provenance, OperationalProvenance):
            _fail("INVALID_PROVENANCE", "provenance type is invalid", "provenance")
        if self.payload.operation not in _OPERATIONS[self.event_type]:
            _fail(
                "INVALID_EVENT_OPERATION",
                "payload operation is not valid for event_type",
                "payload",
                "operation",
            )
        _validate_family_correlation(self.event_type, self.correlation)
        serialized = canonical_operational_event_json(self).encode("utf-8")
        if len(serialized) > MAX_OPERATIONAL_EVENT_BYTES:
            _fail("EVENT_TOO_LARGE", "serialized event exceeds size policy")


def operational_event_to_dict(event: OperationalEvent) -> dict[str, object]:
    if not isinstance(event, OperationalEvent):
        raise TypeError("operational_event_to_dict expects OperationalEvent")
    correlation = event.correlation
    payload = event.payload
    provenance = event.provenance
    return {
        "schema_version": event.schema_version,
        "event_id": event.event_id,
        "event_type": event.event_type.value,
        "occurred_at": _utc_text(event.occurred_at),
        "severity": event.severity.value,
        "source_component": event.source_component,
        "project_id": event.project_id,
        "correlation": {
            "mission_id": correlation.mission_id,
            "workflow_generation": correlation.workflow_generation,
            "user_story_id": correlation.user_story_id,
            "role": correlation.role.value if correlation.role is not None else None,
            "execution_id": correlation.execution_id,
            "assignment_id": correlation.assignment_id,
            "wave_index": correlation.wave_index,
            "group_index": correlation.group_index,
            "gate_id": correlation.gate_id,
            "certification_id": correlation.certification_id,
            "repository_commit": correlation.repository_commit,
        },
        "payload": {
            "operation": payload.operation,
            "outcome": payload.outcome,
            "reason_code": payload.reason_code,
            "duration_ms": payload.duration_ms,
            "attempt": payload.attempt,
            "attributes": [
                {"name": item.name, "value": item.value}
                for item in payload.attributes
            ],
        },
        "provenance": {
            "kind": provenance.kind.value,
            "producer": provenance.producer,
            "source_ref": provenance.source_ref,
        },
    }


def canonical_operational_event_json(event: OperationalEvent) -> str:
    return json.dumps(
        operational_event_to_dict(event),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def operational_event_fingerprint(event: OperationalEvent) -> str:
    """Return a deterministic content fingerprint without granting authority."""

    return hashlib.sha256(canonical_operational_event_json(event).encode("utf-8")).hexdigest()


def operational_event_from_dict(candidate: object) -> OperationalEvent:
    """Strictly parse one schema-shaped mapping without coercion or defaults."""

    root = _mapping(candidate, (), _ROOT_FIELDS)
    correlation_data = _mapping(root["correlation"], ("correlation",), _CORRELATION_FIELDS)
    payload_data = _mapping(root["payload"], ("payload",), _PAYLOAD_FIELDS)
    provenance_data = _mapping(root["provenance"], ("provenance",), _PROVENANCE_FIELDS)
    attributes_data = payload_data["attributes"]
    if not isinstance(attributes_data, list):
        _fail("INVALID_PAYLOAD", "attributes must be an array", "payload", "attributes")
    attributes = tuple(
        OperationalAttribute(
            name=cast(str, item_mapping["name"]),
            value=cast(OperationalScalar, item_mapping["value"]),
        )
        for index, item in enumerate(attributes_data)
        for item_mapping in (
            _mapping(item, ("payload", "attributes", index), _ATTRIBUTE_FIELDS),
        )
    )
    occurred_at = root["occurred_at"]
    if not isinstance(occurred_at, str) or not _UTC_TIMESTAMP_PATTERN.fullmatch(
        occurred_at
    ):
        _fail("INVALID_TIMESTAMP", "occurred_at must be canonical UTC text", "occurred_at")
    try:
        parsed_at = datetime.fromisoformat(occurred_at[:-1] + "+00:00")
    except ValueError as error:
        raise OperationalEventError(
            "INVALID_TIMESTAMP",
            "occurred_at is not ISO 8601",
            path=("occurred_at",),
        ) from error
    return OperationalEvent(
        schema_version=cast(str, root["schema_version"]),
        event_id=cast(str, root["event_id"]),
        event_type=_enum(OperationalEventType, root["event_type"], ("event_type",)),
        occurred_at=parsed_at,
        severity=_enum(OperationalSeverity, root["severity"], ("severity",)),
        source_component=cast(str, root["source_component"]),
        project_id=cast(str, root["project_id"]),
        correlation=OperationalCorrelation(
            mission_id=cast(str | None, correlation_data["mission_id"]),
            workflow_generation=cast(int | None, correlation_data["workflow_generation"]),
            user_story_id=cast(str | None, correlation_data["user_story_id"]),
            role=(
                _enum(MissionRole, correlation_data["role"], ("correlation", "role"))
                if correlation_data["role"] is not None
                else None
            ),
            execution_id=cast(str | None, correlation_data["execution_id"]),
            assignment_id=cast(str | None, correlation_data["assignment_id"]),
            wave_index=cast(int | None, correlation_data["wave_index"]),
            group_index=cast(int | None, correlation_data["group_index"]),
            gate_id=cast(str | None, correlation_data["gate_id"]),
            certification_id=cast(str | None, correlation_data["certification_id"]),
            repository_commit=cast(str | None, correlation_data["repository_commit"]),
        ),
        payload=OperationalEventPayload(
            operation=cast(str, payload_data["operation"]),
            outcome=cast(str | None, payload_data["outcome"]),
            reason_code=cast(str | None, payload_data["reason_code"]),
            duration_ms=cast(int | None, payload_data["duration_ms"]),
            attempt=cast(int | None, payload_data["attempt"]),
            attributes=attributes,
        ),
        provenance=OperationalProvenance(
            kind=_enum(
                OperationalProvenanceKind,
                provenance_data["kind"],
                ("provenance", "kind"),
            ),
            producer=cast(str, provenance_data["producer"]),
            source_ref=cast(str | None, provenance_data["source_ref"]),
        ),
    )


_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "event_type",
        "occurred_at",
        "severity",
        "source_component",
        "project_id",
        "correlation",
        "payload",
        "provenance",
    }
)
_CORRELATION_FIELDS = frozenset(
    {
        "mission_id",
        "workflow_generation",
        "user_story_id",
        "role",
        "execution_id",
        "assignment_id",
        "wave_index",
        "group_index",
        "gate_id",
        "certification_id",
        "repository_commit",
    }
)
_PAYLOAD_FIELDS = frozenset(
    {"operation", "outcome", "reason_code", "duration_ms", "attempt", "attributes"}
)
_PROVENANCE_FIELDS = frozenset({"kind", "producer", "source_ref"})
_ATTRIBUTE_FIELDS = frozenset({"name", "value"})


def _validate_family_correlation(
    event_type: OperationalEventType,
    correlation: OperationalCorrelation,
) -> None:
    requires_mission = event_type in {
        OperationalEventType.MISSION_LIFECYCLE,
        OperationalEventType.ROLE_EXECUTION,
        OperationalEventType.CODEX_EXECUTION,
        OperationalEventType.WORKTREE_LIFECYCLE,
        OperationalEventType.INTEGRATION_GATE,
        OperationalEventType.MERGE_OPERATION,
        OperationalEventType.CONTROL_PLANE_DECISION,
        OperationalEventType.REMEDIATION_RECOVERY,
        OperationalEventType.HUMAN_WAITING,
    }
    if requires_mission and (
        correlation.mission_id is None or correlation.workflow_generation is None
    ):
        _fail(
            "INVALID_CORRELATION",
            "event family requires mission_id and workflow_generation",
            "correlation",
        )
    if event_type is OperationalEventType.ROLE_EXECUTION and correlation.role is None:
        _fail("INVALID_CORRELATION", "role execution requires role", "correlation", "role")
    if event_type is OperationalEventType.CODEX_EXECUTION and (
        correlation.role is None or correlation.execution_id is None
    ):
        _fail(
            "INVALID_CORRELATION",
            "Codex execution requires role and execution_id",
            "correlation",
        )
    if event_type is OperationalEventType.WORKTREE_LIFECYCLE and (
        correlation.user_story_id is None or correlation.assignment_id is None
    ):
        _fail(
            "INVALID_CORRELATION",
            "worktree lifecycle requires story and assignment IDs",
            "correlation",
        )
    if event_type in {
        OperationalEventType.INTEGRATION_GATE,
        OperationalEventType.MERGE_OPERATION,
    } and (correlation.wave_index is None or correlation.group_index is None):
        _fail(
            "INVALID_CORRELATION",
            "integration and merge events require wave/group correlation",
            "correlation",
        )


def _mapping(
    candidate: object,
    path: tuple[ErrorPathPart, ...],
    expected: frozenset[str],
) -> Mapping[str, object]:
    if not isinstance(candidate, Mapping) or any(
        not isinstance(key, str) for key in candidate
    ):
        raise OperationalEventError("INVALID_STRUCTURE", "object is required", path=path)
    keys = set(candidate)
    if keys != expected:
        raise OperationalEventError(
            "INVALID_STRUCTURE",
            "object fields must match the closed contract",
            path=path,
        )
    return cast(Mapping[str, object], candidate)


def _enum(enum_type: type[Enum], value: object, path: tuple[ErrorPathPart, ...]):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as error:
        raise OperationalEventError("INVALID_ENUM", "enum value is invalid", path=path) from error


def _validate_event_id(value: object) -> None:
    if not isinstance(value, str) or not _UUID_PATTERN.fullmatch(value):
        _fail("INVALID_EVENT_ID", "event_id must be a canonical lowercase UUID", "event_id")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError as error:
        raise OperationalEventError(
            "INVALID_EVENT_ID",
            "event_id must be a canonical lowercase UUID",
            path=("event_id",),
        ) from error


def _required_text(value: object, name: str, *, maximum: int) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        _fail("INVALID_TEXT", f"{name} is absent or too long", name)
    if normalize("NFC", value) != value or value != value.strip():
        _fail("NON_CANONICAL_TEXT", f"{name} must be trimmed NFC text", name)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _fail("INVALID_TEXT", f"{name} contains control characters", name)
    _reject_secret(value, (name,))


def _optional_text(value: object, name: str, *, maximum: int) -> None:
    if value is not None:
        _required_text(value, name, maximum=maximum)


def _optional_identifier(value: object, name: str) -> None:
    if value is not None and (
        not isinstance(value, str) or not _CORRELATION_ID_PATTERN.fullmatch(value)
    ):
        _fail("INVALID_CORRELATION", f"{name} is not a canonical identifier", name)


def _canonical_token(value: object, pattern: re.Pattern[str], name: str) -> None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _fail("INVALID_TOKEN", f"{name} is not canonical", name)


def _optional_integer(
    value: object,
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> None:
    if value is not None and (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        _fail("INVALID_INTEGER", f"{name} is outside policy", name)


def _validate_scalar(value: object, path: tuple[ErrorPathPart, ...]) -> None:
    if isinstance(value, str):
        if len(value) > MAX_OPERATIONAL_STRING_LENGTH:
            raise OperationalEventError("PAYLOAD_TOO_LARGE", "string exceeds policy", path=path)
        if normalize("NFC", value) != value:
            raise OperationalEventError("NON_CANONICAL_TEXT", "string must use NFC", path=path)
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise OperationalEventError("INVALID_PAYLOAD", "control characters are forbidden", path=path)
        _reject_secret(value, path)
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    raise OperationalEventError(
        "INVALID_PAYLOAD",
        "attribute value must be a finite JSON scalar",
        path=path,
    )


def _reject_secret(value: str, path: tuple[ErrorPathPart, ...]) -> None:
    if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
        raise OperationalEventError(
            "SECRET_MATERIAL",
            "secret-like material is forbidden in operational events",
            path=path,
        )


def _is_sensitive_attribute_name(name: str) -> bool:
    folded = name.casefold()
    segments = frozenset(folded.split("_"))
    sensitive_segments = {
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "env",
        "environment",
        "password",
        "secret",
        "secrets",
        "stderr",
        "stdout",
        "token",
    }
    return (
        folded in _SENSITIVE_ATTRIBUTE_NAMES
        or "api_key" in folded
        or not segments.isdisjoint(sensitive_segments)
    )


def _utc_text(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _fail(code: str, message: str, *path: ErrorPathPart) -> None:
    raise OperationalEventError(code, message, path=path)
