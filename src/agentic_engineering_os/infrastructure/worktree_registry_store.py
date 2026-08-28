"""Strict, atomic persistence for expected Git worktree assignments."""

from __future__ import annotations

import json
import os
import tempfile
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from agentic_engineering_os._worktree_registry_write import _matches_registry_write
from agentic_engineering_os.application import ContractValidator
from agentic_engineering_os.domain import (
    WorktreeAssignment,
    WorktreeRegistry,
    WorktreeStatus,
    to_dict,
)

from .project_state_store import PersistenceError, STATE_DIRECTORY
from ._worktree_identity import derive_assignment_id, derive_branch_name


WORKTREE_REGISTRY_FILENAME = "worktrees.json"
WORKTREE_REGISTRY_VERSION = "1.0"


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


class WorktreeRegistryStore:
    """Persistent registry with no public arbitrary-save operation."""

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
        self._registry_path = self._state_directory / WORKTREE_REGISTRY_FILENAME
        self._validator = validator if validator is not None else ContractValidator()

    @property
    def registry_path(self) -> Path:
        return self._registry_path

    def initialize(self) -> WorktreeRegistry:
        """Explicitly create an empty registry without overwriting one."""

        self._assert_safe_paths(for_write=True)
        if self._registry_path.exists():
            raise PersistenceError(
                "REGISTRY_ALREADY_EXISTS",
                f"worktree registry already exists: {self._registry_path}",
            )
        registry = WorktreeRegistry(
            schema_version=WORKTREE_REGISTRY_VERSION,
            assignments=(),
        )
        self._write_serialized(self._validate_registry(registry))
        return registry

    def load(self) -> WorktreeRegistry:
        """Load an existing strict registry; never synthesize a fallback."""

        self._assert_safe_paths(for_write=False)
        if not self._registry_path.exists():
            raise PersistenceError(
                "REGISTRY_ABSENT",
                f"worktree registry is absent: {self._registry_path}",
            )
        if not self._registry_path.is_file():
            raise PersistenceError(
                "READ_FAILED", f"registry path is not a file: {self._registry_path}"
            )
        try:
            text = self._registry_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise PersistenceError(
                "READ_FAILED", f"worktree registry cannot be read: {self._registry_path}"
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
                "worktree registry is not valid JSON at "
                f"line {error.lineno}, column {error.colno}",
            ) from error

        self._validate_serialized(candidate)
        try:
            registry = _hydrate_registry(_mapping(candidate, "worktree registry"))
        except PersistenceError:
            raise
        except Exception as error:
            raise PersistenceError(
                "INVALID_DOMAIN_DATA",
                f"worktree registry cannot be hydrated: {type(error).__name__}: {error}",
            ) from error
        self._validate_registry(registry)
        return registry

    def _save_authorized(
        self,
        registry: WorktreeRegistry,
        *,
        authorization: object,
        operation: str,
    ) -> Path:
        """Persist one exact manager-authorized legal registry transition."""

        self._assert_safe_paths(for_write=True)
        serialized = self._validate_registry(registry)
        if not self._registry_path.exists():
            raise PersistenceError(
                "INITIALIZATION_REQUIRED",
                "worktree registry must be created through initialize()",
            )
        current = self.load()
        if not _matches_registry_write(
            authorization,
            store=self,
            before=current,
            candidate=registry,
            operation=operation,
        ):
            raise PersistenceError(
                "WRITE_NOT_AUTHORIZED",
                "registry candidate lacks exact WorktreeManager mutation authority",
            )
        _validate_transition(current, registry, operation)
        return self._write_serialized(serialized)

    def _write_serialized(self, serialized: dict[str, object]) -> Path:
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
            os.replace(temporary_path, self._registry_path)
        except Exception as error:
            _cleanup_temporary(temporary_path)
            raise PersistenceError(
                "WRITE_FAILED",
                f"atomic registry write failed: {type(error).__name__}: {error}",
            ) from error
        return self._registry_path

    def _validate_registry(self, registry: WorktreeRegistry) -> dict[str, object]:
        if not isinstance(registry, WorktreeRegistry):
            raise PersistenceError(
                "INVALID_DOMAIN_DATA", "registry snapshot must be WorktreeRegistry"
            )
        try:
            serialized = cast(dict[str, object], to_dict(registry))
        except Exception as error:
            raise PersistenceError(
                "INVALID_DOMAIN_DATA",
                f"registry cannot be serialized: {type(error).__name__}: {error}",
            ) from error
        self._validate_serialized(serialized)
        _validate_integrity(registry)
        return serialized

    def _validate_serialized(self, candidate: object) -> None:
        try:
            validation = self._validator.validate("worktree-registry", candidate)
        except Exception as error:
            raise PersistenceError(
                "VALIDATION_UNAVAILABLE",
                f"registry validation could not be completed: {error}",
            ) from error
        if not validation.is_valid:
            details = "; ".join(
                f"{'.'.join(map(str, issue.path)) or '<root>'}: {issue.message}"
                for issue in validation.errors
            )
            raise PersistenceError(
                "INVALID_SCHEMA", f"registry violates its schema: {details}"
            )

    def _assert_safe_paths(self, *, for_write: bool) -> None:
        if self._state_directory.exists() and self._state_directory.is_symlink():
            raise PersistenceError(
                "UNSAFE_PATH",
                f"state directory cannot be a symlink: {self._state_directory}",
            )
        if self._registry_path.exists() and self._registry_path.is_symlink():
            raise PersistenceError(
                "UNSAFE_PATH",
                f"registry file cannot be a symlink: {self._registry_path}",
            )
        if not for_write and not self._state_directory.exists():
            return
        try:
            parent = self._state_directory.parent.resolve(strict=True)
        except OSError as error:
            raise PersistenceError(
                "UNSAFE_PATH", "registry path parent cannot be resolved"
            ) from error
        if parent != self._root:
            raise PersistenceError("UNSAFE_PATH", "registry path escapes repository")


