import json
from dataclasses import FrozenInstanceError, fields
from datetime import datetime
from enum import Enum
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

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


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas"
TIMESTAMP = datetime.fromisoformat("2026-08-27T10:00:00+02:00")
COMMIT = "ed6407590d09f591a37384fb939b9e9a5d55394a"


def enum_values(enum_type: type[Enum]) -> list[str]:
    return [member.value for member in enum_type]


def load_schema(name: str) -> dict[str, object]:
    with (SCHEMA_DIR / name).open(encoding="utf-8") as stream:
        return json.load(stream)


def validate(schema_name: str, model: object) -> None:
    validator = Draft202012Validator(
        load_schema(schema_name), format_checker=FormatChecker()
    )
    validator.validate(to_dict(model))


def make_user_story() -> UserStory:
    return UserStory(
        schema_version="1.0",
        id="US-0001",
        title="Canonical domain models",
        description="Represent the certified contracts as Python data.",
        status=UserStoryStatus.PROPOSED,
        priority=1,
        risk=RiskLevel.LOW,
        depends_on=(),
        scope=UserStoryScope(
            allowed_paths=("src/agentic_engineering_os/domain/",),
            forbidden_paths=("schemas/",),
        ),
        acceptance_criteria=(
            AcceptanceCriterion(
                id="AC-001",
                description="The domain models can be constructed.",
                mandatory=True,
            ),
        ),
        required_gates=("GATE-0001",),
        human_approval=HumanApproval(
            required=False,
            approved=False,
            approved_by=None,
            approved_at=None,
        ),
        metadata=UserStoryMetadata(
            created_at=TIMESTAMP,
            created_by="human-operator",
            updated_at=TIMESTAMP,
        ),
    )


def make_evidence() -> Evidence:
    return Evidence(
        evidence_id="EV-0001",
        evidence_type=EvidenceType.TEST_RESULT,
        subject="US-0001",
        result="1 passed",
        source="pytest",
        command="python -m pytest -q",
        exit_code=0,
        artifact="captured pytest output",
        commit=COMMIT,
        timestamp=TIMESTAMP,
        producer="Codex/Tester",
    )


def make_gate() -> Gate:
    return Gate(
        gate_id="GATE-0001",
        subject="US-0001/AC-001",
        required=True,
        result=GateResult.PASS,
        evidence_refs=("EV-0001",),
        evaluated_at=TIMESTAMP,
        evaluator="Codex/Tester",
    )


def make_audit_event() -> AuditEvent:
    return AuditEvent(
        event_id="EVENT-0001",
        timestamp=TIMESTAMP,
        event_type=AuditEventType.GATE_EVALUATED,
        subject="US-0001/GATE-0001",
        actor="Codex",
        role="Tester",
        repository_commit=COMMIT,
        payload={"gate_id": "GATE-0001", "result": "PASS"},
    )


def make_certification() -> Certification:
    return Certification(
        certification_id="CERT-0001",
        subject="US-0001",
        result=CertificationResult.CERTIFIED,
        commit=COMMIT,
        acceptance_results={"AC-001": "PASS"},
        gate_results={"GATE-0001": "PASS"},
        human_approval={
            "required": False,
            "approved": False,
            "evidence_ref": None,
        },
        evidence_refs=("EV-0001",),
        certified_at=TIMESTAMP,
        certifier="Codex/Certifier",
    )


def test_enums_match_canonical_schemas_exactly() -> None:
    user_story = load_schema("user-story.schema.json")
    evidence = load_schema("evidence.schema.json")
    gate = load_schema("gate.schema.json")
    audit_event = load_schema("audit-event.schema.json")
    certification = load_schema("certification.schema.json")

    assert enum_values(UserStoryStatus) == user_story["properties"]["status"]["enum"]
    assert enum_values(RiskLevel) == user_story["properties"]["risk"]["enum"]
    assert enum_values(EvidenceType) == evidence["properties"]["evidence_type"]["enum"]
    assert enum_values(GateResult) == gate["properties"]["result"]["enum"]
    assert enum_values(AuditEventType) == audit_event["properties"]["event_type"]["enum"]
    assert (
        enum_values(CertificationResult)
        == certification["properties"]["result"]["enum"]
    )


@pytest.mark.parametrize(
    "enum_type",
    [
        UserStoryStatus,
        RiskLevel,
        EvidenceType,
        GateResult,
        AuditEventType,
        CertificationResult,
    ],
)
def test_unknown_enum_values_fail_closed(enum_type: type[Enum]) -> None:
    with pytest.raises(ValueError):
        enum_type("MAGIC")


def test_models_match_their_phase_0_schemas() -> None:
    validate("user-story.schema.json", make_user_story())
    validate("evidence.schema.json", make_evidence())
    validate("gate.schema.json", make_gate())
    validate("audit-event.schema.json", make_audit_event())
    validate("certification.schema.json", make_certification())


def test_user_story_and_acceptance_criterion_are_constructed() -> None:
    story = make_user_story()

    assert story.id == "US-0001"
    assert story.acceptance_criteria[0] == AcceptanceCriterion(
        id="AC-001",
        description="The domain models can be constructed.",
        mandatory=True,
    )


def test_historical_models_are_frozen() -> None:
    immutable_models = (
        make_evidence(),
        make_gate(),
        make_audit_event(),
        make_certification(),
    )

    for model in immutable_models:
        with pytest.raises(FrozenInstanceError):
            model.subject = "changed"


def test_project_state_defaults_are_independent() -> None:
    first = ProjectState(schema_version="1.0")
    second = ProjectState(schema_version="1.0")

    first.user_stories.append(make_user_story())
    first.evidence.append(make_evidence())

    assert second.user_stories == []
    assert second.evidence == []
    for field_name in (
        "user_stories",
        "evidence",
        "gates",
        "certifications",
        "audit_events",
    ):
        assert getattr(first, field_name) is not getattr(second, field_name)


def test_serialization_is_json_compatible_and_deterministic() -> None:
    first = to_dict(make_certification())
    second = to_dict(make_certification())

    assert first == second
    assert first["result"] == "CERTIFIED"
    assert first["certified_at"] == "2026-08-27T10:00:00+02:00"
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_model_fields_match_canonical_root_fields() -> None:
    pairs = (
        (UserStory, "user-story.schema.json"),
        (Evidence, "evidence.schema.json"),
        (Gate, "gate.schema.json"),
        (AuditEvent, "audit-event.schema.json"),
        (Certification, "certification.schema.json"),
    )

    for model_type, schema_name in pairs:
        schema = load_schema(schema_name)
        assert [item.name for item in fields(model_type)] == schema["required"]


def test_to_dict_rejects_non_dataclass_values() -> None:
    with pytest.raises(TypeError):
        to_dict(object())
