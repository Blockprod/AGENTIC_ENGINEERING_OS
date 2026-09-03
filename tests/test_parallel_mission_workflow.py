import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    AcceptanceCheck,
    AcceptanceResult,
    ArchitectResult,
    ArchitectVerdict,
    ArtifactCheck,
    CertificationContext,
    CertificationService,
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
    ImplementerInput,
    ImplementerResult,
    ImplementerVerdict,
    IntegratedStoryContext,
    ParallelCoordinationError,
    ParallelIntegrationAttempt,
    ParallelMissionWorkflow,
    ParallelMissionWorkflowError,
    ParallelStoryDossier,
    ParallelStoryStage,
    ProvenanceKind,
    ReviewDimension,
    ReviewFinding,
    ReviewSeverity,
    ReviewerResult,
    ReviewerVerdict,
    StateTransitionService,
    TestCaseType,
    TesterAcceptanceResult,
    TesterPlan,
    TesterResult,
    TesterTestCase,
    TesterVerdict,
    TesterVerificationResult,
    TransitionContext,
    VerificationOutcome,
    VerificationResult,
)
from agentic_engineering_os.application.integrated_story_context import role_result_fingerprint
from agentic_engineering_os.application.integration_gate import integration_gate_fingerprint
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
from agentic_engineering_os.infrastructure import (
    MissionStateStore,
    ProjectStateStore,
    WorktreeManager,
)
from tests._validated_execution_ledger import write_validated_implementer_execution


NOW = datetime(2026, 8, 28, 23, 0, tzinfo=timezone.utc)
COMMAND = "python -m pytest"

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


def git(root: Path, *arguments: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(root), *arguments],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if process.returncode:
        raise AssertionError(process.stderr)
    return process.stdout.strip()


def make_story(
    identifier: str,
    *,
    depends_on: tuple[str, ...] = (),
    allowed: tuple[str, ...] | None = None,
    human_required: bool = False,
) -> UserStory:
    suffix = identifier.casefold().replace("us-", "")
    return UserStory(
        schema_version="1.0",
        id=identifier,
        title=f"Parallel story {identifier}",
        description="Exercise one isolated parallel change.",
        status=UserStoryStatus.PROPOSED,
        priority=1,
        risk=RiskLevel.LOW,
        depends_on=depends_on,
        scope=UserStoryScope(
            allowed
            if allowed is not None
            else (f"src/{suffix}.py", f"tests/test_{suffix}.py"),
            (),
        ),
        acceptance_criteria=(
            AcceptanceCriterion(f"AC-{suffix}", "The isolated result is observable.", True),
        ),
        required_gates=(f"GATE-{suffix}",),
        human_approval=HumanApproval(human_required, False, None, None),
        metadata=UserStoryMetadata(NOW, "Codex/Architect", NOW),
    )


@dataclass
class Harness:
    root: Path
    worktrees: Path
    baseline: str
    mission_store: MissionStateStore
    project_store: ProjectStateStore
    manager: WorktreeManager
    control: ControlLoop
    workflow: ParallelMissionWorkflow

    def restarted(self) -> ParallelMissionWorkflow:
        manager = WorktreeManager(
            repository_root=self.root,
            worktree_root=self.worktrees,
        )
        return ParallelMissionWorkflow(
            mission_store=self.mission_store,
            project_store=self.project_store,
            control_loop=self.control,
            worktree_manager=manager,
        )


