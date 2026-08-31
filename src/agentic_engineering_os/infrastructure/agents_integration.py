"""Bounded, version-aware integration of the Agentic OS AGENTS.md section."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from agentic_engineering_os.domain import (
    AGENTS_MANAGED_SECTION,
    AGENTS_MANAGED_SECTION_VERSION,
    AgentsIntegrationInspection,
    ManagedSectionStatus,
)


_TARGET_NAME = "AGENTS.md"
_MAX_AGENTS_BYTES = 256_000
_MARKER_PATTERN = re.compile(
    r"<!-- (BEGIN|END) AGENTIC_ENGINEERING_OS MANAGED SECTION v([0-9]+) -->"
)


class AgentsIntegrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class AgentsIntegrationService:
    """Inspect and apply only the fixed, canonical AGENTS.md managed section."""

    @property
    def managed_version(self) -> str:
        return AGENTS_MANAGED_SECTION_VERSION

    @property
    def canonical_content(self) -> str:
        return AGENTS_MANAGED_SECTION

    def inspect(self, content: bytes | None) -> AgentsIntegrationInspection:
        if content is None:
            return AgentsIntegrationInspection(
                ManagedSectionStatus.FILE_ABSENT, None, None
            )
        fingerprint = _sha256(content)
        try:
            text = content.decode("utf-8")
        except UnicodeError:
            return AgentsIntegrationInspection(
                ManagedSectionStatus.UNKNOWN, None, fingerprint
            )
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = normalized.splitlines()
        candidates = [
            (index, line, _MARKER_PATTERN.fullmatch(line))
            for index, line in enumerate(lines)
            if _is_marker_candidate(line)
        ]
        if not candidates:
            return AgentsIntegrationInspection(
                ManagedSectionStatus.SECTION_ABSENT, None, fingerprint
            )
        if len(candidates) != 2 or any(match is None for _, _, match in candidates):
            return AgentsIntegrationInspection(
                ManagedSectionStatus.AMBIGUOUS, None, fingerprint
            )
        first_index, _, first = candidates[0]
        second_index, _, second = candidates[1]
        assert first is not None and second is not None
        if (
            first.group(1) != "BEGIN"
            or second.group(1) != "END"
            or first.group(2) != second.group(2)
            or first_index >= second_index
        ):
            return AgentsIntegrationInspection(
                ManagedSectionStatus.AMBIGUOUS, None, fingerprint
            )
        version = first.group(2)
        if version != AGENTS_MANAGED_SECTION_VERSION:
            return AgentsIntegrationInspection(
                ManagedSectionStatus.UPGRADE_REQUIRED, version, fingerprint
            )
        observed = "\n".join(lines[first_index : second_index + 1])
        status = (
            ManagedSectionStatus.CURRENT
            if observed == AGENTS_MANAGED_SECTION.rstrip("\n")
            else ManagedSectionStatus.TAMPERED
        )
        return AgentsIntegrationInspection(status, version, fingerprint)

    def create_from_plan(self, root: Path, *, planned_content: str) -> None:
        root = _safe_root(root)
        _require_canonical(planned_content)
        target = _safe_target(root)
        if target.exists() or target.is_symlink():
            raise AgentsIntegrationError(
                "EXPECTED_ABSENT_TARGET_EXISTS", "AGENTS.md is no longer absent"
            )
        temporary = _write_temporary(target.parent, _canonical_bytes(b"\n"))
        try:
            try:
                os.link(temporary, target)
            except FileExistsError as error:
                raise AgentsIntegrationError(
                    "EXPECTED_ABSENT_TARGET_EXISTS",
                    "AGENTS.md appeared during exclusive creation",
                ) from error
            if target.is_symlink() or target.read_bytes() != _canonical_bytes(b"\n"):
                raise AgentsIntegrationError(
                    "WRITE_VERIFICATION_FAILED", "created AGENTS.md bytes differ"
                )
            _fsync_directory(target.parent)
        finally:
            _cleanup(temporary)

    def integrate_from_plan(
        self,
        root: Path,
        *,
        expected_fingerprint: str,
        planned_content: str,
    ) -> None:
        root = _safe_root(root)
        _require_canonical(planned_content)
        if not _is_sha256(expected_fingerprint):
            raise AgentsIntegrationError(
                "EXPECTED_STATE_MISMATCH", "expected AGENTS.md fingerprint is invalid"
            )
        target = _safe_target(root)
        original = _read_target(root, target)
        inspection = self.inspect(original)
        if (
            inspection.status is not ManagedSectionStatus.SECTION_ABSENT
            or inspection.content_fingerprint != expected_fingerprint
        ):
            raise AgentsIntegrationError(
                "EXPECTED_STATE_MISMATCH", "AGENTS.md changed after planning"
            )
        newline = _detect_newline(original)
        separator = b"" if not original else (
            newline if original.endswith((b"\n", b"\r")) else newline + newline
        )
        replacement = original + separator + _canonical_bytes(newline)
        temporary = _write_temporary(target.parent, replacement)
        try:
            current = _read_target(root, target)
            if _sha256(current) != expected_fingerprint:
                raise AgentsIntegrationError(
                    "EXPECTED_STATE_MISMATCH", "AGENTS.md changed before replacement"
                )
            os.replace(temporary, target)
            written = target.read_bytes()
            if target.is_symlink() or written != replacement:
                raise AgentsIntegrationError(
                    "WRITE_VERIFICATION_FAILED", "integrated AGENTS.md bytes differ"
                )
            if self.inspect(written).status is not ManagedSectionStatus.CURRENT:
                raise AgentsIntegrationError(
                    "WRITE_VERIFICATION_FAILED", "managed section is not canonical"
                )
            _fsync_directory(target.parent)
        finally:
            _cleanup(temporary)


def _is_marker_candidate(line: str) -> bool:
    folded = line.casefold()
    return (
        "agentic_engineering_os" in folded
        and "managed" in folded
        and ("begin" in folded or "end" in folded)
    )


def _safe_root(root: Path) -> Path:
    candidate = Path(root)
    if not candidate.is_absolute() or candidate.is_symlink():
        raise AgentsIntegrationError("INVALID_REPOSITORY_ROOT", "root is not canonical")
    cursor = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise AgentsIntegrationError("SYMLINK_ESCAPE", "root contains a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise AgentsIntegrationError("INVALID_REPOSITORY_ROOT", "root is absent") from error
    if not resolved.is_dir() or _path_key(resolved) != _path_key(candidate):
        raise AgentsIntegrationError("INVALID_REPOSITORY_ROOT", "root is not canonical")
    return resolved


def _safe_target(root: Path) -> Path:
    target = root / _TARGET_NAME
    if target.is_symlink():
        raise AgentsIntegrationError("SYMLINK_ESCAPE", "AGENTS.md is a symlink")
    return target


def _read_target(root: Path, target: Path) -> bytes:
    if target.is_symlink():
        raise AgentsIntegrationError("SYMLINK_ESCAPE", "AGENTS.md is a symlink")
    try:
        resolved = target.resolve(strict=True)
    except OSError as error:
        raise AgentsIntegrationError("EXPECTED_STATE_MISMATCH", "AGENTS.md is absent") from error
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise AgentsIntegrationError("SYMLINK_ESCAPE", "AGENTS.md escapes root") from error
    if not resolved.is_file() or resolved.stat().st_size > _MAX_AGENTS_BYTES:
        raise AgentsIntegrationError("EXPECTED_STATE_MISMATCH", "AGENTS.md is not bounded text")
    try:
        return resolved.read_bytes()
    except OSError as error:
        raise AgentsIntegrationError("READ_FAILED", "AGENTS.md cannot be read") from error


def _require_canonical(planned_content: str) -> None:
    if planned_content != AGENTS_MANAGED_SECTION:
        raise AgentsIntegrationError(
            "INVALID_PLANNED_CONTENT", "planned AGENTS.md content is not canonical"
        )


def _canonical_bytes(newline: bytes) -> bytes:
    return AGENTS_MANAGED_SECTION.encode("utf-8").replace(b"\n", newline)


def _detect_newline(content: bytes) -> bytes:
    positions = tuple(
        (position, newline)
        for newline in (b"\r\n", b"\n", b"\r")
        if (position := content.find(newline)) >= 0
    )
    if not positions:
        return b"\n"
    _, selected = min(positions, key=lambda item: (item[0], -len(item[1])))
    return selected


def _write_temporary(directory: Path, content: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(
        dir=directory, prefix=".agentic-agents.", suffix=".tmp"
    )
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        _cleanup(path)
        raise
    return path


def _cleanup(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False))).casefold()


def _is_sha256(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
