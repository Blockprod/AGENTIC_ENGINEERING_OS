import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

import agentic_engineering_os.infrastructure.project_state_store as store_module
from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    AuditEvent,
    AuditEventType,
    Certification,
    CertificationResult,
    Evidence,
    EvidenceType,
    Gate,
    GateResult,
    HumanApproval,
    ProjectState,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
    to_dict,
)
from agentic_engineering_os.infrastructure import PersistenceError, ProjectStateStore


COMMIT = "a4145afe755d62064b1ba0924399a2ba073e70e8"
NOW = datetime(2026, 8, 27, 16, 0, tzinfo=timezone.utc)


def user_story(
    story_id: str = "US-0001",
    *,
    depends_on: tuple[str, ...] = (),
) -> UserStory:
    return UserStory(
        schema_version="1.0",
        id=story_id,
        title="Mémoire persistante déterministe",
        description="Persist all canonical categories as validated JSON.",
        status=UserStoryStatus.CERTIFIED,
        priority=1,
        risk=RiskLevel.HIGH,
        depends_on=depends_on,
        scope=UserStoryScope(
            allowed_paths=("src/", ".agentic-engineering-os/state.json"),
            forbidden_paths=(),
        ),
        acceptance_criteria=(
            AcceptanceCriterion(
                id="AC-001",
                description="The state round trip is exact.",
                mandatory=True,
            ),
        ),
        required_gates=("GATE-001",),
        human_approval=HumanApproval(
            required=False,
            approved=False,
            approved_by=None,
            approved_at=None,
        ),
        metadata=UserStoryMetadata(
            created_at=NOW,
            created_by="human-operator",
            updated_at=NOW,
        ),
    )


def evidence(evidence_id: str = "EV-001") -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.TEST_RESULT,
        subject="US-0001",
        result={"tests": 159, "résultat": "réussi"},
        source="pytest",
        command="python -m pytest -q",
        exit_code=0,
        artifact="captured output",
        commit=COMMIT,
        timestamp=NOW,
        producer="Codex/Tester",
    )


def gate(gate_id: str = "GATE-001", evidence_id: str = "EV-001") -> Gate:
    return Gate(
        gate_id=gate_id,
        subject="US-0001",
        required=True,
        result=GateResult.PASS,
        evidence_refs=(evidence_id,),
        evaluated_at=NOW,
        evaluator="Codex/Reviewer",
    )


def certification(
    certification_id: str = "CERT-001",
    evidence_id: str = "EV-001",
    gate_id: str = "GATE-001",
) -> Certification:
    return Certification(
        certification_id=certification_id,
        subject="US-0001",
        result=CertificationResult.CERTIFIED,
        commit=COMMIT,
        acceptance_results={"AC-001": "PASS"},
        gate_results={gate_id: "PASS"},
        human_approval={
            "required": False,
            "approved": False,
            "evidence_ref": None,
        },
        evidence_refs=(evidence_id,),
        certified_at=NOW,
        certifier="Codex/Certifier",
    )


def audit_event(event_id: str = "EVENT-001") -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        timestamp=NOW,
        event_type=AuditEventType.CERTIFICATION_GRANTED,
        subject="US-0001",
        actor="Codex",
        role="Certifier",
        repository_commit=COMMIT,
        payload={"certification_id": "CERT-001", "result": "CERTIFIED"},
    )


def full_state() -> ProjectState:
    return ProjectState(
        schema_version="1.0",
        user_stories=[user_story()],
        evidence=[evidence()],
        gates=[gate()],
        certifications=[certification()],
        audit_events=[audit_event()],
    )


def write_json(path: Path, candidate: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidate), encoding="utf-8")


def test_initialize_creates_only_canonical_empty_state(tmp_path: Path) -> None:
    store = ProjectStateStore(tmp_path)

    state = store.initialize()

    assert to_dict(state) == {
        "schema_version": "1.0",
        "user_stories": [],
        "evidence": [],
        "gates": [],
        "certifications": [],
        "audit_events": [],
    }
    assert store.state_path == tmp_path / ".agentic-engineering-os" / "state.json"
    assert store.state_path.is_file()
    assert [item.relative_to(tmp_path).as_posix() for item in tmp_path.rglob("*")] == [
        ".agentic-engineering-os",
        ".agentic-engineering-os/state.json",
    ]


