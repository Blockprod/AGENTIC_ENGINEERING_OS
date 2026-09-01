"""Strict, atomic and fail-closed maintenance-state persistence."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from agentic_engineering_os._maintenance_write import _matches_maintenance_write
from agentic_engineering_os.domain.maintenance import (
    MaintenanceRecord,
    maintenance_record_from_dict,
    maintenance_record_to_dict,
)

from .project_state_store import PersistenceError, STATE_DIRECTORY


MAINTENANCE_FILENAME = "maintenance.json"
MAINTENANCE_LOCK_FILENAME = ".maintenance.lock"
MAX_MAINTENANCE_BYTES = 32_768


class MaintenanceStateStore:
    """Expose reads; accept writes only carrying exact service authority."""

    def __init__(self, repository_root: Path | str) -> None:
        try:
            self._root = Path(repository_root).resolve(strict=True)
        except OSError as error:
            raise PersistenceError("INVALID_REPOSITORY_ROOT", "repository root cannot be resolved") from error
        if not self._root.is_dir():
            raise PersistenceError("INVALID_REPOSITORY_ROOT", "repository root is not a directory")
        self._directory = self._root / STATE_DIRECTORY
        self._path = self._directory / MAINTENANCE_FILENAME
        self._lock_path = self._directory / MAINTENANCE_LOCK_FILENAME

    @property
    def maintenance_path(self) -> Path:
        return self._path

    @property
    def repository_root(self) -> Path:
        return self._root

    def load(self) -> MaintenanceRecord:
        self._assert_safe_paths(for_write=False)
        if not self._path.exists():
            raise PersistenceError("MAINTENANCE_STATE_ABSENT", "maintenance state must be initialized explicitly")
        if not self._path.is_file():
            raise PersistenceError("READ_FAILED", "maintenance state path is not a file")
        try:
            if self._path.stat().st_size > MAX_MAINTENANCE_BYTES:
                raise PersistenceError("STATE_TOO_LARGE", "maintenance state exceeds policy")
            data = json.loads(
                self._path.read_text(encoding="utf-8"),
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
            return maintenance_record_from_dict(data)
        except PersistenceError:
            raise
        except (OSError, UnicodeError) as error:
            raise PersistenceError("READ_FAILED", "maintenance state cannot be read") from error
        except (json.JSONDecodeError, ValueError, TypeError) as error:
            raise PersistenceError("INVALID_MAINTENANCE_STATE", f"maintenance state is invalid: {error}") from error

    def _initialize_authorized(self, record: MaintenanceRecord, *, authorization: object) -> Path:
        self._assert_safe_paths(for_write=True)
        descriptor = self._acquire_write_lock()
        try:
            if self._path.exists():
                raise PersistenceError("MAINTENANCE_STATE_EXISTS", "maintenance state is already initialized")
            if not _matches_maintenance_write(authorization, store=self, before=None, after=record, operation="INITIALIZE"):
                raise PersistenceError("WRITE_NOT_AUTHORIZED", "maintenance initialization lacks exact service authority")
            return self._write(record)
        finally:
            self._release_write_lock(descriptor)

    def _replace_authorized(self, record: MaintenanceRecord, *, authorization: object) -> Path:
        descriptor = self._acquire_write_lock()
        try:
            before = self.load()
            if not _matches_maintenance_write(authorization, store=self, before=before, after=record, operation="TRANSITION"):
                raise PersistenceError("WRITE_NOT_AUTHORIZED", "maintenance transition lacks exact service authority")
            if record.revision != before.revision + 1 or record.previous_fingerprint != before.fingerprint:
                raise PersistenceError("INVALID_REVISION", "maintenance transition does not extend current state")
            return self._write(record)
        finally:
            self._release_write_lock(descriptor)

    def _acquire_write_lock(self) -> int:
        try:
            self._directory.mkdir(parents=False, exist_ok=True)
            self._assert_safe_paths(for_write=True)
            return os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise PersistenceError("CONCURRENT_WRITE", "maintenance state write lock is already held") from error
        except PersistenceError:
            raise
        except OSError as error:
            raise PersistenceError("WRITE_LOCK_FAILED", "maintenance state write lock cannot be acquired") from error

    def _release_write_lock(self, descriptor: int) -> None:
        try:
            os.close(descriptor)
            self._lock_path.unlink()
        except OSError as error:
            raise PersistenceError("WRITE_LOCK_FAILED", "maintenance state write lock cannot be released safely") from error

    def _write(self, record: MaintenanceRecord) -> Path:
        text = json.dumps(
            maintenance_record_to_dict(record), ensure_ascii=False, sort_keys=True,
            indent=2, separators=(",", ": "), allow_nan=False,
        ) + "\n"
        if len(text.encode("utf-8")) > MAX_MAINTENANCE_BYTES:
            raise PersistenceError("STATE_TOO_LARGE", "maintenance state exceeds policy")
        try:
            self._directory.mkdir(parents=False, exist_ok=True)
            self._assert_safe_paths(for_write=True)
            descriptor, name = tempfile.mkstemp(dir=self._directory, prefix=".maintenance.", suffix=".tmp", text=True)
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
        except PersistenceError:
            raise
        except Exception as error:
            raise PersistenceError("WRITE_FAILED", f"atomic maintenance write failed: {type(error).__name__}: {error}") from error
        return self._path

    def _assert_safe_paths(self, *, for_write: bool) -> None:
        if self._directory.exists() and self._directory.is_symlink():
            raise PersistenceError("UNSAFE_PATH", "state directory cannot be a symlink")
        if self._path.exists() and self._path.is_symlink():
            raise PersistenceError("UNSAFE_PATH", "maintenance state cannot be a symlink")
        if self._lock_path.exists() and self._lock_path.is_symlink():
            raise PersistenceError("UNSAFE_PATH", "maintenance write lock cannot be a symlink")
        if not for_write and not self._directory.exists():
            return
        try:
            parent = self._directory.parent.resolve(strict=True)
        except OSError as error:
            raise PersistenceError("UNSAFE_PATH", "maintenance state parent cannot be resolved") from error
        if parent != self._root:
            raise PersistenceError("UNSAFE_PATH", "maintenance state escapes repository")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-JSON constant: {value}")
