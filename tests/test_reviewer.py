from datetime import datetime

import pytest

from agentic_engineering_os.application import (
    ImplementerResult,
    ImplementerVerdict,
    ReviewDimension,
    ReviewFinding,
    ReviewSeverity,
    ReviewerInput,
    ReviewerInputError,
    ReviewerResult,
    ReviewerResultValidator,
    ReviewerVerdict,
    RoleHandoff,
    TestCaseType,
    TesterAcceptanceResult,
    TesterPlan,
    TesterResult,
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


COMMIT = "1a8e19e516bf9637aa4eed5945172e162e35d9d7"
TIMESTAMP = datetime.fromisoformat("2026-08-28T18:00:00+02:00")
COMMAND = "python -m pytest tests/test_feature.py"

for _contract_type in (
    TestCaseType,
    TesterAcceptanceResult,
    TesterPlan,
    TesterResult,
    TesterTestCase,
    TesterVerdict,
    TesterVerificationResult,
):
    _contract_type.__test__ = False


def story(
    *,
    status: UserStoryStatus = UserStoryStatus.REVIEW,
    human_required: bool = False,
    human_approved: bool = False,
) -> UserStory:
    return UserStory(
        schema_version="1.0",
        id="US-0001",
        title="Feature under review",
        description="Review engineering quality after behavioral verification.",
        status=status,
        priority=1,
        risk=RiskLevel.HIGH,
        depends_on=(),
        scope=UserStoryScope(("src/", "tests/"), ("src/forbidden.py",)),
        acceptance_criteria=(
            AcceptanceCriterion("AC-001", "Behavior is observable.", True),
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
        "mission_id": "P2.7",
        "subject": "US-0001",
        "user_story_id": "US-0001",
        "observed_commit": COMMIT,
        "summary": "Implementation is ready for testing.",
        "files_changed": ("src/feature.py", "tests/test_feature.py"),
        "tests_added_or_modified": ("tests/test_feature.py",),
        "verification_commands": (COMMAND,),
        "verification_results": (
            VerificationResult(COMMAND, True, VerificationOutcome.PASS, 0, "4 passed"),
        ),
        "assumptions": (),
        "findings": (),
        "blockers": (),
        "recommended_next_role": MissionRole.TESTER,
        "verdict": ImplementerVerdict.READY_FOR_TEST,
    }
    values.update(overrides)
    return ImplementerResult(**values)  # type: ignore[arg-type]


def make_test_case(identifier: str, case_type: TestCaseType) -> TesterTestCase:
    return TesterTestCase(
        identifier,
        case_type,
        "Exercise the behavior.",
        "Contract is satisfied.",
        "Observed expected result.",
        True,
        True,
        GateResult.PASS,
    )


def make_tester_result(**overrides: object) -> TesterResult:
    cases = tuple(
        make_test_case(f"TC-{index:03d}", case_type)
        for index, case_type in enumerate(TestCaseType, start=1)
    )
    values: dict[str, object] = {
        "mission_id": "P2.7",
        "subject": "US-0001",
        "user_story_id": "US-0001",
        "observed_commit": COMMIT,
        "summary": "Behavioral verification passed independently.",
        "test_plan": TesterPlan(
            ("AC-001",),
            ("Positive",),
            ("Negative",),
            ("Edge",),
            ("Regression",),
            (COMMAND,),
        ),
        "acceptance_results": (
            TesterAcceptanceResult("AC-001", GateResult.PASS, ("TC-001",), "Pass."),
        ),
        "test_cases": cases,
        "test_files_changed": ("tests/test_feature.py",),
        "verification_commands": (COMMAND,),
        "verification_results": (
            TesterVerificationResult(
                COMMAND, True, True, GateResult.PASS, 0, "4 passed"
            ),
        ),
        "findings": (),
        "blockers": (),
        "recommended_next_role": MissionRole.REVIEWER,
        "verdict": TesterVerdict.READY_FOR_REVIEW,
    }
    values.update(overrides)
    return TesterResult(**values)  # type: ignore[arg-type]


def handoff(**overrides: object) -> RoleHandoff:
    values: dict[str, object] = {
        "from_role": MissionRole.ORCHESTRATOR,
        "to_role": MissionRole.REVIEWER,
        "mission_id": "P2.7",
        "subject": "US-0001",
        "objective": "Review engineering quality independently.",
        "observed_commit": COMMIT,
        "operating_step": OperatingStep.REPORT,
        "blockers": (),
        "instructions": "Review quality without modifying or certifying.",
    }
    values.update(overrides)
    return RoleHandoff(**values)  # type: ignore[arg-type]


def reviewer_input(**story_overrides: object) -> ReviewerInput:
    return ReviewerInput.from_handoff(
        handoff(), story(**story_overrides), implementer_result(), make_tester_result()
    )


def finding(
    identifier: str = "RF-001",
    *,
    dimension: ReviewDimension = ReviewDimension.MAINTAINABILITY,
    severity: ReviewSeverity = ReviewSeverity.INFO,
    path: str = "src/feature.py",
    blocking: bool = False,
) -> ReviewFinding:
    return ReviewFinding(
        identifier,
        dimension,
        severity,
        "Fact observed during independent review.",
        ("Inspection located the fact at the affected path.",),
        (path,),
        blocking,
    )


def reviewer_result(**overrides: object) -> ReviewerResult:
    values: dict[str, object] = {
        "mission_id": "P2.7",
        "subject": "US-0001",
        "user_story_id": "US-0001",
        "observed_commit": COMMIT,
        "summary": "Independent engineering-quality review completed.",
        "dimensions_reviewed": tuple(ReviewDimension),
        "reviewed_paths": ("src/feature.py", "tests/test_feature.py"),
        "findings": (
            finding(),
            finding(
                "RF-002",
                dimension=ReviewDimension.TEST_QUALITY,
                severity=ReviewSeverity.MINOR,
                path="tests/test_feature.py",
            ),
        ),
        "blockers": (),
        "recommended_next_role": MissionRole.CERTIFIER,
        "verdict": ReviewerVerdict.READY_FOR_CERTIFICATION,
    }
    values.update(overrides)
    return ReviewerResult(**values)  # type: ignore[arg-type]


def mapping(**overrides: object) -> dict[str, object]:
    candidate = to_dict(reviewer_result())
    candidate.update(overrides)  # type: ignore[arg-type]
    return candidate  # type: ignore[return-value]


def validate(candidate: ReviewerResult | dict[str, object]):
    return ReviewerResultValidator().validate(candidate, reviewer_input=reviewer_input())


def test_valid_reviewer_input_requires_all_prior_artifacts() -> None:
    assigned = story()
    implementation = implementer_result()
    testing = make_tester_result()

    actual = ReviewerInput.from_handoff(handoff(), assigned, implementation, testing)

    assert actual.user_story == assigned and actual.user_story is not assigned
    assert actual.implementer_result == implementation
    assert actual.tester_result == testing


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"from_role": MissionRole.TESTER}, "originate from ORCHESTRATOR"),
        ({"to_role": MissionRole.CERTIFIER}, "target REVIEWER"),
        ({"operating_step": OperatingStep.CONTROLLED_TRANSITION}, "target REPORT"),
        ({"subject": "US-9999"}, "identify the UserStory"),
        ({"blockers": ("context unavailable",)}, "not assignable"),
    ],
)
def test_invalid_handoff_is_rejected(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ReviewerInputError, match=message):
        ReviewerInput.from_handoff(
            handoff(**changes), story(), implementer_result(), make_tester_result()
        )


