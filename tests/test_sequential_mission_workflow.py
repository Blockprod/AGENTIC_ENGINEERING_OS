from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    ALLOWED_TRANSITIONS,
    AcceptanceCheck,
    AcceptanceResult,
    ArchitectResult,
    ArchitectVerdict,
    ArtifactCheck,
    CertificationContext,
    CertificationService,
    CertifierFinding,
    CertifierRecommendedAction,
    CertifierResult,
    CertifierVerdict,
    ContractValidator,
    ControlLoop,
    EvidenceObservation,
    EvidenceProvenance,
    EvidenceRecorder,
    GateCheck,
    GateContract,
    GateEvaluationContext,
    GateEvaluator,
    HumanApprovalCheck,
    ImplementerResult,
    ImplementerVerdict,
    Orchestrator,
    ProvenanceKind,
    ReviewDimension,
    ReviewFinding,
    ReviewSeverity,
    ReviewerResult,
    ReviewerVerdict,
    SequentialMissionWorkflow,
    SequentialMissionWorkflowError,
    StateTransitionService,
    TestCaseType,
    TesterAcceptanceResult,
    TesterInputError as WorkflowTesterInputError,
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
    EvidenceType,
    GateResult,
    HumanApproval,
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
)
from agentic_engineering_os.infrastructure import MissionStateStore, ProjectStateStore


COMMIT = "449f757d069c5e6a5febe58a67911193ac4dcb17"
OTHER_COMMIT = "a" * 40
NOW = datetime(2026, 8, 28, 22, 0, tzinfo=timezone.utc)
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


def proposed_story(*, human_required: bool = False) -> UserStory:
    return UserStory(
        schema_version="1.0",
        id="US-0001",
        title="Sequential mission",
        description="Exercise the complete deterministic role chain.",
        status=UserStoryStatus.PROPOSED,
        priority=1,
        risk=RiskLevel.HIGH,
        depends_on=(),
        scope=UserStoryScope(("src/", "tests/"), ("src/forbidden.py",)),
        acceptance_criteria=(
            AcceptanceCriterion("AC-001", "The workflow completes.", True),
        ),
        required_gates=("GATE-TESTS",),
        human_approval=HumanApproval(human_required, False, None, None),
        metadata=UserStoryMetadata(NOW, "Codex/Architect", NOW),
    )


def architect_result(*, human_required: bool = False) -> ArchitectResult:
    return ArchitectResult(
        mission_id="P2.9",
        workflow_generation=0,
        subject="US-0001",
        observed_commit=COMMIT,
        summary="One minimal User Story is specified.",
        assumptions=(),
        decisions=(),
        risks=(),
        blockers=(),
        user_stories=(proposed_story(human_required=human_required),),
        recommended_next_role=MissionRole.IMPLEMENTER,
        verdict=ArchitectVerdict.READY,
    )


def implementer_result(
    *, version: int = 1, workflow_generation: int = 0
) -> ImplementerResult:
    return ImplementerResult(
        mission_id="P2.9",
        workflow_generation=workflow_generation,
        subject="US-0001",
        user_story_id="US-0001",
        observed_commit=COMMIT,
        summary=f"Implementation version {version} is ready.",
        files_changed=("src/feature.py", "tests/test_feature.py"),
        tests_added_or_modified=("tests/test_feature.py",),
        verification_commands=(COMMAND,),
        verification_results=(
            VerificationResult(COMMAND, True, VerificationOutcome.PASS, 0, "pass"),
        ),
        assumptions=(),
        findings=(),
        blockers=(),
        recommended_next_role=MissionRole.TESTER,
        verdict=ImplementerVerdict.READY_FOR_TEST,
    )


def blocked_implementer_result() -> ImplementerResult:
    candidate = implementer_result()
    return replace(
        candidate,
        files_changed=(),
        tests_added_or_modified=(),
        verification_results=(
            VerificationResult(COMMAND, True, VerificationOutcome.FAIL, 1, "failed"),
        ),
        blockers=("Required verification failed.",),
        recommended_next_role=MissionRole.ORCHESTRATOR,
        verdict=ImplementerVerdict.BLOCKED,
    )


def _test_cases(*, failed: bool = False) -> tuple[TesterTestCase, ...]:
    cases = []
    for index, kind in enumerate(TestCaseType, 1):
        verdict = (
            GateResult.FAIL
            if failed and kind is TestCaseType.REGRESSION
            else GateResult.PASS
        )
        cases.append(
            TesterTestCase(
                f"TC-{index:03d}",
                kind,
                "Exercise behavior.",
                "Expected result.",
                "Observed result.",
                True,
                True,
                verdict,
            )
        )
    return tuple(cases)


def make_tester_result(
    *,
    failed: bool = False,
    blocked: bool = False,
    workflow_generation: int = 0,
) -> TesterResult:
    if blocked:
        acceptance = GateResult.UNKNOWN
        cases = (
            TesterTestCase(
                "TC-001",
                TestCaseType.POSITIVE,
                "Exercise behavior.",
                "Expected result.",
                "Not executed.",
                True,
                False,
                GateResult.UNKNOWN,
            ),
        )
        verification = GateResult.UNKNOWN
        verdict = TesterVerdict.BLOCKED
        findings: tuple[str, ...] = ()
        blockers = ("Required environment is unavailable.",)
        next_role = MissionRole.ORCHESTRATOR
    elif failed:
        acceptance = GateResult.FAIL
        cases = _test_cases(failed=True)
        verification = GateResult.FAIL
        verdict = TesterVerdict.REMEDIATION_REQUIRED
        findings = ("Regression failure is demonstrated.",)
        blockers = ()
        next_role = MissionRole.IMPLEMENTER
    else:
        acceptance = GateResult.PASS
        cases = _test_cases()
        verification = GateResult.PASS
        verdict = TesterVerdict.READY_FOR_REVIEW
        findings = ()
        blockers = ()
        next_role = MissionRole.REVIEWER
    executed = not blocked
    exit_code = 1 if failed else 0 if executed else None
    return TesterResult(
        mission_id="P2.9",
        workflow_generation=workflow_generation,
        subject="US-0001",
        user_story_id="US-0001",
        observed_commit=COMMIT,
        summary="Independent verification completed.",
        test_plan=TesterPlan(
            ("AC-001",),
            ("positive",),
            ("negative",),
            ("edge",),
            ("regression",),
            (COMMAND,),
        ),
        acceptance_results=(
            TesterAcceptanceResult("AC-001", acceptance, ("TC-001",), "observed"),
        ),
        test_cases=cases,
        test_files_changed=("tests/test_feature.py",) if executed else (),
        verification_commands=(COMMAND,),
        verification_results=(
            TesterVerificationResult(
                COMMAND,
                True,
                executed,
                verification,
                exit_code,
                "observed",
            ),
        ),
        findings=findings,
        blockers=blockers,
        recommended_next_role=next_role,
        verdict=verdict,
    )


