"""Strict atomic storage for restart-safe Codex execution facts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from agentic_engineering_os.application.execution_state import (
    EXECUTION_LEDGER_VERSION,
    CodexExecutionLedger,
    CodexExecutionStatus,
    ExecutionStateError,
    _matches_execution_write,
    record_from_data,
    record_to_data,
    validate_ledger,
)

from .project_state_store import PersistenceError, STATE_DIRECTORY


EXECUTION_LEDGER_FILENAME = "executions.json"


class ExecutionStateStore:
    """Persist exact service-authorized transitions; no arbitrary public save."""

    def __init__(self, repository_root: Path | str, *, max_output_characters: int = 1_000_000) -> None:
        try:
            self._root = Path(repository_root).resolve(strict=True)
        except OSError as error:
            raise PersistenceError("INVALID_REPOSITORY_ROOT", "repository root cannot be resolved") from error
        if not self._root.is_dir():
            raise PersistenceError("INVALID_REPOSITORY_ROOT", "repository root is not a directory")
        if not isinstance(max_output_characters, int) or isinstance(max_output_characters, bool) or max_output_characters <= 0:
            raise ValueError("max_output_characters must be a positive integer")
        self._state_directory = self._root / STATE_DIRECTORY
        self._path = self._state_directory / EXECUTION_LEDGER_FILENAME
        self._max_output_characters = max_output_characters

    @property
    def ledger_path(self) -> Path:
        return self._path

    def initialize(self) -> CodexExecutionLedger:
        self._assert_safe_paths(for_write=True)
        if self._path.exists():
            raise PersistenceError("LEDGER_ALREADY_EXISTS", "execution ledger already exists")
        ledger = CodexExecutionLedger(EXECUTION_LEDGER_VERSION, ())
        self._validate(ledger)
        self._write(ledger)
        return ledger

    def load(self) -> CodexExecutionLedger:
        self._assert_safe_paths(for_write=False)
        if not self._path.exists():
            raise PersistenceError("LEDGER_ABSENT", "execution ledger is absent")
        if not self._path.is_file():
            raise PersistenceError("READ_FAILED", "execution ledger path is not a file")
        try:
            if self._path.stat().st_size > 16_000_000:
                raise PersistenceError("LEDGER_TOO_LARGE", "execution ledger exceeds policy")
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw, object_pairs_hook=_strict_object, parse_constant=_reject_constant)
        except PersistenceError:
            raise
        except (OSError, UnicodeError) as error:
            raise PersistenceError("READ_FAILED", "execution ledger cannot be read") from error
        except (json.JSONDecodeError, ValueError) as error:
            raise PersistenceError("INVALID_JSON", f"execution ledger is not strict JSON: {error}") from error
        if not isinstance(data, dict) or set(data) != {"schema_version", "records"}:
            raise PersistenceError("INVALID_SCHEMA", "execution ledger has unknown or missing fields")
        if not isinstance(data["records"], list):
            raise PersistenceError("INVALID_SCHEMA", "execution records must be an array")
        try:
            ledger = CodexExecutionLedger(
                schema_version=data["schema_version"],
                records=tuple(record_from_data(item) for item in data["records"]),
            )
            self._validate(ledger)
        except ExecutionStateError as error:
            raise PersistenceError(error.code, error.message) from error
        return ledger

    def _replace_authorized(self, candidate: CodexExecutionLedger, *, authorization: object, operation: str) -> Path:
        self._assert_safe_paths(for_write=True)
        if not self._path.exists():
            raise PersistenceError("INITIALIZATION_REQUIRED", "execution ledger must be initialized explicitly")
        before = self.load()
        self._validate(candidate)
        if not _matches_execution_write(authorization, store=self, before=before, after=candidate, operation=operation):
            raise PersistenceError("WRITE_NOT_AUTHORIZED", "execution mutation lacks exact service authority")
        _validate_transition(before, candidate, operation)
        return self._write(candidate)

    def _validate(self, ledger: CodexExecutionLedger) -> None:
        try:
            validate_ledger(ledger, max_output_characters=self._max_output_characters)
        except ExecutionStateError as error:
            raise PersistenceError(error.code, error.message) from error

    def _write(self, ledger: CodexExecutionLedger) -> Path:
        payload = {"schema_version": ledger.schema_version, "records": [record_to_data(item) for item in ledger.records]}
        try:
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, separators=(",", ": "), allow_nan=False) + "\n"
            self._state_directory.mkdir(parents=False, exist_ok=True)
            self._assert_safe_paths(for_write=True)
            descriptor, name = tempfile.mkstemp(dir=self._state_directory, prefix=".executions.", suffix=".tmp", text=True)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self._path)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        except Exception as error:
            raise PersistenceError("WRITE_FAILED", f"atomic execution ledger write failed: {type(error).__name__}: {error}") from error
        return self._path

    def _assert_safe_paths(self, *, for_write: bool) -> None:
        if self._state_directory.exists() and self._state_directory.is_symlink():
            raise PersistenceError("UNSAFE_PATH", "state directory cannot be a symlink")
        if self._path.exists() and self._path.is_symlink():
            raise PersistenceError("UNSAFE_PATH", "execution ledger cannot be a symlink")
        if not for_write and not self._state_directory.exists():
            return
        try:
            parent = self._state_directory.parent.resolve(strict=True)
        except OSError as error:
            raise PersistenceError("UNSAFE_PATH", "execution ledger parent cannot be resolved") from error
        if parent != self._root:
            raise PersistenceError("UNSAFE_PATH", "execution ledger escapes repository")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-JSON constant: {value}")


def _validate_transition(before: CodexExecutionLedger, after: CodexExecutionLedger, operation: str) -> None:
    old = {item.execution_id: item for item in before.records}
    new = {item.execution_id: item for item in after.records}
    added = set(new) - set(old)
    removed = set(old) - set(new)
    changed = [key for key in set(old) & set(new) if old[key] != new[key]]
    valid = False
    if operation == "PLAN":
        valid = len(added) == 1 and not removed and not changed and new[next(iter(added))].status is CodexExecutionStatus.PLANNED
    elif not added and not removed and len(changed) == 1:
        previous, candidate = old[changed[0]], new[changed[0]]
        transitions = {
            "MARK_RUNNING": (CodexExecutionStatus.PLANNED, {CodexExecutionStatus.RUNNING}),
            "RECORD_OBSERVATION": (CodexExecutionStatus.RUNNING, {CodexExecutionStatus.OBSERVED, CodexExecutionStatus.FAILED, CodexExecutionStatus.INTERRUPTED}),
            "RECORD_INTAKE": (CodexExecutionStatus.OBSERVED, {CodexExecutionStatus.VALIDATED, CodexExecutionStatus.FAILED}),
        }
        rule = transitions.get(operation)
        valid = rule is not None and previous.status is rule[0] and candidate.status in rule[1] and _identity(previous) == _identity(candidate)
    if not valid:
        raise PersistenceError("INVALID_EXECUTION_TRANSITION", f"execution mutation does not match {operation}")


def _identity(record: object) -> tuple[object, ...]:
    from agentic_engineering_os.application.execution_state import CodexExecutionRecord
    assert isinstance(record, CodexExecutionRecord)
    return tuple(getattr(record, name) for name in (
        "execution_id", "semantic_fingerprint", "request_id", "context_fingerprint", "mission_id",
        "workflow_generation", "role", "subject", "repository_root", "worktree_path", "cwd",
        "expected_commit", "compiled_prompt_fingerprint", "expected_result_contract", "executable", "created_at",
    ))