def make_harness(tmp_path: Path, stories: tuple[UserStory, ...]) -> Harness:
    root = tmp_path / "repository"
    worktrees = tmp_path / "worktrees"
    root.mkdir()
    worktrees.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "P3.11 Test Operator")
    git(root, "config", "user.email", "p3.11@example.invalid")
    (root / ".gitignore").write_text(".agentic-engineering-os/\n", encoding="utf-8")
    (root / "README.md").write_text("baseline\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "test: baseline")
    baseline = git(root, "rev-parse", "HEAD").casefold()
    mission_store = MissionStateStore(root)
    project_store = ProjectStateStore(root)
    project_store.initialize()
    mission_store.initialize(
        MissionState(
            schema_version="1.0",
            mission_id="P3.11",
            workflow_generation=0,
            status=MissionStatus.ACTIVE,
            role=MissionRole.ORCHESTRATOR,
            objective="Execute a parallel mission end to end.",
            subject="parallel-mission",
            operating_step=OperatingStep.ACT,
            next_action="Plan the certified dependency frontier.",
            observed_commit=baseline,
            updated_at=NOW,
            blockers=[],
        )
    )
    validator = ContractValidator()
    control = ControlLoop(
        state_store=project_store,
        evidence_recorder_factory=lambda state: EvidenceRecorder(
            state, validator=validator, clock=lambda: NOW
        ),
        gate_evaluator=GateEvaluator(validator=validator, clock=lambda: NOW),
        certification_service=CertificationService(
            validator=validator, clock=lambda: NOW
        ),
        transition_service=StateTransitionService(),
    )
    for story in stories:
        control.add_user_story(story)
    for story in stories:
        control.transition_user_story(
            story.id,
            UserStoryStatus.PLANNED,
            context=TransitionContext(preconditions_proven=True),
        )
    manager = WorktreeManager(repository_root=root, worktree_root=worktrees)
    manager.initialize_registry()
    workflow = ParallelMissionWorkflow(
        mission_store=mission_store,
        project_store=project_store,
        control_loop=control,
        worktree_manager=manager,
    )
    return Harness(
        root,
        worktrees,
        baseline,
        mission_store,
        project_store,
        manager,
        control,
        workflow,
    )


def implement_group(harness: Harness, workflow: ParallelMissionWorkflow, plan, index: int):
    prepared = workflow.prepare_group(plan, index)
    branch_results = {}
    members = []
    for context in prepared.contexts:
        story = next(
            item
            for item in harness.project_store.load().user_stories
            if item.id == context.user_story_id
        )
        suffix = story.id.casefold().replace("us-", "")
        changed = (f"src/{suffix}.py", f"tests/test_{suffix}.py")
        path = Path(context.worktree_path)
        for relative in changed:
            target = path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                f"VALUE = '{story.id}-g{plan.workflow_generation}'\n",
                encoding="utf-8",
            )
        candidate = ImplementerResult(
            mission_id=plan.mission_id,
            workflow_generation=plan.workflow_generation,
            subject=story.id,
            user_story_id=story.id,
            observed_commit=plan.baseline_commit,
            summary="Isolated implementation completed.",
            files_changed=changed,
            tests_added_or_modified=(changed[1],),
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
        member = workflow.submit_member(
            prepared,
            context.assignment_id,
            candidate,
            execution_id=write_validated_implementer_execution(
                context, candidate, observed_at=NOW
            ),
            implementer_input=ImplementerInput.from_handoff(context.handoff, story),
        )
        branch_results[story.id] = candidate
        members.append(member)
    return prepared, branch_results, workflow.complete_group(prepared, tuple(members))


def make_tester_result(
    story: UserStory, commit: str, *, generation: int = 0
) -> TesterResult:
    criterion = story.acceptance_criteria[0].id
    cases = tuple(
        TesterTestCase(
            f"TC-{index:03d}", kind, "Exercise behavior.", "Pass.", "Pass.",
            True, True, GateResult.PASS
        )
        for index, kind in enumerate(TestCaseType, 1)
    )
    return TesterResult(
        mission_id="P3.11",
        workflow_generation=generation,
        subject=story.id,
        user_story_id=story.id,
        observed_commit=commit,
        summary="Independent verification completed.",
        test_plan=TesterPlan(
            (criterion,), ("positive",), ("negative",), ("edge",),
            ("regression",), (COMMAND,)
        ),
        acceptance_results=(
            TesterAcceptanceResult(criterion, GateResult.PASS, ("TC-001",), "pass"),
        ),
        test_cases=cases,
        test_files_changed=(story.scope.allowed_paths[1],),
        verification_commands=(COMMAND,),
        verification_results=(
            TesterVerificationResult(COMMAND, True, True, GateResult.PASS, 0, "pass"),
        ),
        findings=(),
        blockers=(),
        recommended_next_role=MissionRole.REVIEWER,
        verdict=TesterVerdict.READY_FOR_REVIEW,
    )


def make_integrated_context(
    attempt: ParallelIntegrationAttempt,
    story_id: str,
    implementation: ImplementerResult,
    *,
    architect_fingerprint: str = "a" * 64,
) -> IntegratedStoryContext:
    member = next(
        item for item in attempt.group_result.member_results if item.user_story_id == story_id
    )
    integrated = attempt.merge_result.integration_commit
    assert integrated is not None
    return IntegratedStoryContext(
        mission_id=attempt.plan.mission_id,
        workflow_generation=attempt.plan.workflow_generation,
        user_story_id=story_id,
        assignment_id=member.assignment_id,
        architect_subject=story_id,
        architect_baseline_commit=attempt.plan.baseline_commit,
        architect_result_fingerprint=architect_fingerprint,
        implementer_execution_id=f"test-{member.assignment_id}",
        implementer_result_fingerprint=role_result_fingerprint(implementation),
        worktree_baseline_commit=attempt.plan.baseline_commit,
        implementation_commit=member.result_commit,
        integration_gate_fingerprint=integration_gate_fingerprint(attempt.gate_result),
        integrated_commit=integrated,
    )
def certify_member(
    harness: Harness,
    workflow: ParallelMissionWorkflow,
    attempt: ParallelIntegrationAttempt,
    story_id: str,
    branch_result: ImplementerResult,
) -> ParallelStoryDossier:
    commit = attempt.merge_result.integration_commit
    assert commit is not None
    generation = attempt.plan.workflow_generation
    story = next(
        item for item in harness.project_store.load().user_stories if item.id == story_id
    )
    implementation = branch_result
    architecture = ArchitectResult(
        mission_id=attempt.plan.mission_id,
        workflow_generation=generation,
        subject=story.id,
        observed_commit=attempt.plan.baseline_commit,
        summary="Story contract is explicit.",
        assumptions=(),
        decisions=(),
        risks=(),
        blockers=(),
        user_stories=(replace(story, status=UserStoryStatus.PROPOSED),),
        recommended_next_role=MissionRole.IMPLEMENTER,
        verdict=ArchitectVerdict.READY,
    )
    integrated_context = make_integrated_context(
        attempt,
        story_id,
        implementation,
        architect_fingerprint=role_result_fingerprint(architecture),
    )
    dossier = workflow.accept_integrated_implementer(
        attempt,
        story_id,
        implementation,
        integrated_context=integrated_context,
    )
    testing = make_tester_result(story, commit, generation=generation)
    dossier = workflow.accept_tester(dossier, testing)
    criterion = story.acceptance_criteria[0].id
    suffix = story.id.casefold().replace("us-", "")
    acceptance_evidence = f"EV-AC-{suffix}-G{generation}"
    gate_evidence = f"EV-GATE-{suffix}-G{generation}"
    workflow.record_evidence(
        EvidenceObservation(
            EvidenceType.ACCEPTANCE_CRITERION_CHECK,
            criterion,
            True,
            EvidenceProvenance(ProvenanceKind.CODEX, "pytest", "Codex/Tester"),
            True,
            artifact="acceptance output",
            commit=commit,
        ),
        evidence_id=acceptance_evidence,
        timestamp=NOW,
    )
    workflow.record_evidence(
        EvidenceObservation(
            EvidenceType.TEST_RESULT,
            story.id,
            True,
            EvidenceProvenance(ProvenanceKind.TOOL, "pytest", "pytest"),
            True,
            artifact="gate output",
            commit=commit,
        ),
        evidence_id=gate_evidence,
        timestamp=NOW,
    )
    workflow.evaluate_gate(
        GateContract(
            story.required_gates[0], story.id, True, (gate_evidence,),
            lambda _: GateResult.PASS, True, "Codex/Tester"
        ),
        context=GateEvaluationContext(expected_commit=commit),
        evaluated_at=NOW,
    )
    review = ReviewerResult(
        mission_id=attempt.plan.mission_id,
        workflow_generation=generation,
        subject=story.id,
        user_story_id=story.id,
        observed_commit=commit,
        summary="Review completed.",
        dimensions_reviewed=tuple(ReviewDimension),
        reviewed_paths=implementation.files_changed,
        findings=(),
        blockers=(),
        recommended_next_role=MissionRole.CERTIFIER,
        verdict=ReviewerVerdict.READY_FOR_CERTIFICATION,
    )
    dossier = workflow.accept_reviewer(dossier, review)
    certifier = CertifierResult(
        mission_id=attempt.plan.mission_id,
        workflow_generation=generation,
        subject=story.id,
        user_story_id=story.id,
        observed_commit=commit,
        summary="Dossier is ready for the Control Plane.",
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
            AcceptanceCheck(
                criterion, True, GateResult.PASS, (acceptance_evidence,), "Proven."
            ),
        ),
        gate_checks=(
            GateCheck(
                story.required_gates[0], True, True, GateResult.PASS,
                (gate_evidence,), True, False, "Gate passed."
            ),
        ),
        evidence_refs=(acceptance_evidence, gate_evidence),
        human_approval_check=HumanApprovalCheck(False, False, True, None, "Not required."),
        findings=(),
        blockers=(),
        recommended_action=CertifierRecommendedAction.SUBMIT_TO_CONTROL_PLANE,
        verdict=CertifierVerdict.READY_FOR_CONTROL_PLANE,
    )
    return workflow.submit_certifier(
        dossier,
        certifier,
        architect_result=architecture,
        acceptance_results=(
            AcceptanceResult(criterion, GateResult.PASS, (acceptance_evidence,)),
        ),
        certification_context=CertificationContext(),
        certifier="Codex/Certifier",
        updated_at=NOW,
        certification_id=f"CERT-{suffix}-G{generation}",
    )