def test_save_load_round_trip_preserves_all_five_categories(tmp_path: Path) -> None:
    store = ProjectStateStore(tmp_path)
    expected = full_state()

    store.save(expected)
    actual = store.load()

    assert to_dict(actual) == to_dict(expected)
    assert actual.user_stories[0].status is UserStoryStatus.CERTIFIED
    assert actual.evidence[0].evidence_type is EvidenceType.TEST_RESULT
    assert actual.gates[0].result is GateResult.PASS
    assert actual.certifications[0].result is CertificationResult.CERTIFIED
    assert actual.audit_events[0].event_type is AuditEventType.CERTIFICATION_GRANTED
    assert actual.evidence[0].timestamp == NOW
    assert actual.user_stories[0].title == "Mémoire persistante déterministe"
    with pytest.raises(TypeError):
        actual.evidence[0].result["tests"] = 0


def test_serialization_is_deterministic_utf8(tmp_path: Path) -> None:
    store = ProjectStateStore(tmp_path)
    state = full_state()

    store.save(state)
    first = store.state_path.read_bytes()
    store.save(state)
    second = store.state_path.read_bytes()

    assert first == second
    assert "Mémoire persistante déterministe" in first.decode("utf-8")
    assert first.endswith(b"\n")


def test_successive_saves_replace_state_nominally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, Path]] = []
    replace = os.replace

    def observe_replace(source: Path, target: Path) -> None:
        calls.append((Path(source), Path(target)))
        replace(source, target)

    monkeypatch.setattr(store_module.os, "replace", observe_replace)
    store = ProjectStateStore(tmp_path)
    store.save(ProjectState(schema_version="1.0"))
    updated = full_state()
    store.save(updated)

    assert to_dict(store.load()) == to_dict(updated)
    assert len(calls) == 2
    assert all(source.parent == target.parent for source, target in calls)
    assert all(target == store.state_path for _, target in calls)


def test_load_absent_state_does_not_create_directory(tmp_path: Path) -> None:
    store = ProjectStateStore(tmp_path)

    with pytest.raises(PersistenceError) as captured:
        store.load()

    assert captured.value.code == "STATE_ABSENT"
    assert list(tmp_path.iterdir()) == []


def test_incomplete_temp_file_is_never_authoritative(tmp_path: Path) -> None:
    directory = tmp_path / ".agentic-engineering-os"
    directory.mkdir()
    (directory / ".state.orphan.tmp").write_text("{}", encoding="utf-8")
    store = ProjectStateStore(tmp_path)

    with pytest.raises(PersistenceError) as captured:
        store.load()

    assert captured.value.code == "STATE_ABSENT"
    assert not store.state_path.exists()


def test_corrupt_json_is_explicit_and_never_replaced_by_empty_state(
    tmp_path: Path,
) -> None:
    store = ProjectStateStore(tmp_path)
    corrupt = b'{"schema_version":'
    store.state_path.parent.mkdir()
    store.state_path.write_bytes(corrupt)

    with pytest.raises(PersistenceError) as captured:
        store.load()

    assert captured.value.code == "INVALID_JSON"
    assert store.state_path.read_bytes() == corrupt


def test_load_rejects_duplicate_root_json_key_without_modifying_file(
    tmp_path: Path,
) -> None:
    store = ProjectStateStore(tmp_path)
    valid_state = json.dumps(to_dict(ProjectState(schema_version="1.0")))
    ambiguous_state = valid_state.replace(
        '"schema_version": "1.0"',
        '"schema_version": "discarded", "schema_version": "1.0"',
        1,
    ).encode()
    store.state_path.parent.mkdir()
    store.state_path.write_bytes(ambiguous_state)

    with pytest.raises(PersistenceError, match="duplicate JSON key") as captured:
        store.load()

    assert captured.value.code == "INVALID_JSON"
    assert "schema_version" in captured.value.message
    assert store.state_path.read_bytes() == ambiguous_state


