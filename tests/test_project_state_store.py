import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import agentic_engineering_os.infrastructure.project_state_store as store_module
from agentic_engineering_os._authoritative_write import _issue_authoritative_write
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
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    ProjectState,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
    to_dict,
)
from agentic_engineering_os.infrastructure import (
    MissionStateStore,
    PersistenceError,
    ProjectStateStore,
)


COMMIT = "a4145afe755d62064b1ba0924399a2ba073e70e8"
OTHER_COMMIT = "2c65f6b79c75a8986171abb94d386f022ea23988"
NOW = datetime(2026, 8, 27, 16, 0, tzinfo=timezone.utc)


def user_story(
    story_id: str = "US-0001",
    *,
    depends_on: tuple[str, ...] = (),
    status: UserStoryStatus = UserStoryStatus.CERTIFIED,
) -> UserStory:
    return UserStory(
        schema_version="1.0",
        id=story_id,
        title="Mémoire persistante déterministe",
        description="Persist all canonical categories as validated JSON.",
        status=status,
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


def acceptance_evidence(evidence_id: str = "EV-AC-001") -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.ACCEPTANCE_CRITERION_CHECK,
        subject="AC-001",
        result=True,
        source="pytest",
        command=None,
        exit_code=None,
        artifact="captured acceptance result",
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
    evidence_id: str = "EV-AC-001",
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
        evidence_refs=(evidence_id, "EV-001"),
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
        evidence=[evidence(), acceptance_evidence()],
        gates=[gate()],
        certifications=[certification()],
        audit_events=[audit_event()],
    )


def not_applicable_state(
    authorities: tuple[str, ...] = ("GATE-001",),
) -> ProjectState:
    state = full_state()
    state.gates[0] = replace(
        state.gates[0],
        result=GateResult.NOT_APPLICABLE,
        evidence_refs=(),
    )
    state.certifications[0] = replace(
        state.certifications[0],
        gate_results={"GATE-001": "NOT_APPLICABLE"},
        evidence_refs=("EV-AC-001",),
        authorized_not_applicable_gates=authorities,
    )
    return state


def write_json(path: Path, candidate: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidate), encoding="utf-8")


def authorized_save(
    store: ProjectStateStore,
    candidate: ProjectState,
    *,
    operation: str = "TEST_PROJECT_STATE_WRITE",
) -> None:
    try:
        current = store.load()
    except PersistenceError as error:
        if error.code != "STATE_ABSENT":
            raise
        current = store.initialize()
    authorization = _issue_authoritative_write(
        store_kind="PROJECT_STATE",
        store=store,
        before_state=current,
        candidate_state=candidate,
        operation=operation,
    )
    store.save(candidate, authorization=authorization, operation=operation)


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

    authorized_save(store, expected)
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


def test_direct_valid_certified_snapshot_requires_mutation_authority(
    tmp_path: Path,
) -> None:
    store = ProjectStateStore(tmp_path)
    before = store.initialize()
    candidate = full_state()

    with pytest.raises(PersistenceError) as captured:
        store.save(candidate)

    assert captured.value.code == "WRITE_NOT_AUTHORIZED"
    assert to_dict(store.load()) == to_dict(before)


def test_coordinated_certified_and_completed_forgery_is_refused(
    tmp_path: Path,
) -> None:
    project_store = ProjectStateStore(tmp_path)
    project_before = project_store.initialize()
    mission_store = MissionStateStore(tmp_path)
    mission_before = mission_store.initialize(
        MissionState(
            schema_version="1.0",
            mission_id="P2.10-R2",
            workflow_generation=0,
            status=MissionStatus.ACTIVE,
            role=MissionRole.ORCHESTRATOR,
            objective="Prevent coordinated authoritative state forgery.",
            subject="B2",
            operating_step=OperatingStep.RECONSTRUCT,
            next_action="Run the controlled workflow.",
            observed_commit=COMMIT,
            updated_at=NOW,
        )
    )
    forged_project = full_state()
    forged_mission = replace(
        mission_before,
        status=MissionStatus.COMPLETED,
        role=MissionRole.CERTIFIER,
        operating_step=OperatingStep.REPORT,
        next_action="Report forged completion.",
    )

    with pytest.raises(PersistenceError, match="WRITE_NOT_AUTHORIZED"):
        project_store.save(forged_project)
    with pytest.raises(PersistenceError, match="WRITE_NOT_AUTHORIZED"):
        mission_store.save(forged_mission)

    assert to_dict(project_store.load()) == to_dict(project_before)
    assert to_dict(mission_store.load()) == to_dict(mission_before)


