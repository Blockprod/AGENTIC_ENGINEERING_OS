"""Validated JSON persistence for operational mission memory."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from agentic_engineering_os.application import ContractValidator
from agentic_engineering_os.domain import (
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    to_dict,
)

from .project_state_store import PersistenceError, STATE_DIRECTORY


MISSION_FILENAME = "mission.json"


class _DuplicateJsonKeyError(ValueError):
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


class MissionStateStore:
    """Atomic local mission store; it has no project-control authority."""

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
        self._mission_path = self._state_directory / MISSION_FILENAME
        self._validator = validator if validator is not None else ContractValidator()

    @property
    def mission_path(self) -> Path:
        return self._mission_path

    def initialize(self, state: MissionState) -> MissionState:
        """Explicitly create mission state; never infer or overwrite it."""

        self._assert_safe_paths(for_write=True)
        if self._mission_path.exists():
            raise PersistenceError(
                "MISSION_ALREADY_EXISTS",
                f"mission state already exists: {self._mission_path}",
            )
        self.save(state)
        return state

    def load(self) -> MissionState:
        """Load existing mission state without creating a fallback."""

        self._assert_safe_paths(for_write=False)
        if not self._mission_path.exists():
            raise PersistenceError(
                "MISSION_ABSENT", f"mission state is absent: {self._mission_path}"
            )
        if not self._mission_path.is_file():
            raise PersistenceError(
                "READ_FAILED", f"mission path is not a file: {self._mission_path}"
            )
        try:
            text = self._mission_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise PersistenceError(
                "READ_FAILED", f"mission state cannot be read: {self._mission_path}"
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
                "mission state is not valid JSON at "
                f"line {error.lineno}, column {error.colno}",
            ) from error

        self._validate_serialized(candidate)
        try:
            state = _hydrate_mission_state(_mapping(candidate, "mission state"))
        except PersistenceError:
            raise
        except Exception as error:
            raise PersistenceError(
                "INVALID_DOMAIN_DATA",
                f"mission state cannot be hydrated: {type(error).__name__}: {error}",
            ) from error
        self._validate_state(state)
        return state

    def save(self, state: MissionState) -> Path:
        """Validate and atomically replace mission.json, preserving prior state."""

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
            os.replace(temporary_path, self._mission_path)
        except Exception as error:
            _cleanup_temporary(temporary_path)
            raise PersistenceError(
                "WRITE_FAILED",
                f"atomic mission write failed: {type(error).__name__}: {error}",
            ) from error
        return self._mission_path

    def _validate_state(self, state: MissionState) -> dict[str, object]:
        if not isinstance(state, MissionState):
            raise PersistenceError(
                "INVALID_DOMAIN_DATA", "save requires an explicit MissionState"
            )
        try:
            serialized = cast(dict[str, object], to_dict(state))
        except Exception as error:
            raise PersistenceError(
                "INVALID_DOMAIN_DATA",
                f"MissionState cannot be serialized: {type(error).__name__}: {error}",
            ) from error
        self._validate_serialized(serialized)
        return serialized

    def _validate_serialized(self, candidate: object) -> None:
        try:
            validation = self._validator.validate("mission-state", candidate)
        except Exception as error:
            raise PersistenceError(
                "VALIDATION_UNAVAILABLE",
                f"MissionState validation could not be completed: {error}",
            ) from error
        if not validation.is_valid:
            details = "; ".join(
                f"{'.'.join(map(str, issue.path)) or '<root>'}: {issue.message}"
                for issue in validation.errors
            )
            raise PersistenceError(
                "INVALID_SCHEMA", f"MissionState violates its schema: {details}"
            )

    def _assert_safe_paths(self, *, for_write: bool) -> None:
        if self._state_directory.exists() and self._state_directory.is_symlink():
            raise PersistenceError(
                "UNSAFE_PATH",
                f"state directory cannot be a symlink: {self._state_directory}",
            )
        if self._mission_path.exists() and self._mission_path.is_symlink():
            raise PersistenceError(
                "UNSAFE_PATH",
                f"mission file cannot be a symlink: {self._mission_path}",
            )
        if not for_write and not self._state_directory.exists():
            return
        try:
            parent = self._state_directory.parent.resolve(strict=True)
        except OSError as error:
            raise PersistenceError(
                "UNSAFE_PATH", "mission path parent cannot be resolved"
            ) from error
        if parent != self._root:
            raise PersistenceError(
                "UNSAFE_PATH", "mission path escapes repository root"
            )


def _write_temporary(directory: Path, text: str) -> Path:
    descriptor, name = tempfile.mkstemp(
        dir=directory,
        prefix=".mission.",
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
            "INVALID_DOMAIN_DATA", "MissionState is not JSON serializable"
        ) from error


def _hydrate_mission_state(data: Mapping[str, object]) -> MissionState:
    return MissionState(
        schema_version=_string(data["schema_version"], "schema_version"),
        mission_id=_string(data["mission_id"], "mission_id"),
        status=MissionStatus(_string(data["status"], "status")),
        role=MissionRole(_string(data["role"], "role")),
        objective=_string(data["objective"], "objective"),
        subject=_string(data["subject"], "subject"),
        operating_step=OperatingStep(
            _string(data["operating_step"], "operating_step")
        ),
        next_action=_string(data["next_action"], "next_action"),
        observed_commit=_string(data["observed_commit"], "observed_commit"),
        updated_at=_datetime(data["updated_at"], "updated_at"),
        blockers=_strings(data["blockers"], "blockers"),
    )


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PersistenceError("INVALID_DOMAIN_DATA", f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise PersistenceError("INVALID_DOMAIN_DATA", f"{label} must be a string")
    return value


def _strings(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        raise PersistenceError("INVALID_DOMAIN_DATA", f"{label} must be an array")
    return [_string(item, label) for item in value]


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
