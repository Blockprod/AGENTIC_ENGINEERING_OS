from datetime import datetime

import pytest

from agentic_engineering_os.application import (
    AcceptanceCheck, ArchitectResult, ArchitectVerdict, ArtifactCheck,
    CertifierFinding, CertifierInput, CertifierInputError,
    CertifierRecommendedAction, CertifierResult, CertifierResultValidator,
    CertifierVerdict, GateCheck, HumanApprovalCheck, ImplementerResult,
    ImplementerVerdict, ReviewDimension, ReviewerResult, ReviewerVerdict,
    RoleHandoff, TestCaseType, TesterAcceptanceResult, TesterPlan, TesterResult,
    TesterTestCase, TesterVerdict, TesterVerificationResult, VerificationOutcome,
    VerificationResult,
)
from agentic_engineering_os.domain import (
    AcceptanceCriterion, Evidence, EvidenceType, Gate, GateResult, HumanApproval,
    MissionRole, OperatingStep, RiskLevel, UserStory, UserStoryMetadata,
    UserStoryScope, UserStoryStatus, to_dict,
)

for _contract_type in (TestCaseType, TesterAcceptanceResult, TesterPlan, TesterResult, TesterTestCase, TesterVerdict, TesterVerificationResult):
    _contract_type.__test__ = False


COMMIT = "b390426f356e6fe8a9f638c9b04e807f75ae5ced"
OTHER_COMMIT = "a" * 40
NOW = datetime.fromisoformat("2026-08-28T20:00:00+02:00")
COMMAND = "python -m pytest tests/test_feature.py"
DEFAULT = object()


def story(*, status=UserStoryStatus.CERTIFICATION, human=False, producer="Alice"):
    return UserStory(
        "1.0", "US-0001", "Feature", "A controlled feature dossier.", status, 1,
        RiskLevel.HIGH, (), UserStoryScope(("src/", "tests/"), ("src/no.py",)),
        (AcceptanceCriterion("AC-001", "Behavior works.", True),),
        ("GATE-TESTS",),
        HumanApproval(human, human, producer if human else None, NOW if human else None),
        UserStoryMetadata(NOW, "Codex/Architect", NOW),
    )


def architect(**overrides):
    values = dict(
        mission_id="P2.8", subject="US-0001", observed_commit=COMMIT,
        summary="Architecture ready.", assumptions=(), decisions=(), risks=(), blockers=(),
        user_stories=(story(status=UserStoryStatus.PROPOSED),),
        recommended_next_role=MissionRole.IMPLEMENTER, verdict=ArchitectVerdict.READY,
    )
    values.update(overrides)
    return ArchitectResult(**values)


def implementer(**overrides):
    values = dict(
        mission_id="P2.8", subject="US-0001", user_story_id="US-0001",
        observed_commit=COMMIT, summary="Implemented.",
        files_changed=("src/feature.py", "tests/test_feature.py"),
        tests_added_or_modified=("tests/test_feature.py",), verification_commands=(COMMAND,),
        verification_results=(VerificationResult(COMMAND, True, VerificationOutcome.PASS, 0, "pass"),),
        assumptions=(), findings=(), blockers=(), recommended_next_role=MissionRole.TESTER,
        verdict=ImplementerVerdict.READY_FOR_TEST,
    )
    values.update(overrides)
    return ImplementerResult(**values)


def make_tester_result(**overrides):
    cases = tuple(TesterTestCase(f"TC-{i}", kind, "Exercise.", "Expected.", "Observed.", True, True, GateResult.PASS) for i, kind in enumerate(TestCaseType, 1))
    values = dict(
        mission_id="P2.8", subject="US-0001", user_story_id="US-0001", observed_commit=COMMIT,
        summary="Tested.", test_plan=TesterPlan(("AC-001",), ("p",), ("n",), ("e",), ("r",), (COMMAND,)),
        acceptance_results=(TesterAcceptanceResult("AC-001", GateResult.PASS, ("TC-1",), "pass"),),
        test_cases=cases, test_files_changed=("tests/test_feature.py",), verification_commands=(COMMAND,),
        verification_results=(TesterVerificationResult(COMMAND, True, True, GateResult.PASS, 0, "pass"),),
        findings=(), blockers=(), recommended_next_role=MissionRole.REVIEWER,
        verdict=TesterVerdict.READY_FOR_REVIEW,
    )
    values.update(overrides)
    return TesterResult(**values)