def test_serialization_is_deterministic_utf8(tmp_path: Path) -> None:
    store = ProjectStateStore(tmp_path)
    state = full_state()

    authorized_save(store, state)
    first = store.state_path.read_bytes()
    authorized_save(store, state)
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
    store.initialize()
    updated = full_state()
    authorized_save(store, updated)

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
    store.initialize()

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
    store.initialize()
    previous = store.state_path.read_bytes()
    invalid = full_state()
    invalid.evidence.append(invalid.evidence[0])

    with pytest.raises(PersistenceError):
        store.save(invalid)

    assert store.state_path.read_bytes() == previous
    assert to_dict(store.load()) == to_dict(original)


def test_direct_certified_mutation_without_certification_is_refused(
    tmp_path: Path,
) -> None:
    store = ProjectStateStore(tmp_path)
    original = ProjectState(
        schema_version="1.0",
        user_stories=[user_story(status=UserStoryStatus.CERTIFICATION)],
    )
    authorized_save(store, original)
    previous = store.state_path.read_bytes()
    bypass = store.load()
    bypass.user_stories[0].status = UserStoryStatus.CERTIFIED

    with pytest.raises(PersistenceError) as captured:
        store.save(bypass)

    assert captured.value.code == "INVALID_STATE_INTEGRITY"
    assert "US-0001" in captured.value.message
    assert store.state_path.read_bytes() == previous
    assert store.load().user_stories[0].status is UserStoryStatus.CERTIFICATION


def test_load_refuses_tampered_certified_status_without_modifying_file(
    tmp_path: Path,
) -> None:
    store = ProjectStateStore(tmp_path)
    authorized_save(
        store,
        ProjectState(
            schema_version="1.0",
            user_stories=[user_story(status=UserStoryStatus.CERTIFICATION)],
        )
    )
    candidate = json.loads(store.state_path.read_text(encoding="utf-8"))
    candidate["user_stories"][0]["status"] = "CERTIFIED"
    write_json(store.state_path, candidate)
    tampered = store.state_path.read_bytes()

    with pytest.raises(PersistenceError) as captured:
        store.load()

    assert captured.value.code == "INVALID_STATE_INTEGRITY"
    assert "US-0001" in captured.value.message
    assert store.state_path.read_bytes() == tampered


@pytest.mark.parametrize(
    "result",
    (None, CertificationResult.BLOCKED, CertificationResult.REJECTED),
    ids=("missing", "blocked", "rejected"),
)
def test_non_certifying_record_cannot_back_a_certified_status(
    tmp_path: Path,
    result: CertificationResult | None,
) -> None:
    if result is None:
        state = ProjectState(schema_version="1.0", user_stories=[user_story()])
    else:
        state = full_state()
        state.certifications[0] = replace(state.certifications[0], result=result)
    store = ProjectStateStore(tmp_path)

    with pytest.raises(PersistenceError) as captured:
        store.save(state)

    assert captured.value.code == "INVALID_STATE_INTEGRITY"
    assert "US-0001" in captured.value.message
    assert not store.state_path.exists()


