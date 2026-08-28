from datetime import datetime

import pytest

from agentic_engineering_os.application import (
    ImplementerResult,
    ImplementerVerdict,
    RoleHandoff,
    TestCaseType,
    TesterAcceptanceResult,
    TesterInput,
    TesterInputError,
    TesterPlan,
    TesterResult,
    TesterResultValidator,
    TesterTestCase,
    TesterVerdict,
    TesterVerificationResult,
    VerificationOutcome,
    VerificationResult,
)
from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    GateResult,
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


COMMIT = "a6d78c2e800f3a247f8474a0b674190599ea049f"
TIMESTAMP = datetime.fromisoformat("2026-08-28T17:00:00+02:00")
COMMAND = "python -m pytest tests/test_feature.py"

for _contract_type in (
    TestCaseType,
    TesterAcceptanceResult,
    TesterInput,
    TesterInputError,
    TesterPlan,
    TesterResult,
    TesterResultValidator,
    TesterTestCase,
    TesterVerdict,
    TesterVerificationResult,
):
    _contract_type.__test__ = False


def story(
    *,
    status: UserStoryStatus = UserStoryStatus.TESTING,
    allowed: tuple[str, ...] = ("src/", "tests/"),
    forbidden: tuple[str, ...] = ("tests/forbidden.py",),
    human_required: bool = False,
    human_approved: bool = False,
) -> UserStory:
    return UserStory(
        schema_version="1.0",
        id="US-0001",
        title="Feature under test",
        description="Verify the feature adversarially.",
        status=status,
        priority=1,
        risk=RiskLevel.MEDIUM,
        depends_on=(),
        scope=UserStoryScope(allowed, forbidden),
        acceptance_criteria=(
            AcceptanceCriterion("AC-001", "Expected behavior is observable.", True),
            AcceptanceCriterion("AC-002", "Optional diagnostic is available.", False),
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


def implementer_result(**overrides: object) -> ImplementerResult:
    values: dict[str, object] = {
        "mission_id": "P2.6",
        "subject": "US-0001",
        "user_story_id": "US-0001",
        "observed_commit": COMMIT,
        "summary": "Implementation is ready for independent testing.",
        "files_changed": ("src/feature.py", "tests/test_feature.py"),
        "tests_added_or_modified": ("tests/test_feature.py",),
        "verification_commands": (COMMAND,),
        "verification_results": (
            VerificationResult(COMMAND, True, VerificationOutcome.PASS, 0, "1 passed"),
        ),
        "assumptions": (),
        "findings": (),
        "blockers": (),
        "recommended_next_role": MissionRole.TESTER,
        "verdict": ImplementerVerdict.READY_FOR_TEST,
    }
    values.update(overrides)
    return ImplementerResult(**values)  # type: ignore[arg-type]


def handoff(**overrides: object) -> RoleHandoff:
    values: dict[str, object] = {
        "from_role": MissionRole.ORCHESTRATOR,
        "to_role": MissionRole.TESTER,
        "mission_id": "P2.6",
        "subject": "US-0001",
        "objective": "Try to falsify the assigned implementation.",
        "observed_commit": COMMIT,
        "operating_step": OperatingStep.VERIFY,
        "blockers": (),
        "instructions": "Verify independently and report failures honestly.",
    }
    values.update(overrides)
    return RoleHandoff(**values)  # type: ignore[arg-type]


def make_tester_input(**story_overrides: object) -> TesterInput:
    return TesterInput.from_handoff(
        handoff(), story(**story_overrides), implementer_result()
    )


def make_test_case(
    identifier: str,
    case_type: TestCaseType,
    *,
    verdict: GateResult = GateResult.PASS,
    required: bool = True,
    executed: bool = True,
) -> TesterTestCase:
    return TesterTestCase(
        id=identifier,
        type=case_type,
        objective=f"Exercise {case_type.value.lower()} behavior.",
        expected_result="Contract behavior is preserved.",
        observed_result="Observed execution result.",
        required=required,
        executed=executed,
        verdict=verdict,
    )


def acceptance(result: GateResult = GateResult.PASS) -> TesterAcceptanceResult:
    evidence = () if result in {GateResult.UNKNOWN, GateResult.NOT_APPLICABLE} else ("TC-001",)
    return TesterAcceptanceResult("AC-001", result, evidence, "Independent observation.")


def verification(
    result: GateResult = GateResult.PASS,
    *,
    required: bool = True,
    executed: bool = True,
) -> TesterVerificationResult:
    exit_code = 0 if result is GateResult.PASS else None
    if result is GateResult.FAIL:
        exit_code = 1
    return TesterVerificationResult(
        COMMAND, required, executed, result, exit_code, "Observable command result."
    )


def result(**overrides: object) -> TesterResult:
    values: dict[str, object] = {
        "mission_id": "P2.6",
        "subject": "US-0001",
        "user_story_id": "US-0001",
        "observed_commit": COMMIT,
        "summary": "Independent adversarial verification completed.",
        "test_plan": TesterPlan(
            acceptance_criteria=("AC-001",),
            positive_tests=("Verify expected behavior.",),
            negative_tests=("Reject invalid input.",),
            edge_cases=("Exercise empty boundary.",),
            regressions=("Re-run existing regression.",),
            commands=(COMMAND,),
        ),
        "acceptance_results": (acceptance(),),
        "test_cases": (
            make_test_case("TC-001", TestCaseType.POSITIVE),
            make_test_case("TC-002", TestCaseType.NEGATIVE),
            make_test_case("TC-003", TestCaseType.EDGE),
            make_test_case("TC-004", TestCaseType.REGRESSION),
        ),
        "test_files_changed": ("tests/test_feature.py",),
        "verification_commands": (COMMAND,),
        "verification_results": (verification(),),
        "findings": (),
        "blockers": (),
        "recommended_next_role": MissionRole.REVIEWER,
        "verdict": TesterVerdict.READY_FOR_REVIEW,
    }
    values.update(overrides)
    return TesterResult(**values)  # type: ignore[arg-type]


def mapping(**overrides: object) -> dict[str, object]:
    candidate = to_dict(result())
    candidate.update(overrides)  # type: ignore[arg-type]
    return candidate  # type: ignore[return-value]


def validate(candidate: TesterResult | dict[str, object]):
    return TesterResultValidator().validate(candidate, tester_input=make_tester_input())


def test_valid_tester_input_uses_testing_story_and_ready_implementer_result() -> None:
    assigned = story()
    implementation = implementer_result()

    actual = TesterInput.from_handoff(handoff(), assigned, implementation)

    assert actual.mission_id == "P2.6"
    assert actual.user_story == assigned and actual.user_story is not assigned
    assert actual.implementer_result == implementation
    assert actual.implementer_result is not implementation


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"from_role": MissionRole.IMPLEMENTER}, "originate from ORCHESTRATOR"),
        ({"to_role": MissionRole.REVIEWER}, "target TESTER"),
        ({"operating_step": OperatingStep.RECORD_EVIDENCE}, "target VERIFY"),
        ({"subject": "US-9999"}, "identify the UserStory"),
        ({"blockers": ("environment missing",)}, "not assignable"),
    ],
)
def test_invalid_handoff_is_rejected(changes: dict[str, object], message: str) -> None:
    with pytest.raises(TesterInputError, match=message):
        TesterInput.from_handoff(handoff(**changes), story(), implementer_result())


