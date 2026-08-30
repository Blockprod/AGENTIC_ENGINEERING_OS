"""Persistent operational facts for restart-safe Codex execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Protocol, cast

from agentic_engineering_os.domain import MissionRole

from .codex_runtime import (
    CodexExecutionBinding,
    CodexExecutionObservation,
    CodexJsonlEvent,
    GitExecutionObservation,
    InvalidJsonlLine,
)
from .prompt_compiler import CompiledPrompt


EXECUTION_LEDGER_VERSION = "1.0"
_SHA40 = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class CodexExecutionStatus(str, Enum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    OBSERVED = "OBSERVED"
    VALIDATED = "VALIDATED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class RestartDisposition(str, Enum):
    SAFE_NOT_STARTED = "SAFE_NOT_STARTED"
    INTAKE_REPLAY_AVAILABLE = "INTAKE_REPLAY_AVAILABLE"
    VALIDATED_NO_RERUN = "VALIDATED_NO_RERUN"
    NEW_REQUEST_REQUIRED = "NEW_REQUEST_REQUIRED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    STALE_OR_INCONSISTENT = "STALE_OR_INCONSISTENT"


@dataclass(frozen=True, slots=True)
class ExecutionExecutableIdentity:
    path: str
    version: str
    sha256: str


@dataclass(frozen=True, slots=True)
class CodexExecutionRecord:
    execution_id: str
    semantic_fingerprint: str
    request_id: str
    context_fingerprint: str
    mission_id: str
    workflow_generation: int
    role: MissionRole
    subject: str
    repository_root: str
    worktree_path: str | None
    cwd: str
    expected_commit: str
    compiled_prompt_fingerprint: str
    expected_result_contract: str
    executable: ExecutionExecutableIdentity
    status: CodexExecutionStatus
    created_at: datetime
    updated_at: datetime
    observation: CodexExecutionObservation | None = None
    validated_result_json: str | None = None
    validated_result_fingerprint: str | None = None
    failure_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CodexExecutionLedger:
    schema_version: str
    records: tuple[CodexExecutionRecord, ...]


@dataclass(frozen=True, slots=True)
class RestartInspection:
    execution_id: str
    status: CodexExecutionStatus
    disposition: RestartDisposition
    current_git: GitExecutionObservation
    can_execute_current_request: bool
    can_replay_intake: bool
    blind_retry_allowed: bool
    operator_intervention_required: bool
    reasons: tuple[str, ...]


class ExecutionStateError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class ExecutionLedgerStorePort(Protocol):
    def initialize(self) -> CodexExecutionLedger: ...
    def load(self) -> CodexExecutionLedger: ...
    def _replace_authorized(
        self,
        candidate: CodexExecutionLedger,
        *,
        authorization: object,
        operation: str,
    ) -> Path: ...


class ExecutionGitObserverPort(Protocol):
    def observe(self, cwd: str) -> GitExecutionObservation: ...


def compiled_prompt_fingerprint(compiled: CompiledPrompt) -> str:
    if not isinstance(compiled, CompiledPrompt):
        raise ExecutionStateError(
            "INVALID_COMPILED_PROMPT", "compiled prompt type is invalid"
        )
    payload = {
        "request_id": compiled.request_id,
        "context_fingerprint": compiled.context_fingerprint,
        "mission_id": compiled.mission_id,
        "workflow_generation": compiled.workflow_generation,
        "role": compiled.role.value,
        "subject": compiled.subject,
        "repository_root": compiled.repository_root,
        "worktree_path": compiled.worktree_path,
        "observed_commit": compiled.observed_commit,
        "expected_result_contract": compiled.expected_result_contract,
        "prompt_text": compiled.prompt_text,
        "character_count": compiled.character_count,
        "section_count": compiled.section_count,
        "cognitive_item_count": compiled.cognitive_item_count,
    }
    return _fingerprint_json(payload)


def semantic_execution_fingerprint(compiled: CompiledPrompt) -> str:
    """Bind execution semantics independently from a replaceable UUID-like id."""

    payload = {
        "context_fingerprint": compiled.context_fingerprint,
        "mission_id": compiled.mission_id,
        "workflow_generation": compiled.workflow_generation,
        "role": compiled.role.value,
        "subject": compiled.subject,
        "repository_root": _path_key(compiled.repository_root),
        "worktree_path": (
            _path_key(compiled.worktree_path)
            if compiled.worktree_path is not None
            else None
        ),
        "observed_commit": compiled.observed_commit.casefold(),
        "expected_result_contract": compiled.expected_result_contract,
        "compiled_prompt_fingerprint": compiled_prompt_fingerprint(compiled),
    }
    return _fingerprint_json(payload)


def derive_execution_id(compiled: CompiledPrompt) -> str:
    payload = {
        "request_id": compiled.request_id,
        "semantic_fingerprint": semantic_execution_fingerprint(compiled),
    }
    return f"cx-{_fingerprint_json(payload)[:24]}"


def _record_semantic_fingerprint(record: CodexExecutionRecord) -> str:
    return _fingerprint_json(
        {
            "context_fingerprint": record.context_fingerprint,
            "mission_id": record.mission_id,
            "workflow_generation": record.workflow_generation,
            "role": record.role.value,
            "subject": record.subject,
            "repository_root": _path_key(record.repository_root),
            "worktree_path": _path_key(record.worktree_path) if record.worktree_path is not None else None,
            "observed_commit": record.expected_commit,
            "expected_result_contract": record.expected_result_contract,
            "compiled_prompt_fingerprint": record.compiled_prompt_fingerprint,
        }
    )


def _record_execution_id(record: CodexExecutionRecord) -> str:
    return f"cx-{_fingerprint_json({'request_id': record.request_id, 'semantic_fingerprint': record.semantic_fingerprint})[:24]}"


def validate_record(
    record: CodexExecutionRecord,
    *,
    max_output_characters: int,
) -> None:
    if not isinstance(record, CodexExecutionRecord):
        raise ExecutionStateError("INVALID_RECORD", "record type is invalid")
    strings = (
        record.execution_id,
        record.semantic_fingerprint,
        record.request_id,
        record.context_fingerprint,
        record.mission_id,
        record.subject,
        record.repository_root,
        record.cwd,
        record.expected_commit,
        record.compiled_prompt_fingerprint,
        record.expected_result_contract,
    )
    if not all(isinstance(value, str) and value for value in strings):
        raise ExecutionStateError("INVALID_RECORD", "record strings must be explicit")
    if not re.fullmatch(r"cx-[0-9a-f]{24}", record.execution_id):
        raise ExecutionStateError("INVALID_RECORD", "execution id is not canonical")
    if not all(
        _SHA256.fullmatch(value)
        for value in (
            record.semantic_fingerprint,
            record.context_fingerprint,
            record.compiled_prompt_fingerprint,
        )
    ):
        raise ExecutionStateError("INVALID_RECORD", "record fingerprints are invalid")
    if not _SHA40.fullmatch(record.expected_commit):
        raise ExecutionStateError("INVALID_RECORD", "expected commit is invalid")
    if (
        not isinstance(record.workflow_generation, int)
        or isinstance(record.workflow_generation, bool)
        or record.workflow_generation < 0
        or not isinstance(record.role, MissionRole)
        or not isinstance(record.status, CodexExecutionStatus)
    ):
        raise ExecutionStateError("INVALID_RECORD", "record lifecycle binding is invalid")
    _validate_absolute_path(record.repository_root, "repository_root")
    _validate_absolute_path(record.cwd, "cwd")
    if record.worktree_path is not None:
        _validate_absolute_path(record.worktree_path, "worktree_path")
    expected_cwd = record.worktree_path or record.repository_root
    if _path_key(record.cwd) != _path_key(expected_cwd):
        raise ExecutionStateError("INVALID_RECORD", "record cwd binding is inconsistent")
    _validate_executable(record.executable)
    if record.semantic_fingerprint != _record_semantic_fingerprint(record):
        raise ExecutionStateError("FORGED_EXECUTION_IDENTITY", "execution semantic fingerprint is inconsistent")
    if record.execution_id != _record_execution_id(record):
        raise ExecutionStateError("FORGED_EXECUTION_IDENTITY", "execution identifier is inconsistent")
    _validate_utc(record.created_at, "created_at")
    _validate_utc(record.updated_at, "updated_at")
    if record.updated_at < record.created_at:
        raise ExecutionStateError("INVALID_RECORD", "record timestamps are inverted")
    if not isinstance(record.failure_reasons, tuple) or any(
        not isinstance(reason, str) or not reason for reason in record.failure_reasons
    ):
        raise ExecutionStateError("INVALID_RECORD", "failure reasons are invalid")
    _validate_status_payload(record, max_output_characters=max_output_characters)


def validate_ledger(
    ledger: CodexExecutionLedger,
    *,
    max_output_characters: int,
) -> None:
    if (
        not isinstance(ledger, CodexExecutionLedger)
        or ledger.schema_version != EXECUTION_LEDGER_VERSION
        or not isinstance(ledger.records, tuple)
    ):
        raise ExecutionStateError("INVALID_LEDGER", "execution ledger version is invalid")
    for record in ledger.records:
        validate_record(record, max_output_characters=max_output_characters)
    identifiers = [record.execution_id for record in ledger.records]
    semantics = [record.semantic_fingerprint for record in ledger.records]
    requests = [record.request_id for record in ledger.records]
    if identifiers != sorted(identifiers):
        raise ExecutionStateError("INVALID_LEDGER", "records are not canonically ordered")
    if any(len(values) != len(set(values)) for values in (identifiers, semantics, requests)):
        raise ExecutionStateError(
            "DUPLICATE_EXECUTION", "execution identity or semantics are duplicated"
        )


def record_to_data(record: CodexExecutionRecord) -> dict[str, object]:
    return {
        "execution_id": record.execution_id,
        "semantic_fingerprint": record.semantic_fingerprint,
        "request_id": record.request_id,
        "context_fingerprint": record.context_fingerprint,
        "mission_id": record.mission_id,
        "workflow_generation": record.workflow_generation,
        "role": record.role.value,
        "subject": record.subject,
        "repository_root": record.repository_root,
        "worktree_path": record.worktree_path,
        "cwd": record.cwd,
        "expected_commit": record.expected_commit,
        "compiled_prompt_fingerprint": record.compiled_prompt_fingerprint,
        "expected_result_contract": record.expected_result_contract,
        "executable": {
            "path": record.executable.path,
            "version": record.executable.version,
            "sha256": record.executable.sha256,
        },
        "status": record.status.value,
        "created_at": _utc_text(record.created_at),
        "updated_at": _utc_text(record.updated_at),
        "observation": (
            observation_to_data(record.observation)
            if record.observation is not None
            else None
        ),
        "validated_result_json": record.validated_result_json,
        "validated_result_fingerprint": record.validated_result_fingerprint,
        "failure_reasons": list(record.failure_reasons),
    }


def record_from_data(data: object) -> CodexExecutionRecord:
    candidate = _exact_mapping(
        data,
        {
            "execution_id", "semantic_fingerprint", "request_id",
            "context_fingerprint", "mission_id", "workflow_generation", "role",
            "subject", "repository_root", "worktree_path", "cwd", "expected_commit",
            "compiled_prompt_fingerprint", "expected_result_contract", "executable",
            "status", "created_at", "updated_at", "observation",
            "validated_result_json", "validated_result_fingerprint", "failure_reasons",
        },
        "execution record",
    )
    executable = _exact_mapping(
        candidate["executable"], {"path", "version", "sha256"}, "executable"
    )
    return CodexExecutionRecord(
        execution_id=_string(candidate["execution_id"]),
        semantic_fingerprint=_string(candidate["semantic_fingerprint"]),
        request_id=_string(candidate["request_id"]),
        context_fingerprint=_string(candidate["context_fingerprint"]),
        mission_id=_string(candidate["mission_id"]),
        workflow_generation=_integer(candidate["workflow_generation"]),
        role=MissionRole(_string(candidate["role"])),
        subject=_string(candidate["subject"]),
        repository_root=_string(candidate["repository_root"]),
        worktree_path=_optional_string(candidate["worktree_path"]),
        cwd=_string(candidate["cwd"]),
        expected_commit=_string(candidate["expected_commit"]),
        compiled_prompt_fingerprint=_string(candidate["compiled_prompt_fingerprint"]),
        expected_result_contract=_string(candidate["expected_result_contract"]),
        executable=ExecutionExecutableIdentity(
            _string(executable["path"]),
            _string(executable["version"]),
            _string(executable["sha256"]),
        ),
        status=CodexExecutionStatus(_string(candidate["status"])),
        created_at=_datetime(candidate["created_at"]),
        updated_at=_datetime(candidate["updated_at"]),
        observation=(
            observation_from_data(candidate["observation"])
            if candidate["observation"] is not None
            else None
        ),
        validated_result_json=_optional_string(candidate["validated_result_json"]),
        validated_result_fingerprint=_optional_string(
            candidate["validated_result_fingerprint"]
        ),
        failure_reasons=_string_tuple(candidate["failure_reasons"]),
    )


def observation_to_data(observation: CodexExecutionObservation) -> dict[str, object]:
    return {
        "request_id": observation.request_id,
        "context_fingerprint": observation.context_fingerprint,
        "executable_path": observation.executable_path,
        "executable_version": observation.executable_version,
        "executable_sha256": observation.executable_sha256,
        "cwd": observation.cwd,
        "invocation": list(observation.invocation),
        "started_at": _optional_utc_text(observation.started_at),
        "ended_at": _utc_text(observation.ended_at),
        "process_id": observation.process_id,
        "thread_id": observation.thread_id,
        "exit_code": observation.exit_code,
        "stdout": observation.stdout,
        "stderr": observation.stderr,
        "stdout_truncated": observation.stdout_truncated,
        "stderr_truncated": observation.stderr_truncated,
        "events": [
            {
                "line_number": event.line_number,
                "event_type": event.event_type,
                "raw_line": event.raw_line,
                "payload_json": event.payload_json,
            }
            for event in observation.events
        ],
        "invalid_jsonl_lines": [
            {
                "line_number": line.line_number,
                "raw_line": line.raw_line,
                "error": line.error,
            }
            for line in observation.invalid_jsonl_lines
        ],
        "final_output": observation.final_output,
        "timed_out": observation.timed_out,
        "interrupted": observation.interrupted,
        "tool_failure_observed": observation.tool_failure_observed,
        "git_before": _git_to_data(observation.git_before),
        "git_after": _git_to_data(observation.git_after),
        "issues": list(observation.issues),
    }


def observation_from_data(data: object) -> CodexExecutionObservation:
    fields = {
        "request_id", "context_fingerprint", "executable_path", "executable_version",
        "executable_sha256", "cwd", "invocation", "started_at", "ended_at",
        "process_id", "thread_id", "exit_code", "stdout", "stderr",
        "stdout_truncated", "stderr_truncated", "events", "invalid_jsonl_lines",
        "final_output", "timed_out", "interrupted", "tool_failure_observed",
        "git_before", "git_after", "issues",
    }
    candidate = _exact_mapping(data, fields, "execution observation")
    events_data = _list(candidate["events"], "events")
    invalid_data = _list(candidate["invalid_jsonl_lines"], "invalid_jsonl_lines")
    return CodexExecutionObservation(
        request_id=_string(candidate["request_id"]),
        context_fingerprint=_string(candidate["context_fingerprint"]),
        executable_path=_optional_string(candidate["executable_path"]),
        executable_version=_optional_string(candidate["executable_version"]),
        executable_sha256=_optional_string(candidate["executable_sha256"]),
        cwd=_string(candidate["cwd"]),
        invocation=_string_tuple(candidate["invocation"]),
        started_at=_optional_datetime(candidate["started_at"]),
        ended_at=_datetime(candidate["ended_at"]),
        process_id=_optional_integer(candidate["process_id"]),
        thread_id=_optional_string(candidate["thread_id"]),
        exit_code=_optional_integer(candidate["exit_code"]),
        stdout=_string(candidate["stdout"]),
        stderr=_string(candidate["stderr"]),
        stdout_truncated=_boolean(candidate["stdout_truncated"]),
        stderr_truncated=_boolean(candidate["stderr_truncated"]),
        events=tuple(_event_from_data(item) for item in events_data),
        invalid_jsonl_lines=tuple(_invalid_line_from_data(item) for item in invalid_data),
        final_output=_optional_string(candidate["final_output"]),
        timed_out=_boolean(candidate["timed_out"]),
        interrupted=_boolean(candidate["interrupted"]),
        tool_failure_observed=_boolean(candidate["tool_failure_observed"]),
        git_before=_git_from_data(candidate["git_before"]),
        git_after=_git_from_data(candidate["git_after"]),
        issues=_string_tuple(candidate["issues"]),
    )


def result_json_fingerprint(value: str) -> str:
    candidate = _strict_json_object(value, "validated RoleResult")
    canonical = _canonical_json(candidate)
    if value != canonical:
        raise ExecutionStateError(
            "NON_CANONICAL_RESULT", "validated RoleResult JSON is not canonical"
        )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_result_json(candidate: object) -> str:
    return _canonical_json(candidate)


def _validate_status_payload(
    record: CodexExecutionRecord, *, max_output_characters: int
) -> None:
    empty = record.observation is None
    has_result = record.validated_result_json is not None
    has_result_fingerprint = record.validated_result_fingerprint is not None
    if record.status in {CodexExecutionStatus.PLANNED, CodexExecutionStatus.RUNNING}:
        valid = empty and not has_result and not has_result_fingerprint and not record.failure_reasons
    elif record.status is CodexExecutionStatus.OBSERVED:
        valid = not empty and not has_result and not has_result_fingerprint and not record.failure_reasons
    elif record.status is CodexExecutionStatus.VALIDATED:
        valid = not empty and has_result and has_result_fingerprint and not record.failure_reasons
    else:
        valid = not empty and not has_result and not has_result_fingerprint and bool(record.failure_reasons)
    if not valid:
        raise ExecutionStateError(
            "INVALID_RECORD", "record payload contradicts execution lifecycle"
        )
    if record.observation is not None:
        _validate_observation(record, max_output_characters=max_output_characters)
    if has_result:
        assert record.validated_result_json is not None
        expected = result_json_fingerprint(record.validated_result_json)
        if expected != record.validated_result_fingerprint:
            raise ExecutionStateError(
                "INVALID_RECORD", "validated RoleResult fingerprint is inconsistent"
            )


def _validate_observation(
    record: CodexExecutionRecord, *, max_output_characters: int
) -> None:
    observation = cast(CodexExecutionObservation, record.observation)
    if (
        observation.request_id != record.request_id
        or observation.context_fingerprint != record.context_fingerprint
        or _path_key(observation.cwd) != _path_key(record.cwd)
    ):
        raise ExecutionStateError(
            "OBSERVATION_BINDING_MISMATCH", "observation differs from execution record"
        )
    if observation.executable_path is not None and _path_key(
        observation.executable_path
    ) != _path_key(record.executable.path):
        raise ExecutionStateError(
            "OBSERVATION_BINDING_MISMATCH", "observation executable path differs"
        )
    if (
        observation.executable_version is not None
        and observation.executable_version != record.executable.version
    ) or (
        observation.executable_sha256 is not None
        and observation.executable_sha256 != record.executable.sha256
    ):
        raise ExecutionStateError(
            "OBSERVATION_BINDING_MISMATCH", "observation executable identity differs"
        )
    if len(observation.stdout) > max_output_characters or len(
        observation.stderr
    ) > max_output_characters:
        raise ExecutionStateError(
            "OBSERVATION_TOO_LARGE", "persisted execution streams exceed policy"
        )
    if len(_canonical_json(observation_to_data(observation))) > max_output_characters * 4:
        raise ExecutionStateError(
            "OBSERVATION_TOO_LARGE", "persisted execution observation exceeds bounded policy"
        )
    _validate_utc(observation.ended_at, "observation.ended_at")
    if observation.started_at is not None:
        _validate_utc(observation.started_at, "observation.started_at")


def _validate_executable(executable: ExecutionExecutableIdentity) -> None:
    if not isinstance(executable, ExecutionExecutableIdentity):
        raise ExecutionStateError("INVALID_RECORD", "executable identity is invalid")
    _validate_absolute_path(executable.path, "executable.path")
    if not isinstance(executable.version, str) or not executable.version:
        raise ExecutionStateError("INVALID_RECORD", "executable version is invalid")
    if not _SHA256.fullmatch(executable.sha256):
        raise ExecutionStateError("INVALID_RECORD", "executable digest is invalid")


def _event_from_data(data: object) -> CodexJsonlEvent:
    item = _exact_mapping(
        data, {"line_number", "event_type", "raw_line", "payload_json"}, "event"
    )
    return CodexJsonlEvent(
        _integer(item["line_number"]),
        _optional_string(item["event_type"]),
        _string(item["raw_line"]),
        _string(item["payload_json"]),
    )


def _invalid_line_from_data(data: object) -> InvalidJsonlLine:
    item = _exact_mapping(data, {"line_number", "raw_line", "error"}, "invalid line")
    return InvalidJsonlLine(
        _integer(item["line_number"]),
        _string(item["raw_line"]),
        _string(item["error"]),
    )


def _git_to_data(value: GitExecutionObservation | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {"head_commit": value.head_commit, "clean": value.clean, "error": value.error}


def _git_from_data(value: object) -> GitExecutionObservation | None:
    if value is None:
        return None
    item = _exact_mapping(value, {"head_commit", "clean", "error"}, "Git observation")
    clean = item["clean"]
    if clean is not None:
        clean = _boolean(clean)
    return GitExecutionObservation(
        _optional_string(item["head_commit"]),
        cast(bool | None, clean),
        _optional_string(item["error"]),
    )


def _ledger_write_boundary():
    @dataclass(frozen=True, slots=True)
    class Authorization:
        store: object
        operation: str
        before: str
        after: str

    def issue(
        *, store: object, before: CodexExecutionLedger, after: CodexExecutionLedger, operation: str
    ) -> object:
        return Authorization(store, operation, _ledger_fingerprint(before), _ledger_fingerprint(after))

    def matches(
        authorization: object,
        *,
        store: object,
        before: CodexExecutionLedger,
        after: CodexExecutionLedger,
        operation: str,
    ) -> bool:
        return (
            isinstance(authorization, Authorization)
            and authorization.store is store
            and authorization.operation == operation
            and authorization.before == _ledger_fingerprint(before)
            and authorization.after == _ledger_fingerprint(after)
        )

    return issue, matches


def _ledger_fingerprint(ledger: CodexExecutionLedger) -> str:
    return _fingerprint_json(
        {
            "schema_version": ledger.schema_version,
            "records": [record_to_data(record) for record in ledger.records],
        }
    )


def _fingerprint_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ExecutionStateError("INVALID_JSON_DATA", "value is not canonical JSON") from error


def _strict_json_object(value: str, label: str) -> dict[str, object]:
    try:
        candidate = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise ExecutionStateError("INVALID_JSON_DATA", f"{label} is invalid JSON") from error
    if not isinstance(candidate, dict):
        raise ExecutionStateError("INVALID_JSON_DATA", f"{label} must be an object")
    return candidate


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    candidate: dict[str, object] = {}
    for key, value in pairs:
        if key in candidate:
            raise ValueError(f"duplicate key: {key}")
        candidate[key] = value
    return candidate


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-JSON constant: {value}")


def _exact_mapping(value: object, fields: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ExecutionStateError(
            "INVALID_JSON_DATA", f"{label} has unknown or missing fields"
        )
    return cast(dict[str, object], value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ExecutionStateError("INVALID_JSON_DATA", "expected string")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return _string(value)


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ExecutionStateError("INVALID_JSON_DATA", "expected strict integer")
    return value


def _optional_integer(value: object) -> int | None:
    if value is None:
        return None
    return _integer(value)


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise ExecutionStateError("INVALID_JSON_DATA", "expected strict boolean")
    return value


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ExecutionStateError("INVALID_JSON_DATA", f"{label} must be an array")
    return value


def _string_tuple(value: object) -> tuple[str, ...]:
    items = _list(value, "string tuple")
    if not all(isinstance(item, str) for item in items):
        raise ExecutionStateError("INVALID_JSON_DATA", "expected string array")
    return tuple(cast(list[str], items))


def _datetime(value: object) -> datetime:
    text = _string(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ExecutionStateError("INVALID_JSON_DATA", "invalid timestamp") from error
    _validate_utc(parsed, "timestamp")
    return parsed


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else _datetime(value)


def _utc_text(value: datetime) -> str:
    _validate_utc(value, "timestamp")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_utc_text(value: datetime | None) -> str | None:
    return None if value is None else _utc_text(value)


def _validate_utc(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ExecutionStateError("INVALID_RECORD", f"{label} must be UTC")


def _validate_absolute_path(value: str, label: str) -> None:
    if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts:
        raise ExecutionStateError("INVALID_RECORD", f"{label} must be an absolute path")


def _path_key(value: str) -> str:
    return os.path.normcase(str(Path(value).resolve(strict=False))).casefold()


(_issue_execution_write, _matches_execution_write) = _ledger_write_boundary()