def reviewer_result(
    *,
    remediation: bool = False,
    blocked: bool = False,
    workflow_generation: int = 0,
) -> ReviewerResult:
    if remediation:
        findings = (
            ReviewFinding(
                "RF-001",
                ReviewDimension.AUTHORITY_SAFETY,
                ReviewSeverity.CRITICAL,
                "Authority boundary bypass.",
                ("A direct mutation was observed.",),
                ("src/feature.py",),
                True,
            ),
        )
        blockers: tuple[str, ...] = ()
        verdict = ReviewerVerdict.REMEDIATION_REQUIRED
        next_role = MissionRole.IMPLEMENTER
    elif blocked:
        findings = ()
        blockers = ("Review context is unavailable.",)
        verdict = ReviewerVerdict.BLOCKED
        next_role = MissionRole.ORCHESTRATOR
    else:
        findings = ()
        blockers = ()
        verdict = ReviewerVerdict.READY_FOR_CERTIFICATION
        next_role = MissionRole.CERTIFIER
    return ReviewerResult(
        mission_id="P2.9",
        workflow_generation=workflow_generation,
        subject="US-0001",
        user_story_id="US-0001",
        observed_commit=COMMIT,
        summary="Engineering review completed.",
        dimensions_reviewed=tuple(ReviewDimension) if not blocked else (),
        reviewed_paths=("src/feature.py", "tests/test_feature.py") if not blocked else (),
        findings=findings,
        blockers=blockers,
        recommended_next_role=next_role,
        verdict=verdict,
    )


def certifier_result(
    *,
    not_applicable: bool = False,
    human: bool = False,
    workflow_generation: int = 0,
) -> CertifierResult:
    gate_result = GateResult.NOT_APPLICABLE if not_applicable else GateResult.PASS
    gate_refs = () if not_applicable else ("EV-GATE",)
    evidence_refs = ["EV-AC"]
    if not not_applicable:
        evidence_refs.append("EV-GATE")
    if human:
        evidence_refs.append("EV-HUMAN")
    return CertifierResult(
        mission_id="P2.9",
        workflow_generation=workflow_generation,
        subject="US-0001",
        user_story_id="US-0001",
        observed_commit=COMMIT,
        summary="The dossier is ready for the Control Plane.",
        artifact_checks=tuple(
            ArtifactCheck(role, True, True, "Present and coherent.")
            for role in (
                MissionRole.ARCHITECT,
                MissionRole.IMPLEMENTER,
                MissionRole.TESTER,
                MissionRole.REVIEWER,
            )
        ),
        acceptance_checks=(
            AcceptanceCheck("AC-001", True, GateResult.PASS, ("EV-AC",), "Proven."),
        ),
        gate_checks=(
            GateCheck(
                "GATE-TESTS",
                True,
                True,
                gate_result,
                gate_refs,
                True,
                not_applicable,
                "Evaluated by GateEvaluator.",
            ),
        ),
        evidence_refs=tuple(evidence_refs),
        human_approval_check=HumanApprovalCheck(
            human,
            human,
            True,
            "EV-HUMAN" if human else None,
            "Attributable." if human else "Not required.",
        ),
        findings=(),
        blockers=(),
        recommended_action=CertifierRecommendedAction.SUBMIT_TO_CONTROL_PLANE,
        verdict=CertifierVerdict.READY_FOR_CONTROL_PLANE,
    )


def blocked_certifier_result() -> CertifierResult:
    return replace(
        certifier_result(),
        gate_checks=(
            GateCheck(
                "GATE-TESTS",
                False,
                False,
                GateResult.UNKNOWN,
                (),
                False,
                False,
                "Gate unavailable.",
            ),
        ),
        evidence_refs=("EV-AC",),
        blockers=("Required Gate is unavailable.",),
        recommended_action=CertifierRecommendedAction.RESOLVE_BLOCKERS,
        verdict=CertifierVerdict.BLOCKED,
    )


def remediation_certifier_result() -> CertifierResult:
    return replace(
        certifier_result(),
        gate_checks=(
            GateCheck(
                "GATE-TESTS",
                True,
                True,
                GateResult.FAIL,
                ("EV-GATE",),
                True,
                False,
                "Gate failure is proven.",
            ),
        ),
        findings=(CertifierFinding("GATE_FAIL", "Required Gate failed.", True),),
        recommended_action=CertifierRecommendedAction.RETURN_FOR_REMEDIATION,
        verdict=CertifierVerdict.REMEDIATION_REQUIRED,
    )


def mission() -> MissionState:
    return MissionState(
        schema_version="1.0",
        mission_id="P2.9",
        workflow_generation=0,
        status=MissionStatus.ACTIVE,
        role=MissionRole.ORCHESTRATOR,
        objective="Execute one sequential agentic mission.",
        subject="US-0001",
        operating_step=OperatingStep.UNDERSTAND_CONTRACT,
        next_action="Route to Architect.",
        observed_commit=COMMIT,
        updated_at=NOW,
    )