@pytest.mark.parametrize(
    "status", [item for item in UserStoryStatus if item is not UserStoryStatus.TESTING]
)
def test_only_testing_story_is_eligible(status: UserStoryStatus) -> None:
    with pytest.raises(TesterInputError, match="must be TESTING"):
        make_tester_input(status=status)


def test_incoherent_implementer_result_is_rejected() -> None:
    with pytest.raises(TesterInputError, match="incoherent"):
        TesterInput.from_handoff(
            handoff(), story(), implementer_result(user_story_id="US-9999")
        )


def test_implementer_required_unknown_is_not_proof_for_tester_input() -> None:
    implementation = implementer_result(
        verification_results=(
            VerificationResult(
                COMMAND, True, VerificationOutcome.UNKNOWN, None, "Not executed"
            ),
        )
    )

    with pytest.raises(TesterInputError, match="not PASS"):
        TesterInput.from_handoff(handoff(), story(), implementation)


@pytest.mark.parametrize("identity", ["Codex/Fake", "codex/Fake", "CODEX/Fake", "CoDeX/Fake"])
def test_codex_cannot_supply_required_human_decision(identity: str) -> None:
    candidate = story(human_required=True, human_approved=True)
    candidate.human_approval.approved_by = identity

    with pytest.raises(TesterInputError, match="Human approval"):
        TesterInput.from_handoff(handoff(), candidate, implementer_result())


