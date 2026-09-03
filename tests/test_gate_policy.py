from dataclasses import replace
from datetime import datetime, timezone

import pytest

from agentic_engineering_os.application import (
    GatePolicyResolutionError,
    instantiate_gate_id,
    resolve_story_policies,
)
from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    HumanApproval,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
)
from agentic_engineering_os.infrastructure import ProjectConfigurationValidator

from test_project_configuration import valid_candidate


def story(
    identifier: str,
    *,
    required_gates: tuple[str, ...] | None = None,
) -> UserStory:
    now = datetime(2026, 9, 3, tzinfo=timezone.utc)
    gates = required_gates or (instantiate_gate_id("tests", identifier),)
    return UserStory(
        schema_version="1.0",
        id=identifier,
        title=f"Verify {identifier}",
        description="Exercise the configured verification policy.",
        status=UserStoryStatus.READY,
        priority=1,
        risk=RiskLevel.MEDIUM,
        depends_on=(),
        scope=UserStoryScope(allowed_paths=("src",), forbidden_paths=()),
        acceptance_criteria=(
            AcceptanceCriterion(
                id="AC-001",
                description="The configured verification passes.",
                mandatory=True,
            ),
        ),
        required_gates=gates,
        human_approval=HumanApproval(
            required=False,
            approved=False,
            approved_by=None,
            approved_at=None,
        ),
        metadata=UserStoryMetadata(
            created_at=now,
            created_by="Codex/Architect",
            updated_at=now,
        ),
    )


def configuration():
    return ProjectConfigurationValidator().validate(valid_candidate())


def test_gate_identity_is_scoped_to_policy_and_story() -> None:
    assert instantiate_gate_id("tests", "US-0001") == "tests::US-0001"


def test_one_policy_produces_distinct_instances_for_two_stories() -> None:
    first = resolve_story_policies(configuration(), story("US-0001"))
    second = resolve_story_policies(configuration(), story("US-0002"))

    assert first[0].gate_id == "tests::US-0001"
    assert second[0].gate_id == "tests::US-0002"
    assert first[0].gate_id != second[0].gate_id
    assert first[0].verification_commands[0].command_id == "tests"


@pytest.mark.parametrize(
    "gate_id",
    [
        "GATE-TESTS",
        "unknown::US-0001",
        "tests::US-0002",
        "TESTS::US-0001",
    ],
)
def test_unknown_raw_case_variant_or_cross_story_gate_is_refused(gate_id: str) -> None:
    with pytest.raises(GatePolicyResolutionError) as captured:
        resolve_story_policies(
            configuration(),
            story("US-0001", required_gates=(gate_id,)),
        )

    assert captured.value.code == "UNKNOWN_GATE_POLICY_INSTANCE"


def test_required_policy_instance_cannot_be_omitted() -> None:
    candidate = story("US-0001")
    candidate.required_gates = ()

    with pytest.raises(GatePolicyResolutionError) as captured:
        resolve_story_policies(configuration(), candidate)

    assert captured.value.code == "REQUIRED_GATE_POLICY_MISSING"


def test_optional_policy_is_resolved_only_when_story_names_its_instance() -> None:
    candidate = valid_candidate()
    candidate["gate_policies"].insert(  # type: ignore[union-attr]
        0,
        {
            "policy_id": "optional",
            "verification_command_ids": ["tests"],
            "aggregation": "ALL_REQUIRED_PASS",
            "required": False,
            "repository_dependent": True,
        },
    )
    config = ProjectConfigurationValidator().validate(candidate)
    user_story = story(
        "US-0001",
        required_gates=("optional::US-0001", "tests::US-0001"),
    )

    assert tuple(
        item.gate_id for item in resolve_story_policies(config, user_story)
    ) == ("optional::US-0001", "tests::US-0001")
    user_story.required_gates = ("tests::US-0001",)
    assert tuple(
        item.gate_id for item in resolve_story_policies(config, user_story)
    ) == ("tests::US-0001",)


def test_historical_configuration_blocks_automatic_required_gate_resolution() -> None:
    historical = replace(configuration(), gate_policies=())

    with pytest.raises(GatePolicyResolutionError) as captured:
        resolve_story_policies(historical, story("US-0001"))

    assert captured.value.code == "GATE_POLICY_MISSING"


@pytest.mark.parametrize(
    ("policy_id", "story_id", "code"),
    [
        ("tests:unsafe", "US-0001", "INVALID_POLICY_ID"),
        (" tests", "US-0001", "INVALID_POLICY_ID"),
        ("tests", "US-1", "INVALID_USER_STORY_ID"),
    ],
)
def test_gate_identity_rejects_ambiguous_components(
    policy_id: str, story_id: str, code: str
) -> None:
    with pytest.raises(GatePolicyResolutionError) as captured:
        instantiate_gate_id(policy_id, story_id)

    assert captured.value.code == code
