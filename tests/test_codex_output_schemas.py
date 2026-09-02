from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from agentic_engineering_os.application import ContractValidator
from agentic_engineering_os.application.codex_output_schema import (
    CodexOutputSchemaError,
    build_codex_output_schema,
    codex_output_schema_path,
)
from agentic_engineering_os.domain import MissionRole


ROOT = Path(__file__).resolve().parents[1]
ROLES = (
    MissionRole.ARCHITECT,
    MissionRole.IMPLEMENTER,
    MissionRole.TESTER,
    MissionRole.REVIEWER,
    MissionRole.CERTIFIER,
)
FORBIDDEN_TRANSPORT_KEYWORDS = frozenset(
    {"allOf", "anyOf", "oneOf", "if", "then", "else", "not", "contains", "uniqueItems", "$ref"}
)


def load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def names(role: MissionRole) -> tuple[str, str]:
    stem = f"{role.value.casefold()}-result"
    return f"{stem}.schema.json", f"{stem}.codex.schema.json"


def transport_validator(role: MissionRole) -> Draft202012Validator:
    schema = load(codex_output_schema_path(role))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.mark.parametrize("role", ROLES)
def test_packaged_transport_schema_is_exact_generated_projection(role: MissionRole) -> None:
    canonical_name, transport_name = names(role)
    source_canonical = load(ROOT / "schemas" / canonical_name)
    user_story = load(ROOT / "schemas" / "user-story.schema.json")
    generated = build_codex_output_schema(
        source_canonical,
        external_schemas={str(user_story["$id"]): user_story},
    )
    source_transport = load(ROOT / "schemas" / transport_name)
    packaged_path = codex_output_schema_path(role)

    assert source_transport == generated
    assert packaged_path.read_bytes() == (ROOT / "schemas" / transport_name).read_bytes()


@pytest.mark.parametrize("role", ROLES)
def test_transport_schema_uses_closed_codex_subset(role: MissionRole) -> None:
    schema = load(codex_output_schema_path(role))

    def inspect(node: object) -> None:
        if isinstance(node, list):
            for item in node:
                inspect(item)
            return
        if not isinstance(node, dict):
            return
        assert FORBIDDEN_TRANSPORT_KEYWORDS.isdisjoint(node)
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False
            assert set(node["required"]) == set(node["properties"])
        if "enum" in node:
            assert "type" in node
        for value in node.values():
            inspect(value)

    inspect(schema)


@pytest.mark.parametrize("role", ROLES)
def test_valid_role_payload_passes_transport_and_canonical_contract(role: MissionRole) -> None:
    canonical_name, _ = names(role)
    fixture_name = canonical_name.replace(".schema.json", ".json")
    payload = load(ROOT / "tests" / "fixtures" / "valid" / fixture_name)

    transport_validator(role).validate(payload)
    assert ContractValidator().validate(canonical_name.removesuffix(".schema.json"), payload).is_valid


@pytest.mark.parametrize("role", ROLES)
def test_transport_and_canonical_reject_common_adversarial_shapes(role: MissionRole) -> None:
    canonical_name, _ = names(role)
    contract = canonical_name.removesuffix(".schema.json")
    fixture_name = canonical_name.replace(".schema.json", ".json")
    payload = load(ROOT / "tests" / "fixtures" / "valid" / fixture_name)
    malformed_nested = {
        MissionRole.ARCHITECT: ("decisions", [{}]),
        MissionRole.IMPLEMENTER: ("verification_results", [{}]),
        MissionRole.TESTER: ("test_plan", {}),
        MissionRole.REVIEWER: ("findings", [{}]),
        MissionRole.CERTIFIER: ("human_approval_check", {}),
    }[role]
    variants = []
    missing = deepcopy(payload)
    missing.pop("summary")
    variants.append(missing)
    variants.append({**deepcopy(payload), "unexpected": True})
    variants.append({**deepcopy(payload), "verdict": "CERTIFIED"})
    variants.append({**deepcopy(payload), "workflow_generation": "zero"})
    variants.append({**deepcopy(payload), "role": "ORCHESTRATOR"})
    variants.append({**deepcopy(payload), malformed_nested[0]: malformed_nested[1]})

    validator = transport_validator(role)
    for candidate in variants:
        assert list(validator.iter_errors(candidate))
        assert not ContractValidator().validate(contract, candidate).is_valid


@pytest.mark.parametrize("role", ROLES)
def test_transport_only_breadth_cannot_bypass_canonical_semantics(role: MissionRole) -> None:
    canonical_name, _ = names(role)
    contract = canonical_name.removesuffix(".schema.json")
    fixture_name = canonical_name.replace(".schema.json", ".json")
    candidate = load(ROOT / "tests" / "fixtures" / "valid" / fixture_name)
    if role is MissionRole.ARCHITECT:
        candidate["blockers"] = ["contradicts READY"]
    elif role is MissionRole.IMPLEMENTER:
        candidate["files_changed"] = []
    elif role is MissionRole.TESTER:
        candidate["acceptance_results"] = []
    elif role is MissionRole.REVIEWER:
        candidate["dimensions_reviewed"] = []
    else:
        candidate["blockers"] = ["contradicts READY_FOR_CONTROL_PLANE"]

    transport_validator(role).validate(candidate)
    assert not ContractValidator().validate(contract, candidate).is_valid


def test_transport_rejects_certifier_self_authority_escalation() -> None:
    payload = load(ROOT / "tests" / "fixtures" / "valid" / "certifier-result.json")
    payload["verdict"] = "CERTIFIED"

    assert list(transport_validator(MissionRole.CERTIFIER).iter_errors(payload))


def test_conflicting_reference_siblings_fail_closed() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["value"],
        "properties": {
            "value": {"$ref": "#/$defs/value", "minLength": 2},
        },
        "$defs": {"value": {"type": "string", "minLength": 1}},
    }

    with pytest.raises(CodexOutputSchemaError, match="conflicting constraint"):
        build_codex_output_schema(schema)
