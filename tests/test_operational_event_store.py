from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_engineering_os.domain import (
    Evidence,
    MissionRole,
    OperationalAttribute,
    OperationalCorrelation,
    OperationalEvent,
    OperationalEventPayload,
    OperationalEventType,
    OperationalProvenance,
    OperationalProvenanceKind,
    OperationalSeverity,
    operational_event_fingerprint,
)
from agentic_engineering_os.infrastructure import (
    OperationalEventQuery,
    OperationalEventStore,
    OperationalEventStoreError,
    StructuredEventLogger,
)
import agentic_engineering_os.infrastructure.operational_event_store as store_module


def _event(
    index: int = 1,
    *,
    event_type: OperationalEventType = OperationalEventType.OPERATIONAL_ANOMALY,
    operation: str = "DETECTED",
    correlation: OperationalCorrelation | None = None,
    value: str = "observed",
) -> OperationalEvent:
    return OperationalEvent(
        schema_version="1.0",
        event_id=f"00000000-0000-4000-8000-{index:012d}",
        event_type=event_type,
        occurred_at=datetime(2026, 9, 1, 8, index % 60, tzinfo=timezone.utc),
        severity=OperationalSeverity.WARNING,
        source_component="OperationalObserver",
        project_id="project-one",
        correlation=correlation or OperationalCorrelation(),
        payload=OperationalEventPayload(
            operation=operation,
            attributes=(OperationalAttribute("summary", value),),
        ),
        provenance=OperationalProvenance(
            kind=OperationalProvenanceKind.DETERMINISTIC_COMPONENT,
            producer="OperationalObserver",
        ),
    )


def _segment(root: Path, number: int = 1) -> Path:
    return (
        root
        / ".agentic-engineering-os"
        / "operational-events"
        / f"segment-{number:06d}.jsonl"
    )


def _write_raw_segment(root: Path, content: bytes, number: int = 1) -> Path:
    path = _segment(root, number)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _link_directory(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as first_error:
        if sys.platform != "win32":
            pytest.fail(f"directory symlink unavailable: {first_error}")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"directory junction unavailable: {result.stderr or result.stdout}")


def test_append_read_round_trip_is_canonical_and_durable(tmp_path: Path) -> None:
    store = OperationalEventStore(tmp_path)
    event = _event()

    receipt = store.append(event)

    assert receipt.event_id == event.event_id
    assert receipt.fingerprint == operational_event_fingerprint(event)
    assert receipt.segment == "segment-000001.jsonl"
    assert receipt.record_index == 1
    assert store.read() == (event,)
    raw = _segment(tmp_path).read_bytes()
    assert raw.endswith(b"\n") and b"\r\n" not in raw
    assert len(raw.splitlines()) == 1


def test_restart_reads_the_same_observation(tmp_path: Path) -> None:
    OperationalEventStore(tmp_path).append(_event())
    assert OperationalEventStore(tmp_path).read() == (_event(),)


def test_multiple_unicode_events_preserve_append_order(tmp_path: Path) -> None:
    store = OperationalEventStore(tmp_path)
    events = (_event(1, value="échec contrôlé"), _event(2), _event(3))
    for event in events:
        store.append(event)

    assert store.read() == events
    assert "échec contrôlé" in _segment(tmp_path).read_text(encoding="utf-8")


def test_exact_query_filters_without_inference(tmp_path: Path) -> None:
    store = OperationalEventStore(tmp_path)
    mission = OperationalCorrelation(
        mission_id="mission-1",
        workflow_generation=2,
        role=MissionRole.IMPLEMENTER,
        execution_id="execution-1",
    )
    codex = _event(
        2,
        event_type=OperationalEventType.CODEX_EXECUTION,
        operation="FINISHED",
        correlation=mission,
    )
    store.append(_event(1))
    store.append(codex)

    assert store.query(
        OperationalEventQuery(
            event_type=OperationalEventType.CODEX_EXECUTION,
            severity=OperationalSeverity.WARNING,
            project_id="project-one",
            mission_id="mission-1",
            execution_id="execution-1",
        )
    ) == (codex,)
    assert store.query(OperationalEventQuery(mission_id="absent")) == ()