def test_certification_for_another_story_cannot_back_certified_status(
    tmp_path: Path,
) -> None:
    state = full_state()
    second_story = user_story("US-0002", status=UserStoryStatus.CERTIFICATION)
    second_story.required_gates = ()
    state.user_stories.append(second_story)
    state.certifications[0] = replace(
        state.certifications[0],
        subject="US-0002",
        gate_results={},
        evidence_refs=("EV-AC-001",),
    )
    store = ProjectStateStore(tmp_path)

    with pytest.raises(PersistenceError) as captured:
        store.save(state)

    assert captured.value.code == "INVALID_STATE_INTEGRITY"
    assert "US-0001" in captured.value.message


@pytest.mark.parametrize(
    "status",
    tuple(
        status
        for status in UserStoryStatus
        if status is not UserStoryStatus.CERTIFIED
    ),
)
def test_non_certified_statuses_do_not_require_certification(
    tmp_path: Path,
    status: UserStoryStatus,
) -> None:
    store = ProjectStateStore(tmp_path)
    state = ProjectState(
        schema_version="1.0",
        user_stories=[user_story(status=status)],
    )

    authorized_save(store, state)

    assert store.load().user_stories[0].status is status


def test_certified_status_with_applicable_certification_round_trips(
    tmp_path: Path,
) -> None:
    store = ProjectStateStore(tmp_path)
    state = full_state()

    authorized_save(store, state)

    assert store.load().user_stories[0].status is UserStoryStatus.CERTIFIED


def test_authorized_not_applicable_gate_round_trips_exactly(tmp_path: Path) -> None:
    store = ProjectStateStore(tmp_path)
    state = not_applicable_state()

    authorized_save(store, state)
    reloaded = store.load()

    assert reloaded.gates[0].result is GateResult.NOT_APPLICABLE
    assert reloaded.certifications[0].authorized_not_applicable_gates == (
        "GATE-001",
    )


def test_legacy_not_applicable_dossier_without_authority_field_is_refused(
    tmp_path: Path,
) -> None:
    store = ProjectStateStore(tmp_path)
    authorized_save(store, not_applicable_state())
    candidate = json.loads(store.state_path.read_text(encoding="utf-8"))
    del candidate["certifications"][0]["authorized_not_applicable_gates"]
    write_json(store.state_path, candidate)

    with pytest.raises(PersistenceError) as captured:
        store.load()

    assert captured.value.code == "INVALID_SCHEMA"


def test_unknown_not_applicable_authority_is_refused(tmp_path: Path) -> None:
    state = full_state()
    state.certifications[0] = replace(
        state.certifications[0],
        authorized_not_applicable_gates=("GATE-UNKNOWN",),
    )

    with pytest.raises(PersistenceError) as captured:
        ProjectStateStore(tmp_path).save(state)

    assert captured.value.code == "INVALID_CERTIFICATION_INTEGRITY"
    assert "NOT_APPLICABLE_AUTHORITY_UNKNOWN" in captured.value.message


def test_authority_for_the_wrong_required_gate_is_refused(tmp_path: Path) -> None:
    state = not_applicable_state(authorities=("GATE-002",))
    state.user_stories[0].required_gates = ("GATE-001", "GATE-002")
    state.gates.append(gate(gate_id="GATE-002"))
    state.certifications[0] = replace(
        state.certifications[0],
        gate_results={
            "GATE-001": "NOT_APPLICABLE",
            "GATE-002": "PASS",
        },
        evidence_refs=("EV-AC-001", "EV-001"),
    )

    with pytest.raises(PersistenceError) as captured:
        ProjectStateStore(tmp_path).save(state)

    assert captured.value.code == "INVALID_CERTIFICATION_INTEGRITY"
    assert "NOT_APPLICABLE_AUTHORITY_MISSING" in captured.value.message
    assert "NOT_APPLICABLE_AUTHORITY_UNUSED" in captured.value.message


def test_duplicate_not_applicable_authority_is_refused(tmp_path: Path) -> None:
    state = not_applicable_state(authorities=("GATE-001", "GATE-001"))

    with pytest.raises(PersistenceError) as captured:
        ProjectStateStore(tmp_path).save(state)

    assert captured.value.code == "INVALID_SCHEMA"


