from datetime import datetime

import pytest

from agentic_engineering_os.application import (
    ArchitectDecision,
    ArchitectDecisionKind,
    ArchitectInput,
    ArchitectInputError,
    ArchitectResult,
    ArchitectResultValidator,
    ArchitectVerdict,
    ContractValidator,
    RoleHandoff,
)
from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    HumanApproval,
    MissionRole,
    OperatingStep,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
    to_dict,
)


COMMIT = "d32e5148e895185668c97626106a4537cb739952"
TIMESTAMP = datetime.fromisoformat("2026-08-28T15:00:00+02:00")


def architect_handoff(**overrides: object) -> RoleHandoff:
    values: dict[str, object] = {
        "from_role": MissionRole.ORCHESTRATOR,
        "to_role": MissionRole.ARCHITECT,
        "mission_id": "P2.4",
        "subject": "Architect role contract",
        "objective": "Define a bounded Architect role.",
        "observed_commit": COMMIT,
        "operating_step": OperatingStep.UNDERSTAND_CONTRACT,
        "blockers": (),
        "instructions": "Specify the minimal solution without coding it.",
    }
    values.update(overrides)
    return RoleHandoff(**values)  # type: ignore[arg-type]


def user_story(
    identifier: str,
    *,
    depends_on: tuple[str, ...] = (),
    status: UserStoryStatus = UserStoryStatus.PROPOSED,
    human_required: bool = False,
) -> UserStory:
    return UserStory(
        schema_version="1.0",
        id=identifier,
        title=f"Candidate {identifier}",
        description=f"Implement the observable scope for {identifier}.",
        status=status,
        priority=1,
        risk=RiskLevel.MEDIUM,
        depends_on=depends_on,
        scope=UserStoryScope(
            allowed_paths=(f"src/{identifier.lower()}.py",),
            forbidden_paths=(".agentic-engineering-os/state.json",),
        ),
        acceptance_criteria=(
            AcceptanceCriterion(
                id="AC-001",
                description=f"The behavior for {identifier} is verified.",
                mandatory=True,
            ),
        ),
        required_gates=("GATE-TESTS",),
        human_approval=HumanApproval(
            required=human_required,
            approved=False,
            approved_by=None,
            approved_at=None,
        ),
        metadata=UserStoryMetadata(
            created_at=TIMESTAMP,
            created_by="Codex/Architect",
            updated_at=TIMESTAMP,
        ),
    )


def architect_result(**overrides: object) -> ArchitectResult:
    values: dict[str, object] = {
        "mission_id": "P2.4",
        "subject": "Architect role contract",
        "observed_commit": COMMIT,
        "summary": "Define a minimal contract and validation boundary.",
        "assumptions": ("Phase 0 contracts remain authoritative.",),
        "decisions": (
            ArchitectDecision(
                kind=ArchitectDecisionKind.ARCHITECTURAL,
                description="Reuse ContractValidator for User Stories.",
            ),
        ),
        "risks": ("Unvalidated free-form output could be mistaken for fact.",),
        "blockers": (),
        "user_stories": (
            user_story("US-0001"),
            user_story("US-0002", depends_on=("US-0001",)),
        ),
        "recommended_next_role": MissionRole.IMPLEMENTER,
        "verdict": ArchitectVerdict.READY,
    }
    values.update(overrides)
    return ArchitectResult(**values)  # type: ignore[arg-type]


def test_architect_input_is_derived_from_architect_handoff() -> None:
    handoff = architect_handoff(blockers=("Explicit context",))

    actual = ArchitectInput.from_handoff(
        handoff,
        constraints=("Do not modify business code.",),
    )

    assert actual.mission_id == handoff.mission_id
    assert actual.objective == handoff.objective
    assert actual.subject == handoff.subject
    assert actual.observed_commit == handoff.observed_commit
    assert actual.blockers == ("Explicit context",)
    assert actual.instructions == handoff.instructions
    assert actual.constraints == ("Do not modify business code.",)


def test_architect_input_rejects_handoff_for_another_role() -> None:
    handoff = architect_handoff(to_role=MissionRole.IMPLEMENTER)

    with pytest.raises(ArchitectInputError, match="must target ARCHITECT"):
        ArchitectInput.from_handoff(handoff)


def test_architect_input_rejects_wrong_operating_step() -> None:
    handoff = architect_handoff(operating_step=OperatingStep.ACT)

    with pytest.raises(ArchitectInputError, match="UNDERSTAND_CONTRACT"):
        ArchitectInput.from_handoff(handoff)


def test_architect_input_rejects_scalar_constraints() -> None:
    with pytest.raises(ArchitectInputError, match="collection"):
        ArchitectInput.from_handoff(
            architect_handoff(), constraints="not-a-collection"
        )


def test_valid_architect_result_with_multiple_user_stories() -> None:
    candidate = architect_result()

    result = ArchitectResultValidator().validate(candidate)

    assert result.is_valid
    assert candidate.role is MissionRole.ARCHITECT
    assert len(candidate.user_stories) == 2
    assert candidate.assumptions
    assert candidate.risks
    assert candidate.blockers == ()


def test_architect_result_schema_and_serialization_are_deterministic() -> None:
    candidate = architect_result()
    first = to_dict(candidate)
    second = to_dict(candidate)

    assert first == second
    assert ContractValidator().validate("architect-result", first).is_valid
    assert first["role"] == "ARCHITECT"
    assert first["verdict"] == "READY"


def test_result_context_must_match_architect_input() -> None:
    architect_input = ArchitectInput.from_handoff(architect_handoff())
    candidate = architect_result(subject="A different subject")

    result = ArchitectResultValidator().validate(
        candidate, architect_input=architect_input
    )

    assert not result.is_valid
    assert result.errors[0].code == "ARCHITECT_CONTEXT_MISMATCH"