def test_ready_for_review_nominal_covers_adversarial_cases_and_schema() -> None:
    candidate = result()
    validation = validate(candidate)

    assert validation.is_valid
    assert candidate.role is MissionRole.TESTER
    assert candidate.verdict is TesterVerdict.READY_FOR_REVIEW
    assert {case.type for case in candidate.test_cases} == set(TestCaseType)


@pytest.mark.parametrize("field", ["mission_id", "subject", "user_story_id", "observed_commit"])
def test_result_context_must_match_tester_input(field: str) -> None:
    value = "0" * 40 if field == "observed_commit" else "US-9999" if field == "user_story_id" else "different"

    validation = validate(mapping(**{field: value}))

    assert not validation.is_valid
    assert any(issue.code == "TESTER_CONTEXT_MISMATCH" for issue in validation.errors)


def test_unknown_acceptance_criterion_is_rejected() -> None:
    plan = to_dict(result().test_plan)
    plan["acceptance_criteria"] = ["AC-999"]
    candidate = mapping(
        test_plan=plan,
        acceptance_results=[
            {"acceptance_criterion_id": "AC-999", "result": "PASS", "evidence": ["observation"], "notes": "Unknown."}
        ],
    )

    assert any(issue.code == "UNKNOWN_ACCEPTANCE_CRITERION" for issue in validate(candidate).errors)


@pytest.mark.parametrize("outcome", [GateResult.FAIL, GateResult.UNKNOWN])
def test_mandatory_acceptance_failure_or_unknown_forbids_ready(outcome: GateResult) -> None:
    validation = validate(result(acceptance_results=(acceptance(outcome),)))

    expected = "FAIL_FORBIDS_READY_FOR_REVIEW" if outcome is GateResult.FAIL else "UNKNOWN_FORBIDS_READY_FOR_REVIEW"
    assert any(issue.code == expected for issue in validation.errors)


def test_required_test_failure_forbids_ready() -> None:
    cases = list(result().test_cases)
    cases[-1] = make_test_case("TC-004", TestCaseType.REGRESSION, verdict=GateResult.FAIL)

    assert any(issue.code == "FAIL_FORBIDS_READY_FOR_REVIEW" for issue in validate(result(test_cases=tuple(cases))).errors)


def test_not_executed_command_cannot_be_pass() -> None:
    candidate = mapping()
    candidate["verification_results"][0]["executed"] = False  # type: ignore[index]

    assert not validate(candidate).is_valid


def test_required_unknown_command_requires_blocked() -> None:
    validation = validate(result(verification_results=(verification(GateResult.UNKNOWN, executed=False),)))

    assert any(issue.code == "UNKNOWN_FORBIDS_READY_FOR_REVIEW" for issue in validation.errors)


def test_blocker_forbids_ready_for_review() -> None:
    assert not validate(mapping(blockers=["Environment unavailable"])).is_valid


