import json
from datetime import datetime
from pathlib import Path

import pytest

import agentic_engineering_os.infrastructure.mission_state_store as store_module
from agentic_engineering_os._authoritative_write import _issue_authoritative_write
from agentic_engineering_os.domain import (
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    to_dict,
)
from agentic_engineering_os.infrastructure import MissionStateStore, PersistenceError


COMMIT = "fa231554666b7f5dd8fd15a609a4d6f96e59fe41"
UPDATED_AT = datetime.fromisoformat("2026-08-28T10:00:00+02:00")


def mission_state(**overrides: object) -> MissionState:
    values: dict[str, object] = {
        "schema_version": "1.0",
        "mission_id": "P2.2",
        "workflow_generation": 0,
        "status": MissionStatus.ACTIVE,
        "role": MissionRole.IMPLEMENTER,
        "objective": "Persist operational mission memory.",
        "subject": "Mission P2.2",
        "operating_step": OperatingStep.ACT,
        "next_action": "Implement the mission state store.",
        "observed_commit": COMMIT,
        "updated_at": UPDATED_AT,
    }
    values.update(overrides)
    return MissionState(**values)  # type: ignore[arg-type]


def write_candidate(store: MissionStateStore, candidate: object) -> None:
    store.mission_path.parent.mkdir()
    store.mission_path.write_text(json.dumps(candidate), encoding="utf-8")


def authorized_save(
    store: MissionStateStore,
    candidate: MissionState,
    *,
    operation: str = "TEST_MISSION_STATE_WRITE",
) -> None:
    current = store.load()
    authorization = _issue_authoritative_write(
        store_kind="MISSION_STATE",
        store=store,
        before_state=current,
        candidate_state=candidate,
        operation=operation,
    )
    store.save(candidate, authorization=authorization, operation=operation)


def test_initialize_creates_only_explicit_mission_state(tmp_path: Path) -> None:
    store = MissionStateStore(tmp_path)
    expected = mission_state()

    actual = store.initialize(expected)

    assert actual is expected
    assert store.mission_path == tmp_path / ".agentic-engineering-os" / "mission.json"
    assert to_dict(store.load()) == to_dict(expected)
    assert not (tmp_path / ".agentic-engineering-os" / "state.json").exists()


def test_save_load_round_trip_preserves_unicode_and_operational_fields(
    tmp_path: Path,
) -> None:
    store = MissionStateStore(tmp_path)
    expected = mission_state(
        objective="Vérifier la mémoire persistante.",
        blockers=["Décision humaine requise"],
        next_action="Présenter les éléments à l’opérateur.",
    )

    store.initialize(expected)
    first = store.load()
    second = store.load()

    assert to_dict(first) == to_dict(expected)
    assert to_dict(second) == to_dict(expected)
    assert "Vérifier" in store.mission_path.read_text(encoding="utf-8")


def test_save_updates_state_atomically_in_the_same_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MissionStateStore(tmp_path)
    store.initialize(mission_state())
    observed: dict[str, Path] = {}
    original_replace = store_module.os.replace

    def observe_replace(source: Path, destination: Path) -> None:
        observed["source"] = Path(source)
        observed["destination"] = Path(destination)
        assert Path(source).exists()
        original_replace(source, destination)

    monkeypatch.setattr(store_module.os, "replace", observe_replace)
    updated = mission_state(operating_step=OperatingStep.VERIFY)

    authorized_save(store, updated)

    assert observed["source"].parent == store.mission_path.parent
    assert observed["destination"] == store.mission_path
    assert to_dict(store.load()) == to_dict(updated)


def test_load_absent_state_does_not_create_fallback(tmp_path: Path) -> None:
    store = MissionStateStore(tmp_path)

    with pytest.raises(PersistenceError) as captured:
        store.load()

    assert captured.value.code == "MISSION_ABSENT"
    assert not store.mission_path.parent.exists()


def test_corrupt_json_is_distinct_and_never_replaced_with_empty_state(
    tmp_path: Path,
) -> None:
    store = MissionStateStore(tmp_path)
    store.mission_path.parent.mkdir()
    corrupt = b'{"mission_id":'
    store.mission_path.write_bytes(corrupt)

    with pytest.raises(PersistenceError) as captured:
        store.load()

    assert captured.value.code == "INVALID_JSON"
    assert store.mission_path.read_bytes() == corrupt


def test_duplicate_json_key_is_refused_without_modifying_file(tmp_path: Path) -> None:
    store = MissionStateStore(tmp_path)
    ambiguous = b'{"schema_version":"1.0","schema_version":"1.0"}'
    store.mission_path.parent.mkdir()
    store.mission_path.write_bytes(ambiguous)

    with pytest.raises(PersistenceError, match="duplicate JSON key") as captured:
        store.load()

    assert captured.value.code == "INVALID_JSON"
    assert store.mission_path.read_bytes() == ambiguous


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "UNKNOWN"),
        ("role", "OPERATOR"),
        ("operating_step", "EXECUTE"),
        ("mission_id", ""),
    ],
)
def test_invalid_required_values_fail_closed_on_load(
    tmp_path: Path, field: str, value: str
) -> None:
    store = MissionStateStore(tmp_path)
    candidate = to_dict(mission_state())
    candidate[field] = value
    write_candidate(store, candidate)

    with pytest.raises(PersistenceError) as captured:
        store.load()

    assert captured.value.code == "INVALID_SCHEMA"