def reviewer(**overrides):
    values = dict(
        mission_id="P2.8", subject="US-0001", user_story_id="US-0001", observed_commit=COMMIT,
        summary="Reviewed.", dimensions_reviewed=tuple(ReviewDimension),
        reviewed_paths=("src/feature.py", "tests/test_feature.py"), findings=(), blockers=(),
        recommended_next_role=MissionRole.CERTIFIER, verdict=ReviewerVerdict.READY_FOR_CERTIFICATION,
    )
    values.update(overrides)
    return ReviewerResult(**values)


def evidence(identifier="EV-AC", *, kind=EvidenceType.ACCEPTANCE_CRITERION_CHECK, subject="AC-001", result="PASS", producer="Codex/Tester", source="pytest", commit=COMMIT):
    return Evidence(identifier, kind, subject, result, source, None, None, None, commit, NOW, producer)


def gate(*, result=GateResult.PASS, refs=("EV-GATE",)):
    return Gate("GATE-TESTS", "US-0001", True, result, refs, NOW, "Codex/Tester")


def handoff(**overrides):
    values = dict(from_role=MissionRole.ORCHESTRATOR, to_role=MissionRole.CERTIFIER,
        mission_id="P2.8", subject="US-0001", objective="Inspect proof dossier.",
        observed_commit=COMMIT, operating_step=OperatingStep.CONTROLLED_TRANSITION,
        blockers=(), instructions="Inspect without certifying.")
    values.update(overrides)
    return RoleHandoff(**values)


def certifier_input(*, assigned=None, architecture=DEFAULT, implementation=DEFAULT, testing=DEFAULT, review=DEFAULT, evidence_items=None, gates=None, authority=frozenset()):
    assigned_story = assigned or story()
    if architecture is DEFAULT:
        candidate_story = story(status=UserStoryStatus.PROPOSED)
        candidate_story.human_approval.required = assigned_story.human_approval.required
        architecture = architect(user_stories=(candidate_story,))
    return CertifierInput.from_handoff(
        handoff(), assigned_story, architecture,
        implementer() if implementation is DEFAULT else implementation,
        make_tester_result() if testing is DEFAULT else testing, reviewer() if review is DEFAULT else review,
        (evidence(), evidence("EV-GATE", kind=EvidenceType.TEST_RESULT, subject="US-0001", result=True)) if evidence_items is None else evidence_items,
        (gate(),) if gates is None else gates,
        authorized_not_applicable_gate_ids=authority,
    )


def result(**overrides):
    values = dict(
        mission_id="P2.8", subject="US-0001", user_story_id="US-0001", observed_commit=COMMIT,
        summary="Dossier ready.",
        artifact_checks=tuple(ArtifactCheck(role, True, True, "Present and coherent.") for role in (MissionRole.ARCHITECT, MissionRole.IMPLEMENTER, MissionRole.TESTER, MissionRole.REVIEWER)),
        acceptance_checks=(AcceptanceCheck("AC-001", True, GateResult.PASS, ("EV-AC",), "Supported."),),
        gate_checks=(GateCheck("GATE-TESTS", True, True, GateResult.PASS, ("EV-GATE",), True, False, "Supported."),),
        evidence_refs=("EV-AC", "EV-GATE"),
        human_approval_check=HumanApprovalCheck(False, False, True, None, "Not required."),
        findings=(), blockers=(), recommended_action=CertifierRecommendedAction.SUBMIT_TO_CONTROL_PLANE,
        verdict=CertifierVerdict.READY_FOR_CONTROL_PLANE,
    )
    values.update(overrides)
    return CertifierResult(**values)


def validate(candidate=None, context=None):
    return CertifierResultValidator().validate(candidate or result(), certifier_input=context or certifier_input())


def test_nominal_dossier_is_ready_for_control_plane():
    assert validate().is_valid


def test_schema_fixes_role_to_certifier():
    data = to_dict(result()); data["role"] = "REVIEWER"
    assert not validate(data).is_valid


def test_input_is_deep_copied_and_does_not_mutate_story():
    original = story()
    context = certifier_input(assigned=original)
    assert context.user_story is not original and original.status is UserStoryStatus.CERTIFICATION


