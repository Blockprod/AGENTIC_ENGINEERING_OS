"""Exclusive repository-wide mission operation lock."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from .project_state_store import PersistenceError


class RepositoryOperationLock:
    """Serialize mutating mission invocations without claiming crash recovery."""

    def __init__(self, repository_root: Path | str) -> None:
        self._root = Path(repository_root).resolve(strict=True)
        identity = hashlib.sha256(
            os.path.normcase(str(self._root)).casefold().encode("utf-8")
        ).hexdigest()
        self._directory = (
            Path(tempfile.gettempdir()).resolve(strict=True)
            / "agentic-engineering-os-mission-locks"
        )
        self._path = self._directory / f"{identity}.lock"
        self._descriptor: int | None = None

    @property
    def lock_path(self) -> Path:
        return self._path

    def __enter__(self) -> "RepositoryOperationLock":
        if self._descriptor is not None:
            raise PersistenceError("LOCK_ALREADY_HELD", "mission lock is already held")
        try:
            self._directory.mkdir(parents=False, exist_ok=True)
        except OSError as error:
            raise PersistenceError(
                "LOCK_FAILED", "external mission lock directory cannot be created"
            ) from error
        if _unsafe_path(self._directory) or _unsafe_path(self._path):
            raise PersistenceError("UNSAFE_PATH", "mission lock path cannot be a symlink")
        try:
            resolved_parent = self._directory.resolve(strict=True)
        except OSError as error:
            raise PersistenceError(
                "LOCK_FAILED", "external mission lock directory is unavailable"
            ) from error
        temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
        if resolved_parent.parent != temporary_root or self._path.parent != resolved_parent:
            raise PersistenceError("UNSAFE_PATH", "mission lock path escapes its boundary")
        try:
            descriptor = os.open(
                self._path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            )
            os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
            self._descriptor = descriptor
        except FileExistsError as error:
            raise PersistenceError(
                "CONCURRENT_MISSION_OPERATION",
                "another mutating mission invocation holds the repository lock",
            ) from error
        except OSError as error:
            raise PersistenceError("LOCK_FAILED", "mission lock cannot be acquired") from error
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is not None:
            try:
                os.close(descriptor)
            finally:
                try:
                    self._path.unlink()
                except FileNotFoundError:
                    pass


def _unsafe_path(path: Path) -> bool:
    if not path.exists():
        return path.is_symlink()
    try:
        stat = path.lstat()
    except OSError:
        return True
    reparse_flag = getattr(stat, "st_file_attributes", 0) & 0x400
    return path.is_symlink() or bool(reparse_flag)


__all__ = ["RepositoryOperationLock"]
