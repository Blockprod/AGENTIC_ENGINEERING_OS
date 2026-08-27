"""Validated JSON persistence for the canonical ProjectState."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn, cast

from agentic_engineering_os.application import ContractValidator
from agentic_engineering_os.application.certification_integrity import (
    certified_dossier_issues,
)
from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    AuditEvent,
    AuditEventType,
    Certification,
    CertificationResult,
    Evidence,
    EvidenceType,
    Gate,
    GateResult,
    HumanApproval,
    ProjectState,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
    to_dict,
)


STATE_DIRECTORY = ".agentic-engineering-os"
STATE_FILENAME = "state.json"
SCHEMA_VERSION = "1.0"


class PersistenceError(RuntimeError):
    """Persistent state could not be read, validated, or written safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class _DuplicateJsonKeyError(ValueError):
    """A JSON object contains an ambiguous repeated member name."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    candidate: dict[str, Any] = {}
    for key, value in pairs:
        if key in candidate:
            raise _DuplicateJsonKeyError(key)
        candidate[key] = value
    return candidate


class ProjectStateStore:
    """Atomic local ProjectState store; V1 expects a single writer."""

    def __init__(
        self,
        repository_root: Path | str,
        *,
        validator: ContractValidator | None = None,
    ) -> None:
        root = Path(repository_root)
        try:
            self._root = root.resolve(strict=True)
        except OSError as error:
            raise PersistenceError(
                "INVALID_REPOSITORY_ROOT",
                f"repository root cannot be resolved: {root}",
            ) from error
        if not self._root.is_dir():
            raise PersistenceError(
                "INVALID_REPOSITORY_ROOT",
                f"repository root is not a directory: {self._root}",
            )
        self._state_directory = self._root / STATE_DIRECTORY
        self._state_path = self._state_directory / STATE_FILENAME
        self._validator = validator if validator is not None else ContractValidator()

    @property
    def state_path(self) -> Path:
        return self._state_path

    def initialize(self, *, schema_version: str = SCHEMA_VERSION) -> ProjectState:
        """Explicitly create a new empty state; never overwrite existing state."""

        self._assert_safe_paths(for_write=True)
        if self._state_path.exists():
            raise PersistenceError(
                "STATE_ALREADY_EXISTS",
                f"authoritative state already exists: {self._state_path}",
            )
        state = ProjectState(schema_version=schema_version)
        self.save(state)
        return state

    def load(self) -> ProjectState:
        """Load an existing state or fail without creating any fallback state."""

        self._assert_safe_paths(for_write=False)
        if not self._state_path.exists():
            raise PersistenceError(
                "STATE_ABSENT", f"authoritative state is absent: {self._state_path}"
            )
        if not self._state_path.is_file():
            raise PersistenceError(
                "READ_FAILED", f"state path is not a file: {self._state_path}"
            )
        try:
            text = self._state_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise PersistenceError(
                "READ_FAILED", f"state cannot be read: {self._state_path}"
            ) from error
        try:
            candidate = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
        except _DuplicateJsonKeyError as error:
            raise PersistenceError(
                "INVALID_JSON", f"duplicate JSON key: {error.key}"
            ) from error
        except json.JSONDecodeError as error:
            raise PersistenceError(
                "INVALID_JSON",
                f"state is not valid JSON at line {error.lineno}, column {error.colno}",
            ) from error

        self._validate_serialized(candidate)
        try:
            state = _hydrate_project_state(_mapping(candidate, "project state"))
        except PersistenceError:
            raise
        except Exception as error:
            raise PersistenceError(
                "INVALID_DOMAIN_DATA",
                f"state cannot be hydrated: {type(error).__name__}: {error}",
            ) from error
        self._validate_state(state)
        return state

    def save(self, state: ProjectState) -> Path:
        """Validate and atomically replace state.json, preserving prior state."""

        self._assert_safe_paths(for_write=True)
        serialized = self._validate_state(state)
        text = _canonical_json(serialized)

        try:
            self._state_directory.mkdir(parents=False, exist_ok=True)
        except OSError as error:
            raise PersistenceError(
                "WRITE_FAILED",
                f"state directory cannot be created: {self._state_directory}",
            ) from error
        self._assert_safe_paths(for_write=True)

        temporary_path: Path | None = None
        try:
            temporary_path = _write_temporary(self._state_directory, text)
            os.replace(temporary_path, self._state_path)
        except Exception as error:
            _cleanup_temporary(temporary_path)
            raise PersistenceError(
                "WRITE_FAILED",
                f"atomic state write failed: {type(error).__name__}: {error}",
            ) from error
        return self._state_path

    def _validate_state(self, state: ProjectState) -> dict[str, object]:
        if not isinstance(state, ProjectState):
            raise PersistenceError(
                "INVALID_DOMAIN_DATA", "save requires an explicit ProjectState"
            )
        try:
            serialized = cast(dict[str, object], to_dict(state))
        except Exception as error:
            raise PersistenceError(
                "INVALID_DOMAIN_DATA",
                f"ProjectState cannot be serialized: {type(error).__name__}: {error}",
            ) from error
        self._validate_serialized(serialized)
        _validate_integrity(state)
        return serialized

    def _validate_serialized(self, candidate: object) -> None:
        try:
            validation = self._validator.validate("project-state", candidate)
        except Exception as error:
            raise PersistenceError(
                "VALIDATION_UNAVAILABLE",
                f"ProjectState validation could not be completed: {error}",
            ) from error
        if not validation.is_valid:
            details = "; ".join(
                f"{'.'.join(map(str, issue.path)) or '<root>'}: {issue.message}"
                for issue in validation.errors
            )
            raise PersistenceError(
                "INVALID_SCHEMA", f"ProjectState violates its schema: {details}"
            )

    def _assert_safe_paths(self, *, for_write: bool) -> None:
        if self._state_directory.exists() and self._state_directory.is_symlink():
            raise PersistenceError(
                "UNSAFE_PATH", f"state directory cannot be a symlink: {self._state_directory}"
            )
        if self._state_path.exists() and self._state_path.is_symlink():
            raise PersistenceError(
                "UNSAFE_PATH", f"state file cannot be a symlink: {self._state_path}"
            )
        if not for_write and not self._state_directory.exists():
            return
        try:
            parent = self._state_directory.parent.resolve(strict=True)
        except OSError as error:
            raise PersistenceError(
                "UNSAFE_PATH", "state path parent cannot be resolved"
            ) from error
        if parent != self._root:
            raise PersistenceError("UNSAFE_PATH", "state path escapes repository root")


def _write_temporary(directory: Path, text: str) -> Path:
    descriptor, name = tempfile.mkstemp(
        dir=directory,
        prefix=".state.",
        suffix=".tmp",
        text=True,
    )
    path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        _cleanup_temporary(path)
        raise
    return path


def _cleanup_temporary(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _canonical_json(candidate: object) -> str:
    try:
        return json.dumps(
            candidate,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
        ) + "\n"
    except (TypeError, ValueError) as error:
        raise PersistenceError(
            "INVALID_DOMAIN_DATA", "ProjectState is not JSON serializable"
        ) from error


def _validate_integrity(state: ProjectState) -> None:
    story_ids = _unique_ids(state.user_stories, "id", "User Story")
    evidence_ids = _unique_ids(state.evidence, "evidence_id", "Evidence")
    gate_ids = _unique_ids(state.gates, "gate_id", "Gate")
    _unique_ids(state.certifications, "certification_id", "Certification")
    _unique_ids(state.audit_events, "event_id", "Audit Event")

    for story in state.user_stories:
        if story.id in story.depends_on:
            _integrity_error(f"User Story {story.id} cannot depend on itself")
        criterion_ids = [criterion.id for criterion in story.acceptance_criteria]
        duplicate_criteria = sorted(
            {
                criterion_id
                for criterion_id in criterion_ids
                if criterion_ids.count(criterion_id) > 1
            }
        )
        if duplicate_criteria:
            raise PersistenceError(
                "DUPLICATE_ID",
                f"duplicate Acceptance Criterion ids in {story.id}: "
                f"{duplicate_criteria}",
            )
        missing_dependencies = set(story.depends_on) - story_ids
        if missing_dependencies:
            _integrity_error(
                f"User Story {story.id} references missing dependencies: "
                f"{sorted(missing_dependencies)}"
            )

    for gate in state.gates:
        missing_evidence = set(gate.evidence_refs) - evidence_ids
        if missing_evidence:
            _integrity_error(
                f"Gate {gate.gate_id} references missing Evidence: "
                f"{sorted(missing_evidence)}"
            )

    stories_by_id = {story.id: story for story in state.user_stories}
    for certification in state.certifications:
        story = stories_by_id.get(certification.subject)
        if story is None:
            _integrity_error(
                f"Certification {certification.certification_id} references missing "
                f"User Story: {certification.subject}"
            )
        missing_evidence = set(certification.evidence_refs) - evidence_ids
        if missing_evidence:
            _integrity_error(
                f"Certification {certification.certification_id} references missing "
                f"Evidence: {sorted(missing_evidence)}"
            )
        missing_gates = set(certification.gate_results) - gate_ids
        if missing_gates:
            _integrity_error(
                f"Certification {certification.certification_id} references missing "
                f"Gates: {sorted(missing_gates)}"
            )
        criterion_ids = {criterion.id for criterion in story.acceptance_criteria}
        missing_criteria = set(certification.acceptance_results) - criterion_ids
        if missing_criteria:
            _integrity_error(
                f"Certification {certification.certification_id} references missing "
                f"Acceptance Criteria: {sorted(missing_criteria)}"
            )
        human_ref = certification.human_approval.get("evidence_ref")
        if isinstance(human_ref, str) and human_ref not in evidence_ids:
            _integrity_error(
                f"Certification {certification.certification_id} references missing "
                f"Human Evidence: {human_ref}"
            )
        integrity_issues = certified_dossier_issues(
            story,
            certification,
            state.gates,
            state.evidence,
        )
        if integrity_issues:
            details = "; ".join(
                f"{issue.code}: {issue.message}" for issue in integrity_issues
            )
            raise PersistenceError(
                "INVALID_CERTIFICATION_INTEGRITY",
                f"Certification {certification.certification_id} has no "
                f"authoritative dossier for User Story {story.id}: {details}",
            )

    for story in state.user_stories:
        if story.status is not UserStoryStatus.CERTIFIED:
            continue
        results = {
            certification.result
            for certification in state.certifications
            if certification.subject == story.id
        }
        if CertificationResult.CERTIFIED not in results:
            raise PersistenceError(
                "INVALID_STATE_INTEGRITY",
                f"User Story {story.id} is CERTIFIED without an applicable "
                "CERTIFIED Certification",
            )
        if results != {CertificationResult.CERTIFIED}:
            raise PersistenceError(
                "INVALID_STATE_INTEGRITY",
                f"User Story {story.id} is CERTIFIED with contradictory "
                "Certification results",
            )


def _unique_ids(items: Sequence[object], field: str, label: str) -> set[str]:
    values = [getattr(item, field) for item in items]
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise PersistenceError(
            "DUPLICATE_ID", f"duplicate {label} ids: {duplicates}"
        )
    return set(values)


def _integrity_error(message: str) -> NoReturn:
    raise PersistenceError("INVALID_REFERENCE", message)


def _hydrate_project_state(data: Mapping[str, object]) -> ProjectState:
    return ProjectState(
        schema_version=_string(data["schema_version"], "schema_version"),
        user_stories=[
            _hydrate_user_story(_mapping(item, "user_stories item"))
            for item in _sequence(data["user_stories"], "user_stories")
        ],
        evidence=[
            _hydrate_evidence(_mapping(item, "evidence item"))
            for item in _sequence(data["evidence"], "evidence")
        ],
        gates=[
            _hydrate_gate(_mapping(item, "gates item"))
            for item in _sequence(data["gates"], "gates")
        ],
        certifications=[
            _hydrate_certification(_mapping(item, "certifications item"))
            for item in _sequence(data["certifications"], "certifications")
        ],
        audit_events=[
            _hydrate_audit_event(_mapping(item, "audit_events item"))
            for item in _sequence(data["audit_events"], "audit_events")
        ],
    )


def _hydrate_user_story(data: Mapping[str, object]) -> UserStory:
    scope = _mapping(data["scope"], "scope")
    approval = _mapping(data["human_approval"], "human_approval")
    metadata = _mapping(data["metadata"], "metadata")
    return UserStory(
        schema_version=_string(data["schema_version"], "schema_version"),
        id=_string(data["id"], "id"),
        title=_string(data["title"], "title"),
        description=_string(data["description"], "description"),
        status=UserStoryStatus(_string(data["status"], "status")),
        priority=_integer(data["priority"], "priority"),
        risk=RiskLevel(_string(data["risk"], "risk")),
        depends_on=tuple(_strings(data["depends_on"], "depends_on")),
        scope=UserStoryScope(
            allowed_paths=tuple(_strings(scope["allowed_paths"], "allowed_paths")),
            forbidden_paths=tuple(
                _strings(scope["forbidden_paths"], "forbidden_paths")
            ),
        ),
        acceptance_criteria=tuple(
            AcceptanceCriterion(
                id=_string(criterion["id"], "criterion.id"),
                description=_string(
                    criterion["description"], "criterion.description"
                ),
                mandatory=_boolean(criterion["mandatory"], "criterion.mandatory"),
            )
            for criterion in (
                _mapping(item, "acceptance criterion")
                for item in _sequence(
                    data["acceptance_criteria"], "acceptance_criteria"
                )
            )
        ),
        required_gates=tuple(_strings(data["required_gates"], "required_gates")),
        human_approval=HumanApproval(
            required=_boolean(approval["required"], "human_approval.required"),
            approved=_boolean(approval["approved"], "human_approval.approved"),
            approved_by=_optional_string(
                approval["approved_by"], "human_approval.approved_by"
            ),
            approved_at=_optional_datetime(
                approval["approved_at"], "human_approval.approved_at"
            ),
        ),
        metadata=UserStoryMetadata(
            created_at=_datetime(metadata["created_at"], "metadata.created_at"),
            created_by=_string(metadata["created_by"], "metadata.created_by"),
            updated_at=_datetime(metadata["updated_at"], "metadata.updated_at"),
        ),
    )


def _hydrate_evidence(data: Mapping[str, object]) -> Evidence:
    return Evidence(
        evidence_id=_string(data["evidence_id"], "evidence_id"),
        evidence_type=EvidenceType(_string(data["evidence_type"], "evidence_type")),
        subject=_string(data["subject"], "subject"),
        result=cast(Any, _freeze_json(data["result"])),
        source=_string(data["source"], "source"),
        command=_optional_string(data["command"], "command"),
        exit_code=_optional_integer(data["exit_code"], "exit_code"),
        artifact=_optional_string(data["artifact"], "artifact"),
        commit=_optional_string(data["commit"], "commit"),
        timestamp=_datetime(data["timestamp"], "timestamp"),
        producer=_string(data["producer"], "producer"),
    )


def _hydrate_gate(data: Mapping[str, object]) -> Gate:
    return Gate(
        gate_id=_string(data["gate_id"], "gate_id"),
        subject=_string(data["subject"], "subject"),
        required=_boolean(data["required"], "required"),
        result=GateResult(_string(data["result"], "result")),
        evidence_refs=tuple(_strings(data["evidence_refs"], "evidence_refs")),
        evaluated_at=_datetime(data["evaluated_at"], "evaluated_at"),
        evaluator=_string(data["evaluator"], "evaluator"),
    )


def _hydrate_certification(data: Mapping[str, object]) -> Certification:
    return Certification(
        certification_id=_string(data["certification_id"], "certification_id"),
        subject=_string(data["subject"], "subject"),
        result=CertificationResult(_string(data["result"], "result")),
        commit=_string(data["commit"], "commit"),
        acceptance_results=cast(
            Mapping[str, Any],
            _freeze_json(_mapping(data["acceptance_results"], "acceptance_results")),
        ),
        gate_results=cast(
            Mapping[str, Any],
            _freeze_json(_mapping(data["gate_results"], "gate_results")),
        ),
        human_approval=cast(
            Mapping[str, Any],
            _freeze_json(_mapping(data["human_approval"], "human_approval")),
        ),
        evidence_refs=tuple(_strings(data["evidence_refs"], "evidence_refs")),
        certified_at=_datetime(data["certified_at"], "certified_at"),
        certifier=_string(data["certifier"], "certifier"),
        authorized_not_applicable_gates=tuple(
            _strings(
                data["authorized_not_applicable_gates"],
                "authorized_not_applicable_gates",
            )
        ),
    )


def _hydrate_audit_event(data: Mapping[str, object]) -> AuditEvent:
    return AuditEvent(
        event_id=_string(data["event_id"], "event_id"),
        timestamp=_datetime(data["timestamp"], "timestamp"),
        event_type=AuditEventType(_string(data["event_type"], "event_type")),
        subject=_string(data["subject"], "subject"),
        actor=_string(data["actor"], "actor"),
        role=_string(data["role"], "role"),
        repository_commit=_string(data["repository_commit"], "repository_commit"),
        payload=cast(
            Mapping[str, Any], _freeze_json(_mapping(data["payload"], "payload"))
        ),
    )


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PersistenceError("INVALID_DOMAIN_DATA", f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise PersistenceError("INVALID_DOMAIN_DATA", f"{label} must be an array")
    return value


def _strings(value: object, label: str) -> list[str]:
    return [_string(item, label) for item in _sequence(value, label)]


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise PersistenceError("INVALID_DOMAIN_DATA", f"{label} must be a string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _string(value, label)


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PersistenceError("INVALID_DOMAIN_DATA", f"{label} must be an integer")
    return value


def _optional_integer(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _integer(value, label)


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise PersistenceError("INVALID_DOMAIN_DATA", f"{label} must be a boolean")
    return value


def _datetime(value: object, label: str) -> datetime:
    text = _string(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise PersistenceError(
            "INVALID_DOMAIN_DATA", f"{label} must be an ISO 8601 datetime"
        ) from error
    if parsed.tzinfo is None:
        raise PersistenceError(
            "INVALID_DOMAIN_DATA", f"{label} must include a timezone"
        )
    return parsed


def _optional_datetime(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    return _datetime(value, label)


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value