@pytest.mark.parametrize(
    ("location", "needle", "replacement", "duplicate_key"),
    [
        (
            "collection",
            '"user_stories": [',
            '"user_stories": [], "user_stories": [',
            "user_stories",
        ),
        (
            "nested-user-story",
            '"status": "CERTIFIED"',
            '"status": "discarded", "status": "CERTIFIED"',
            "status",
        ),
        (
            "nested-evidence",
            '"producer": "Codex/Tester"',
            '"producer": "discarded", "producer": "Codex/Tester"',
            "producer",
        ),
        (
            "deep-audit-payload",
            '"payload": {"certification_id": "CERT-001", "result": "CERTIFIED"}',
            (
                '"payload": {"certification_id": "CERT-001", '
                '"result": "discarded", "result": "CERTIFIED"}'
            ),
            "result",
        ),
    ],
)
def test_load_rejects_duplicate_json_keys_at_every_nested_depth(
    tmp_path: Path,
    location: str,
    needle: str,
    replacement: str,
    duplicate_key: str,
) -> None:
    store = ProjectStateStore(tmp_path)
    valid_state = json.dumps(to_dict(full_state()), ensure_ascii=False)
    assert needle in valid_state, f"invalid adversarial fixture for {location}"
    ambiguous_state = valid_state.replace(needle, replacement, 1).encode("utf-8")
    store.state_path.parent.mkdir()
    store.state_path.write_bytes(ambiguous_state)

    with pytest.raises(PersistenceError, match="duplicate JSON key") as captured:
        store.load()

    assert captured.value.code == "INVALID_JSON"
    assert duplicate_key in captured.value.message
    assert store.state_path.read_bytes() == ambiguous_state


def test_schema_invalid_state_is_refused_on_load(tmp_path: Path) -> None:
    store = ProjectStateStore(tmp_path)
    candidate = to_dict(ProjectState(schema_version="1.0"))
    candidate["schema_version"] = "2.0"
    write_json(store.state_path, candidate)

    with pytest.raises(PersistenceError) as captured:
        store.load()

    assert captured.value.code == "INVALID_SCHEMA"


def test_schema_invalid_state_is_refused_before_save(tmp_path: Path) -> None:
    store = ProjectStateStore(tmp_path)

    with pytest.raises(PersistenceError) as captured:
        store.save(ProjectState(schema_version="2.0"))

    assert captured.value.code == "INVALID_SCHEMA"
    assert not store.state_path.exists()


def test_unknown_enum_is_refused_on_load(tmp_path: Path) -> None:
    store = ProjectStateStore(tmp_path)
    candidate = to_dict(full_state())
    candidate["user_stories"][0]["status"] = "MAGIC"
    write_json(store.state_path, candidate)

    with pytest.raises(PersistenceError) as captured:
        store.load()

    assert captured.value.code == "INVALID_SCHEMA"


def test_read_failure_is_explicit_and_has_no_empty_fallback(tmp_path: Path) -> None:
    store = ProjectStateStore(tmp_path)
    store.state_path.mkdir(parents=True)

    with pytest.raises(PersistenceError) as captured:
        store.load()

    assert captured.value.code == "READ_FAILED"


def test_hydration_failure_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProjectStateStore(tmp_path)
    store.save(ProjectState(schema_version="1.0"))

    def fail_hydration(candidate: object) -> ProjectState:
        raise ValueError("simulated hydration failure")

    monkeypatch.setattr(store_module, "_hydrate_project_state", fail_hydration)

    with pytest.raises(PersistenceError) as captured:
        store.load()

    assert captured.value.code == "INVALID_DOMAIN_DATA"


@pytest.mark.parametrize(
    "category",
    ["user_stories", "evidence", "gates", "certifications", "audit_events"],
)
def test_duplicate_ids_are_refused_before_save(
    tmp_path: Path, category: str
) -> None:
    state = full_state()
    getattr(state, category).append(getattr(state, category)[0])
    store = ProjectStateStore(tmp_path)

    with pytest.raises(PersistenceError) as captured:
        store.save(state)

    assert captured.value.code == "DUPLICATE_ID"
    assert not store.state_path.exists()