def test_real_multiwave_mission_reconstructs_after_restarts_and_certifies(tmp_path: Path) -> None:
    harness = make_harness(
        tmp_path,
        (
            make_story("US-0001"),
            make_story("US-0002"),
            make_story("US-0003", depends_on=("US-0001", "US-0002")),
        ),
    )
    plan = harness.workflow.plan_current()
    assert tuple(
        tuple(member.user_story_id for member in item.members)
        for item in plan.waves.waves
    ) == (
        ("US-0001", "US-0002"),
        ("US-0003",),
    )
    prepared = harness.workflow.prepare_group(plan, 0)
    restarted = harness.restarted()
    assert restarted.prepare_group(plan, 0).assignment_ids == prepared.assignment_ids
    _, branches, group = implement_group_from_prepared(harness, restarted, prepared)
    attempt = restarted.integrate_group(plan, group, updated_at=NOW)
    assert attempt.merge_result is not None
    post_merge = harness.restarted()
    assert certify_member(harness, post_merge, attempt, "US-0001", branches["US-0001"]).stage is ParallelStoryStage.CERTIFIED
    assert certify_member(harness, post_merge, attempt, "US-0002", branches["US-0002"]).stage is ParallelStoryStage.CERTIFIED
    assert next(item for item in harness.project_store.load().user_stories if item.id == "US-0003").status is UserStoryStatus.PLANNED
    wave_two = harness.restarted()
    second_plan = wave_two.plan_current()
    assert second_plan.execution_plan.groups[0].user_story_ids == ("US-0003",)
    _, second_branches, second_group = implement_group(harness, wave_two, second_plan, 0)
    second_attempt = wave_two.integrate_group(second_plan, second_group, updated_at=NOW)
    final_workflow = harness.restarted()
    assert certify_member(harness, final_workflow, second_attempt, "US-0003", second_branches["US-0003"]).stage is ParallelStoryStage.CERTIFIED
    head = git(harness.root, "rev-parse", "HEAD").casefold()
    assert final_workflow.finalize(current_commit=head, updated_at=NOW).status is MissionStatus.COMPLETED
    assert harness.mission_store.load().status is MissionStatus.COMPLETED


