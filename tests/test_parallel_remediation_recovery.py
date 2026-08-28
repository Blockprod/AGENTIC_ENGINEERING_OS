from dataclasses import replace
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    ImplementerInput,
    ImplementerResult,
    ImplementerVerdict,
    IntegrationFinding,
    IntegrationFindingCode,
    IntegrationGate,
    IntegrationGateClassification,
    IntegrationGateContext,
    MergeResult,
    MergeStatus,
    ParallelIntegrationAttempt,
    ParallelMissionWorkflow,
    ParallelMissionWorkflowError,
    ParallelRecoveryStatus,
    ParallelRemediationStage,
    ParallelStoryStage,
    ReviewDimension,
    ReviewFinding,
    ReviewSeverity,
    ReviewerResult,
    ReviewerVerdict,
    TestCaseType,
    TesterVerdict,
    VerificationOutcome,
    VerificationResult,
)
from agentic_engineering_os.domain import GateResult, MissionRole, MissionStatus, UserStoryStatus
from agentic_engineering_os.infrastructure import WorktreeManagerError

from test_parallel_mission_workflow import (
    COMMAND,
    NOW,
    certify_member,
    git,
    implement_group,
    make_harness,
    make_story,
    make_tester_result,
)


def _candidate(context, story, *, declared=None) -> ImplementerResult:
    suffix = story.id.casefold().replace("us-", "")
    changed = declared or (f"src/{suffix}.py", f"tests/test_{suffix}.py")
    return ImplementerResult(
        mission_id=context.handoff.mission_id,
        workflow_generation=context.workflow_generation,
        subject=story.id,
        user_story_id=story.id,
        observed_commit=context.baseline_commit,
        summary="Generation-specific isolated result.",
        files_changed=changed,
        tests_added_or_modified=tuple(
            item for item in changed if item.startswith("tests/")
        ),
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


def _commit_context(context, story) -> tuple[str, ...]:
    suffix = story.id.casefold().replace("us-", "")
    changed = (f"src/{suffix}.py", f"tests/test_{suffix}.py")
    path = Path(context.worktree_path)
    for relative in changed:
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"VALUE = '{story.id}-g{context.workflow_generation}'\n",
            encoding="utf-8",
        )
    git(path, "add", ".")
    git(path, "commit", "-m", f"feat: generation {context.workflow_generation} {story.id}")
    return changed


def _failed_tester(story, commit: str, generation: int):
    passing = make_tester_result(story, commit, generation=generation)
    return replace(
        passing,
        acceptance_results=(
            replace(passing.acceptance_results[0], result=GateResult.FAIL),
        ),
        test_cases=tuple(
            replace(item, verdict=GateResult.FAIL)
            if item.type is TestCaseType.REGRESSION
            else item
            for item in passing.test_cases
        ),
        verification_results=(
            replace(
                passing.verification_results[0],
                result=GateResult.FAIL,
                exit_code=1,
            ),
        ),
        findings=("Regression failure is attributable to this story.",),
        recommended_next_role=MissionRole.IMPLEMENTER,
        verdict=TesterVerdict.REMEDIATION_REQUIRED,
    )


def _remediation_reviewer(story, commit: str, generation: int, paths):
    return ReviewerResult(
        mission_id="P3.11",
        workflow_generation=generation,
        subject=story.id,
        user_story_id=story.id,
        observed_commit=commit,
        summary="Blocking review finding.",
        dimensions_reviewed=tuple(ReviewDimension),
        reviewed_paths=paths,
        findings=(
            ReviewFinding(
                "RF-001",
                ReviewDimension.AUTHORITY_SAFETY,
                ReviewSeverity.CRITICAL,
                "Correction is required.",
                ("The defect is attributable to this story.",),
                (paths[0],),
                True,
            ),
        ),
        blockers=(),
        recommended_next_role=MissionRole.IMPLEMENTER,
        verdict=ReviewerVerdict.REMEDIATION_REQUIRED,
    )


