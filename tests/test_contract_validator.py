import json
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    ContractValidator,
    ParseError,
    ValidationError,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures"
SCHEMA_DIR = ROOT / "schemas"

VALID_FIXTURES = [
    ("user-story", "user-story.json"),
    ("evidence", "evidence.json"),
    ("gate", "gate.json"),
    ("audit-event", "audit-event.json"),
    ("certification", "certification.json"),
    ("implementer-result", "implementer-result.json"),
]

INVALID_FIXTURES = [
    ("user-story", "user-story-unknown-status.json"),
    ("user-story", "user-story-invalid-priority.json"),
    ("user-story", "user-story-duplicate-dependencies.json"),
    ("gate", "gate-unknown-result.json"),
    ("certification", "certification-unknown-verdict.json"),
    ("implementer-result", "implementer-result-certified.json"),
]


def load_fixture(category: str, filename: str) -> dict[str, object]:
    with (FIXTURE_DIR / category / filename).open(encoding="utf-8") as stream:
        return json.load(stream)


@pytest.mark.parametrize(("contract", "filename"), VALID_FIXTURES)
def test_valid_phase_0_fixtures_pass(contract: str, filename: str) -> None:
    result = ContractValidator().validate(contract, load_fixture("valid", filename))

    assert result.is_valid
    assert result.contract == contract
    assert result.errors == ()


@pytest.mark.parametrize(("contract", "filename"), INVALID_FIXTURES)
def test_invalid_phase_0_fixtures_fail(contract: str, filename: str) -> None:
    result = ContractValidator().validate(contract, load_fixture("invalid", filename))

    assert not result.is_valid
    assert result.contract == contract
    assert result.errors


def test_unknown_contract_fails_explicitly() -> None:
    with pytest.raises(ValidationError, match="unknown contract type") as captured:
        ContractValidator().validate("unknown", {})

    assert captured.value.contract == "unknown"


def test_user_story_cannot_depend_on_itself() -> None:
    candidate = load_fixture("valid", "user-story.json")
    candidate["depends_on"] = [candidate["id"]]

    result = ContractValidator().validate("user-story", candidate)

    assert not result.is_valid
    assert [error.code for error in result.errors] == ["SELF_DEPENDENCY"]
    assert result.errors[0].path == ("depends_on",)


def test_acceptance_criterion_ids_must_be_unique() -> None:
    candidate = load_fixture("valid", "user-story.json")
    candidate["acceptance_criteria"].append(
        {
            "id": "AC-001",
            "description": "A different criterion with the same identifier.",
            "mandatory": False,
        }
    )

    result = ContractValidator().validate("user-story", candidate)

    assert not result.is_valid
    assert [error.code for error in result.errors] == [
        "DUPLICATE_ACCEPTANCE_CRITERION_ID"
    ]


def test_missing_required_schema_fails_explicitly(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="schema cannot be resolved"):
        ContractValidator(tmp_path).validate("gate", {})


def test_invalid_draft_2020_12_schema_fails_explicitly(tmp_path: Path) -> None:
    (tmp_path / "gate.schema.json").write_text(
        json.dumps({"type": "not-a-json-schema-type"}), encoding="utf-8"
    )

    with pytest.raises(ValidationError, match="not valid Draft 2020-12"):
        ContractValidator(tmp_path).validate("gate", {})


def test_malformed_schema_json_fails_explicitly(tmp_path: Path) -> None:
    (tmp_path / "gate.schema.json").write_text("{", encoding="utf-8")

    with pytest.raises(ParseError, match="schema is not valid JSON"):
        ContractValidator(tmp_path).validate("gate", {})


def test_structurally_invalid_data_returns_explainable_errors() -> None:
    result = ContractValidator().validate("evidence", {"evidence_id": "EV-0001"})

    assert not result.is_valid
    assert result.errors
    assert all(error.code == "SCHEMA_VIOLATION" for error in result.errors)
    assert any("required property" in error.message for error in result.errors)


def test_unknown_enum_value_is_rejected_at_its_path() -> None:
    candidate = load_fixture("valid", "gate.json")
    candidate["result"] = "MAGIC"

    result = ContractValidator().validate("gate", candidate)

    assert not result.is_valid
    assert result.errors[0].code == "SCHEMA_VIOLATION"
    assert result.errors[0].path == ("result",)


def test_non_json_candidate_data_is_rejected() -> None:
    candidate = load_fixture("valid", "audit-event.json")
    candidate["payload"] = {"unsupported": object()}

    result = ContractValidator().validate("audit-event", candidate)

    assert not result.is_valid
    assert result.errors[0].code == "INVALID_JSON_DATA"
    assert result.errors[0].path == ("payload", "unsupported")


class ExplodingMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise RuntimeError("unavailable candidate data")

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError("unavailable candidate data")

    def __len__(self) -> int:
        return 1


def test_unexpected_error_never_becomes_success() -> None:
    with pytest.raises(ValidationError, match="validation could not be completed"):
        ContractValidator().validate("gate", ExplodingMapping())


def test_validation_requires_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr("socket.create_connection", reject_network)

    result = ContractValidator().validate(
        "certification", load_fixture("valid", "certification.json")
    )

    assert result.is_valid


def test_repeated_validation_is_deterministic() -> None:
    candidate = load_fixture("invalid", "user-story-invalid-priority.json")
    validator = ContractValidator(SCHEMA_DIR)

    assert validator.validate("user-story", candidate) == validator.validate(
        "user-story", candidate
    )