def test_plan_current_consumes_ready_story_without_relaxing_readiness(
    tmp_path: Path,
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    harness.control.transition_user_story(
        "US-0001",
        UserStoryStatus.READY,
        context=TransitionContext(preconditions_proven=True),
    )

    plan = harness.workflow.plan_current()

    assert plan.readiness.ready_ids == ("US-0001",)
    assert plan.execution_plan.groups[0].user_story_ids == ("US-0001",)


def implement_group_from_prepared(harness: Harness, workflow, prepared):
    branch_results = {}
    members = []
    for context in prepared.contexts:
        story = next(item for item in harness.project_store.load().user_stories if item.id == context.user_story_id)
        suffix = story.id.casefold().replace("us-", "")
        changed = (f"src/{suffix}.py", f"tests/test_{suffix}.py")
        path = Path(context.worktree_path)
        for relative in changed:
            target = path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                f"VALUE = '{story.id}-g{prepared.workflow_generation}'\n",
                encoding="utf-8",
            )
        candidate = ImplementerResult(
            mission_id=prepared.contexts[0].handoff.mission_id,
            workflow_generation=prepared.workflow_generation,
            subject=story.id,
            user_story_id=story.id, observed_commit=prepared.baseline_commit,
            summary="Isolated implementation completed.", files_changed=changed,
            tests_added_or_modified=(changed[1],), verification_commands=(COMMAND,),
            verification_results=(VerificationResult(COMMAND, True, VerificationOutcome.PASS, 0, "pass"),),
            assumptions=(), findings=(), blockers=(), recommended_next_role=MissionRole.TESTER,
            verdict=ImplementerVerdict.READY_FOR_TEST,
        )
        members.append(workflow.submit_member(
            prepared,
            context.assignment_id,
            candidate,
            execution_id=write_validated_implementer_execution(
                context, candidate, observed_at=NOW
            ),
            implementer_input=ImplementerInput.from_handoff(context.handoff, story),
        ))
        branch_results[story.id] = candidate
    return prepared, branch_results, workflow.complete_group(prepared, tuple(members))