def test_remediation_requires_finding_and_explicit_failure() -> None:
    without_finding = mapping(
        verdict="REMEDIATION_REQUIRED",
        recommended_next_role="IMPLEMENTER",
    )
    without_failure = result(
        findings=("Unsubstantiated concern.",),
        recommended_next_role=MissionRole.IMPLEMENTER,
        verdict=TesterVerdict.REMEDIATION_REQUIRED,
    )

    assert not validate(without_finding).is_valid
    assert any(issue.code == "REMEDIATION_WITHOUT_FAILURE" for issue in validate(without_failure).errors)


@pytest.mark.parametrize(
    "path",
    ["src/feature.py", "../tests/test_feature.py", "C:/repo/tests/test_feature.py", "tests\\test_feature.py", "tests/forbidden.py"],
)
def test_production_traversal_absolute_ambiguous_and_forbidden_paths_are_rejected(path: str) -> None:
    assert not validate(result(test_files_changed=(path,))).is_valid


def test_unknown_verdict_and_missing_required_field_are_rejected() -> None:
    unknown = mapping(verdict="CERTIFIED")
    missing = mapping()
    del missing["test_cases"]

    assert not validate(unknown).is_valid
    assert not validate(missing).is_valid


def test_implementer_ready_then_tester_finds_regression_and_requests_remediation() -> None:
    context = make_tester_input()
    cases = list(result().test_cases)
    cases[-1] = make_test_case("TC-004", TestCaseType.REGRESSION, verdict=GateResult.FAIL)
    candidate = result(
        acceptance_results=(acceptance(GateResult.FAIL),),
        test_cases=tuple(cases),
        verification_results=(verification(GateResult.FAIL),),
        findings=("Regression TC-004 proves the mandatory behavior is broken.",),
        recommended_next_role=MissionRole.IMPLEMENTER,
        verdict=TesterVerdict.REMEDIATION_REQUIRED,
    )

    validation = TesterResultValidator().validate(candidate, tester_input=context)

    assert context.implementer_result.verdict is ImplementerVerdict.READY_FOR_TEST
    assert validation.is_valid
    assert candidate.verdict is TesterVerdict.REMEDIATION_REQUIRED
    assert candidate.test_files_changed == ("tests/test_feature.py",)


def test_blocked_is_valid_for_required_unknown_without_inventing_success() -> None:
    candidate = result(
        acceptance_results=(acceptance(GateResult.UNKNOWN),),
        test_cases=(make_test_case("TC-001", TestCaseType.POSITIVE, verdict=GateResult.UNKNOWN, executed=False),),
        verification_results=(verification(GateResult.UNKNOWN, executed=False),),
        blockers=("Required environment is unavailable.",),
        recommended_next_role=MissionRole.ORCHESTRATOR,
        verdict=TesterVerdict.BLOCKED,
    )

    assert validate(candidate).is_valid


def test_tester_cannot_claim_other_role_certification_or_human_approval() -> None:
    for candidate in (
        mapping(role="REVIEWER"),
        mapping(verdict="CERTIFIED"),
        mapping(human_approval={"approved": True}),
    ):
        assert not validate(candidate).is_valid


@pytest.mark.parametrize("target", ["story", "implementation", "context"])
def test_assignment_tampering_is_rejected(target: str) -> None:
    context = make_tester_input()
    if target == "story":
        context.user_story.status = UserStoryStatus.REVIEW
    elif target == "implementation":
        object.__setattr__(context.implementer_result, "summary", "Changed")
    else:
        object.__setattr__(context, "objective", "Changed")

    validation = TesterResultValidator().validate(result(), tester_input=context)

    assert validation.errors[0].code == "TESTER_INPUT_TAMPERED"


def test_validation_does_not_change_user_story_status_or_create_evidence() -> None:
    context = make_tester_input()
    before = to_dict(context.user_story)

    validation = TesterResultValidator().validate(result(), tester_input=context)

    assert validation.is_valid
    assert to_dict(context.user_story) == before
    assert context.user_story.status is UserStoryStatus.TESTING
    assert "evidence_id" not in to_dict(result())