@pytest.mark.parametrize(
    "status", [item for item in UserStoryStatus if item is not UserStoryStatus.REVIEW]
)
def test_only_review_story_is_eligible(status: UserStoryStatus) -> None:
    with pytest.raises(ReviewerInputError, match="must be REVIEW"):
        reviewer_input(status=status)


def test_incompatible_tester_result_is_rejected() -> None:
    incompatible = make_tester_result(
        acceptance_results=(
            TesterAcceptanceResult("AC-001", GateResult.FAIL, ("TC-001",), "Fail."),
        ),
        findings=("Regression found.",),
        recommended_next_role=MissionRole.IMPLEMENTER,
        verdict=TesterVerdict.REMEDIATION_REQUIRED,
    )

    with pytest.raises(ReviewerInputError, match="READY_FOR_REVIEW"):
        ReviewerInput.from_handoff(
            handoff(), story(), implementer_result(), incompatible
        )


def test_ready_tester_result_with_mandatory_failure_is_rejected() -> None:
    incoherent = make_tester_result(
        acceptance_results=(
            TesterAcceptanceResult("AC-001", GateResult.FAIL, ("TC-001",), "Fail."),
        )
    )

    with pytest.raises(ReviewerInputError, match="mandatory criterion"):
        ReviewerInput.from_handoff(
            handoff(), story(), implementer_result(), incoherent
        )


