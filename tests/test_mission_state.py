import json
from datetime import datetime
from pathlib import Path

import pytest

from agentic_engineering_os.application import ContractValidator
from agentic_engineering_os.domain import (
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    to_dict,
)


ROOT = Path(__file__).resolve().parents[1]
COMMIT = "fa231554666b7f5dd8fd15a609a4d6f96e59fe41"
UPDATED_AT = datetime.fromisoformat("2026-08-28T10:00:00+02:00")


def mission_state(**overrides: object) -> MissionState:
    values: dict[str, object] = {
        "schema_version": "1.0",
        "mission_id": "P2.2",
        "workflow_generation": 0,
        "status": MissionStatus.ACTIVE,
        "role": MissionRole.IMPLEMENTER,
        "objective": "Persister la mémoire opérationnelle de mission.",
        "subject": "Mission P2.2",
        "operating_step": OperatingStep.ACT,
        "next_action": "Implémenter le stockage.",
        "observed_commit": COMMIT,
        "updated_at": UPDATED_AT,
    }
    values.update(overrides)
    return MissionState(**values)  # type: ignore[arg-type]


def test_mission_status_values_are_canonical() -> None:
    assert [member.value for member in MissionStatus] == [
        "ACTIVE",
        "BLOCKED",
        "COMPLETED",
        "CANCELLED",
    ]


def test_all_six_roles_validate() -> None:
    validator = ContractValidator()

    for role in MissionRole:
        assert validator.validate(
            "mission-state", to_dict(mission_state(role=role))
        ).is_valid

    assert len(MissionRole) == 6


def test_all_nine_operating_steps_validate() -> None:
    validator = ContractValidator()

    for step in OperatingStep:
        assert validator.validate(
            "mission-state", to_dict(mission_state(operating_step=step))
        ).is_valid

    assert [member.value for member in OperatingStep] == [
        "RECONSTRUCT",
        "PREFLIGHT",
        "UNDERSTAND_CONTRACT",
        "PROVE_READINESS",
        "ACT",
        "VERIFY",
        "RECORD_EVIDENCE",
        "CONTROLLED_TRANSITION",
        "REPORT",
    ]


def test_blockers_are_independent_between_mutable_instances() -> None:
    first = mission_state(mission_id="P2.2-A")
    second = mission_state(mission_id="P2.2-B")

    first.blockers.append("A real blocker")

    assert first.blockers == ["A real blocker"]
    assert second.blockers == []


def test_blocked_state_requires_an_explicit_reason() -> None:
    result = ContractValidator().validate(
        "mission-state", to_dict(mission_state(status=MissionStatus.BLOCKED))
    )

    assert not result.is_valid
    assert any(issue.path == ("blockers",) for issue in result.errors)


def test_operational_fields_round_trip_to_json_values() -> None:
    candidate = mission_state(
        blockers=["Attendre une décision humaine"],
        next_action="Demander une décision.",
    )

    serialized = to_dict(candidate)

    assert serialized["next_action"] == "Demander une décision."
    assert serialized["workflow_generation"] == 0
    assert serialized["observed_commit"] == COMMIT
    assert serialized["updated_at"] == UPDATED_AT.isoformat()
    assert serialized["blockers"] == ["Attendre une décision humaine"]
    json.dumps(serialized, ensure_ascii=False)


@pytest.mark.parametrize("value", [-1, True, None, "1"])
def test_workflow_generation_must_be_a_non_negative_integer(value: object) -> None:
    candidate = to_dict(mission_state())
    candidate["workflow_generation"] = value

    assert not ContractValidator().validate("mission-state", candidate).is_valid


def test_schema_refuses_unexpected_properties() -> None:
    candidate = to_dict(mission_state())
    candidate["certification_result"] = "CERTIFIED"

    result = ContractValidator().validate("mission-state", candidate)

    assert not result.is_valid


def test_contract_validator_refuses_invalid_timestamp() -> None:
    candidate = to_dict(mission_state())
    candidate["updated_at"] = "not-a-timestamp"

    result = ContractValidator().validate("mission-state", candidate)

    assert not result.is_valid
    assert result.errors[0].code == "INVALID_TIMESTAMP"


def test_mission_schema_is_draft_2020_12() -> None:
    schema = json.loads(
        (ROOT / "schemas" / "mission-state.schema.json").read_text(encoding="utf-8")
    )

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