@pytest.mark.parametrize("field,value", [
    ("to_role", MissionRole.REVIEWER), ("from_role", MissionRole.REVIEWER),
    ("operating_step", OperatingStep.REPORT), ("observed_commit", "HEAD"),
])
def test_invalid_handoff_is_refused(field, value):
    with pytest.raises(CertifierInputError):
        CertifierInput.from_handoff(handoff(**{field: value}), story(), architect(), implementer(), make_tester_result(), reviewer(), (), ())


def test_story_must_be_in_certification_state():
    with pytest.raises(CertifierInputError, match="CERTIFICATION"):
        certifier_input(assigned=story(status=UserStoryStatus.REVIEW))


@pytest.mark.parametrize("field", ["mission_id", "subject", "user_story_id", "observed_commit"])
def test_context_mismatch_is_rejected(field):
    data = to_dict(result()); data[field] = OTHER_COMMIT if field == "observed_commit" else "wrong"
    assert not validate(data).is_valid


@pytest.mark.parametrize("artifact_field", ["architect_result", "implementer_result", "tester_result", "reviewer_result"])
def test_missing_prior_artifact_forbids_ready(artifact_field):
    args = dict(architecture=architect(), implementation=implementer(), testing=make_tester_result(), review=reviewer())
    args[{"architect_result":"architecture", "implementer_result":"implementation", "tester_result":"testing", "reviewer_result":"review"}[artifact_field]] = None
    context = certifier_input(**args)
    assert not validate(context=context).is_valid


def test_wrong_prior_commit_forbids_ready():
    assert not validate(context=certifier_input(implementation=implementer(observed_commit=OTHER_COMMIT))).is_valid


def test_missing_artifact_can_be_reported_as_blocked():
    context = certifier_input(architecture=None)
    checks = list(result().artifact_checks)
    checks[0] = ArtifactCheck(MissionRole.ARCHITECT, False, False, "Missing.")
    candidate = result(artifact_checks=tuple(checks), blockers=("ArchitectResult missing",), recommended_action=CertifierRecommendedAction.RESOLVE_BLOCKERS, verdict=CertifierVerdict.BLOCKED)
    assert CertifierResultValidator().validate(candidate, certifier_input=context).is_valid


def test_reviewer_must_be_ready_for_certification():
    review = reviewer(verdict=ReviewerVerdict.BLOCKED, blockers=("unknown",), recommended_next_role=MissionRole.ORCHESTRATOR)
    assert not validate(context=certifier_input(review=review)).is_valid


def test_missing_acceptance_result_forbids_ready():
    assert not validate(context=certifier_input(testing=make_tester_result(acceptance_results=()))).is_valid


@pytest.mark.parametrize("ac_result", [GateResult.FAIL, GateResult.UNKNOWN])
def test_fail_or_unknown_acceptance_forbids_ready(ac_result):
    changed = make_tester_result(acceptance_results=(TesterAcceptanceResult("AC-001", ac_result, (), "observed"),))
    assert not validate(context=certifier_input(testing=changed)).is_valid


@pytest.mark.parametrize("gate_result", [GateResult.FAIL, GateResult.UNKNOWN])
def test_fail_or_unknown_gate_forbids_ready(gate_result):
    changed_result = result(gate_checks=(GateCheck("GATE-TESTS", True, True, gate_result, ("EV-GATE",), True, False, "Observed."),))
    assert not validate(changed_result, certifier_input(gates=(gate(result=gate_result),))).is_valid


def test_missing_gate_forbids_ready():
    assert not validate(context=certifier_input(gates=())).is_valid


def test_missing_evidence_is_blocked_not_remediation():
    context = certifier_input(evidence_items=(evidence(),))
    blocked = result(
        gate_checks=(GateCheck("GATE-TESTS", True, True, GateResult.PASS, ("EV-GATE",), False, False, "Evidence missing."),),
        evidence_refs=("EV-AC",), blockers=("EV-GATE is unavailable",),
        recommended_action=CertifierRecommendedAction.RESOLVE_BLOCKERS, verdict=CertifierVerdict.BLOCKED,
    )
    assert CertifierResultValidator().validate(blocked, certifier_input=context).is_valid