@pytest.mark.parametrize("result", (GateResult.UNKNOWN, GateResult.FAIL))
def test_not_applicable_authority_cannot_override_non_satisfying_gate(
    tmp_path: Path,
    result: GateResult,
) -> None:
    state = full_state()
    state.gates[0] = replace(state.gates[0], result=result, evidence_refs=())
    state.certifications[0] = replace(
        state.certifications[0],
        gate_results={"GATE-001": result.value},
        evidence_refs=("EV-AC-001",),
        authorized_not_applicable_gates=("GATE-001",),
    )

    with pytest.raises(PersistenceError) as captured:
        ProjectStateStore(tmp_path).save(state)

    assert captured.value.code == "INVALID_CERTIFICATION_INTEGRITY"
    assert "REQUIRED_GATE_NOT_SATISFIED" in captured.value.message
    assert "NOT_APPLICABLE_AUTHORITY_UNUSED" in captured.value.message


def test_multiple_compatible_certifications_do_not_invalidate_state(
    tmp_path: Path,
) -> None:
    store = ProjectStateStore(tmp_path)
    state = full_state()
    state.certifications.append(
        replace(certification(), certification_id="CERT-002")
    )

    authorized_save(store, state)

    assert len(store.load().certifications) == 2


def test_contradictory_certifications_make_certified_status_ambiguous(
    tmp_path: Path,
) -> None:
    store = ProjectStateStore(tmp_path)
    state = full_state()
    state.certifications.append(
        replace(
            certification(),
            certification_id="CERT-BLOCKED",
            result=CertificationResult.BLOCKED,
        )
    )

    with pytest.raises(PersistenceError) as captured:
        store.save(state)

    assert captured.value.code == "INVALID_STATE_INTEGRITY"
    assert "US-0001" in captured.value.message
    assert "contradictory" in captured.value.message


def test_fabricated_complete_looking_certification_references_are_refused(
    tmp_path: Path,
) -> None:
    state = ProjectState(
        schema_version="1.0",
        user_stories=[user_story(status=UserStoryStatus.CERTIFICATION)],
        certifications=[
            replace(
                certification(),
                evidence_refs=("EV-INVENTED",),
                gate_results={"GATE-INVENTED": "PASS"},
            )
        ],
    )
    store = ProjectStateStore(tmp_path)

    with pytest.raises(PersistenceError) as captured:
        store.save(state)

    assert captured.value.code == "INVALID_REFERENCE"


def test_forged_fake_human_certified_dossier_is_refused(tmp_path: Path) -> None:
    producer = "Co\u200bdex/FakeHuman"
    state = full_state()
    story = state.user_stories[0]
    story.human_approval.required = True
    story.human_approval.approved = True
    story.human_approval.approved_by = producer
    story.human_approval.approved_at = NOW
    state.evidence.append(
        Evidence(
            evidence_id="EV-FAKE-HUMAN",
            evidence_type=EvidenceType.HUMAN_APPROVAL,
            subject=story.id,
            result=True,
            source="Human",
            command=None,
            exit_code=None,
            artifact="forged",
            commit=COMMIT,
            timestamp=NOW,
            producer=producer,
        )
    )
    state.certifications[0] = replace(
        state.certifications[0],
        human_approval={
            "required": True,
            "approved": True,
            "result": "PASS",
            "evidence_ref": "EV-FAKE-HUMAN",
        },
        evidence_refs=(*state.certifications[0].evidence_refs, "EV-FAKE-HUMAN"),
    )
    store = ProjectStateStore(tmp_path)

    with pytest.raises(PersistenceError) as captured:
        store.save(state)

    assert captured.value.code == "INVALID_CERTIFICATION_INTEGRITY"
    assert "HUMAN_EVIDENCE_INVALID" in captured.value.message