def make_workflow(tmp_path: Path, *, initialize: bool = True) -> SequentialMissionWorkflow:
    project_store = ProjectStateStore(tmp_path)
    mission_store = MissionStateStore(tmp_path)
    if initialize:
        project_store.initialize()
        mission_store.initialize(mission())
    validator = ContractValidator()
    control_loop = ControlLoop(
        state_store=project_store,
        evidence_recorder_factory=lambda target: EvidenceRecorder(
            target,
            validator=validator,
            clock=lambda: NOW,
        ),
        gate_evaluator=GateEvaluator(validator=validator, clock=lambda: NOW),
        certification_service=CertificationService(
            validator=validator,
            clock=lambda: NOW,
        ),
        transition_service=StateTransitionService(),
    )
    return SequentialMissionWorkflow(
        orchestrator=Orchestrator(
            repository_root=tmp_path,
            mission_store=mission_store,
            project_state_store=project_store,
        ),
        mission_store=mission_store,
        project_store=project_store,
        control_loop=control_loop,
    )


def route(workflow: SequentialMissionWorkflow):
    result = workflow.route(current_commit=COMMIT, updated_at=NOW)
    assert result.handoff is not None
    return result.handoff


def record_acceptance(
    workflow: SequentialMissionWorkflow,
    tested: TesterResult,
    *,
    result: bool,
    evidence_id: str = "EV-AC",
) -> None:
    workflow.record_acceptance_evidence(
        tested,
        EvidenceObservation(
            evidence_type=EvidenceType.ACCEPTANCE_CRITERION_CHECK,
            subject="AC-001",
            result=result,
            provenance=EvidenceProvenance(
                ProvenanceKind.CODEX,
                "pytest",
                "Codex/Tester",
            ),
            repository_dependent=True,
            artifact="test output",
            commit=COMMIT,
        ),
        evidence_id=evidence_id,
        timestamp=NOW,
    )


def record_gate(
    workflow: SequentialMissionWorkflow,
    *,
    not_applicable: bool = False,
    result: GateResult = GateResult.PASS,
) -> None:
    refs: tuple[str, ...]
    if not_applicable:
        refs = ()
        condition = lambda _: GateResult.NOT_APPLICABLE
        context = GateEvaluationContext(not_applicable_reason="Explicitly inapplicable")
    else:
        workflow.record_evidence(
            EvidenceObservation(
                EvidenceType.TEST_RESULT,
                "US-0001",
                True,
                EvidenceProvenance(
                    ProvenanceKind.TOOL,
                    "pytest",
                    "pytest",
                ),
                True,
                artifact="gate output",
                commit=COMMIT,
            ),
            evidence_id="EV-GATE",
            timestamp=NOW,
        )
        refs = ("EV-GATE",)
        condition = lambda _: result
        context = GateEvaluationContext(expected_commit=COMMIT)
    workflow.evaluate_gate(
        GateContract(
            "GATE-TESTS",
            "US-0001",
            True,
            refs,
            condition,
            not not_applicable,
            "Codex/Tester",
        ),
        context=context,
        evaluated_at=NOW,
    )


def advance_to_tester(
    workflow: SequentialMissionWorkflow,
    *,
    architecture: ArchitectResult | None = None,
    implementation: ImplementerResult | None = None,
) -> tuple[ArchitectResult, ImplementerResult]:
    architecture = architecture or architect_result()
    implementation = implementation or implementer_result()
    workflow.accept_architect(route(workflow), architecture, updated_at=NOW)
    workflow.accept_implementer(route(workflow), implementation, updated_at=NOW)
    return architecture, implementation


def advance_to_certifier(
    workflow: SequentialMissionWorkflow,
    *,
    record_authoritative_gate: bool = True,
) -> tuple[ArchitectResult, ImplementerResult, TesterResult, ReviewerResult]:
    architecture, implementation = advance_to_tester(workflow)
    testing = make_tester_result()
    workflow.accept_tester(
        route(workflow),
        testing,
        implementer_result=implementation,
        updated_at=NOW,
    )
    record_acceptance(workflow, testing, result=True)
    review = reviewer_result()
    workflow.accept_reviewer(
        route(workflow),
        review,
        implementer_result=implementation,
        tester_result=testing,
        updated_at=NOW,
    )
    if record_authoritative_gate:
        record_gate(workflow)
    return architecture, implementation, testing, review


def finish(
    workflow: SequentialMissionWorkflow,
    artifacts: tuple[ArchitectResult, ImplementerResult, TesterResult, ReviewerResult],
    *,
    not_applicable: bool = False,
    human: bool = False,
    handoff=None,
) -> None:
    architecture, implementation, testing, review = artifacts
    allowance = frozenset({"GATE-TESTS"}) if not_applicable else frozenset()
    context = CertificationContext(
        allowed_not_applicable_gate_ids=allowance,
        human_approval_evidence_id="EV-HUMAN" if human else None,
    )
    result = workflow.submit_control_plane(
        handoff or route(workflow),
        certifier_result(
            not_applicable=not_applicable,
            human=human,
            workflow_generation=review.workflow_generation,
        ),
        architect_result=architecture,
        implementer_result=implementation,
        tester_result=testing,
        reviewer_result=review,
        acceptance_results=(
            AcceptanceResult("AC-001", GateResult.PASS, ("EV-AC",)),
        ),
        certification_context=context,
        certifier="Codex/Certifier",
        current_commit=COMMIT,
        updated_at=NOW,
        authorized_not_applicable_gate_ids=allowance,
        certification_id="CERT-P2-9",
    )
    assert result.status is MissionStatus.COMPLETED