def test_structured_logger_accepts_only_typed_events(tmp_path: Path) -> None:
    logger = StructuredEventLogger(OperationalEventStore(tmp_path))
    assert logger.record(_event()).event_id == _event().event_id
    with pytest.raises(OperationalEventStoreError, match="INVALID_EVENT"):
        logger.record("free-form message")  # type: ignore[arg-type]
    assert not hasattr(logger, "info")
    assert not hasattr(logger, "warning")


def test_rotation_is_deterministic_and_retention_refuses_overflow(tmp_path: Path) -> None:
    store = OperationalEventStore(tmp_path, max_segment_bytes=1_024, max_segments=2)
    store.append(_event(1))
    store.append(_event(2))
    assert [path.name for path in store.event_directory.iterdir()] == [
        "segment-000001.jsonl",
        "segment-000002.jsonl",
    ]
    with pytest.raises(OperationalEventStoreError, match="RETENTION_LIMIT_REACHED"):
        store.append(_event(3))
    assert store.read() == (_event(1), _event(2))


def test_duplicate_event_id_is_refused_across_restart(tmp_path: Path) -> None:
    OperationalEventStore(tmp_path).append(_event())
    with pytest.raises(OperationalEventStoreError, match="DUPLICATE_EVENT_ID"):
        OperationalEventStore(tmp_path).append(_event(value="different fact"))


def test_same_content_shape_with_distinct_event_id_is_not_heuristically_deduplicated(
    tmp_path: Path,
) -> None:
    store = OperationalEventStore(tmp_path)
    store.append(_event(1))
    store.append(_event(2))
    assert len(store.read()) == 2


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"{not-json}\n", "INVALID_RECORD"),
        (
            b'{"record_version":"1.0","record_version":"1.0","fingerprint":"x","event":{}}\n',
            "INVALID_RECORD",
        ),
        (b'{"record_version":"1.0"}', "TRUNCATED_RECORD"),
        (b"\xff\n", "INVALID_RECORD"),
        (b"{}\r\n", "INVALID_RECORD"),
    ],
)
def test_malformed_duplicate_truncated_or_noncanonical_records_fail_closed(
    tmp_path: Path, content: bytes, code: str
) -> None:
    _write_raw_segment(tmp_path, content)
    with pytest.raises(OperationalEventStoreError) as caught:
        OperationalEventStore(tmp_path).read()
    assert caught.value.code == code
    assert caught.value.segment == "segment-000001.jsonl"
    assert caught.value.line == 1


def test_corruption_in_middle_is_not_skipped(tmp_path: Path) -> None:
    store = OperationalEventStore(tmp_path)
    store.append(_event(1))
    store.append(_event(2))
    lines = _segment(tmp_path).read_bytes().splitlines(keepends=True)
    _segment(tmp_path).write_bytes(lines[0] + b"{broken}\n" + lines[1])

    with pytest.raises(OperationalEventStoreError) as caught:
        store.read()
    assert caught.value.code == "INVALID_RECORD"
    assert caught.value.line == 2


