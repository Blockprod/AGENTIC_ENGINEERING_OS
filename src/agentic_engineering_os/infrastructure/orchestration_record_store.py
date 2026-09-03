"""Strict atomic persistence for non-authoritative orchestration references."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from agentic_engineering_os.application.orchestration_record import (
    ORCHESTRATION_RECORD_VERSION,
    CertificationReference,
    OrchestrationRecord,
    ParallelIntegrationReference,
    RoleExecutionReference,
    record_to_data,
)
from agentic_engineering_os.application.mission_admission import MissionRequest
from agentic_engineering_os.domain import MissionRole
from .project_state_store import PersistenceError, STATE_DIRECTORY


ORCHESTRATION_RECORD_FILENAME = "orchestration.json"
MAX_ORCHESTRATION_RECORD_BYTES = 2_000_000


class OrchestrationRecordStore:
    def __init__(self, repository_root: Path | str) -> None:
        try:
            self._root = Path(repository_root).resolve(strict=True)
        except OSError as error:
            raise PersistenceError("INVALID_REPOSITORY_ROOT", "repository root cannot be resolved") from error
        self._directory = self._root / STATE_DIRECTORY
        self._path = self._directory / ORCHESTRATION_RECORD_FILENAME

    @property
    def record_path(self) -> Path:
        return self._path

    def load(self) -> OrchestrationRecord:
        self._assert_safe()
        if not self._path.exists():
            raise PersistenceError("ORCHESTRATION_RECORD_ABSENT", "orchestration record is absent")
        try:
            if not self._path.is_file() or self._path.stat().st_size > MAX_ORCHESTRATION_RECORD_BYTES:
                raise PersistenceError(
                    "INVALID_ORCHESTRATION_RECORD",
                    "orchestration record is not a bounded regular file",
                )
            data = json.loads(self._path.read_text(encoding="utf-8"), object_pairs_hook=_strict_object, parse_constant=_reject_constant)
            record = _from_data(data)
            self._validate_repository_binding(record)
            return record
        except PersistenceError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as error:
            raise PersistenceError("INVALID_ORCHESTRATION_RECORD", str(error)) from error

    def initialize(self, record: OrchestrationRecord) -> Path:
        self._assert_safe()
        if self._path.exists():
            raise PersistenceError("ORCHESTRATION_RECORD_EXISTS", "orchestration record already exists")
        return self._write(record)

    def replace(self, record: OrchestrationRecord, *, expected_fingerprint: str) -> Path:
        current = self.load()
        if current.fingerprint != expected_fingerprint:
            raise PersistenceError("ORCHESTRATION_RECORD_CHANGED", "record changed since reconstruction")
        return self._write(record)

    def _write(self, record: OrchestrationRecord) -> Path:
        if not isinstance(record, OrchestrationRecord):
            raise PersistenceError("INVALID_ORCHESTRATION_RECORD", "canonical record is required")
        self._validate_repository_binding(record)
        text = json.dumps(record_to_data(record), ensure_ascii=False, sort_keys=True, indent=2, separators=(",", ": "), allow_nan=False) + "\n"
        self._directory.mkdir(parents=False, exist_ok=True)
        self._assert_safe()
        descriptor, name = tempfile.mkstemp(dir=self._directory, prefix=".orchestration.", suffix=".tmp", text=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self._path)
        except Exception:
            Path(name).unlink(missing_ok=True)
            raise
        return self._path

    def _assert_safe(self) -> None:
        if self._directory.is_symlink() or self._path.is_symlink():
            raise PersistenceError("UNSAFE_PATH", "orchestration path cannot be a symlink")

    def _validate_repository_binding(self, record: OrchestrationRecord) -> None:
        try:
            request_root = Path(record.request.repository_root).resolve(strict=True)
        except OSError as error:
            raise PersistenceError(
                "FOREIGN_ORCHESTRATION_RECORD", "request repository cannot be resolved"
            ) from error
        if os.path.normcase(str(request_root)).casefold() != os.path.normcase(
            str(self._root)
        ).casefold():
            raise PersistenceError(
                "FOREIGN_ORCHESTRATION_RECORD",
                "orchestration request belongs to a different repository",
            )


def _from_data(value: object) -> OrchestrationRecord:
    legacy_fields = {"schema_version", "mission_id", "request", "request_fingerprint", "baseline_commit", "workflow_generation", "plan_fingerprint", "execution_references", "user_story_ids"}
    version_1_1_fields = {*legacy_fields, "parallel_integration"}
    current_fields = {*version_1_1_fields, "certification_references"}
    if not isinstance(value, dict):
        raise ValueError("record has unknown or missing fields")
    schema_fields = {
        "1.0": legacy_fields,
        "1.1": version_1_1_fields,
        ORCHESTRATION_RECORD_VERSION: current_fields,
    }
    expected_fields = schema_fields.get(value.get("schema_version"))
    if expected_fields is None or set(value) != expected_fields:
        raise ValueError("record has unknown or missing fields")
    request = value["request"]
    if not isinstance(request, dict) or set(request) != {"objective", "repository_root", "requested_scope", "verification_command_ids"}:
        raise ValueError("request has unknown or missing fields")
    references = value["execution_references"]
    if not isinstance(references, list):
        raise ValueError("execution_references must be an array")
    return OrchestrationRecord(
        schema_version=ORCHESTRATION_RECORD_VERSION,
        mission_id=str(value["mission_id"]),
        request=MissionRequest(str(request["objective"]), str(request["repository_root"]), _strings(request["requested_scope"]), _strings(request["verification_command_ids"])),
        request_fingerprint=str(value["request_fingerprint"]),
        baseline_commit=str(value["baseline_commit"]),
        workflow_generation=_integer(value["workflow_generation"]),
        plan_fingerprint=None if value["plan_fingerprint"] is None else str(value["plan_fingerprint"]),
        execution_references=tuple(_reference(item) for item in references),
        user_story_ids=_strings(value["user_story_ids"]),
        parallel_integration=_parallel_reference(value.get("parallel_integration")),
        certification_references=tuple(
            _certification_reference(item)
            for item in value.get("certification_references", [])
        ),
    )


def _certification_reference(value: object) -> CertificationReference:
    expected = {
        "user_story_id",
        "workflow_generation",
        "certification_id",
        "certification_fingerprint",
        "commit",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("Certification reference has unknown or missing fields")
    return CertificationReference(
        str(value["user_story_id"]),
        _integer(value["workflow_generation"]),
        str(value["certification_id"]),
        str(value["certification_fingerprint"]),
        str(value["commit"]),
    )


def _parallel_reference(value: object) -> ParallelIntegrationReference | None:
    if value is None:
        return None
    expected = {"plan_fingerprint", "wave_index", "group_index", "assignment_ids", "gate_fingerprint", "integrated_commit"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("parallel integration reference has unknown or missing fields")
    return ParallelIntegrationReference(
        str(value["plan_fingerprint"]),
        _integer(value["wave_index"]),
        _integer(value["group_index"]),
        _strings(value["assignment_ids"]),
        None if value["gate_fingerprint"] is None else str(value["gate_fingerprint"]),
        None if value["integrated_commit"] is None else str(value["integrated_commit"]),
    )


def _reference(value: object) -> RoleExecutionReference:
    if not isinstance(value, dict) or set(value) != {"role", "subject", "workflow_generation", "request_id", "execution_id", "result_fingerprint"}:
        raise ValueError("execution reference has unknown or missing fields")
    return RoleExecutionReference(MissionRole(str(value["role"])), str(value["subject"]), _integer(value["workflow_generation"]), str(value["request_id"]), str(value["execution_id"]), str(value["result_fingerprint"]))


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("expected string array")
    return tuple(value)


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("expected integer")
    return value


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError(f"non-JSON constant: {value}")
