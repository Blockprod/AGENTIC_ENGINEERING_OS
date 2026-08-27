"""Data-only domain models derived from the certified Phase 0 contracts."""

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import TypeAlias, cast

from .enums import (
    AuditEventType,
    CertificationResult,
    EvidenceType,
    GateResult,
    RiskLevel,
    UserStoryStatus,
)


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class AcceptanceCriterion:
    id: str
    description: str
    mandatory: bool


@dataclass(frozen=True, slots=True)
class UserStoryScope:
    allowed_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]


@dataclass(slots=True)
class HumanApproval:
    required: bool
    approved: bool
    approved_by: str | None
    approved_at: datetime | None


@dataclass(slots=True)
class UserStoryMetadata:
    created_at: datetime
    created_by: str
    updated_at: datetime


@dataclass(slots=True)
class UserStory:
    """A mutable aggregate whose future changes must be service-controlled."""

    schema_version: str
    id: str
    title: str
    description: str
    status: UserStoryStatus
    priority: int
    risk: RiskLevel
    depends_on: tuple[str, ...]
    scope: UserStoryScope
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    required_gates: tuple[str, ...]
    human_approval: HumanApproval
    metadata: UserStoryMetadata


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    evidence_type: EvidenceType
    subject: str
    result: JsonValue
    source: str
    command: str | None
    exit_code: int | None
    artifact: str | None
    commit: str | None
    timestamp: datetime
    producer: str


@dataclass(frozen=True, slots=True)
class Gate:
    gate_id: str
    subject: str
    required: bool
    result: GateResult
    evidence_refs: tuple[str, ...]
    evaluated_at: datetime
    evaluator: str


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    timestamp: datetime
    event_type: AuditEventType
    subject: str
    actor: str
    role: str
    repository_commit: str
    payload: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class Certification:
    certification_id: str
    subject: str
    result: CertificationResult
    commit: str
    acceptance_results: Mapping[str, JsonValue]
    gate_results: Mapping[str, JsonValue]
    human_approval: Mapping[str, JsonValue]
    evidence_refs: tuple[str, ...]
    certified_at: datetime
    certifier: str
    authorized_not_applicable_gates: tuple[str, ...] = ()


@dataclass(slots=True)
class ProjectState:
    """Minimal aggregate; its persistent V1 contract remains reserved for P1.8."""

    schema_version: str
    user_stories: list[UserStory] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    gates: list[Gate] = field(default_factory=list)
    certifications: list[Certification] = field(default_factory=list)
    audit_events: list[AuditEvent] = field(default_factory=list)


def to_dict(model: object) -> dict[str, JsonValue]:
    """Return a deterministic, JSON-compatible mapping for a dataclass model."""

    if not is_dataclass(model) or isinstance(model, type):
        raise TypeError("to_dict expects a dataclass instance")
    return {
        item.name: _to_json_value(getattr(model, item.name))
        for item in fields(model)
    }


def _to_json_value(value: object) -> JsonValue:
    if isinstance(value, Enum):
        return cast(str, value.value)
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return to_dict(value)
    if isinstance(value, Mapping):
        return {
            str(key): _to_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_to_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")