def test_normal_golden_path_persists_certified_terminal_state(tmp_path: Path) -> None:
    workflow = make_workflow(tmp_path)
    artifacts = advance_to_certifier(workflow)

    finish(workflow, artifacts)

    project = ProjectStateStore(tmp_path).load()
    persisted_mission = MissionStateStore(tmp_path).load()
    assert project.user_stories[0].status is UserStoryStatus.CERTIFIED
    assert project.certifications[0].result.value == "CERTIFIED"
    assert persisted_mission.status is MissionStatus.COMPLETED
    assert persisted_mission.blockers == []


def test_workflow_transitions_are_a_subset_of_the_28_normative_transitions() -> None:
    used = {
        (UserStoryStatus.PROPOSED, UserStoryStatus.PLANNED),
        (UserStoryStatus.PLANNED, UserStoryStatus.READY),
        (UserStoryStatus.READY, UserStoryStatus.IN_PROGRESS),
        (UserStoryStatus.IN_PROGRESS, UserStoryStatus.IMPLEMENTED),
        (UserStoryStatus.IN_PROGRESS, UserStoryStatus.BLOCKED),
        (UserStoryStatus.IMPLEMENTED, UserStoryStatus.TESTING),
        (UserStoryStatus.TESTING, UserStoryStatus.REVIEW),
        (UserStoryStatus.TESTING, UserStoryStatus.REJECTED),
        (UserStoryStatus.REVIEW, UserStoryStatus.CERTIFICATION),
        (UserStoryStatus.REVIEW, UserStoryStatus.REJECTED),
        (UserStoryStatus.CERTIFICATION, UserStoryStatus.CERTIFIED),
        (UserStoryStatus.CERTIFICATION, UserStoryStatus.REJECTED),
        (UserStoryStatus.REJECTED, UserStoryStatus.REMEDIATION_REQUIRED),
        (UserStoryStatus.REMEDIATION_REQUIRED, UserStoryStatus.READY),
    }

    assert len(ALLOWED_TRANSITIONS) == 28
    assert used < ALLOWED_TRANSITIONS


def test_restart_after_tester_reloads_and_finishes_without_runtime_instances(
    tmp_path: Path,
) -> None:
    first = make_workflow(tmp_path)
    architecture, implementation = advance_to_tester(first)
    testing = make_tester_result()
    first.accept_tester(
        route(first),
        testing,
        implementer_result=implementation,
        updated_at=NOW,
    )
    record_acceptance(first, testing, result=True)

    resumed = make_workflow(tmp_path, initialize=False)
    review = reviewer_result()
    resumed.accept_reviewer(
        route(resumed),
        review,
        implementer_result=implementation,
        tester_result=testing,
        updated_at=NOW,
    )
    record_gate(resumed)
    finish(resumed, (architecture, implementation, testing, review))

    assert ProjectStateStore(tmp_path).load().user_stories[0].status is UserStoryStatus.CERTIFIED
    assert MissionStateStore(tmp_path).load().status is MissionStatus.COMPLETED


def test_commit_divergence_forces_reconstruct_and_forbids_stage_use(
    tmp_path: Path,
) -> None:
    workflow = make_workflow(tmp_path)

    routed = workflow.route(current_commit=OTHER_COMMIT, updated_at=NOW)

    assert routed.current_step is OperatingStep.RECONSTRUCT
    assert routed.current_role is MissionRole.ORCHESTRATOR
    with pytest.raises(SequentialMissionWorkflowError) as captured:
        workflow.accept_architect(
            replace(routed.handoff, to_role=MissionRole.ARCHITECT),
            architect_result(),
            updated_at=NOW,
        )
    assert captured.value.code == "ROLE_CHAIN_VIOLATION"


@pytest.mark.parametrize("payload", ["PASS", "FAIL", 1, 0, False])
def test_acceptance_bridge_refuses_non_boolean_or_contradictory_payloads(
    tmp_path: Path,
    payload: object,
) -> None:
    workflow = make_workflow(tmp_path)
    _, implementation = advance_to_tester(workflow)
    testing = make_tester_result()
    workflow.accept_tester(
        route(workflow),
        testing,
        implementer_result=implementation,
        updated_at=NOW,
    )

    with pytest.raises(SequentialMissionWorkflowError) as captured:
        workflow.record_acceptance_evidence(
            testing,
            EvidenceObservation(
                EvidenceType.ACCEPTANCE_CRITERION_CHECK,
                "AC-001",
                payload,
                EvidenceProvenance(
                    ProvenanceKind.CODEX,
                    "pytest",
                    "Codex/Tester",
                ),
                True,
                commit=COMMIT,
            ),
            evidence_id="EV-BAD",
            timestamp=NOW,
        )
    assert captured.value.code == "ACCEPTANCE_EVIDENCE_CONTRADICTION"
    assert ProjectStateStore(tmp_path).load().evidence == []


def test_role_chain_bypasses_are_refused(tmp_path: Path) -> None:
    workflow = make_workflow(tmp_path)
    architect_handoff = route(workflow)

    with pytest.raises(SequentialMissionWorkflowError) as reviewer_bypass:
        workflow.accept_reviewer(
            architect_handoff,
            reviewer_result(),
            implementer_result=implementer_result(),
            tester_result=make_tester_result(),
            updated_at=NOW,
        )
    assert reviewer_bypass.value.code == "ROLE_CHAIN_VIOLATION"

    with pytest.raises(SequentialMissionWorkflowError) as control_bypass:
        workflow.submit_control_plane(
            architect_handoff,
            certifier_result(),
            architect_result=architect_result(),
            implementer_result=implementer_result(),
            tester_result=make_tester_result(),
            reviewer_result=reviewer_result(),
            acceptance_results=(),
            certification_context=CertificationContext(),
            certifier="Codex/Certifier",
            current_commit=COMMIT,
            updated_at=NOW,
        )
    assert control_bypass.value.code == "ROLE_CHAIN_VIOLATION"
    assert ProjectStateStore(tmp_path).load().certifications == []