def test_ready_tester_result_with_incoherent_commands_is_rejected() -> None:
    incoherent = make_tester_result(verification_commands=("different command",))

    with pytest.raises(ReviewerInputError, match="commands are incoherent"):
        ReviewerInput.from_handoff(
            handoff(), story(), implementer_result(), incoherent
        )


def test_incoherent_prior_artifact_is_rejected() -> None:
    with pytest.raises(ReviewerInputError, match="incoherent"):
        ReviewerInput.from_handoff(
            handoff(), story(), implementer_result(), make_tester_result(user_story_id="US-9999")
        )


@pytest.mark.parametrize("identity", ["Codex/Fake", "codex/Fake", "CODEX/Fake", "CoDeX/Fake"])
def test_codex_cannot_supply_required_human_decision(identity: str) -> None:
    candidate = story(human_required=True, human_approved=True)
    candidate.human_approval.approved_by = identity

    with pytest.raises(ReviewerInputError, match="Human approval"):
        ReviewerInput.from_handoff(
            handoff(), candidate, implementer_result(), make_tester_result()
        )


def test_missing_required_human_decision_blocks_reviewer_input() -> None:
    with pytest.raises(ReviewerInputError, match="Human approval"):
        reviewer_input(human_required=True)


def test_ready_nominal_reviews_all_dimensions_with_nonblocking_findings() -> None:
    candidate = reviewer_result()
    validation = validate(candidate)

    assert validation.is_valid
    assert candidate.role is MissionRole.REVIEWER
    assert set(candidate.dimensions_reviewed) == set(ReviewDimension)
    assert {item.severity for item in candidate.findings} == {
        ReviewSeverity.INFO,
        ReviewSeverity.MINOR,
    }


def test_missing_required_dimension_forbids_ready() -> None:
    validation = validate(
        reviewer_result(dimensions_reviewed=tuple(ReviewDimension)[:-1])
    )

    assert not validation.is_valid


def test_blocking_finding_forbids_ready() -> None:
    validation = validate(
        reviewer_result(
            findings=(
                finding(severity=ReviewSeverity.MAJOR, blocking=True),
            )
        )
    )

    assert any(issue.code == "BLOCKING_FINDING_FORBIDS_READY" for issue in validation.errors)


def test_blocker_forbids_ready() -> None:
    assert not validate(mapping(blockers=["Architecture context unavailable"])).is_valid


def test_remediation_requires_blocking_finding() -> None:
    candidate = reviewer_result(
        recommended_next_role=MissionRole.IMPLEMENTER,
        verdict=ReviewerVerdict.REMEDIATION_REQUIRED,
    )

    assert any(issue.code == "REMEDIATION_WITHOUT_BLOCKING_FINDING" for issue in validate(candidate).errors)


def test_blocked_represents_inability_to_conclude() -> None:
    candidate = reviewer_result(
        dimensions_reviewed=(),
        reviewed_paths=(),
        findings=(),
        blockers=("Required architecture context is unavailable.",),
        recommended_next_role=MissionRole.ORCHESTRATOR,
        verdict=ReviewerVerdict.BLOCKED,
    )

    assert validate(candidate).is_valid


@pytest.mark.parametrize(
    "candidate",
    [
        finding(severity=ReviewSeverity.CRITICAL, blocking=False),
        finding(severity=ReviewSeverity.INFO, blocking=True),
    ],
)
def test_severity_and_blocking_must_be_coherent(candidate: ReviewFinding) -> None:
    assert not validate(reviewer_result(findings=(candidate,))).is_valid


@pytest.mark.parametrize("path", ["../src/feature.py", "C:/repo/src/feature.py", "src\\feature.py"])
def test_ambiguous_finding_paths_are_rejected(path: str) -> None:
    candidate = reviewer_result(
        reviewed_paths=(path, "tests/test_feature.py"),
        findings=(finding(path=path),),
    )

    assert not validate(candidate).is_valid