def test_unknown_record_version_is_refused(tmp_path: Path) -> None:
    store = OperationalEventStore(tmp_path)
    store.append(_event())
    raw = _segment(tmp_path).read_text(encoding="utf-8")
    _segment(tmp_path).write_text(
        raw.replace('"record_version":"1.0"', '"record_version":"9.0"'),
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(OperationalEventStoreError, match="UNKNOWN_RECORD_VERSION"):
        store.read()


def test_fingerprint_tampering_is_refused(tmp_path: Path) -> None:
    store = OperationalEventStore(tmp_path)
    store.append(_event())
    record = json.loads(_segment(tmp_path).read_text(encoding="utf-8"))
    record["fingerprint"] = "0" * 64
    _segment(tmp_path).write_text(
        json.dumps(record, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(OperationalEventStoreError, match="FINGERPRINT_MISMATCH"):
        store.read()


def test_invalid_object_is_rejected_before_storage_creation(tmp_path: Path) -> None:
    store = OperationalEventStore(tmp_path)
    with pytest.raises(OperationalEventStoreError, match="INVALID_EVENT"):
        store.append(object())  # type: ignore[arg-type]
    assert not store.event_directory.exists()


@pytest.mark.parametrize("unsafe", ["token=synthetic-secret", "x" * 2049])
def test_boundary_revalidates_forged_secret_or_oversized_event(
    tmp_path: Path, unsafe: str
) -> None:
    event = _event()
    object.__setattr__(event.payload.attributes[0], "value", unsafe)
    with pytest.raises(OperationalEventStoreError, match="INVALID_EVENT"):
        OperationalEventStore(tmp_path).append(event)
    assert not _segment(tmp_path).exists()


def test_io_failure_never_returns_success_and_leaves_observable_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = os.fsync
    calls = 0

    def fail_record_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(store_module.os, "fsync", fail_record_fsync)
    store = OperationalEventStore(tmp_path)
    with pytest.raises(OperationalEventStoreError) as caught:
        store.append(_event())
    assert caught.value.code == "DURABILITY_UNKNOWN"
    assert not (store.event_directory / ".writer.lock").exists()


def test_partial_write_remains_detectable_and_is_not_repaired(tmp_path: Path) -> None:
    _write_raw_segment(tmp_path, b'{"record_version":"1.0"')
    store = OperationalEventStore(tmp_path)
    with pytest.raises(OperationalEventStoreError, match="TRUNCATED_RECORD"):
        store.read()
    with pytest.raises(OperationalEventStoreError, match="TRUNCATED_RECORD"):
        store.append(_event())
    assert _segment(tmp_path).read_bytes() == b'{"record_version":"1.0"'


def test_cooperative_writer_lock_refuses_read_and_append(tmp_path: Path) -> None:
    lock = (
        tmp_path
        / ".agentic-engineering-os"
        / "operational-events"
        / ".writer.lock"
    )
    lock.parent.mkdir(parents=True)
    lock.write_text("pid=other\n", encoding="ascii")
    store = OperationalEventStore(tmp_path)
    with pytest.raises(OperationalEventStoreError, match="CONCURRENT_WRITER"):
        store.read()
    with pytest.raises(OperationalEventStoreError, match="CONCURRENT_WRITER"):
        store.append(_event())


def test_threads_on_one_store_are_serialized_without_loss(tmp_path: Path) -> None:
    store = OperationalEventStore(tmp_path)
    events = tuple(_event(index) for index in range(1, 13))
    with ThreadPoolExecutor(max_workers=4) as executor:
        receipts = tuple(executor.map(store.append, events))
    assert len(receipts) == 12
    assert {item.event_id for item in store.read()} == {
        item.event_id for item in events
    }


def test_symlink_event_directory_outside_repository_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    state = root / ".agentic-engineering-os"
    state.mkdir()
    _link_directory(state / "operational-events", outside)
    with pytest.raises(OperationalEventStoreError, match="UNSAFE_PATH"):
        OperationalEventStore(root).append(_event())


def test_relative_or_symlink_repository_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(OperationalEventStoreError, match="INVALID_REPOSITORY_ROOT"):
        OperationalEventStore(Path("relative-repository"))
    root = tmp_path / "repository"
    root.mkdir()
    alias = tmp_path / "alias"
    _link_directory(alias, root)
    with pytest.raises(OperationalEventStoreError, match="INVALID_REPOSITORY_ROOT"):
        OperationalEventStore(alias)


def test_segment_gap_and_unexpected_entry_are_diagnostics(
    tmp_path: Path,
) -> None:
    _write_raw_segment(tmp_path, b"{}\n", number=2)
    with pytest.raises(OperationalEventStoreError, match="SEGMENT_SEQUENCE_INVALID"):
        OperationalEventStore(tmp_path).read()

    _segment(tmp_path, 2).unlink()
    unexpected = _segment(tmp_path).parent / "notes.txt"
    unexpected.write_text("not a journal", encoding="utf-8")
    with pytest.raises(OperationalEventStoreError, match="UNEXPECTED_ENTRY"):
        OperationalEventStore(tmp_path).read()


def test_event_store_has_no_authority_or_state_recovery_api(tmp_path: Path) -> None:
    store = OperationalEventStore(tmp_path)
    event = _event()
    store.append(event)
    assert not isinstance(event, Evidence)
    for forbidden in (
        "to_evidence",
        "to_audit_event",
        "to_gate",
        "to_certification",
        "recover_project_state",
        "save_project_state",
        "certify",
    ):
        assert not hasattr(store, forbidden)