def test_implementer_failure_opens_new_generation_and_preserves_siblings(tmp_path: Path) -> None:
    harness = make_harness(
        tmp_path,
        (
            make_story("US-0001"),
            make_story("US-0002"),
            make_story("US-0003", depends_on=("US-0001", "US-0002")),
        ),
    )
    plan = harness.workflow.plan_current()
    prepared = harness.workflow.prepare_group(plan, 0)
    first = prepared.contexts[0]
    story = next(item for item in harness.project_store.load().user_stories if item.id == first.user_story_id)
    changed = _commit_context(first, story)
    completed = harness.workflow.submit_member(
        prepared,
        first.assignment_id,
        _candidate(first, story, declared=changed),
        implementer_input=ImplementerInput.from_handoff(first.handoff, story),
    )
    assert completed.result_commit
    failed = harness.workflow.fail_member(prepared, prepared.contexts[1].assignment_id)
    old_registry = harness.manager.registry_store.load()

    remediation = harness.restarted().remediate_failed_group(
        plan,
        failed,
        affected_user_story_ids=(prepared.contexts[1].user_story_id,),
        updated_at=NOW,
    )
    assert remediation.triggering_stage is ParallelRemediationStage.IMPLEMENTER
    assert remediation.previous_generation == 0
    assert remediation.new_generation == 1
    assert remediation.affected_user_story_ids == ("US-0002",)
    assert remediation.reexecution_user_story_ids == ("US-0001", "US-0002")
    assert harness.mission_store.load().workflow_generation == 1
    assert harness.manager.registry_store.load() == old_registry
    assert next(item for item in harness.project_store.load().user_stories if item.id == "US-0003").status is UserStoryStatus.PLANNED
    with pytest.raises(ParallelMissionWorkflowError, match="PLAN_STALE"):
        harness.workflow.prepare_group(plan, 0)

    restarted = harness.restarted()
    inspection = restarted.inspect_recovery()
    assert inspection.active_generation == 1
    assert len(inspection.stale_assignment_ids) == 2
    with pytest.raises(WorktreeManagerError, match="NOT_RESUMABLE") as stale:
        harness.manager.resume(
            old_registry.assignments[0].assignment_id,
            current_generation=1,
        )
    assert "STALE_GENERATION" in stale.value.reasons
    new_plan = restarted.plan_current()
    assert new_plan.execution_plan.groups[0].user_story_ids == ("US-0001", "US-0002")
    _, branches, group = implement_group(harness, restarted, new_plan, 0)
    assert not {
        item.assignment_id for item in old_registry.assignments
    }.intersection(
        item.assignment_id
        for item in harness.manager.registry_store.load().assignments
        if item.workflow_generation == 1
    )
    attempt = restarted.integrate_group(new_plan, group, updated_at=NOW)
    certify_member(harness, harness.restarted(), attempt, "US-0001", branches["US-0001"])
    certify_member(harness, harness.restarted(), attempt, "US-0002", branches["US-0002"])
    assert harness.restarted().plan_current().execution_plan.groups[0].user_story_ids == ("US-0003",)