def test_certified_dossier_missing_required_gate_is_refused(tmp_path: Path) -> None:
    state = full_state()
    state.gates.clear()
    store = ProjectStateStore(tmp_path)

    with pytest.raises(PersistenceError) as captured:
        store.save(state)

    assert captured.value.code == "INVALID_REFERENCE"


def test_certified_dossier_missing_persisted_evidence_is_refused(
    tmp_path: Path,
) -> None:
    state = full_state()
    state.evidence = [
        item for item in state.evidence if item.evidence_id != "EV-AC-001"
    ]
    store = ProjectStateStore(tmp_path)

    with pytest.raises(PersistenceError) as captured:
        store.save(state)

    assert captured.value.code == "INVALID_REFERENCE"


def test_certified_dossier_wrong_gate_subject_is_refused(tmp_path: Path) -> None:
    state = full_state()
    state.gates[0] = replace(state.gates[0], subject="US-OTHER")
    store = ProjectStateStore(tmp_path)

    with pytest.raises(PersistenceError) as captured:
        store.save(state)

    assert captured.value.code == "INVALID_CERTIFICATION_INTEGRITY"
    assert "REQUIRED_GATE_MISMATCH" in captured.value.message


def test_certified_dossier_wrong_evidence_commit_is_refused(tmp_path: Path) -> None:
    state = full_state()
    state.evidence[0] = replace(state.evidence[0], commit=OTHER_COMMIT)
    store = ProjectStateStore(tmp_path)

    with pytest.raises(PersistenceError) as captured:
        store.save(state)

    assert captured.value.code == "INVALID_CERTIFICATION_INTEGRITY"
    assert "EVIDENCE_COMMIT_MISMATCH" in captured.value.message


def test_certified_dossier_requires_attributable_certifier(tmp_path: Path) -> None:
    state = full_state()
    state.certifications[0] = replace(state.certifications[0], certifier="/")
    store = ProjectStateStore(tmp_path)

    with pytest.raises(PersistenceError) as captured:
        store.save(state)

    assert captured.value.code == "INVALID_CERTIFICATION_INTEGRITY"
    assert "CERTIFIER_NOT_ATTRIBUTABLE" in captured.value.message


def test_fake_gate_and_evidence_cannot_complete_certified_dossier(
    tmp_path: Path,
) -> None:
    state = full_state()
    state.evidence[1] = replace(
        state.evidence[1],
        evidence_type=EvidenceType.TEST_RESULT,
        subject="US-0001",
    )
    store = ProjectStateStore(tmp_path)

    with pytest.raises(PersistenceError) as captured:
        store.save(state)

    assert captured.value.code == "INVALID_CERTIFICATION_INTEGRITY"
    assert "ACCEPTANCE_EVIDENCE_INVALID" in captured.value.message


def test_manual_json_tampering_cannot_remove_certification_dossier(
    tmp_path: Path,
) -> None:
    store = ProjectStateStore(tmp_path)
    authorized_save(store, full_state())
    candidate = json.loads(store.state_path.read_text(encoding="utf-8"))
    candidate["certifications"][0]["evidence_refs"] = []
    write_json(store.state_path, candidate)
    tampered = store.state_path.read_bytes()

    with pytest.raises(PersistenceError) as captured:
        store.load()

    assert captured.value.code == "INVALID_CERTIFICATION_INTEGRITY"
    assert store.state_path.read_bytes() == tampered


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
        authorized_save(store, full_state())

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
        authorized_save(store, full_state())

    assert captured.value.code == "WRITE_FAILED"
    assert store.state_path.read_bytes() == previous
    assert list(store.state_path.parent.glob(".state.*.tmp")) == []


def test_initialize_never_overwrites_existing_state(tmp_path: Path) -> None:
    store = ProjectStateStore(tmp_path)
    authorized_save(store, full_state())
    previous = store.state_path.read_bytes()

    with pytest.raises(PersistenceError) as captured:
        store.initialize()

    assert captured.value.code == "STATE_ALREADY_EXISTS"
    assert store.state_path.read_bytes() == previous