def test_tester_remediation_requires_implementer_and_retest(tmp_path: Path) -> None:
    workflow = make_workflow(tmp_path)
    architecture, implementation_v1 = advance_to_tester(workflow)
    failed = make_tester_result(failed=True)
    record_acceptance(workflow, failed, result=False, evidence_id="EV-AC-FAIL")

    remediated = workflow.accept_tester(
        route(workflow),
        failed,
        implementer_result=implementation_v1,
        updated_at=NOW,
    )

    assert remediated.current_step is OperatingStep.ACT
    assert remediated.workflow_generation == 1
    assert ProjectStateStore(tmp_path).load().user_stories[0].status is UserStoryStatus.IN_PROGRESS
    implementer_handoff = route(workflow)
    with pytest.raises(SequentialMissionWorkflowError):
        workflow.accept_reviewer(
            implementer_handoff,
            reviewer_result(),
            implementer_result=implementation_v1,
            tester_result=failed,
            updated_at=NOW,
        )

    implementation_v2 = implementer_result(version=2, workflow_generation=1)
    workflow.accept_implementer(
        implementer_handoff,
        implementation_v2,
        updated_at=NOW,
    )
    passed = make_tester_result(workflow_generation=1)
    workflow.accept_tester(
        route(workflow),
        passed,
        implementer_result=implementation_v2,
        updated_at=NOW,
    )
    record_acceptance(workflow, passed, result=True)
    review = reviewer_result(workflow_generation=1)
    workflow.accept_reviewer(
        route(workflow),
        review,
        implementer_result=implementation_v2,
        tester_result=passed,
        updated_at=NOW,
    )
    record_gate(workflow)
    finish(workflow, (architecture, implementation_v2, passed, review))
    evidence = ProjectStateStore(tmp_path).load().evidence
    assert {item.result for item in evidence if item.subject == "AC-001"} == {False, True}


def test_reviewer_remediation_requires_implementer_retest_and_rereview(
    tmp_path: Path,
) -> None:
    workflow = make_workflow(tmp_path)
    architecture, implementation_v1 = advance_to_tester(workflow)
    testing_v1 = make_tester_result()
    workflow.accept_tester(
        route(workflow),
        testing_v1,
        implementer_result=implementation_v1,
        updated_at=NOW,
    )
    blocking_review = reviewer_result(remediation=True)
    remediation = workflow.accept_reviewer(
        route(workflow),
        blocking_review,
        implementer_result=implementation_v1,
        tester_result=testing_v1,
        updated_at=NOW,
    )
    assert remediation.current_step is OperatingStep.ACT
    assert remediation.workflow_generation == 1

    implementation_v2 = implementer_result(version=2, workflow_generation=1)
    workflow.accept_implementer(route(workflow), implementation_v2, updated_at=NOW)
    testing_v2 = make_tester_result(workflow_generation=1)
    tester_handoff = route(workflow)
    with pytest.raises(SequentialMissionWorkflowError):
        workflow.accept_reviewer(
            tester_handoff,
            reviewer_result(workflow_generation=1),
            implementer_result=implementation_v2,
            tester_result=testing_v2,
            updated_at=NOW,
        )
    workflow.accept_tester(
        tester_handoff,
        testing_v2,
        implementer_result=implementation_v2,
        updated_at=NOW,
    )
    record_acceptance(workflow, testing_v2, result=True)
    clean_review = reviewer_result(workflow_generation=1)
    workflow.accept_reviewer(
        route(workflow),
        clean_review,
        implementer_result=implementation_v2,
        tester_result=testing_v2,
        updated_at=NOW,
    )
    record_gate(workflow)
    finish(workflow, (architecture, implementation_v2, testing_v2, clean_review))


def test_tester_remediation_rejects_stale_future_mixed_and_wrong_commit_results(
    tmp_path: Path,
) -> None:
    workflow = make_workflow(tmp_path)
    architecture = architect_result()
    workflow.accept_architect(route(workflow), architecture, updated_at=NOW)
    stale_handoff = route(workflow)
    implementation_v0 = implementer_result()
    workflow.accept_implementer(stale_handoff, implementation_v0, updated_at=NOW)
    testing_v0 = make_tester_result(failed=True)
    workflow.accept_tester(
        route(workflow),
        testing_v0,
        implementer_result=implementation_v0,
        updated_at=NOW,
    )

    persisted = MissionStateStore(tmp_path).load()
    assert persisted.workflow_generation == 1
    with pytest.raises(SequentialMissionWorkflowError) as stale_handoff_error:
        workflow.accept_implementer(stale_handoff, implementation_v0, updated_at=NOW)
    assert stale_handoff_error.value.code == "ROLE_CHAIN_VIOLATION"

    generation_1_handoff = route(workflow)
    for inadmissible in (
        implementation_v0,
        implementer_result(workflow_generation=2),
        replace(
            implementer_result(workflow_generation=1),
            observed_commit=OTHER_COMMIT,
        ),
    ):
        with pytest.raises(SequentialMissionWorkflowError) as error:
            workflow.accept_implementer(
                generation_1_handoff,
                inadmissible,
                updated_at=NOW,
            )
        assert error.value.code == "INVALID_IMPLEMENTER_RESULT"

    implementation_v1 = implementer_result(version=2, workflow_generation=1)
    workflow.accept_implementer(
        generation_1_handoff,
        implementation_v1,
        updated_at=NOW,
    )
    tester_handoff = route(workflow)
    for inadmissible in (
        make_tester_result(),
        make_tester_result(workflow_generation=2),
        replace(
            make_tester_result(workflow_generation=1),
            observed_commit=OTHER_COMMIT,
        ),
    ):
        with pytest.raises(SequentialMissionWorkflowError) as error:
            workflow.accept_tester(
                tester_handoff,
                inadmissible,
                implementer_result=implementation_v1,
                updated_at=NOW,
            )
        assert error.value.code == "INVALID_TESTER_RESULT"

    with pytest.raises(WorkflowTesterInputError):
        workflow.accept_tester(
            tester_handoff,
            make_tester_result(workflow_generation=1),
            implementer_result=implementation_v0,
            updated_at=NOW,
        )
    assert ProjectStateStore(tmp_path).load().user_stories[0].status is UserStoryStatus.TESTING


