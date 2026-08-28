from datetime import datetime

import pytest

from agentic_engineering_os.application import (
    ContractValidator,
    ImplementerInput,
    ImplementerInputError,
    ImplementerResult,
    ImplementerResultValidator,
    ImplementerVerdict,
    RoleHandoff,
    VerificationOutcome,
    VerificationResult,
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


COMMIT = "b633fd9b7ea9ec2cee8c0c176c6e7dab7c03e9f9"
TIMESTAMP = datetime.fromisoformat("2026-08-28T16:00:00+02:00")
COMMAND = "python -m pytest tests/test_feature.py"


def story(
    *,
    status: UserStoryStatus = UserStoryStatus.IN_PROGRESS,
    allowed: tuple[str, ...] = ("src/", "tests/"),
    forbidden: tuple[str, ...] = ("src/secret.py",),
    human_required: bool = False,
    human_approved: bool = False,
) -> UserStory:
    return UserStory(
        schema_version="1.0",
        id="US-0001",
        title="Bounded feature",
        description="Implement only the assigned feature.",
        status=status,
        priority=1,
        risk=RiskLevel.MEDIUM,
        depends_on=(),
        scope=UserStoryScope(allowed_paths=allowed, forbidden_paths=forbidden),
        acceptance_criteria=(
            AcceptanceCriterion("AC-001", "The feature is observable.", True),
        ),
        required_gates=("GATE-TESTS",),
        human_approval=HumanApproval(
            required=human_required,
            approved=human_approved,
            approved_by="Alice" if human_approved else None,
            approved_at=TIMESTAMP if human_approved else None,
        ),
        metadata=UserStoryMetadata(TIMESTAMP, "Codex/Architect", TIMESTAMP),
    )


def handoff(**overrides: object) -> RoleHandoff:
    values: dict[str, object] = {
        "from_role": MissionRole.ORCHESTRATOR,
        "to_role": MissionRole.IMPLEMENTER,
        "mission_id": "P2.5",
        "workflow_generation": 0,
        "subject": "US-0001",
        "objective": "Implement the bounded User Story.",
        "observed_commit": COMMIT,
        "operating_step": OperatingStep.ACT,
        "blockers": (),
        "instructions": "Change only files allowed by the User Story.",
    }
    values.update(overrides)
    return RoleHandoff(**values)  # type: ignore[arg-type]


def implementer_input(**story_overrides: object) -> ImplementerInput:
    return ImplementerInput.from_handoff(handoff(), story(**story_overrides))


def verification(
    result: VerificationOutcome = VerificationOutcome.PASS,
    *,
    required: bool = True,
) -> VerificationResult:
    exit_code = 0 if result is VerificationOutcome.PASS else None
    if result is VerificationOutcome.FAIL:
        exit_code = 1
    return VerificationResult(COMMAND, required, result, exit_code, "Observed result")


def output(**overrides: object) -> ImplementerResult:
    values: dict[str, object] = {
        "mission_id": "P2.5",
        "workflow_generation": 0,
        "subject": "US-0001",
        "user_story_id": "US-0001",
        "observed_commit": COMMIT,
        "summary": "Implemented the assigned behavior within scope.",
        "files_changed": ("src/feature.py", "tests/test_feature.py"),
        "tests_added_or_modified": ("tests/test_feature.py",),
        "verification_commands": (COMMAND,),
        "verification_results": (verification(),),
        "assumptions": (),
        "findings": (),
        "blockers": (),
        "recommended_next_role": MissionRole.TESTER,
        "verdict": ImplementerVerdict.READY_FOR_TEST,
    }
    values.update(overrides)
    return ImplementerResult(**values)  # type: ignore[arg-type]


def mapping(**overrides: object) -> dict[str, object]:
    candidate = to_dict(output())
    candidate.update(overrides)  # type: ignore[arg-type]
    return candidate  # type: ignore[return-value]


def test_input_is_derived_from_explicit_implementer_handoff_and_story() -> None:
    assigned = story()
    result = ImplementerInput.from_handoff(handoff(), assigned)

    assert result.mission_id == "P2.5"
    assert result.user_story == assigned
    assert result.user_story is not assigned
    assert result.observed_commit == COMMIT
    assert result.objective
    assert result.blockers == ()
    assert result.instructions


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"from_role": MissionRole.ARCHITECT}, "originate from ORCHESTRATOR"),
        ({"to_role": MissionRole.TESTER}, "target IMPLEMENTER"),
        ({"operating_step": OperatingStep.PROVE_READINESS}, "target ACT"),
        ({"subject": "US-9999"}, "identify the UserStory"),
        ({"blockers": ("dependency unknown",)}, "not assignable"),
    ],
)
def test_input_rejects_invalid_handoff(change: dict[str, object], message: str) -> None:
    with pytest.raises(ImplementerInputError, match=message):
        ImplementerInput.from_handoff(handoff(**change), story())