def _validate_integrity(registry: WorktreeRegistry) -> None:
    assignments = registry.assignments
    identifiers = [item.assignment_id for item in assignments]
    if identifiers != sorted(identifiers):
        raise PersistenceError(
            "NON_CANONICAL_REGISTRY", "assignments must be ordered by assignment_id"
        )
    if len(set(identifiers)) != len(identifiers):
        raise PersistenceError("DUPLICATE_ASSIGNMENT", "assignment IDs must be unique")
    live = [item for item in assignments if item.status is not WorktreeStatus.CLEANED]
    _require_unique_normalized(
        [item.branch_name for item in live], "branch_name"
    )
    _require_unique_normalized(
        [item.worktree_path for item in live], "worktree_path"
    )
    for item in assignments:
        path = Path(item.worktree_path)
        if not path.is_absolute():
            raise PersistenceError(
                "INVALID_WORKTREE_PATH",
                f"worktree path must be absolute: {item.assignment_id}",
            )
        if ".." in path.parts:
            raise PersistenceError(
                "INVALID_WORKTREE_PATH",
                f"worktree path cannot contain traversal: {item.assignment_id}",
            )
        if item.worktree_path != str(path):
            raise PersistenceError(
                "INVALID_WORKTREE_PATH",
                f"worktree path separators are not canonical: {item.assignment_id}",
            )
        try:
            expected_id = derive_assignment_id(
                item.mission_id,
                item.user_story_id,
                item.workflow_generation,
                item.baseline_commit,
            )
            expected_branch = derive_branch_name(
                item.user_story_id,
                item.workflow_generation,
                expected_id,
            )
        except ValueError as error:
            raise PersistenceError(
                "INVALID_ASSIGNMENT_IDENTITY",
                f"assignment identity is invalid: {item.assignment_id}: {error}",
            ) from error
        if item.assignment_id != expected_id or item.branch_name != expected_branch:
            raise PersistenceError(
                "INVALID_ASSIGNMENT_IDENTITY",
                f"assignment identity is not deterministic: {item.assignment_id}",
            )
        if path.name != item.assignment_id:
            raise PersistenceError(
                "INVALID_WORKTREE_PATH",
                f"worktree path must end with assignment ID: {item.assignment_id}",
            )


def _require_unique_normalized(values: list[str], label: str) -> None:
    normalized = [unicodedata.normalize("NFC", value).casefold() for value in values]
    if len(set(normalized)) != len(normalized):
        raise PersistenceError(
            "DUPLICATE_ACTIVE_RESOURCE", f"non-cleaned {label} values must be unique"
        )