def test_finding_path_must_be_declared_as_reviewed() -> None:
    validation = validate(
        reviewer_result(findings=(finding(path="docs/impact.md"),))
    )

    assert any(issue.code == "FINDING_PATH_NOT_REVIEWED" for issue in validation.errors)


def test_out_of_scope_impact_can_be_reported_but_not_modified() -> None:
    candidate = reviewer_result(
        reviewed_paths=("src/feature.py", "tests/test_feature.py", "docs/impact.md"),
        findings=(
            finding(
                dimension=ReviewDimension.SCOPE,
                severity=ReviewSeverity.MAJOR,
                path="docs/impact.md",
                blocking=True,
            ),
        ),
        recommended_next_role=MissionRole.IMPLEMENTER,
        verdict=ReviewerVerdict.REMEDIATION_REQUIRED,
    )

    assert validate(candidate).is_valid
    assert "files_changed" not in to_dict(candidate)


def test_unknown_verdict_and_missing_field_are_rejected() -> None:
    unknown = mapping(verdict="CERTIFIED")
    missing = mapping()
    del missing["dimensions_reviewed"]

    assert not validate(unknown).is_valid
    assert not validate(missing).is_valid


@pytest.mark.parametrize("field", ["mission_id", "subject", "user_story_id", "observed_commit"])
def test_result_context_must_match_reviewer_input(field: str) -> None:
    value = "0" * 40 if field == "observed_commit" else "US-9999" if field == "user_story_id" else "different"

    validation = validate(mapping(**{field: value}))

    assert any(issue.code == "REVIEWER_CONTEXT_MISMATCH" for issue in validation.errors)


def test_tester_ready_but_authority_bypass_requires_remediation() -> None:
    context = reviewer_input()
    authority_finding = finding(
        dimension=ReviewDimension.AUTHORITY_SAFETY,
        severity=ReviewSeverity.CRITICAL,
        path="src/feature.py",
        blocking=True,
    )
    candidate = reviewer_result(
        findings=(authority_finding,),
        recommended_next_role=MissionRole.IMPLEMENTER,
        verdict=ReviewerVerdict.REMEDIATION_REQUIRED,
    )

    validation = ReviewerResultValidator().validate(candidate, reviewer_input=context)

    assert context.tester_result.verdict is TesterVerdict.READY_FOR_REVIEW
    assert validation.is_valid
    assert candidate.verdict is ReviewerVerdict.REMEDIATION_REQUIRED


def test_duplication_of_authority_logic_requires_remediation_without_edits() -> None:
    candidate = reviewer_result(
        findings=(
            finding(
                dimension=ReviewDimension.DUPLICATION,
                severity=ReviewSeverity.MAJOR,
                blocking=True,
            ),
        ),
        recommended_next_role=MissionRole.IMPLEMENTER,
        verdict=ReviewerVerdict.REMEDIATION_REQUIRED,
    )

    assert validate(candidate).is_valid
    assert "files_changed" not in to_dict(candidate)


def test_reviewer_cannot_claim_certification_or_human_approval() -> None:
    for candidate in (
        mapping(verdict="CERTIFIED"),
        mapping(human_approval={"approved": True}),
        mapping(certification={"result": "CERTIFIED"}),
    ):
        assert not validate(candidate).is_valid


@pytest.mark.parametrize("target", ["story", "implementation", "testing", "context"])
def test_context_alteration_is_rejected(target: str) -> None:
    context = reviewer_input()
    if target == "story":
        context.user_story.status = UserStoryStatus.CERTIFICATION
    elif target == "implementation":
        object.__setattr__(context.implementer_result, "summary", "Changed")
    elif target == "testing":
        object.__setattr__(context.tester_result, "summary", "Changed")
    else:
        object.__setattr__(context, "objective", "Changed")

    validation = ReviewerResultValidator().validate(
        reviewer_result(), reviewer_input=context
    )

    assert validation.errors[0].code == "REVIEWER_INPUT_TAMPERED"


def test_validation_never_changes_status_or_creates_control_plane_evidence() -> None:
    context = reviewer_input()
    before = to_dict(context.user_story)

    validation = ReviewerResultValidator().validate(
        reviewer_result(), reviewer_input=context
    )

    assert validation.is_valid
    assert to_dict(context.user_story) == before
    assert context.user_story.status is UserStoryStatus.REVIEW
    assert "evidence_id" not in to_dict(reviewer_result())
