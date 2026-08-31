"""Bounded, append-oriented persistence for non-authoritative observations."""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentic_engineering_os.application.contract_validator import ContractValidator
from agentic_engineering_os.domain import (
    MAX_OPERATIONAL_EVENT_BYTES,
    OperationalEvent,
    OperationalEventType,
    OperationalSeverity,
    operational_event_fingerprint,
    operational_event_from_dict,
    operational_event_to_dict,
)

from .project_state_store import STATE_DIRECTORY


OPERATIONAL_EVENT_STORE_VERSION = "1.0"
OPERATIONAL_EVENT_DIRECTORY = "operational-events"
MAX_STORE_RECORD_BYTES = MAX_OPERATIONAL_EVENT_BYTES + 512
DEFAULT_MAX_SEGMENT_BYTES = 1_048_576
DEFAULT_MAX_SEGMENTS = 4

_SEGMENT_PATTERN = re.compile(r"^segment-([0-9]{6})\.jsonl$")
_RECORD_FIELDS = frozenset({"record_version", "fingerprint", "event"})


class OperationalEventStoreError(RuntimeError):
    """A store operation failed or the observation journal is not trustworthy."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        segment: str | None = None,
        line: int | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.segment = segment
        self.line = line
        location = ""
        if segment is not None:
            location = f" [{segment}{f':{line}' if line is not None else ''}]"
        super().__init__(f"{code}{location}: {message}")


@dataclass(frozen=True, slots=True)
class OperationalEventAppendReceipt:
    event_id: str
    fingerprint: str
    segment: str
    record_index: int


@dataclass(frozen=True, slots=True)
class OperationalEventQuery:
    event_type: OperationalEventType | None = None
    severity: OperationalSeverity | None = None
    project_id: str | None = None
    mission_id: str | None = None
    execution_id: str | None = None

    def __post_init__(self) -> None:
        if self.event_type is not None and not isinstance(
            self.event_type, OperationalEventType
        ):
            raise ValueError("event_type must be an OperationalEventType")
        if self.severity is not None and not isinstance(
            self.severity, OperationalSeverity
        ):
            raise ValueError("severity must be an OperationalSeverity")
        for name in ("project_id", "mission_id", "execution_id"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not value or value != value.strip()
            ):
                raise ValueError(f"{name} must be non-empty canonical text")


class StructuredEventLogger:
    """Typed logging boundary; it accepts no free-form level/message API."""

    def __init__(self, store: OperationalEventStore) -> None:
        if not isinstance(store, OperationalEventStore):
            raise TypeError("store must be an OperationalEventStore")
        self._store = store

    def record(self, event: OperationalEvent) -> OperationalEventAppendReceipt:
        return self._store.append(event)


class OperationalEventStore:
    """Strict JSONL journal with cooperative single-writer exclusion."""

    def __init__(
        self,
        repository_root: Path | str,
        *,
        max_segment_bytes: int = DEFAULT_MAX_SEGMENT_BYTES,
        max_segments: int = DEFAULT_MAX_SEGMENTS,
        validator: ContractValidator | None = None,
    ) -> None:
        self._root = _resolve_repository_root(repository_root)
        _positive_integer(max_segment_bytes, "max_segment_bytes", maximum=64_000_000)
        _positive_integer(max_segments, "max_segments", maximum=64)
        if max_segment_bytes < 1_024:
            raise ValueError("max_segment_bytes must be at least 1024")
        self._state_directory = self._root / STATE_DIRECTORY
        self._event_directory = self._state_directory / OPERATIONAL_EVENT_DIRECTORY
        self._lock_path = self._event_directory / ".writer.lock"
        self._max_segment_bytes = max_segment_bytes
        self._max_segments = max_segments
        self._validator = validator if validator is not None else ContractValidator()
        self._thread_lock = threading.RLock()

    @property
    def event_directory(self) -> Path:
        return self._event_directory

    def append(self, event: OperationalEvent) -> OperationalEventAppendReceipt:
        """Durably append one validated observation or raise without claiming success."""

        validated = self._validate_event(event)
        record = _record_bytes(validated)
        if len(record) > MAX_STORE_RECORD_BYTES:
            raise OperationalEventStoreError(
                "RECORD_TOO_LARGE", "serialized store record exceeds policy"
            )
        if len(record) > self._max_segment_bytes:
            raise OperationalEventStoreError(
                "RECORD_TOO_LARGE", "record cannot fit in one configured segment"
            )

        with self._thread_lock:
            self._prepare_directory()
            with self._exclusive_writer():
                entries = self._read_entries(ignore_writer_lock=True)
                if any(item.event_id == validated.event_id for item in entries):
                    raise OperationalEventStoreError(
                        "DUPLICATE_EVENT_ID",
                        f"event_id already exists: {validated.event_id}",
                    )
                segments = self._segments()
                target = self._append_target(segments, len(record))
                self._durable_append(target, record)
                return OperationalEventAppendReceipt(
                    event_id=validated.event_id,
                    fingerprint=operational_event_fingerprint(validated),
                    segment=target.name,
                    record_index=len(entries) + 1,
                )

    def read(self) -> tuple[OperationalEvent, ...]:
        """Return observations in segment/append order; never infer business state."""

        with self._thread_lock:
            return self._read_entries(ignore_writer_lock=False)

    def query(self, query: OperationalEventQuery) -> tuple[OperationalEvent, ...]:
        """Apply exact, non-inferential filters while preserving append order."""

        if not isinstance(query, OperationalEventQuery):
            raise TypeError("query must be an OperationalEventQuery")
        return tuple(event for event in self.read() if _matches_query(event, query))

    def _validate_event(self, event: OperationalEvent) -> OperationalEvent:
        if not isinstance(event, OperationalEvent):
            raise OperationalEventStoreError(
                "INVALID_EVENT", "append requires an OperationalEvent"
            )
        try:
            candidate = operational_event_to_dict(event)
            validation = self._validator.validate("operational-event", candidate)
            if not validation.is_valid:
                details = "; ".join(
                    f"{'.'.join(map(str, issue.path)) or '<root>'}: {issue.message}"
                    for issue in validation.errors
                )
                raise OperationalEventStoreError(
                    "INVALID_EVENT", f"event violates its contract: {details}"
                )
            return operational_event_from_dict(candidate)
        except OperationalEventStoreError:
            raise
        except Exception as error:
            raise OperationalEventStoreError(
                "INVALID_EVENT",
                f"event cannot be revalidated: {type(error).__name__}: {error}",
            ) from error

    def _prepare_directory(self) -> None:
        self._assert_safe_paths(allow_absent=True)
        try:
            self._state_directory.mkdir(parents=False, exist_ok=True)
            self._event_directory.mkdir(parents=False, exist_ok=True)
        except OSError as error:
            raise OperationalEventStoreError(
                "WRITE_FAILED", "event store directory cannot be created"
            ) from error
        self._assert_safe_paths(allow_absent=False)

    @contextmanager
    def _exclusive_writer(self) -> Iterator[None]:
        self._assert_safe_paths(allow_absent=False)
        descriptor: int | None = None
        created = False
        try:
            descriptor = os.open(
                self._lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            created = True
            os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
            os.fsync(descriptor)
        except FileExistsError as error:
            raise OperationalEventStoreError(
                "CONCURRENT_WRITER",
                "another or stale cooperative writer lock is present",
            ) from error
        except OSError as error:
            if created:
                try:
                    self._lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise OperationalEventStoreError(
                "LOCK_FAILED", "writer lock cannot be acquired"
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
        try:
            yield
        finally:
            try:
                self._lock_path.unlink()
            except OSError as error:
                raise OperationalEventStoreError(
                    "LOCK_RELEASE_FAILED",
                    "writer lock could not be removed; store requires inspection",
                ) from error

    def _read_entries(self, *, ignore_writer_lock: bool) -> tuple[OperationalEvent, ...]:
        self._assert_safe_paths(allow_absent=True)
        if not self._event_directory.exists():
            return ()
        if self._lock_path.exists() and not ignore_writer_lock:
            raise OperationalEventStoreError(
                "CONCURRENT_WRITER",
                "journal cannot be read while a cooperative writer lock is present",
            )
        segments = self._segments()
        events: list[OperationalEvent] = []
        event_ids: set[str] = set()
        for segment in segments:
            for line_number, candidate in self._read_segment(segment):
                event = self._event_from_record(candidate, segment.name, line_number)
                if event.event_id in event_ids:
                    raise OperationalEventStoreError(
                        "DUPLICATE_EVENT_ID",
                        f"duplicate persisted event_id: {event.event_id}",
                        segment=segment.name,
                        line=line_number,
                    )
                event_ids.add(event.event_id)
                events.append(event)
        return tuple(events)

    def _read_segment(self, segment: Path) -> Iterator[tuple[int, object]]:
        try:
            size = segment.stat().st_size
            if size == 0:
                raise OperationalEventStoreError(
                    "TRUNCATED_RECORD", "segment is empty", segment=segment.name, line=1
                )
            if size > self._max_segment_bytes:
                raise OperationalEventStoreError(
                    "SEGMENT_TOO_LARGE",
                    "segment exceeds configured retention boundary",
                    segment=segment.name,
                )
            raw = segment.read_bytes()
        except OperationalEventStoreError:
            raise
        except OSError as error:
            raise OperationalEventStoreError(
                "READ_FAILED", "segment cannot be read", segment=segment.name
            ) from error
        lines = raw.splitlines(keepends=True)
        for index, raw_line in enumerate(lines, 1):
            if not raw_line.endswith(b"\n"):
                raise OperationalEventStoreError(
                    "TRUNCATED_RECORD",
                    "JSONL record is not newline-terminated",
                    segment=segment.name,
                    line=index,
                )
            if raw_line.endswith(b"\r\n"):
                raise OperationalEventStoreError(
                    "INVALID_RECORD",
                    "JSONL records must use canonical LF line endings",
                    segment=segment.name,
                    line=index,
                )
            if len(raw_line) > MAX_STORE_RECORD_BYTES:
                raise OperationalEventStoreError(
                    "RECORD_TOO_LARGE",
                    "persisted record exceeds policy",
                    segment=segment.name,
                    line=index,
                )
            try:
                text = raw_line[:-1].decode("utf-8")
                if not text:
                    raise ValueError("empty JSONL record")
                candidate = json.loads(
                    text,
                    object_pairs_hook=_strict_object,
                    parse_constant=_reject_constant,
                )
            except (UnicodeError, json.JSONDecodeError, ValueError) as error:
                raise OperationalEventStoreError(
                    "INVALID_RECORD",
                    f"record is not strict UTF-8 JSON: {error}",
                    segment=segment.name,
                    line=index,
                ) from error
            yield index, candidate

    def _event_from_record(
        self, candidate: object, segment: str, line: int
    ) -> OperationalEvent:
        if not isinstance(candidate, Mapping) or set(candidate) != _RECORD_FIELDS:
            raise OperationalEventStoreError(
                "INVALID_RECORD",
                "record fields do not match the closed store contract",
                segment=segment,
                line=line,
            )
        if candidate["record_version"] != OPERATIONAL_EVENT_STORE_VERSION:
            raise OperationalEventStoreError(
                "UNKNOWN_RECORD_VERSION",
                "record_version is not supported",
                segment=segment,
                line=line,
            )
        try:
            event = operational_event_from_dict(candidate["event"])
            validation = self._validator.validate(
                "operational-event", operational_event_to_dict(event)
            )
        except Exception as error:
            raise OperationalEventStoreError(
                "INVALID_EVENT",
                f"persisted event cannot be reconstructed: {error}",
                segment=segment,
                line=line,
            ) from error
        if not validation.is_valid:
            raise OperationalEventStoreError(
                "INVALID_EVENT",
                "persisted event violates the canonical schema",
                segment=segment,
                line=line,
            )
        fingerprint = candidate["fingerprint"]
        if (
            not isinstance(fingerprint, str)
            or fingerprint != operational_event_fingerprint(event)
        ):
            raise OperationalEventStoreError(
                "FINGERPRINT_MISMATCH",
                "record fingerprint does not match canonical event content",
                segment=segment,
                line=line,
            )
        return event

    def _segments(self) -> tuple[Path, ...]:
        if not self._event_directory.exists():
            return ()
        segments: list[tuple[int, Path]] = []
        try:
            entries = tuple(self._event_directory.iterdir())
        except OSError as error:
            raise OperationalEventStoreError(
                "READ_FAILED", "event store directory cannot be enumerated"
            ) from error
        for path in entries:
            if path == self._lock_path:
                continue
            match = _SEGMENT_PATTERN.fullmatch(path.name)
            if match is None or _is_link_like(path) or not path.is_file():
                raise OperationalEventStoreError(
                    "UNEXPECTED_ENTRY",
                    f"unexpected or unsafe event-store entry: {path.name}",
                )
            segments.append((int(match.group(1)), path))
        segments.sort(key=lambda item: item[0])
        expected = list(range(1, len(segments) + 1))
        if [number for number, _ in segments] != expected:
            raise OperationalEventStoreError(
                "SEGMENT_SEQUENCE_INVALID", "segment numbering is not contiguous"
            )
        if len(segments) > self._max_segments:
            raise OperationalEventStoreError(
                "RETENTION_LIMIT_EXCEEDED", "segment count exceeds configured policy"
            )
        return tuple(path for _, path in segments)

    def _append_target(self, segments: tuple[Path, ...], record_size: int) -> Path:
        if not segments:
            return self._event_directory / "segment-000001.jsonl"
        active = segments[-1]
        try:
            active_size = active.stat().st_size
        except OSError as error:
            raise OperationalEventStoreError(
                "READ_FAILED", "active segment size cannot be inspected"
            ) from error
        if active_size + record_size <= self._max_segment_bytes:
            return active
        if len(segments) >= self._max_segments:
            raise OperationalEventStoreError(
                "RETENTION_LIMIT_REACHED",
                "rotation requires a new segment beyond configured retention",
            )
        return self._event_directory / f"segment-{len(segments) + 1:06d}.jsonl"

    def _durable_append(self, target: Path, record: bytes) -> None:
        before_size = target.stat().st_size if target.exists() else 0
        try:
            with target.open("ab", buffering=0) as stream:
                written = stream.write(record)
                if written != len(record):
                    raise OSError(f"short append: {written}/{len(record)} bytes")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            try:
                after_size = target.stat().st_size if target.exists() else 0
            except OSError:
                after_size = -1
            code = "DURABILITY_UNKNOWN" if after_size != before_size else "WRITE_FAILED"
            raise OperationalEventStoreError(
                code,
                "record append did not reach confirmed durable success",
                segment=target.name,
            ) from error

    def _assert_safe_paths(self, *, allow_absent: bool) -> None:
        for path, label in (
            (self._state_directory, "state directory"),
            (self._event_directory, "event directory"),
            (self._lock_path, "writer lock"),
        ):
            if _is_link_like(path):
                raise OperationalEventStoreError(
                    "UNSAFE_PATH", f"{label} cannot be a symlink"
                )
        if self._state_directory.exists() and not self._state_directory.is_dir():
            raise OperationalEventStoreError(
                "UNSAFE_PATH", "state directory is not a directory"
            )
        if self._event_directory.exists() and not self._event_directory.is_dir():
            raise OperationalEventStoreError(
                "UNSAFE_PATH", "event directory is not a directory"
            )
        if allow_absent and not self._state_directory.exists():
            return
        try:
            state_parent = self._state_directory.parent.resolve(strict=True)
        except OSError as error:
            raise OperationalEventStoreError(
                "UNSAFE_PATH", "event store parent cannot be resolved"
            ) from error
        if state_parent != self._root:
            raise OperationalEventStoreError(
                "UNSAFE_PATH", "event store escapes repository root"
            )
        if self._event_directory.exists():
            try:
                event_parent = self._event_directory.parent.resolve(strict=True)
            except OSError as error:
                raise OperationalEventStoreError(
                    "UNSAFE_PATH", "event directory parent cannot be resolved"
                ) from error
            if event_parent != self._state_directory.resolve(strict=True):
                raise OperationalEventStoreError(
                    "UNSAFE_PATH", "event directory escapes repository state directory"
                )


def _resolve_repository_root(repository_root: Path | str) -> Path:
    candidate = Path(repository_root)
    if not candidate.is_absolute():
        raise OperationalEventStoreError(
            "INVALID_REPOSITORY_ROOT", "repository root must be absolute"
        )
    absolute = candidate.absolute()
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor = cursor / part
        if _is_link_like(cursor):
            raise OperationalEventStoreError(
                "INVALID_REPOSITORY_ROOT", "repository root cannot traverse a symlink"
            )
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as error:
        raise OperationalEventStoreError(
            "INVALID_REPOSITORY_ROOT", "repository root cannot be resolved"
        ) from error
    if not resolved.is_dir() or os.path.normcase(str(resolved)) != os.path.normcase(
        str(absolute)
    ):
        raise OperationalEventStoreError(
            "INVALID_REPOSITORY_ROOT", "repository root is not a canonical directory"
        )
    return resolved


def _record_bytes(event: OperationalEvent) -> bytes:
    record = {
        "record_version": OPERATIONAL_EVENT_STORE_VERSION,
        "fingerprint": operational_event_fingerprint(event),
        "event": operational_event_to_dict(event),
    }
    return (
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    candidate: dict[str, Any] = {}
    for key, value in pairs:
        if key in candidate:
            raise ValueError(f"duplicate JSON key: {key}")
        candidate[key] = value
    return candidate


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-JSON constant: {value}")


def _matches_query(event: OperationalEvent, query: OperationalEventQuery) -> bool:
    return (
        (query.event_type is None or event.event_type is query.event_type)
        and (query.severity is None or event.severity is query.severity)
        and (query.project_id is None or event.project_id == query.project_id)
        and (
            query.mission_id is None
            or event.correlation.mission_id == query.mission_id
        )
        and (
            query.execution_id is None
            or event.correlation.execution_id == query.execution_id
        )
    )


def _positive_integer(value: object, name: str, *, maximum: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > maximum
    ):
        raise ValueError(f"{name} must be a positive integer <= {maximum}")


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & 0x400)