@pytest.mark.parametrize(
    "status",
    [status for status in UserStoryStatus if status is not UserStoryStatus.IN_PROGRESS],
)
def test_only_in_progress_story_is_assignable(status: UserStoryStatus) -> None:
    with pytest.raises(ImplementerInputError, match="must be IN_PROGRESS"):
        implementer_input(status=status)


def test_empty_or_unsafe_scope_is_not_assignable() -> None:
    with pytest.raises(ImplementerInputError, match="non-empty"):
        implementer_input(allowed=())
    with pytest.raises(ImplementerInputError, match="traversal"):
        implementer_input(allowed=("../src/",))


def test_required_human_decision_must_be_satisfied_before_implementation() -> None:
    with pytest.raises(ImplementerInputError, match="Human approval"):
        implementer_input(human_required=True)

    assert implementer_input(human_required=True, human_approved=True).user_story


@pytest.mark.parametrize(
    "identity",
    ["Codex/FakeHuman", "codex/FakeHuman", "CODEX/FakeHuman", "CoDeX/FakeHuman"],
)
def test_codex_identity_cannot_simulate_required_human_decision(identity: str) -> None:
    candidate = story(human_required=True, human_approved=True)
    candidate.human_approval.approved_by = identity

    with pytest.raises(ImplementerInputError, match="Human approval"):
        ImplementerInput.from_handoff(handoff(), candidate)


def test_ready_for_test_result_is_structured_and_valid() -> None:
    candidate = output()
    result = ImplementerResultValidator().validate(
        candidate, implementer_input=implementer_input()
    )

    assert result.is_valid
    assert candidate.role is MissionRole.IMPLEMENTER
    assert candidate.verdict is ImplementerVerdict.READY_FOR_TEST
    assert ContractValidator().validate("implementer-result", to_dict(candidate)).is_valid


def test_blocked_result_reports_failed_required_verification() -> None:
    candidate = output(
        files_changed=(),
        tests_added_or_modified=(),
        verification_results=(verification(VerificationOutcome.FAIL),),
        blockers=("Required test failed.",),
        recommended_next_role=MissionRole.ORCHESTRATOR,
        verdict=ImplementerVerdict.BLOCKED,
    )

    assert ImplementerResultValidator().validate(
        candidate, implementer_input=implementer_input()
    ).is_valid


@pytest.mark.parametrize(
    "field",
    ["mission_id", "workflow_generation", "subject", "user_story_id", "observed_commit"],
)
def test_result_context_must_match_assignment(field: str) -> None:
    if field == "observed_commit":
        value = "0" * 40
    elif field == "workflow_generation":
        value = 1
    elif field == "user_story_id":
        value = "US-9999"
    else:
        value = "different"
    result = ImplementerResultValidator().validate(
        mapping(**{field: value}), implementer_input=implementer_input()
    )

    assert not result.is_valid
    assert any(issue.code == "IMPLEMENTER_CONTEXT_MISMATCH" for issue in result.errors)


@pytest.mark.parametrize(
    "changed",
    [
        ("docs/outside.md",),
        ("src/secret.py",),
        ("../src/feature.py",),
        ("C:/repo/src/feature.py",),
        ("src\\feature.py",),
        ("src/feature.py", "SRC/FEATURE.PY"),
        (".agentic-engineering-os/state.json",),
        (".agentic-engineering-os/mission.json",),
    ],
)
def test_out_of_scope_ambiguous_duplicate_and_control_paths_are_rejected(
    changed: tuple[str, ...],
) -> None:
    candidate = mapping(files_changed=list(changed), tests_added_or_modified=[])

    result = ImplementerResultValidator().validate(
        candidate, implementer_input=implementer_input(allowed=("src/", ".agentic-engineering-os/"))
    )

    assert not result.is_valid