def test_invalid_timestamp_fails_closed_on_load(tmp_path: Path) -> None:
    store = MissionStateStore(tmp_path)
    candidate = to_dict(mission_state())
    candidate["updated_at"] = "not-a-timestamp"
    write_candidate(store, candidate)

    with pytest.raises(PersistenceError) as captured:
        store.load()

    assert captured.value.code == "INVALID_SCHEMA"


def test_missing_required_field_is_refused(tmp_path: Path) -> None:
    store = MissionStateStore(tmp_path)
    candidate = to_dict(mission_state())
    del candidate["next_action"]
    write_candidate(store, candidate)

    with pytest.raises(PersistenceError) as captured:
        store.load()

    assert captured.value.code == "INVALID_SCHEMA"


def test_missing_workflow_generation_is_not_silently_migrated(tmp_path: Path) -> None:
    store = MissionStateStore(tmp_path)
    candidate = to_dict(mission_state())
    del candidate["workflow_generation"]
    write_candidate(store, candidate)

    with pytest.raises(PersistenceError) as captured:
        store.load()

    assert captured.value.code == "INVALID_SCHEMA"


@pytest.mark.parametrize("value", [-1, True])
def test_invalid_workflow_generation_fails_closed_on_load(
    tmp_path: Path, value: object
) -> None:
    store = MissionStateStore(tmp_path)
    candidate = to_dict(mission_state())
    candidate["workflow_generation"] = value
    write_candidate(store, candidate)

    with pytest.raises(PersistenceError) as captured:
        store.load()

    assert captured.value.code == "INVALID_SCHEMA"


def test_unknown_property_is_refused(tmp_path: Path) -> None:
    store = MissionStateStore(tmp_path)
    candidate = to_dict(mission_state())
    candidate["gate_result"] = "PASS"
    write_candidate(store, candidate)

    with pytest.raises(PersistenceError) as captured:
        store.load()

    assert captured.value.code == "INVALID_SCHEMA"


def test_invalid_save_preserves_previous_mission_state(tmp_path: Path) -> None:
    store = MissionStateStore(tmp_path)
    original = mission_state()
    store.initialize(original)
    before = store.mission_path.read_bytes()

    with pytest.raises(PersistenceError) as captured:
        store.save(mission_state(mission_id=""))

    assert captured.value.code == "INVALID_SCHEMA"
    assert store.mission_path.read_bytes() == before
    assert to_dict(store.load()) == to_dict(original)


def test_write_failure_preserves_previous_mission_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MissionStateStore(tmp_path)
    original = mission_state()
    store.initialize(original)
    before = store.mission_path.read_bytes()

    def fail_write(directory: Path, text: str) -> Path:
        raise OSError("simulated write failure")

    monkeypatch.setattr(store_module, "_write_temporary", fail_write)

    with pytest.raises(PersistenceError) as captured:
        authorized_save(
            store, mission_state(operating_step=OperatingStep.VERIFY)
        )

    assert captured.value.code == "WRITE_FAILED"
    assert store.mission_path.read_bytes() == before


def test_replace_failure_preserves_state_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MissionStateStore(tmp_path)
    store.initialize(mission_state())
    before = store.mission_path.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(store_module.os, "replace", fail_replace)

    with pytest.raises(PersistenceError) as captured:
        authorized_save(
            store, mission_state(operating_step=OperatingStep.VERIFY)
        )

    assert captured.value.code == "WRITE_FAILED"
    assert store.mission_path.read_bytes() == before
    assert list(store.mission_path.parent.glob(".mission.*.tmp")) == []


def test_initialize_never_overwrites_existing_mission(tmp_path: Path) -> None:
    store = MissionStateStore(tmp_path)
    original = mission_state()
    store.initialize(original)

    with pytest.raises(PersistenceError) as captured:
        store.initialize(mission_state(mission_id="P2.2-second"))

    assert captured.value.code == "MISSION_ALREADY_EXISTS"
    assert to_dict(store.load()) == to_dict(original)


def test_mission_file_cannot_change_project_state_file(tmp_path: Path) -> None:
    state_directory = tmp_path / ".agentic-engineering-os"
    state_directory.mkdir()
    project_state = b'{"authoritative":"project-state"}'
    project_path = state_directory / "state.json"
    project_path.write_bytes(project_state)

    MissionStateStore(tmp_path).initialize(mission_state())

    assert project_path.read_bytes() == project_state