def test_conflicts_unknown_human_and_forged_state_fail_closed(tmp_path: Path) -> None:
    harness = make_harness(
        tmp_path,
        (
            make_story("US-0001", allowed=("src/shared.py", "tests/test_a.py")),
            make_story("US-0002", allowed=("src/shared.py", "tests/test_b.py")),
            make_story("US-0003", allowed=()),
            make_story("US-0004", human_required=True),
        ),
    )
    plan = harness.workflow.plan_current()
    groups = tuple(group.user_story_ids for group in plan.execution_plan.groups)
    assert not any({"US-0001", "US-0002"}.issubset(set(group)) for group in groups)
    assert next(group for group in groups if "US-0003" in group) == ("US-0003",)
    assert "US-0004" not in {item for group in groups for item in group}
    harness.workflow.record_evidence(
        EvidenceObservation(
            EvidenceType.HUMAN_APPROVAL,
            "US-0004",
            True,
            EvidenceProvenance(ProvenanceKind.HUMAN, "Human", "Alice Operator"),
            True,
            artifact="operator approval",
            commit=harness.baseline,
        ),
        evidence_id="EV-HUMAN-0004",
        timestamp=NOW,
    )
    harness.workflow.apply_human_approval(
        "US-0004", "EV-HUMAN-0004", expected_commit=harness.baseline
    )
    approved = harness.workflow.plan_current()
    assert "US-0004" in {
        item
        for group in approved.execution_plan.groups
        for item in group.user_story_ids
    }
    forged = replace(plan, baseline_commit="0" * 40)
    with pytest.raises(ParallelMissionWorkflowError, match="PLAN_STALE"):
        harness.workflow.prepare_group(forged, 0)


