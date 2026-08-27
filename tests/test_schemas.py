import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas"
FIXTURE_DIR = Path(__file__).parent / "fixtures"

SCHEMA_FILES = {
    "user-story": "user-story.schema.json",
    "evidence": "evidence.schema.json",
    "gate": "gate.schema.json",
    "audit-event": "audit-event.schema.json",
    "certification": "certification.schema.json",
    "project-state": "project-state.schema.json",
    "mission-state": "mission-state.schema.json",
    "architect-result": "architect-result.schema.json",
}

VALID_FIXTURES = [(name, f"{name}.json") for name in SCHEMA_FILES]

INVALID_FIXTURES = [
    ("user-story", "user-story-unknown-status.json", ["status"]),
    ("user-story", "user-story-invalid-priority.json", ["priority"]),
    ("user-story", "user-story-duplicate-dependencies.json", ["depends_on"]),
    ("gate", "gate-unknown-result.json", ["result"]),
    ("certification", "certification-unknown-verdict.json", ["result"]),
    ("project-state", "project-state-unknown-version.json", ["schema_version"]),
]


def load_json(path: Path) -> object:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def load_schema(name: str) -> dict[str, object]:
    return load_json(SCHEMA_DIR / SCHEMA_FILES[name])


def validator(name: str) -> Draft202012Validator:
    resources = [
        (schema["$id"], Resource.from_contents(schema))
        for schema in (load_schema(item) for item in SCHEMA_FILES)
    ]
    return Draft202012Validator(
        load_schema(name),
        format_checker=FormatChecker(),
        registry=Registry().with_resources(resources),
    )


@pytest.mark.parametrize("schema_file", SCHEMA_FILES.values())
def test_schema_is_valid_draft_2020_12(schema_file: str) -> None:
    schema = load_json(SCHEMA_DIR / schema_file)
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize(("schema_name", "fixture_file"), VALID_FIXTURES)
def test_valid_fixture_passes(schema_name: str, fixture_file: str) -> None:
    instance = load_json(FIXTURE_DIR / "valid" / fixture_file)
    validator(schema_name).validate(instance)


@pytest.mark.parametrize(
    ("schema_name", "fixture_file", "expected_path"), INVALID_FIXTURES
)
def test_invalid_fixture_fails(
    schema_name: str, fixture_file: str, expected_path: list[str]
) -> None:
    instance = load_json(FIXTURE_DIR / "invalid" / fixture_file)
    errors = list(validator(schema_name).iter_errors(instance))
    assert errors
    assert any(list(error.path) == expected_path for error in errors)


def test_canonical_enums_match_phase_0_contracts() -> None:
    user_story = load_schema("user-story")
    gate = load_schema("gate")
    certification = load_schema("certification")

    assert user_story["properties"]["status"]["enum"] == [
        "PROPOSED",
        "PLANNED",
        "BLOCKED",
        "READY",
        "IN_PROGRESS",
        "IMPLEMENTED",
        "TESTING",
        "REVIEW",
        "CERTIFICATION",
        "CERTIFIED",
        "REJECTED",
        "REMEDIATION_REQUIRED",
        "CANCELLED",
    ]
    assert user_story["properties"]["risk"]["enum"] == [
        "LOW",
        "MEDIUM",
        "HIGH",
        "CRITICAL",
    ]
    assert gate["properties"]["result"]["enum"] == [
        "PASS",
        "FAIL",
        "UNKNOWN",
        "NOT_APPLICABLE",
    ]
    assert certification["properties"]["result"]["enum"] == [
        "CERTIFIED",
        "REJECTED",
        "BLOCKED",
    ]