def test_each_user_story_is_validated_by_contract_validator() -> None:
    candidate = architect_result()
    validator = ContractValidator()

    assert all(
        validator.validate("user-story", to_dict(story)).is_valid
        for story in candidate.user_stories
    )
    assert ArchitectResultValidator(validator).validate(candidate).is_valid


def test_declaring_required_human_approval_without_granting_it_is_valid() -> None:
    candidate = architect_result(
        user_stories=(user_story("US-0001", human_required=True),)
    )

    result = ArchitectResultValidator().validate(candidate)

    assert result.is_valid
    approval = candidate.user_stories[0].human_approval
    assert approval.required
    assert not approval.approved
    assert approval.approved_by is None
    assert approval.approved_at is None


def test_blocked_result_can_report_missing_human_decision() -> None:
    candidate = architect_result(
        decisions=(
            ArchitectDecision(
                kind=ArchitectDecisionKind.HUMAN_REQUIRED,
                description="Human must choose the deployment boundary.",
            ),
        ),
        blockers=("HUMAN_REQUIRED: deployment boundary is unknown",),
        user_stories=(),
        recommended_next_role=MissionRole.ORCHESTRATOR,
        verdict=ArchitectVerdict.BLOCKED,
    )

    assert ArchitectResultValidator().validate(candidate).is_valid


def invalid_mapping(**changes: object) -> dict[str, object]:
    candidate = to_dict(architect_result())
    candidate.update(changes)  # type: ignore[arg-type]
    return candidate  # type: ignore[return-value]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("role", "IMPLEMENTER"),
        ("verdict", "CERTIFIED"),
        ("recommended_next_role", "HUMAN"),
    ],
)
def test_unknown_or_unauthorized_result_values_are_rejected(
    field: str, value: str
) -> None:
    result = ArchitectResultValidator().validate(invalid_mapping(**{field: value}))

    assert not result.is_valid


def test_missing_required_result_field_is_rejected() -> None:
    candidate = invalid_mapping()
    del candidate["summary"]

    result = ArchitectResultValidator().validate(candidate)

    assert not result.is_valid


def test_invalid_user_story_is_rejected() -> None:
    candidate = invalid_mapping()
    candidate["user_stories"][0]["id"] = "INVALID"  # type: ignore[index]

    result = ArchitectResultValidator().validate(candidate)

    assert not result.is_valid
    assert result.errors[0].path[:2] == ("user_stories", 0)


def test_non_proposed_candidate_status_is_rejected() -> None:
    candidate = architect_result(
        user_stories=(user_story("US-0001", status=UserStoryStatus.READY),)
    )

    result = ArchitectResultValidator().validate(candidate)

    assert not result.is_valid
    assert result.errors[0].code == "INVALID_CANDIDATE_STATUS"


def test_invalid_acceptance_criterion_is_rejected() -> None:
    candidate = invalid_mapping()
    stories = candidate["user_stories"]
    stories[0]["acceptance_criteria"][0]["description"] = ""  # type: ignore[index]

    result = ArchitectResultValidator().validate(candidate)

    assert not result.is_valid
    assert any("acceptance_criteria" in issue.path for issue in result.errors)


def test_unresolved_local_dependency_is_rejected() -> None:
    candidate = architect_result(
        user_stories=(user_story("US-0001", depends_on=("US-9999",)),)
    )

    result = ArchitectResultValidator().validate(candidate)

    assert not result.is_valid
    assert result.errors[0].code == "UNRESOLVED_LOCAL_DEPENDENCY"


def test_explicit_known_dependency_is_accepted() -> None:
    candidate = architect_result(
        user_stories=(user_story("US-0001", depends_on=("US-0042",)),)
    )

    result = ArchitectResultValidator().validate(
        candidate, known_user_story_ids=("US-0042",)
    )

    assert result.is_valid


def test_ready_result_with_blocker_is_rejected() -> None:
    candidate = invalid_mapping(blockers=["Required context is missing"])

    result = ArchitectResultValidator().validate(candidate)

    assert not result.is_valid


def test_ready_result_with_human_decision_is_rejected() -> None:
    candidate = invalid_mapping(
        decisions=[
            {
                "kind": "HUMAN_REQUIRED",
                "description": "Human must choose the scope.",
            }
        ]
    )

    result = ArchitectResultValidator().validate(candidate)

    assert not result.is_valid


def test_architect_cannot_grant_human_approval() -> None:
    candidate = invalid_mapping()
    approval = candidate["user_stories"][0]["human_approval"]  # type: ignore[index]
    approval.update(  # type: ignore[union-attr]
        {
            "required": True,
            "approved": True,
            "approved_by": "Codex/Architect",
            "approved_at": TIMESTAMP.isoformat(),
        }
    )

    result = ArchitectResultValidator().validate(candidate)

    assert not result.is_valid
    assert result.errors[0].code == "ARCHITECT_CANNOT_APPROVE_HUMAN"


def test_duplicate_candidate_ids_are_rejected() -> None:
    candidate = architect_result(
        user_stories=(user_story("US-0001"), user_story("US-0001"))
    )

    result = ArchitectResultValidator().validate(candidate)

    assert not result.is_valid
    assert result.errors[0].code == "DUPLICATE_USER_STORY_ID"


def test_non_validatable_output_is_rejected() -> None:
    result = ArchitectResultValidator().validate(object())  # type: ignore[arg-type]

    assert not result.is_valid
    assert result.errors[0].code == "INVALID_ARCHITECT_OUTPUT"