def test_reviewer_and_certifier_results_from_previous_generation_are_stale(
    tmp_path: Path,
) -> None:
    workflow = make_workflow(tmp_path)
    architecture, implementation_v0 = advance_to_tester(workflow)
    testing_v0 = make_tester_result()
    workflow.accept_tester(
        route(workflow),
        testing_v0,
        implementer_result=implementation_v0,
        updated_at=NOW,
    )
    stale_positive_review = reviewer_result()
    stale_certifier = certifier_result()
    workflow.accept_reviewer(
        route(workflow),
        reviewer_result(remediation=True),
        implementer_result=implementation_v0,
        tester_result=testing_v0,
        updated_at=NOW,
    )

    implementation_v1 = implementer_result(version=2, workflow_generation=1)
    workflow.accept_implementer(route(workflow), implementation_v1, updated_at=NOW)
    testing_v1 = make_tester_result(workflow_generation=1)
    workflow.accept_tester(
        route(workflow),
        testing_v1,
        implementer_result=implementation_v1,
        updated_at=NOW,
    )
    reviewer_handoff = route(workflow)
    with pytest.raises(SequentialMissionWorkflowError) as stale_reviewer:
        workflow.accept_reviewer(
            reviewer_handoff,
            stale_positive_review,
            implementer_result=implementation_v1,
            tester_result=testing_v1,
            updated_at=NOW,
        )
    assert stale_reviewer.value.code == "INVALID_REVIEWER_RESULT"

    review_v1 = reviewer_result(workflow_generation=1)
    workflow.accept_reviewer(
        reviewer_handoff,
        review_v1,
        implementer_result=implementation_v1,
        tester_result=testing_v1,
        updated_at=NOW,
    )
    with pytest.raises(SequentialMissionWorkflowError) as stale_certifier_error:
        workflow.submit_control_plane(
            route(workflow),
            stale_certifier,
            architect_result=architecture,
            implementer_result=implementation_v1,
            tester_result=testing_v1,
            reviewer_result=review_v1,
            acceptance_results=(),
            certification_context=CertificationContext(),
            certifier="Codex/Certifier",
            current_commit=COMMIT,
            updated_at=NOW,
        )
    assert stale_certifier_error.value.code == "INVALID_CERTIFIER_RESULT"


def test_multiple_remediations_increment_generation_monotonically(
    tmp_path: Path,
) -> None:
    workflow = make_workflow(tmp_path)
    _, implementation_v0 = advance_to_tester(workflow)
    workflow.accept_tester(
        route(workflow),
        make_tester_result(failed=True),
        implementer_result=implementation_v0,
        updated_at=NOW,
    )
    assert MissionStateStore(tmp_path).load().workflow_generation == 1

    implementation_v1 = implementer_result(version=2, workflow_generation=1)
    workflow.accept_implementer(route(workflow), implementation_v1, updated_at=NOW)
    workflow.accept_tester(
        route(workflow),
        make_tester_result(failed=True, workflow_generation=1),
        implementer_result=implementation_v1,
        updated_at=NOW,
    )
    assert MissionStateStore(tmp_path).load().workflow_generation == 2
    assert route(workflow).workflow_generation == 2


def test_restart_after_remediation_preserves_generation_and_rejects_history(
    tmp_path: Path,
) -> None:
    first = make_workflow(tmp_path)
    architecture, implementation_v0 = advance_to_tester(first)
    failed_v0 = make_tester_result(failed=True)
    record_acceptance(first, failed_v0, result=False, evidence_id="EV-AC-FAIL")
    first.accept_tester(
        route(first),
        failed_v0,
        implementer_result=implementation_v0,
        updated_at=NOW,
    )

    resumed = make_workflow(tmp_path, initialize=False)
    assert MissionStateStore(tmp_path).load().workflow_generation == 1
    implementer_handoff = route(resumed)
    assert implementer_handoff.workflow_generation == 1
    with pytest.raises(SequentialMissionWorkflowError) as stale:
        resumed.accept_implementer(
            implementer_handoff,
            implementation_v0,
            updated_at=NOW,
        )
    assert stale.value.code == "INVALID_IMPLEMENTER_RESULT"

    implementation_v1 = implementer_result(version=2, workflow_generation=1)
    resumed.accept_implementer(implementer_handoff, implementation_v1, updated_at=NOW)
    testing_v1 = make_tester_result(workflow_generation=1)
    resumed.accept_tester(
        route(resumed),
        testing_v1,
        implementer_result=implementation_v1,
        updated_at=NOW,
    )
    record_acceptance(resumed, testing_v1, result=True)
    review_v1 = reviewer_result(workflow_generation=1)
    resumed.accept_reviewer(
        route(resumed),
        review_v1,
        implementer_result=implementation_v1,
        tester_result=testing_v1,
        updated_at=NOW,
    )
    record_gate(resumed)
    finish(resumed, (architecture, implementation_v1, testing_v1, review_v1))