def test_gate_fail_produces_valid_remediation_without_mutation():
    context = certifier_input(gates=(gate(result=GateResult.FAIL),))
    before = to_dict(context.user_story)
    remediation = result(
        gate_checks=(GateCheck("GATE-TESTS", True, True, GateResult.FAIL, ("EV-GATE",), True, False, "Gate failed."),),
        findings=(CertifierFinding("GATE_FAIL", "Required Gate failed.", True),),
        recommended_action=CertifierRecommendedAction.RETURN_FOR_REMEDIATION,
        verdict=CertifierVerdict.REMEDIATION_REQUIRED,
    )
    assert CertifierResultValidator().validate(remediation, certifier_input=context).is_valid
    assert to_dict(context.user_story) == before


def test_not_applicable_requires_explicit_authority():
    check = GateCheck("GATE-TESTS", True, True, GateResult.NOT_APPLICABLE, (), True, False, "No authority.")
    assert not validate(result(gate_checks=(check,), evidence_refs=("EV-AC",)), certifier_input(gates=(gate(result=GateResult.NOT_APPLICABLE, refs=()),))).is_valid


def test_not_applicable_with_explicit_authority_can_be_ready():
    context = certifier_input(gates=(gate(result=GateResult.NOT_APPLICABLE, refs=()),), authority=frozenset({"GATE-TESTS"}))
    candidate = result(gate_checks=(GateCheck("GATE-TESTS", True, True, GateResult.NOT_APPLICABLE, (), True, True, "Explicit authority supplied."),), evidence_refs=("EV-AC",))
    assert CertifierResultValidator().validate(candidate, certifier_input=context).is_valid


def test_valid_human_approval_evidence_is_accepted():
    human = evidence("EV-HUMAN", kind=EvidenceType.HUMAN_APPROVAL, subject="US-0001", result=True, producer="Alice", source="Human")
    context = certifier_input(assigned=story(human=True), evidence_items=(evidence(), evidence("EV-GATE", kind=EvidenceType.TEST_RESULT, subject="US-0001", result=True), human))
    candidate = result(evidence_refs=("EV-AC", "EV-GATE", "EV-HUMAN"), human_approval_check=HumanApprovalCheck(True, True, True, "EV-HUMAN", "Attributable."))
    assert CertifierResultValidator().validate(candidate, certifier_input=context).is_valid


def test_missing_required_human_approval_forbids_ready():
    assert not validate(context=certifier_input(assigned=story(human=True), evidence_items=(evidence(), evidence("EV-GATE", kind=EvidenceType.TEST_RESULT, subject="US-0001", result=True)))).is_valid


@pytest.mark.parametrize("producer", ["Codex/FakeHuman", "codex/FakeHuman", "CODEX/FakeHuman", "CoDeX/FakeHuman"])
def test_codex_identity_cannot_supply_human_approval(producer):
    human = evidence("EV-HUMAN", kind=EvidenceType.HUMAN_APPROVAL, subject="US-0001", result=True, producer=producer, source="Human")
    context = certifier_input(assigned=story(human=True, producer=producer), evidence_items=(evidence(), evidence("EV-GATE", kind=EvidenceType.TEST_RESULT, subject="US-0001", result=True), human))
    assert not validate(context=context).is_valid


def test_tampered_context_is_rejected():
    context = certifier_input(); context.user_story.title = "tampered"
    assert not validate(context=context).is_valid


@pytest.mark.parametrize("verdict", ["CERTIFIED", "REJECTED", "PASS", "MAGIC"])
def test_control_plane_verdicts_are_forbidden(verdict):
    data = to_dict(result()); data["verdict"] = verdict
    assert not validate(data).is_valid


def test_blocker_forbids_ready():
    data = to_dict(result()); data["blockers"] = ["active"]
    assert not validate(data).is_valid


def test_required_result_field_cannot_be_omitted():
    data = to_dict(result()); del data["recommended_action"]
    assert not validate(data).is_valid


def test_remediation_requires_demonstrated_failure():
    candidate = result(findings=(CertifierFinding("UNKNOWN", "Missing information.", False),), recommended_action=CertifierRecommendedAction.RETURN_FOR_REMEDIATION, verdict=CertifierVerdict.REMEDIATION_REQUIRED)
    assert not validate(candidate).is_valid


def test_certifier_has_no_self_certification_or_persistence_api():
    assert not hasattr(CertifierResultValidator, "certify")
    assert not hasattr(CertifierResultValidator, "save")
    assert not hasattr(CertifierResult, "evidence_type")