def test_duplicate_id_is_refused_on_load(tmp_path: Path) -> None:
    store = ProjectStateStore(tmp_path)
    candidate = to_dict(full_state())
    candidate["evidence"].append(candidate["evidence"][0])
    write_json(store.state_path, candidate)

    with pytest.raises(PersistenceError) as captured:
        store.load()

    assert captured.value.code == "DUPLICATE_ID"


def test_invalid_reference_is_refused_on_load(tmp_path: Path) -> None:
    store = ProjectStateStore(tmp_path)
    candidate = to_dict(full_state())
    candidate["gates"][0]["evidence_refs"] = ["EV-MISSING"]
    write_json(store.state_path, candidate)

    with pytest.raises(PersistenceError) as captured:
        store.load()

    assert captured.value.code == "INVALID_REFERENCE"


@pytest.mark.parametrize("invalid_reference", ["dependency", "gate", "certification"])
def test_locally_verifiable_missing_reference_is_refused(
    tmp_path: Path, invalid_reference: str
) -> None:
    state = full_state()
    if invalid_reference == "dependency":
        state.user_stories[0].depends_on = ("US-9999",)
    elif invalid_reference == "gate":
        state.gates[0] = gate(evidence_id="EV-MISSING")
    else:
        state.certifications[0] = certification(evidence_id="EV-MISSING")
    store = ProjectStateStore(tmp_path)

    with pytest.raises(PersistenceError) as captured:
        store.save(state)

    assert captured.value.code == "INVALID_REFERENCE"
    assert not store.state_path.exists()


@pytest.mark.parametrize("violation", ["self-dependency", "duplicate-criterion"])
def test_local_user_story_integrity_is_refused(
    tmp_path: Path, violation: str
) -> None:
    state = full_state()
    if violation == "self-dependency":
        state.user_stories[0].depends_on = ("US-0001",)
    else:
        criterion = state.user_stories[0].acceptance_criteria[0]
        state.user_stories[0].acceptance_criteria = (
            criterion,
            AcceptanceCriterion(
                id=criterion.id,
                description="A different description with the same id.",
                mandatory=True,
            ),
        )
    store = ProjectStateStore(tmp_path)

    with pytest.raises(PersistenceError) as captured:
        store.save(state)

    assert captured.value.code in {"DUPLICATE_ID", "INVALID_REFERENCE"}
    assert not store.state_path.exists()


def test_invalid_save_preserves_previous_authoritative_state(tmp_path: Path) -> None:
    store = ProjectStateStore(tmp_path)
    original = ProjectState(schema_version="1.0")
    store.save(original)
    previous = store.state_path.read_bytes()
    invalid = full_state()
    invalid.evidence.append(invalid.evidence[0])

    with pytest.raises(PersistenceError):
        store.save(invalid)

    assert store.state_path.read_bytes() == previous
    assert to_dict(store.load()) == to_dict(original)


def test_write_failure_before_replace_preserves_previous_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProjectStateStore(tmp_path)
    store.initialize()
    previous = store.state_path.read_bytes()

    def fail_write(directory: Path, text: str) -> Path:
        raise OSError("simulated write failure")

    monkeypatch.setattr(store_module, "_write_temporary", fail_write)

    with pytest.raises(PersistenceError) as captured:
        store.save(full_state())

    assert captured.value.code == "WRITE_FAILED"
    assert store.state_path.read_bytes() == previous


def test_replace_failure_preserves_previous_state_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProjectStateStore(tmp_path)
    store.initialize()
    previous = store.state_path.read_bytes()

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(store_module.os, "replace", fail_replace)

    with pytest.raises(PersistenceError) as captured:
        store.save(full_state())

    assert captured.value.code == "WRITE_FAILED"
    assert store.state_path.read_bytes() == previous
    assert list(store.state_path.parent.glob(".state.*.tmp")) == []


def test_initialize_never_overwrites_existing_state(tmp_path: Path) -> None:
    store = ProjectStateStore(tmp_path)
    store.save(full_state())
    previous = store.state_path.read_bytes()

    with pytest.raises(PersistenceError) as captured:
        store.initialize()

    assert captured.value.code == "STATE_ALREADY_EXISTS"
    assert store.state_path.read_bytes() == previous