def test_human_required_blocks_then_applies_persisted_decision_and_resumes(
    tmp_path: Path,
) -> None:
    workflow = make_workflow(tmp_path)
    architecture = architect_result(human_required=True)
    result = workflow.accept_architect(route(workflow), architecture, updated_at=NOW)
    assert result.status is MissionStatus.BLOCKED
    assert result.blockers == ("HUMAN_REQUIRED",)

    with pytest.raises(Exception):
        workflow.record_evidence(
            EvidenceObservation(
                EvidenceType.HUMAN_APPROVAL,
                "US-0001",
                True,
                EvidenceProvenance(
                    ProvenanceKind.CODEX,
                    "Human",
                    "Codex/FakeHuman",
                ),
                True,
                commit=COMMIT,
            ),
            evidence_id="EV-FAKE",
            timestamp=NOW,
        )
    workflow.record_evidence(
        EvidenceObservation(
            EvidenceType.HUMAN_APPROVAL,
            "US-0001",
            True,
            EvidenceProvenance(ProvenanceKind.HUMAN, "Human", "Alice"),
            True,
            artifact="operator approval",
            commit=COMMIT,
        ),
        evidence_id="EV-HUMAN",
        timestamp=NOW,
    )
    still_blocked = workflow.route(current_commit=COMMIT, updated_at=NOW)
    assert still_blocked.status is MissionStatus.BLOCKED
    assert still_blocked.handoff is None
    workflow = make_workflow(tmp_path, initialize=False)
    resumed = workflow.resume_after_human_approval(
        evidence_id="EV-HUMAN",
        current_commit=COMMIT,
        updated_at=NOW,
    )
    persisted = ProjectStateStore(tmp_path).load().user_stories[0]
    assert resumed.status is MissionStatus.ACTIVE
    assert persisted.human_approval.approved is True
    assert persisted.human_approval.approved_by == "Alice"
    assert persisted.status is UserStoryStatus.IN_PROGRESS

    implementation = implementer_result()
    workflow.accept_implementer(route(workflow), implementation, updated_at=NOW)
    testing = make_tester_result()
    workflow.accept_tester(
        route(workflow),
        testing,
        implementer_result=implementation,
        updated_at=NOW,
    )
    record_acceptance(workflow, testing, result=True)
    review = reviewer_result()
    workflow.accept_reviewer(
        route(workflow),
        review,
        implementer_result=implementation,
        tester_result=testing,
        updated_at=NOW,
    )
    record_gate(workflow)
    finish(workflow, (architecture, implementation, testing, review), human=True)
    assert ProjectStateStore(tmp_path).load().user_stories[0].status is UserStoryStatus.CERTIFIED


def test_not_applicable_requires_allowance_and_persists_it(tmp_path: Path) -> None:
    workflow = make_workflow(tmp_path)
    artifacts = advance_to_certifier(workflow, record_authoritative_gate=False)
    record_gate(workflow, not_applicable=True)

    handoff = route(workflow)
    with pytest.raises(SequentialMissionWorkflowError) as missing:
        workflow.submit_control_plane(
            handoff,
            certifier_result(not_applicable=True),
            architect_result=artifacts[0],
            implementer_result=artifacts[1],
            tester_result=artifacts[2],
            reviewer_result=artifacts[3],
            acceptance_results=(
                AcceptanceResult("AC-001", GateResult.PASS, ("EV-AC",)),
            ),
            certification_context=CertificationContext(),
            certifier="Codex/Certifier",
            current_commit=COMMIT,
            updated_at=NOW,
        )
    assert missing.value.code == "INVALID_CERTIFIER_RESULT"

    finish(workflow, artifacts, not_applicable=True, handoff=handoff)
    certification = ProjectStateStore(tmp_path).load().certifications[0]
    assert certification.authorized_not_applicable_gates == ("GATE-TESTS",)


def test_tester_blocked_never_enters_reviewer(tmp_path: Path) -> None:
    workflow = make_workflow(tmp_path)
    _, implementation = advance_to_tester(workflow)
    blocked = workflow.accept_tester(
        route(workflow),
        make_tester_result(blocked=True),
        implementer_result=implementation,
        updated_at=NOW,
    )
    assert blocked.status is MissionStatus.BLOCKED
    assert ProjectStateStore(tmp_path).load().user_stories[0].status is UserStoryStatus.TESTING
    assert workflow.route(current_commit=COMMIT, updated_at=NOW).handoff is None


def test_invalid_architect_result_does_not_mutate_project_state(tmp_path: Path) -> None:
    workflow = make_workflow(tmp_path)
    invalid = replace(architect_result(), subject="US-9999")

    with pytest.raises(SequentialMissionWorkflowError) as captured:
        workflow.accept_architect(route(workflow), invalid, updated_at=NOW)

    assert captured.value.code == "INVALID_ARCHITECT_RESULT"
    assert ProjectStateStore(tmp_path).load().user_stories == []


@pytest.mark.parametrize(
    "candidate",
    [
        replace(
            implementer_result(),
            files_changed=("outside/feature.py",),
            tests_added_or_modified=(),
        ),
        replace(implementer_result(), subject="US-0002", user_story_id="US-0002"),
    ],
)
def test_invalid_or_cross_story_implementer_output_cannot_reach_tester(
    tmp_path: Path,
    candidate: ImplementerResult,
) -> None:
    workflow = make_workflow(tmp_path)
    workflow.accept_architect(route(workflow), architect_result(), updated_at=NOW)
    handoff = route(workflow)

    with pytest.raises(SequentialMissionWorkflowError) as captured:
        workflow.accept_implementer(handoff, candidate, updated_at=NOW)

    assert captured.value.code == "INVALID_IMPLEMENTER_RESULT"
    assert ProjectStateStore(tmp_path).load().user_stories[0].status is UserStoryStatus.IN_PROGRESS


def test_implementer_blocked_stops_before_tester(tmp_path: Path) -> None:
    workflow = make_workflow(tmp_path)
    workflow.accept_architect(route(workflow), architect_result(), updated_at=NOW)

    result = workflow.accept_implementer(
        route(workflow),
        blocked_implementer_result(),
        updated_at=NOW,
    )

    assert result.status is MissionStatus.BLOCKED
    assert ProjectStateStore(tmp_path).load().user_stories[0].status is UserStoryStatus.BLOCKED
    assert workflow.route(current_commit=COMMIT, updated_at=NOW).handoff is None


