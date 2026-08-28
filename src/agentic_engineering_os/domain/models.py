"""Data-only domain models derived from the certified Phase 0 contracts."""

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import TypeAlias, cast

from .enums import (
    AuditEventType,
    CertificationResult,
    ConflictClassification,
    ConflictReason,
    DeferredReason,
    EvidenceType,
    GateResult,
    MissionRole,
    MissionStatus,
    OperatingStep,
    ReadinessClassification,
    RiskLevel,
    UserStoryStatus,
    WorktreeStatus,
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
    evidence_ref: str | None = None


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


@dataclass(frozen=True, slots=True)
class DAGNode:
    """Immutable projection of one User Story into the logical DAG."""

    user_story_id: str
    status: UserStoryStatus
    priority: int
    risk: RiskLevel
    depends_on: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DAGEdge:
    """Directed dependency edge: dependency -> dependent."""

    dependency_id: str
    dependent_id: str


@dataclass(frozen=True, slots=True)
class DAGSnapshot:
    """Immutable, deterministic and non-persistent ProjectState projection."""

    nodes: tuple[DAGNode, ...]
    edges: tuple[DAGEdge, ...]


@dataclass(frozen=True, slots=True)
class NodeReadiness:
    """One deterministic readiness diagnosis for a DAG node."""

    user_story_id: str
    classification: ReadinessClassification
    satisfied_dependencies: tuple[str, ...]
    unsatisfied_dependencies: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class ReadinessSnapshot:
    """Immutable readiness diagnoses in canonical User Story order."""

    nodes: tuple[NodeReadiness, ...]

    def _ids_for(self, classification: ReadinessClassification) -> tuple[str, ...]:
        return tuple(
            node.user_story_id
            for node in self.nodes
            if node.classification is classification
        )

    @property
    def ready_ids(self) -> tuple[str, ...]:
        return self._ids_for(ReadinessClassification.READY)

    @property
    def waiting_ids(self) -> tuple[str, ...]:
        return self._ids_for(ReadinessClassification.WAITING_DEPENDENCIES)

    @property
    def blocked_ids(self) -> tuple[str, ...]:
        return self._ids_for(ReadinessClassification.BLOCKED)

    @property
    def ineligible_ids(self) -> tuple[str, ...]:
        return self._ids_for(ReadinessClassification.INELIGIBLE)

    @property
    def terminal_ids(self) -> tuple[str, ...]:
        return self._ids_for(ReadinessClassification.TERMINAL)


@dataclass(frozen=True, slots=True)
class WaveMember:
    """Minimal member of one prospective logical execution layer."""

    user_story_id: str
    priority: int
    risk: RiskLevel


@dataclass(frozen=True, slots=True)
class ExecutionWave:
    """One zero-based logical DAG layer, not a concurrency authorization."""

    wave_index: int
    members: tuple[WaveMember, ...]


@dataclass(frozen=True, slots=True)
class DeferredNode:
    """A node excluded from prospective waves with a deterministic reason."""

    user_story_id: str
    reason: DeferredReason
    blocking_dependencies: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WavePlan:
    """Immutable prospective DAG layering with all non-planned nodes explained."""

    waves: tuple[ExecutionWave, ...]
    deferred: tuple[DeferredNode, ...]


@dataclass(frozen=True, slots=True)
class ExecutionConflict:
    """Canonical pairwise compatibility result within one logical Wave."""

    wave_index: int
    left_user_story_id: str
    right_user_story_id: str
    classification: ConflictClassification
    reasons: tuple[ConflictReason, ...]
    overlapping_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConflictAnalysis:
    """Immutable same-Wave pairwise conflict analysis."""

    pairs: tuple[ExecutionConflict, ...]

    def _pairs_for(
        self, classification: ConflictClassification
    ) -> tuple[ExecutionConflict, ...]:
        return tuple(
            pair for pair in self.pairs if pair.classification is classification
        )

    @property
    def safe_pairs(self) -> tuple[ExecutionConflict, ...]:
        return self._pairs_for(ConflictClassification.SAFE)

    @property
    def conflicting_pairs(self) -> tuple[ExecutionConflict, ...]:
        return self._pairs_for(ConflictClassification.CONFLICT)

    @property
    def unknown_pairs(self) -> tuple[ExecutionConflict, ...]:
        return self._pairs_for(ConflictClassification.UNKNOWN)


@dataclass(frozen=True, slots=True)
class WorktreeAssignment:
    """Immutable expected state of one isolated Git worktree assignment."""

    assignment_id: str
    mission_id: str
    user_story_id: str
    workflow_generation: int
    baseline_commit: str
    branch_name: str
    worktree_path: str
    status: WorktreeStatus
    result_commit: str | None


@dataclass(frozen=True, slots=True)
class WorktreeRegistry:
    """Versioned, deterministic expected state for external worktree resources."""

    schema_version: str
    assignments: tuple[WorktreeAssignment, ...]


@dataclass(slots=True)
class MissionState:
    """Operational mission memory without project-control authority."""

    schema_version: str
    mission_id: str
    workflow_generation: int
    status: MissionStatus
    role: MissionRole
    objective: str
    subject: str
    operating_step: OperatingStep
    next_action: str
    observed_commit: str
    updated_at: datetime
    blockers: list[str] = field(default_factory=list)


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
