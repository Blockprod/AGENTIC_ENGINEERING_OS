"""Construction and in-memory recording of canonical Evidence facts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import cast
from uuid import uuid4

from agentic_engineering_os.domain import Evidence, EvidenceType
from agentic_engineering_os.domain.models import JsonValue

from .contract_validator import ContractValidator, ValidationIssue


class ProvenanceKind(str, Enum):
    TOOL = "TOOL"
    CODEX = "CODEX"
    HUMAN = "HUMAN"


@dataclass(frozen=True, slots=True)
class EvidenceProvenance:
    kind: ProvenanceKind | str
    source: str
    producer: str


@dataclass(frozen=True, slots=True)
class EvidenceObservation:
    evidence_type: EvidenceType | str
    subject: str
    result: object
    provenance: EvidenceProvenance
    repository_dependent: bool
    command: str | None = None
    exit_code: int | None = None
    artifact: str | None = None
    commit: str | None = None


class EvidenceRecordingError(RuntimeError):
    """An Evidence could not be proven valid and was not recorded."""

    def __init__(
        self,
        code: str,
        message: str,
        validation_errors: tuple[ValidationIssue, ...] = (),
    ) -> None:
        self.code = code
        self.message = message
        self.validation_errors = validation_errors
        super().__init__(f"{code}: {message}")


class EvidenceRecorder:
    """Create validated immutable Evidence and append it to a provided list."""

    def __init__(
        self,
        target: list[Evidence],
        *,
        validator: ContractValidator | None = None,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._target = target
        self._validator = validator or ContractValidator()
        self._id_factory = id_factory or _new_evidence_id
        self._clock = clock or _utc_now

    def record(
        self,
        observation: EvidenceObservation,
        *,
        evidence_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> Evidence:
        """Return and record one complete Evidence, or fail before appending."""

        try:
            return self._record(observation, evidence_id, timestamp)
        except EvidenceRecordingError:
            raise
        except Exception as error:
            raise EvidenceRecordingError(
                "TECHNICAL_ERROR",
                f"recording could not be completed: {type(error).__name__}: {error}",
            ) from error

    def _record(
        self,
        observation: EvidenceObservation,
        evidence_id: str | None,
        timestamp: datetime | None,
    ) -> Evidence:
        if not isinstance(observation, EvidenceObservation):
            raise EvidenceRecordingError(
                "INVALID_OBSERVATION", "a structured EvidenceObservation is required"
            )

        evidence_type = _evidence_type(observation.evidence_type)
        provenance = _provenance(observation.provenance)
        _validate_traceability(evidence_type, observation, provenance)

        resolved_id = evidence_id if evidence_id is not None else self._id_factory()
        if any(item.evidence_id == resolved_id for item in self._target):
            raise EvidenceRecordingError(
                "DUPLICATE_EVIDENCE_ID",
                f"Evidence id already exists: {resolved_id}",
            )

        resolved_timestamp = _utc_timestamp(
            timestamp if timestamp is not None else self._clock()
        )
        candidate = {
            "evidence_id": resolved_id,
            "evidence_type": evidence_type.value,
            "subject": observation.subject,
            "result": observation.result,
            "source": provenance.source,
            "command": observation.command,
            "exit_code": observation.exit_code,
            "artifact": observation.artifact,
            "commit": observation.commit,
            "timestamp": resolved_timestamp.isoformat(),
            "producer": provenance.producer,
        }

        try:
            validation = self._validator.validate("evidence", candidate)
        except Exception as error:
            raise EvidenceRecordingError(
                "VALIDATION_UNAVAILABLE",
                f"Evidence validation could not be completed: {error}",
            ) from error
        if not validation.is_valid:
            raise EvidenceRecordingError(
                "VALIDATION_FAILED",
                "Evidence violates the canonical contract",
                validation.errors,
            )

        evidence = Evidence(
            evidence_id=resolved_id,
            evidence_type=evidence_type,
            subject=observation.subject,
            result=cast(JsonValue, _freeze_json(observation.result)),
            source=provenance.source,
            command=observation.command,
            exit_code=observation.exit_code,
            artifact=observation.artifact,
            commit=observation.commit,
            timestamp=resolved_timestamp,
            producer=provenance.producer,
        )
        self._target.append(evidence)
        return evidence


def _evidence_type(value: EvidenceType | str) -> EvidenceType:
    try:
        return EvidenceType(value)
    except (TypeError, ValueError) as error:
        raise EvidenceRecordingError(
            "UNKNOWN_EVIDENCE_TYPE", f"unknown Evidence type: {value}"
        ) from error


def _provenance(value: EvidenceProvenance) -> EvidenceProvenance:
    if not isinstance(value, EvidenceProvenance):
        raise EvidenceRecordingError(
            "PROVENANCE_REQUIRED", "explicit Evidence provenance is required"
        )
    try:
        kind = ProvenanceKind(value.kind)
    except (TypeError, ValueError) as error:
        raise EvidenceRecordingError(
            "UNKNOWN_PROVENANCE_KIND", f"unknown provenance kind: {value.kind}"
        ) from error
    if not value.source.strip() or not value.producer.strip():
        raise EvidenceRecordingError(
            "PROVENANCE_REQUIRED", "source and producer must be attributable"
        )
    if kind is ProvenanceKind.CODEX:
        prefix, separator, role = value.producer.partition("/")
        if prefix != "Codex" or not separator or not role.strip():
            raise EvidenceRecordingError(
                "CODEX_ROLE_REQUIRED",
                "Codex provenance must identify an explicit role as Codex/<role>",
            )
    return EvidenceProvenance(
        kind=kind,
        source=value.source,
        producer=value.producer,
    )


def _validate_traceability(
    evidence_type: EvidenceType,
    observation: EvidenceObservation,
    provenance: EvidenceProvenance,
) -> None:
    if not isinstance(observation.repository_dependent, bool):
        raise EvidenceRecordingError(
            "REPOSITORY_CONTEXT_UNKNOWN",
            "repository dependence must be stated explicitly",
        )
    if evidence_type is EvidenceType.GIT_STATE and not observation.repository_dependent:
        raise EvidenceRecordingError(
            "REPOSITORY_CONTEXT_REQUIRED", "GIT_STATE Evidence is repository-dependent"
        )
    if observation.repository_dependent and (
        observation.commit is None or not observation.commit.strip()
    ):
        raise EvidenceRecordingError(
            "COMMIT_REQUIRED",
            "repository-dependent Evidence requires an explicit commit",
        )
    if evidence_type is EvidenceType.COMMAND_RESULT and (
        observation.command is None or observation.exit_code is None
    ):
        raise EvidenceRecordingError(
            "COMMAND_DETAILS_REQUIRED",
            "COMMAND_RESULT Evidence requires command and exit code",
        )
    if evidence_type is EvidenceType.HUMAN_APPROVAL:
        if provenance.kind is not ProvenanceKind.HUMAN:
            raise EvidenceRecordingError(
                "HUMAN_PROVENANCE_REQUIRED",
                "HUMAN_APPROVAL Evidence must be explicitly produced by Human",
            )
        if provenance.source.casefold() != "human" or provenance.producer.startswith(
            "Codex"
        ):
            raise EvidenceRecordingError(
                "HUMAN_ACTOR_REQUIRED",
                "Human Evidence requires source Human and a non-Codex producer",
            )


def _utc_timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise EvidenceRecordingError(
            "UTC_TIMESTAMP_REQUIRED", "Evidence timestamp must be timezone-aware"
        )
    return value.astimezone(timezone.utc)


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _new_evidence_id() -> str:
    return f"EV-{uuid4()}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