def test_reviewer_blocked_stops_before_certifier(tmp_path: Path) -> None:
    workflow = make_workflow(tmp_path)
    _, implementation = advance_to_tester(workflow)
    testing = make_tester_result()
    workflow.accept_tester(
        route(workflow),
        testing,
        implementer_result=implementation,
        updated_at=NOW,
    )

    result = workflow.accept_reviewer(
        route(workflow),
        reviewer_result(blocked=True),
        implementer_result=implementation,
        tester_result=testing,
        updated_at=NOW,
    )

    assert result.status is MissionStatus.BLOCKED
    assert ProjectStateStore(tmp_path).load().user_stories[0].status is UserStoryStatus.REVIEW


def test_certifier_blocked_never_submits_to_control_plane(tmp_path: Path) -> None:
    workflow = make_workflow(tmp_path)
    artifacts = advance_to_certifier(workflow, record_authoritative_gate=False)

    result = workflow.submit_control_plane(
        route(workflow),
        blocked_certifier_result(),
        architect_result=artifacts[0],
        implementer_result=artifacts[1],
        tester_result=artifacts[2],
        reviewer_result=artifacts[3],
        acceptance_results=(
            AcceptanceResult("AC-001", GateResult.PASS, ("EV-AC",)),
        ),
        certification_context=CertificationContext(),
        certifier="Codex/Certifier",
        current_commit=COMMIT,
        updated_at=NOW,
    )

    state = ProjectStateStore(tmp_path).load()
    assert result.status is MissionStatus.BLOCKED
    assert state.certifications == []
    assert state.user_stories[0].status is UserStoryStatus.CERTIFICATION


def test_certifier_remediation_never_submits_and_returns_to_implementer(
    tmp_path: Path,
) -> None:
    workflow = make_workflow(tmp_path)
    artifacts = advance_to_certifier(workflow, record_authoritative_gate=False)
    record_gate(workflow, result=GateResult.FAIL)

    result = workflow.submit_control_plane(
        route(workflow),
        remediation_certifier_result(),
        architect_result=artifacts[0],
        implementer_result=artifacts[1],
        tester_result=artifacts[2],
        reviewer_result=artifacts[3],
        acceptance_results=(
            AcceptanceResult("AC-001", GateResult.PASS, ("EV-AC",)),
        ),
        certification_context=CertificationContext(),
        certifier="Codex/Certifier",
        current_commit=COMMIT,
        updated_at=NOW,
    )

    state = ProjectStateStore(tmp_path).load()
    assert result.current_step is OperatingStep.ACT
    assert result.workflow_generation == 1
    assert state.certifications == []
    assert state.user_stories[0].status is UserStoryStatus.IN_PROGRESS


@pytest.mark.parametrize("mode", ["missing", "wrong_commit"])
def test_missing_or_wrong_commit_acceptance_evidence_blocks_submission(
    tmp_path: Path,
    mode: str,
) -> None:
    workflow = make_workflow(tmp_path)
    architecture, implementation = advance_to_tester(workflow)
    testing = make_tester_result()
    workflow.accept_tester(
        route(workflow),
        testing,
        implementer_result=implementation,
        updated_at=NOW,
    )
    if mode == "wrong_commit":
        workflow.record_evidence(
            EvidenceObservation(
                EvidenceType.ACCEPTANCE_CRITERION_CHECK,
                "AC-001",
                True,
                EvidenceProvenance(ProvenanceKind.CODEX, "pytest", "Codex/Tester"),
                True,
                commit=OTHER_COMMIT,
            ),
            evidence_id="EV-AC",
            timestamp=NOW,
        )
    review = reviewer_result()
    workflow.accept_reviewer(
        route(workflow),
        review,
        implementer_result=implementation,
        tester_result=testing,
        updated_at=NOW,
    )
    record_gate(workflow)

    with pytest.raises(SequentialMissionWorkflowError) as captured:
        workflow.submit_control_plane(
            route(workflow),
            certifier_result(),
            architect_result=architecture,
            implementer_result=implementation,
            tester_result=testing,
            reviewer_result=review,
            acceptance_results=(
                AcceptanceResult("AC-001", GateResult.PASS, ("EV-AC",)),
            ),
            certification_context=CertificationContext(),
            certifier="Codex/Certifier",
            current_commit=COMMIT,
            updated_at=NOW,
        )
    assert captured.value.code == "INVALID_CERTIFIER_RESULT"
    assert ProjectStateStore(tmp_path).load().certifications == []


def test_mission_persistence_failure_prevents_stage_advance(tmp_path: Path) -> None:
    workflow = make_workflow(tmp_path)
    _, implementation = advance_to_tester(workflow)
    handoff = route(workflow)
    original_save = workflow._mission_store.save
    workflow._mission_store.save = lambda _: (_ for _ in ()).throw(OSError("disk full"))

    with pytest.raises(OSError, match="disk full"):
        workflow.accept_tester(
            handoff,
            make_tester_result(),
            implementer_result=implementation,
            updated_at=NOW,
        )

    workflow._mission_store.save = original_save
    persisted = MissionStateStore(tmp_path).load()
    assert persisted.operating_step is OperatingStep.VERIFY
    assert persisted.status is MissionStatus.ACTIVE


def test_project_persistence_failure_stops_before_mission_progress(tmp_path: Path) -> None:
    workflow = make_workflow(tmp_path)
    original_save = workflow._project_store.save
    workflow._project_store.save = lambda _: (_ for _ in ()).throw(OSError("disk full"))
    handoff = route(workflow)

    with pytest.raises(OSError, match="disk full"):
        workflow.accept_architect(handoff, architect_result(), updated_at=NOW)

    workflow._project_store.save = original_save
    assert ProjectStateStore(tmp_path).load().user_stories == []
    assert MissionStateStore(tmp_path).load().operating_step is OperatingStep.UNDERSTAND_CONTRACT