def test_failed_member_and_unproven_merge_cannot_advance_roles(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    plan = harness.workflow.plan_current()
    prepared = harness.workflow.prepare_group(plan, 0)
    failed = harness.workflow.fail_member(prepared, prepared.assignment_ids[0])
    assert failed.status.value == "FAILED"
    assert git(harness.root, "rev-parse", "HEAD").casefold() == harness.baseline
    fake_attempt = ParallelIntegrationAttempt(
        plan=plan,
        group_result=failed,
        gate_context=None,  # type: ignore[arg-type]
        gate_result=None,  # type: ignore[arg-type]
        merge_result=None,
    )
    with pytest.raises(ParallelMissionWorkflowError, match="MERGE_NOT_PROVEN"):
        harness.workflow.accept_integrated_implementer(
            fake_attempt,
            "US-0001",
            object(),  # type: ignore[arg-type]
        )


def test_worktree_reconciliation_separates_integration_namespace(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    integration_path = tmp_path / "integration-resource"
    generic_path = tmp_path / "generic-resource"
    git(
        harness.root,
        "worktree",
        "add",
        "-b",
        "agentic/integration/test-resource",
        str(integration_path),
        harness.baseline,
    )
    assert harness.manager.inspect_all(current_generation=0).anomalies == ()
    git(
        harness.root,
        "worktree",
        "add",
        "-b",
        "agentic/unregistered-resource",
        str(generic_path),
        harness.baseline,
    )
    assert any(
        item.startswith("ORPHAN_AGENTIC_WORKTREE:")
        for item in harness.manager.inspect_all(current_generation=0).anomalies
    )


def test_post_merge_tester_and_reviewer_failures_require_remediation(tmp_path: Path) -> None:
    harness = make_harness(
        tmp_path,
        (make_story("US-0001"), make_story("US-0002")),
    )
    plan = harness.workflow.plan_current()
    _, branches, group = implement_group(harness, harness.workflow, plan, 0)
    attempt = harness.workflow.integrate_group(plan, group, updated_at=NOW)
    commit = attempt.merge_result.integration_commit
    assert commit is not None

    story_one = next(item for item in harness.project_store.load().user_stories if item.id == "US-0001")
    dossier_one = harness.workflow.accept_integrated_implementer(
        attempt,
        "US-0001",
        branches["US-0001"],
        integrated_context=make_integrated_context(
            attempt, "US-0001", branches["US-0001"]
        ),
    )
    passing = make_tester_result(story_one, commit)
    failed_cases = tuple(
        replace(item, verdict=GateResult.FAIL)
        if item.type is TestCaseType.REGRESSION
        else item
        for item in passing.test_cases
    )
    failed_testing = replace(
        passing,
        acceptance_results=(
            replace(passing.acceptance_results[0], result=GateResult.FAIL),
        ),
        test_cases=failed_cases,
        verification_results=(
            replace(
                passing.verification_results[0],
                result=GateResult.FAIL,
                exit_code=1,
            ),
        ),
        findings=("Regression failure is demonstrated.",),
        recommended_next_role=MissionRole.IMPLEMENTER,
        verdict=TesterVerdict.REMEDIATION_REQUIRED,
    )
    assert harness.workflow.accept_tester(
        dossier_one, failed_testing
    ).stage is ParallelStoryStage.REMEDIATION_REQUIRED

    story_two = next(item for item in harness.project_store.load().user_stories if item.id == "US-0002")
    dossier_two = harness.workflow.accept_integrated_implementer(
        attempt,
        "US-0002",
        branches["US-0002"],
        integrated_context=make_integrated_context(
            attempt, "US-0002", branches["US-0002"]
        ),
    )
    dossier_two = harness.workflow.accept_tester(
        dossier_two, make_tester_result(story_two, commit)
    )
    remediation_review = ReviewerResult(
        mission_id="P3.11",
        workflow_generation=0,
        subject="US-0002",
        user_story_id="US-0002",
        observed_commit=commit,
        summary="Blocking review finding.",
        dimensions_reviewed=tuple(ReviewDimension),
        reviewed_paths=branches["US-0002"].files_changed,
        findings=(
            ReviewFinding(
                "RF-001",
                ReviewDimension.AUTHORITY_SAFETY,
                ReviewSeverity.CRITICAL,
                "Authority boundary issue.",
                ("Observed bypass.",),
                (branches["US-0002"].files_changed[0],),
                True,
            ),
        ),
        blockers=(),
        recommended_next_role=MissionRole.IMPLEMENTER,
        verdict=ReviewerVerdict.REMEDIATION_REQUIRED,
    )
    assert harness.workflow.accept_reviewer(
        dossier_two, remediation_review
    ).stage is ParallelStoryStage.REMEDIATION_REQUIRED
    assert harness.project_store.load().certifications == []


def test_undeclared_diff_fails_commit_boundary_before_gate_or_merge(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    plan = harness.workflow.plan_current()
    prepared = harness.workflow.prepare_group(plan, 0)
    context = prepared.contexts[0]
    story = next(item for item in harness.project_store.load().user_stories if item.id == "US-0001")
    path = Path(context.worktree_path)
    for relative in ("src/0001.py", "tests/test_0001.py"):
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("VALUE = 1\n", encoding="utf-8")
    candidate = ImplementerResult(
        mission_id="P3.11",
        workflow_generation=0,
        subject="US-0001",
        user_story_id="US-0001",
        observed_commit=plan.baseline_commit,
        summary="Declaration omits one real diff path.",
        files_changed=("src/0001.py",),
        tests_added_or_modified=(),
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
    execution_id = write_validated_implementer_execution(
        context, candidate, observed_at=NOW
    )
    with pytest.raises(ParallelCoordinationError, match="GROUP_INCOMPLETE"):
        harness.workflow.submit_member(
            prepared,
            context.assignment_id,
            candidate,
            execution_id=execution_id,
            implementer_input=ImplementerInput.from_handoff(context.handoff, story),
        )
    assert git(harness.root, "rev-parse", "HEAD").casefold() == harness.baseline


def test_stale_completed_worktree_blocks_integration(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    plan = harness.workflow.plan_current()
    prepared, _, group = implement_group(harness, harness.workflow, plan, 0)
    path = Path(prepared.contexts[0].worktree_path)
    (path / "src" / "0001.py").write_text("VALUE = 'tampered'\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "test: stale completed tip")
    attempt = harness.workflow.integrate_group(plan, group, updated_at=NOW)
    assert attempt.gate_result.result.value in {"FAIL", "UNKNOWN"}
    assert attempt.merge_result is None
    assert git(harness.root, "rev-parse", "HEAD").casefold() == harness.baseline


def test_conflicting_groups_certify_then_replan_on_new_baseline(tmp_path: Path) -> None:
    harness = make_harness(
        tmp_path,
        (
            make_story(
                "US-0001",
                allowed=("src/0001.py", "tests/test_0001.py", "src/shared.py"),
            ),
            make_story(
                "US-0002",
                allowed=("src/0002.py", "tests/test_0002.py", "src/shared.py"),
            ),
        ),
    )
    first_workflow = harness.workflow
    first_plan = first_workflow.plan_current()
    assert tuple(
        group.user_story_ids for group in first_plan.execution_plan.groups
    ) == (("US-0001",), ("US-0002",))
    _, branches, group = implement_group(harness, first_workflow, first_plan, 0)
    attempt = first_workflow.integrate_group(first_plan, group, updated_at=NOW)
    certify_member(harness, harness.restarted(), attempt, "US-0001", branches["US-0001"])

    second_workflow = harness.restarted()
    second_plan = second_workflow.plan_current()
    assert second_plan.baseline_commit != first_plan.baseline_commit
    assert tuple(
        group.user_story_ids for group in second_plan.execution_plan.groups
    ) == (("US-0002",),)
    _, second_branches, second_group = implement_group(
        harness, second_workflow, second_plan, 0
    )
    second_attempt = second_workflow.integrate_group(
        second_plan, second_group, updated_at=NOW
    )
    certify_member(
        harness,
        harness.restarted(),
        second_attempt,
        "US-0002",
        second_branches["US-0002"],
    )
    assert all(
        story.status is UserStoryStatus.CERTIFIED
        for story in harness.project_store.load().user_stories
    )