def test_gate_fail_remediation_replays_group_and_rejects_old_gate(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"), make_story("US-0002")))
    plan = harness.workflow.plan_current()
    prepared = harness.workflow.prepare_group(plan, 0)
    members = []
    for context in prepared.contexts:
        story = next(item for item in harness.project_store.load().user_stories if item.id == context.user_story_id)
        changed = _commit_context(context, story)
        declared = changed[:1] if story.id == "US-0001" else changed
        members.append(
            harness.workflow.submit_member(
                prepared,
                context.assignment_id,
                _candidate(context, story, declared=declared),
                implementer_input=ImplementerInput.from_handoff(context.handoff, story),
            )
        )
    group = harness.workflow.complete_group(prepared, tuple(members))
    failed_attempt = harness.workflow.integrate_group(plan, group, updated_at=NOW)
    assert failed_attempt.gate_result.result is IntegrationGateClassification.FAIL
    assert failed_attempt.merge_result is None
    remediation = harness.restarted().remediate_integration(
        failed_attempt,
        affected_user_story_ids=("US-0001",),
        updated_at=NOW,
    )
    assert remediation.triggering_stage is ParallelRemediationStage.INTEGRATION_GATE
    assert remediation.reexecution_user_story_ids == ("US-0001", "US-0002")
    with pytest.raises(ParallelMissionWorkflowError, match="PLAN_STALE"):
        harness.workflow.integrate_group(plan, group, updated_at=NOW)
    corrected = harness.restarted()
    corrected_plan = corrected.plan_current()
    _, branches, corrected_group = implement_group(harness, corrected, corrected_plan, 0)
    corrected_attempt = corrected.integrate_group(corrected_plan, corrected_group, updated_at=NOW)
    certify_member(harness, harness.restarted(), corrected_attempt, "US-0001", branches["US-0001"])
    certify_member(harness, harness.restarted(), corrected_attempt, "US-0002", branches["US-0002"])
    assert all(item.status is UserStoryStatus.CERTIFIED for item in harness.project_store.load().user_stories)


def test_tester_failure_uses_forward_remediation_and_rejects_old_roles(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"), make_story("US-0002")))
    plan = harness.workflow.plan_current()
    _, branches, group = implement_group(harness, harness.workflow, plan, 0)
    attempt = harness.workflow.integrate_group(plan, group, updated_at=NOW)
    merged = attempt.merge_result.integration_commit
    assert merged is not None
    certify_member(harness, harness.restarted(), attempt, "US-0002", branches["US-0002"])
    story = next(item for item in harness.project_store.load().user_stories if item.id == "US-0001")
    workflow = harness.restarted()
    old_dossier = workflow.accept_integrated_implementer(
        attempt, "US-0001", replace(branches["US-0001"], observed_commit=merged)
    )
    old_testing = _failed_tester(story, merged, 0)
    blocked_testing = replace(
        make_tester_result(story, merged),
        acceptance_results=(
            replace(
                make_tester_result(story, merged).acceptance_results[0],
                result=GateResult.UNKNOWN,
            ),
        ),
        test_cases=tuple(
            replace(
                item,
                executed=False,
                verdict=GateResult.UNKNOWN,
            )
            for item in make_tester_result(story, merged).test_cases
        ),
        test_files_changed=(),
        verification_results=(
            replace(
                make_tester_result(story, merged).verification_results[0],
                executed=False,
                result=GateResult.UNKNOWN,
                exit_code=None,
            ),
        ),
        findings=(),
        blockers=("Environment unavailable.",),
        recommended_next_role=MissionRole.ORCHESTRATOR,
        verdict=TesterVerdict.BLOCKED,
    )
    assert workflow.accept_tester(
        old_dossier, blocked_testing
    ).stage is ParallelStoryStage.BLOCKED
    remediation_dossier = workflow.accept_tester(old_dossier, old_testing)
    remediation = harness.restarted().remediate_dossier(
        remediation_dossier, updated_at=NOW
    )
    assert remediation.baseline_commit == merged
    assert remediation.triggering_stage is ParallelRemediationStage.TESTER
    assert harness.mission_store.load().workflow_generation == 1
    with pytest.raises(ParallelMissionWorkflowError, match="STALE_ROLE_ARTIFACT"):
        harness.restarted().accept_tester(old_dossier, old_testing)
    corrected = harness.restarted()
    corrected_plan = corrected.plan_current()
    assert corrected_plan.baseline_commit == merged
    _, new_branches, corrected_group = implement_group(harness, corrected, corrected_plan, 0)
    corrected_attempt = corrected.integrate_group(corrected_plan, corrected_group, updated_at=NOW)
    certify_member(harness, harness.restarted(), corrected_attempt, "US-0001", new_branches["US-0001"])
    assert all(item.status is UserStoryStatus.CERTIFIED for item in harness.project_store.load().user_stories)


def test_reviewer_remediation_replays_tester_and_rejects_old_review_dossier(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"), make_story("US-0002")))
    plan = harness.workflow.plan_current()
    _, branches, group = implement_group(harness, harness.workflow, plan, 0)
    attempt = harness.workflow.integrate_group(plan, group, updated_at=NOW)
    merged = attempt.merge_result.integration_commit
    assert merged is not None
    certify_member(harness, harness.restarted(), attempt, "US-0002", branches["US-0002"])
    story = next(item for item in harness.project_store.load().user_stories if item.id == "US-0001")
    workflow = harness.restarted()
    dossier = workflow.accept_integrated_implementer(
        attempt, "US-0001", replace(branches["US-0001"], observed_commit=merged)
    )
    review_stage = workflow.accept_tester(dossier, make_tester_result(story, merged))
    blocked_review = replace(
        _remediation_reviewer(
            story, merged, 0, branches["US-0001"].files_changed
        ),
        dimensions_reviewed=(),
        reviewed_paths=(),
        findings=(),
        blockers=("Review context unavailable.",),
        recommended_next_role=MissionRole.ORCHESTRATOR,
        verdict=ReviewerVerdict.BLOCKED,
    )
    assert workflow.accept_reviewer(
        review_stage, blocked_review
    ).stage is ParallelStoryStage.BLOCKED
    remediation_dossier = workflow.accept_reviewer(
        review_stage,
        _remediation_reviewer(story, merged, 0, branches["US-0001"].files_changed),
    )
    remediation = harness.restarted().remediate_dossier(
        remediation_dossier, updated_at=NOW
    )
    assert remediation.triggering_stage is ParallelRemediationStage.REVIEWER
    with pytest.raises(ParallelMissionWorkflowError, match="STALE_ROLE_ARTIFACT"):
        harness.restarted().accept_reviewer(
            review_stage,
            _remediation_reviewer(story, merged, 0, branches["US-0001"].files_changed),
        )
    corrected = harness.restarted()
    corrected_plan = corrected.plan_current()
    _, new_branches, corrected_group = implement_group(harness, corrected, corrected_plan, 0)
    corrected_attempt = corrected.integrate_group(corrected_plan, corrected_group, updated_at=NOW)
    certify_member(harness, harness.restarted(), corrected_attempt, "US-0001", new_branches["US-0001"])
    assert all(item.status is UserStoryStatus.CERTIFIED for item in harness.project_store.load().user_stories)


class _UnknownGate:
    def __init__(self, manager) -> None:
        self._delegate = IntegrationGate(worktree_manager=manager)

    def evaluate(self, context):
        observed = self._delegate.evaluate(context)
        return replace(
            observed,
            result=IntegrationGateClassification.UNKNOWN,
            findings=(
                IntegrationFinding(
                    IntegrationFindingCode.GIT_STATE_UNKNOWN,
                    "Technical observation is unavailable.",
                    (),
                    (),
                    True,
                    IntegrationGateClassification.UNKNOWN,
                ),
            ),
            integration_order=(),
        )


def test_unknown_blocks_for_explicit_recovery_without_generation_increment(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    unknown_gate = _UnknownGate(harness.manager)
    workflow = ParallelMissionWorkflow(
        mission_store=harness.mission_store,
        project_store=harness.project_store,
        control_loop=harness.control,
        worktree_manager=harness.manager,
        integration_gate=unknown_gate,  # type: ignore[arg-type]
    )
    plan = workflow.plan_current()
    _, _, group = implement_group(harness, workflow, plan, 0)
    attempt = workflow.integrate_group(plan, group, updated_at=NOW)
    assert attempt.gate_result.result is IntegrationGateClassification.UNKNOWN
    with pytest.raises(ParallelMissionWorkflowError, match="RECOVERY_REQUIRED"):
        workflow.remediate_integration(
            attempt, affected_user_story_ids=("US-0001",), updated_at=NOW
        )
    inspection = workflow.block_for_recovery(attempt, updated_at=NOW)
    assert inspection.status is ParallelRecoveryStatus.BLOCKED
    assert harness.mission_store.load().status is MissionStatus.BLOCKED
    assert harness.mission_store.load().workflow_generation == 0
    restarted = harness.restarted()
    assert restarted.inspect_recovery().status is ParallelRecoveryStatus.BLOCKED
    resumed = restarted.resume_recovery(updated_at=NOW)
    assert resumed.status is ParallelRecoveryStatus.READY
    assert harness.mission_store.load().status is MissionStatus.ACTIVE
    assert harness.mission_store.load().workflow_generation == 0


def test_blocked_implementer_is_recorded_failed_before_remediation(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    plan = harness.workflow.plan_current()
    prepared = harness.workflow.prepare_group(plan, 0)
    context = prepared.contexts[0]
    story = next(item for item in harness.project_store.load().user_stories if item.id == "US-0001")
    candidate = replace(
        _candidate(context, story),
        files_changed=(),
        tests_added_or_modified=(),
        verification_results=(
            VerificationResult(
                COMMAND, True, VerificationOutcome.FAIL, 1, "failed"
            ),
        ),
        blockers=("Implementation verification failed.",),
        recommended_next_role=MissionRole.ORCHESTRATOR,
        verdict=ImplementerVerdict.BLOCKED,
    )
    failed = harness.workflow.record_blocked_member(
        prepared,
        context.assignment_id,
        candidate,
        implementer_input=ImplementerInput.from_handoff(context.handoff, story),
    )
    assert failed.status.value == "FAILED"
    assignment = harness.manager.registry_store.load().assignments[0]
    assert assignment.status.value == "FAILED"
    assert Path(assignment.worktree_path).exists()
    harness.workflow.remediate_failed_group(
        plan,
        failed,
        affected_user_story_ids=("US-0001",),
        updated_at=NOW,
    )
    assert Path(assignment.worktree_path).exists()


def _unmerged_pass_attempt(harness):
    plan = harness.workflow.plan_current()
    _, _, group = implement_group(harness, harness.workflow, plan, 0)
    gate_context = IntegrationGateContext(
        coordination_input=plan.coordination_input,
        parallel_plan=plan.execution_plan,
        group_result=group,
        current_mission_state=harness.mission_store.load(),
    )
    gate = IntegrationGate(worktree_manager=harness.manager).evaluate(gate_context)
    assert gate.result is IntegrationGateClassification.PASS
    return plan, group, gate_context, gate


def test_merge_failed_remediates_but_merge_blocked_requires_recovery(tmp_path: Path) -> None:
    failed_root = tmp_path / "failed"
    failed_root.mkdir()
    failed_harness = make_harness(failed_root, (make_story("US-0001"),))
    plan, group, context, gate = _unmerged_pass_attempt(failed_harness)
    failed_merge = MergeResult(
        mission_id=plan.mission_id,
        workflow_generation=0,
        wave_index=0,
        group_index=0,
        baseline_commit=plan.baseline_commit,
        integration_order=gate.integration_order,
        member_commits=tuple(item.result_commit for item in group.member_results),
        integration_commit=None,
        primary_before=plan.baseline_commit,
        primary_after=plan.baseline_commit,
        result=MergeStatus.FAILED,
        findings=(),
    )
    failed_attempt = ParallelIntegrationAttempt(
        plan, group, context, gate, failed_merge
    )
    remediation = failed_harness.restarted().remediate_integration(
        failed_attempt,
        affected_user_story_ids=("US-0001",),
        updated_at=NOW,
    )
    assert remediation.triggering_stage is ParallelRemediationStage.MERGE
    assert git(failed_harness.root, "rev-parse", "HEAD").casefold() == plan.baseline_commit

    blocked_root = tmp_path / "blocked"
    blocked_root.mkdir()
    blocked_harness = make_harness(blocked_root, (make_story("US-0001"),))
    plan, group, context, gate = _unmerged_pass_attempt(blocked_harness)
    blocked_merge = replace(
        failed_merge,
        baseline_commit=plan.baseline_commit,
        primary_before=plan.baseline_commit,
        primary_after=plan.baseline_commit,
        result=MergeStatus.BLOCKED,
    )
    blocked_attempt = ParallelIntegrationAttempt(
        plan, group, context, gate, blocked_merge
    )
    with pytest.raises(ParallelMissionWorkflowError, match="RECOVERY_REQUIRED"):
        blocked_harness.workflow.remediate_integration(
            blocked_attempt,
            affected_user_story_ids=("US-0001",),
            updated_at=NOW,
        )
    inspection = blocked_harness.workflow.block_for_recovery(
        blocked_attempt, updated_at=NOW
    )
    assert inspection.status is ParallelRecoveryStatus.BLOCKED
    assert blocked_harness.mission_store.load().workflow_generation == 0


def _failed_single_group(harness):
    plan = harness.workflow.plan_current()
    prepared = harness.workflow.prepare_group(plan, 0)
    failed = harness.workflow.fail_member(prepared, prepared.assignment_ids[0])
    return plan, failed


def test_project_failure_leaves_old_generation_authoritative(
    tmp_path: Path, monkeypatch
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    plan, failed = _failed_single_group(harness)

    def refuse_transition(*args, **kwargs):
        raise RuntimeError("project persistence unavailable")

    monkeypatch.setattr(harness.control, "transition_user_story", refuse_transition)
    with pytest.raises(RuntimeError, match="project persistence unavailable"):
        harness.workflow.remediate_failed_group(
            plan,
            failed,
            affected_user_story_ids=("US-0001",),
            updated_at=NOW,
        )
    assert harness.mission_store.load().workflow_generation == 0
    assert all(
        item.workflow_generation == 0
        for item in harness.manager.registry_store.load().assignments
    )


class _FailingMissionStore:
    def __init__(self, delegate) -> None:
        self.delegate = delegate

    def load(self):
        return self.delegate.load()

    def save(self, *args, **kwargs):
        raise RuntimeError("mission persistence unavailable")


def test_mission_failure_never_authorizes_new_generation_resources(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    plan, failed = _failed_single_group(harness)
    workflow = ParallelMissionWorkflow(
        mission_store=_FailingMissionStore(harness.mission_store),
        project_store=harness.project_store,
        control_loop=harness.control,
        worktree_manager=harness.manager,
    )
    with pytest.raises(RuntimeError, match="mission persistence unavailable"):
        workflow.remediate_failed_group(
            plan,
            failed,
            affected_user_story_ids=("US-0001",),
            updated_at=NOW,
        )
    assert harness.mission_store.load().workflow_generation == 0
    assert harness.project_store.load().user_stories[0].status is UserStoryStatus.READY
    assert not any(
        item.workflow_generation == 1
        for item in harness.manager.registry_store.load().assignments
    )
    old_plan = harness.workflow.plan_current()
    with pytest.raises(Exception):
        harness.workflow.prepare_group(old_plan, 0)