def _validate_transition(
    before: WorktreeRegistry,
    candidate: WorktreeRegistry,
    operation: str,
) -> None:
    before_by_id = {item.assignment_id: item for item in before.assignments}
    after_by_id = {item.assignment_id: item for item in candidate.assignments}
    added = sorted(set(after_by_id) - set(before_by_id))
    removed = sorted(set(before_by_id) - set(after_by_id))
    changed = sorted(
        identifier
        for identifier in set(before_by_id) & set(after_by_id)
        if before_by_id[identifier] != after_by_id[identifier]
    )
    if operation == "PLAN":
        valid = (
            len(added) == 1
            and not removed
            and not changed
            and after_by_id[added[0]].status is WorktreeStatus.PLANNED
        )
    else:
        transitions = {
            "ACTIVATE": (WorktreeStatus.PLANNED, WorktreeStatus.ACTIVE),
            "COMPLETE": (WorktreeStatus.ACTIVE, WorktreeStatus.COMPLETED),
            "FAIL": (WorktreeStatus.ACTIVE, WorktreeStatus.FAILED),
            "CLEANUP": (
                (WorktreeStatus.COMPLETED, WorktreeStatus.FAILED),
                WorktreeStatus.CLEANED,
            ),
        }
        transition = transitions.get(operation)
        valid = not added and not removed and len(changed) == 1 and transition is not None
        if valid:
            old = before_by_id[changed[0]]
            new = after_by_id[changed[0]]
            source, target = transition
            sources = source if isinstance(source, tuple) else (source,)
            valid = (
                old.status in sources
                and new.status is target
                and _identity_fields(old) == _identity_fields(new)
                and (
                    (operation == "COMPLETE" and new.result_commit is not None)
                    or (operation != "COMPLETE" and new.result_commit == old.result_commit)
                )
            )
    if not valid:
        raise PersistenceError(
            "INVALID_REGISTRY_TRANSITION",
            f"registry mutation does not match operation {operation}",
        )


def _identity_fields(assignment: WorktreeAssignment) -> tuple[object, ...]:
    return (
        assignment.assignment_id,
        assignment.mission_id,
        assignment.user_story_id,
        assignment.workflow_generation,
        assignment.baseline_commit,
        assignment.branch_name,
        assignment.worktree_path,
    )


def _hydrate_registry(data: Mapping[str, object]) -> WorktreeRegistry:
    assignments = data["assignments"]
    if not isinstance(assignments, list):
        raise PersistenceError("INVALID_DOMAIN_DATA", "assignments must be an array")
    return WorktreeRegistry(
        schema_version=_string(data["schema_version"], "schema_version"),
        assignments=tuple(
            _hydrate_assignment(_mapping(item, "assignment")) for item in assignments
        ),
    )


def _hydrate_assignment(data: Mapping[str, object]) -> WorktreeAssignment:
    result_commit = data["result_commit"]
    if result_commit is not None and not isinstance(result_commit, str):
        raise PersistenceError("INVALID_DOMAIN_DATA", "result_commit must be a string or null")
    return WorktreeAssignment(
        assignment_id=_string(data["assignment_id"], "assignment_id"),
        mission_id=_string(data["mission_id"], "mission_id"),
        user_story_id=_string(data["user_story_id"], "user_story_id"),
        workflow_generation=_integer(
            data["workflow_generation"], "workflow_generation"
        ),
        baseline_commit=_string(data["baseline_commit"], "baseline_commit"),
        branch_name=_string(data["branch_name"], "branch_name"),
        worktree_path=_string(data["worktree_path"], "worktree_path"),
        status=WorktreeStatus(_string(data["status"], "status")),
        result_commit=result_commit,
    )


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PersistenceError("INVALID_DOMAIN_DATA", f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise PersistenceError("INVALID_DOMAIN_DATA", f"{label} must be a string")
    return value


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PersistenceError("INVALID_DOMAIN_DATA", f"{label} must be an integer")
    return value


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
            "INVALID_DOMAIN_DATA", "registry is not JSON serializable"
        ) from error


def _write_temporary(directory: Path, text: str) -> Path:
    descriptor, name = tempfile.mkstemp(
        dir=directory,
        prefix=".worktrees.",
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