def test_forbidden_path_wins_over_allowed_path() -> None:
    result = ImplementerResultValidator().validate(
        mapping(files_changed=["src/secret.py"], tests_added_or_modified=[]),
        implementer_input=implementer_input(),
    )

    assert any(issue.code == "FORBIDDEN_PATH" for issue in result.errors)


def test_test_path_must_be_changed_and_in_scope() -> None:
    result = ImplementerResultValidator().validate(
        mapping(tests_added_or_modified=["tests/other.py"]),
        implementer_input=implementer_input(),
    )

    assert any(issue.code == "TEST_NOT_IN_CHANGED_FILES" for issue in result.errors)


@pytest.mark.parametrize(
    "outcome",
    [VerificationOutcome.FAIL, VerificationOutcome.UNKNOWN, VerificationOutcome.NOT_APPLICABLE],
)
def test_required_non_pass_verification_blocks_ready_for_test(
    outcome: VerificationOutcome,
) -> None:
    result = ImplementerResultValidator().validate(
        output(verification_results=(verification(outcome),)),
        implementer_input=implementer_input(),
    )

    assert any(issue.code == "REQUIRED_VERIFICATION_NOT_PASS" for issue in result.errors)


def test_optional_unknown_verification_does_not_fabricate_a_pass() -> None:
    candidate = output(verification_results=(verification(VerificationOutcome.UNKNOWN, required=False),))

    result = ImplementerResultValidator().validate(
        candidate, implementer_input=implementer_input()
    )

    assert result.is_valid
    assert candidate.verification_results[0].result is VerificationOutcome.UNKNOWN


def test_every_declared_command_requires_exactly_one_observable_result() -> None:
    missing = mapping(verification_results=[])
    duplicate = mapping(
        verification_results=[to_dict(verification()), to_dict(verification(VerificationOutcome.UNKNOWN, required=False))]
    )

    for candidate in (missing, duplicate):
        result = ImplementerResultValidator().validate(
            candidate, implementer_input=implementer_input()
        )
        assert not result.is_valid


def test_schema_rejects_fabricated_pass_exit_code() -> None:
    candidate = mapping()
    candidate["verification_results"][0]["exit_code"] = 1  # type: ignore[index]

    assert not ImplementerResultValidator().validate(
        candidate, implementer_input=implementer_input()
    ).is_valid


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("role", "TESTER"),
        ("verdict", "PASS"),
        ("verdict", "CERTIFIED"),
        ("recommended_next_role", "CERTIFIER"),
        ("human_approval", {"approved": True}),
    ],
)
def test_implementer_cannot_claim_other_authority(field: str, value: object) -> None:
    assert not ImplementerResultValidator().validate(
        mapping(**{field: value}), implementer_input=implementer_input()
    ).is_valid


def test_validation_does_not_mutate_story_contract_or_status() -> None:
    assigned = story()
    original = to_dict(assigned)
    context = ImplementerInput.from_handoff(handoff(), assigned)

    ImplementerResultValidator().validate(output(), implementer_input=context)

    assert to_dict(assigned) == original
    assert to_dict(context.user_story) == original
    assert assigned.status is UserStoryStatus.IN_PROGRESS


@pytest.mark.parametrize("target", ["status", "scope", "criterion", "approval"])
def test_assignment_contract_tampering_is_rejected(target: str) -> None:
    context = implementer_input(human_required=True, human_approved=True)
    if target == "status":
        context.user_story.status = UserStoryStatus.IMPLEMENTED
    elif target == "scope":
        context.user_story.scope = UserStoryScope(("docs/",), ())
    elif target == "criterion":
        context.user_story.acceptance_criteria = (
            AcceptanceCriterion("AC-001", "Reduced criterion.", True),
        )
    else:
        context.user_story.human_approval.approved_by = "Codex/FakeHuman"

    result = ImplementerResultValidator().validate(output(), implementer_input=context)

    assert not result.is_valid
    assert result.errors[0].code == "IMPLEMENTER_INPUT_TAMPERED"
