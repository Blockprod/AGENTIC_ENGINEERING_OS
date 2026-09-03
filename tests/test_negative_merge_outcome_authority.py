from dataclasses import replace
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    MergeContext,
    MergeCoordinationError,
    MergeCoordinator,
    MergeFindingCode,
    MergeResult,
    MergeStatus,
    ParallelIntegrationAttempt,
    ParallelMissionWorkflowError,
    ParallelRecoveryStatus,
    ParallelRemediationStage,
    ParallelStoryStage,
)
from agentic_engineering_os.infrastructure import GitMergeResult
from agentic_engineering_os.infrastructure.project_state_store import PersistenceError

from test_parallel_mission_workflow import (
    NOW,
    git,
    implement_group,
    make_integrated_context,
    make_harness,
    make_story,
)
from test_parallel_remediation_recovery import _failed_tester, _unmerged_pass_attempt


def _record_failed(harness, monkeypatch: pytest.MonkeyPatch):
    plan, group, context, gate = _unmerged_pass_attempt(harness)
    coordinator = MergeCoordinator(worktree_manager=harness.manager)
    adapter = harness.manager._integration_git_adapter

    def fail_merge(path: Path, commit: str, *, message: str) -> GitMergeResult:
        return GitMergeResult(False, adapter.current_head(path))

    monkeypatch.setattr(adapter, "merge_no_ff", fail_merge)
    result = coordinator.merge(MergeContext(context, gate))
    assert result.result is MergeStatus.FAILED
    assert result.findings[0].code is MergeFindingCode.GIT_MERGE_CONFLICT
    return ParallelIntegrationAttempt(plan, group, context, gate, result)


def _record_blocked(harness, monkeypatch: pytest.MonkeyPatch):
    plan, group, context, gate = _unmerged_pass_attempt(harness)
    coordinator = MergeCoordinator(worktree_manager=harness.manager)

    def unavailable(*args, **kwargs) -> None:
        raise OSError("simulated unavailable integration resource")

    monkeypatch.setattr(coordinator, "_prepare_integration_resource", unavailable)
    result = coordinator.merge(MergeContext(context, gate))
    assert result.result is MergeStatus.BLOCKED
    return ParallelIntegrationAttempt(plan, group, context, gate, result)


def _remediate(workflow, attempt):
    return workflow.remediate_integration(
        attempt,
        affected_user_story_ids=tuple(
            item.user_story_id for item in attempt.group_result.member_results
        ),
        updated_at=NOW,
    )


def test_public_forged_failed_all_fields_wrong_cannot_mutate_mission(
    tmp_path: Path,
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    plan, group, context, gate = _unmerged_pass_attempt(harness)
    forged = MergeResult(
        mission_id="FORGED",
        workflow_generation=999,
        wave_index=999,
        group_index=999,
        baseline_commit="0" * 40,
        integration_order=(),
        member_commits=(),
        integration_commit=None,
        primary_before="1" * 40,
        primary_after="2" * 40,
        result=MergeStatus.FAILED,
        findings=(),
    )
    attempt = ParallelIntegrationAttempt(plan, group, context, gate, forged)

    with pytest.raises(ParallelMissionWorkflowError, match="UNTRUSTED_MERGE_OUTCOME"):
        _remediate(harness.restarted(), attempt)

    assert harness.mission_store.load().workflow_generation == 0


def test_exact_public_failed_without_authoritative_attempt_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = make_harness(source_root, (make_story("US-0001"),))
    real = _record_failed(source, monkeypatch).merge_result
    assert real is not None

    target_root = tmp_path / "target"
    target_root.mkdir()
    target = make_harness(target_root, (make_story("US-0001"),))
    plan, group, context, gate = _unmerged_pass_attempt(target)
    forged = replace(
        real,
        mission_id=plan.mission_id,
        workflow_generation=plan.workflow_generation,
        wave_index=gate.wave_index,
        group_index=gate.group_index,
        baseline_commit=plan.baseline_commit,
        integration_order=gate.integration_order,
        member_commits=tuple(item.result_commit for item in group.member_results),
        primary_before=plan.baseline_commit,
        primary_after=plan.baseline_commit,
    )

    with pytest.raises(ParallelMissionWorkflowError, match="UNTRUSTED_MERGE_OUTCOME"):
        _remediate(
            target.restarted(),
            ParallelIntegrationAttempt(plan, group, context, gate, forged),
        )
    assert target.mission_store.load().workflow_generation == 0


def test_every_bound_field_and_status_tampering_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make_harness(
        tmp_path, (make_story("US-0001"), make_story("US-0002"))
    )
    attempt = _record_failed(harness, monkeypatch)
    result = attempt.merge_result
    assert result is not None
    variants = (
        replace(result, mission_id="OTHER"),
        replace(result, workflow_generation=1),
        replace(result, wave_index=result.wave_index + 1),
        replace(result, group_index=result.group_index + 1),
        replace(result, baseline_commit="0" * 40),
        replace(result, integration_order=tuple(reversed(result.integration_order))),
        replace(result, member_commits=result.member_commits[:-1]),
        replace(result, member_commits=(*result.member_commits, "0" * 40)),
        replace(result, member_commits=("0" * 40, *result.member_commits[1:])),
    )

    for forged in variants:
        with pytest.raises(
            ParallelMissionWorkflowError, match="UNTRUSTED_MERGE_OUTCOME"
        ):
            _remediate(harness.restarted(), replace(attempt, merge_result=forged))
        assert harness.mission_store.load().workflow_generation == 0

    forged_blocked = replace(result, result=MergeStatus.BLOCKED)
    with pytest.raises(ParallelMissionWorkflowError, match="UNTRUSTED_MERGE_OUTCOME"):
        harness.restarted().block_for_recovery(
            replace(attempt, merge_result=forged_blocked), updated_at=NOW
        )
    assert harness.mission_store.load().workflow_generation == 0

    remediation = _remediate(harness.restarted(), attempt)
    assert remediation.triggering_stage is ParallelRemediationStage.MERGE
    assert harness.mission_store.load().workflow_generation == 1


def test_legitimate_failed_survives_restart_and_cannot_be_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    attempt = _record_failed(harness, monkeypatch)

    remediation = _remediate(harness.restarted(), attempt)
    assert remediation.previous_generation == 0
    assert remediation.new_generation == 1

    with pytest.raises(ParallelMissionWorkflowError, match="PLAN_STALE"):
        _remediate(harness.restarted(), attempt)
    assert harness.mission_store.load().workflow_generation == 1


def test_cross_mission_and_cross_group_replay_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = make_harness(source_root, (make_story("US-0001"),))
    source_attempt = _record_failed(source, monkeypatch)

    target_root = tmp_path / "target"
    target_root.mkdir()
    target = make_harness(target_root, (make_story("US-0001"),))
    plan, group, context, gate = _unmerged_pass_attempt(target)
    cross_mission = ParallelIntegrationAttempt(
        plan, group, context, gate, source_attempt.merge_result
    )
    with pytest.raises(ParallelMissionWorkflowError, match="UNTRUSTED_MERGE_OUTCOME"):
        _remediate(target.restarted(), cross_mission)

    source_result = source_attempt.merge_result
    assert source_result is not None
    cross_group = replace(source_result, group_index=source_result.group_index + 1)
    with pytest.raises(ParallelMissionWorkflowError, match="UNTRUSTED_MERGE_OUTCOME"):
        _remediate(source.restarted(), replace(source_attempt, merge_result=cross_group))

    assert source.mission_store.load().workflow_generation == 0
    assert target.mission_store.load().workflow_generation == 0


def test_primary_drift_makes_recorded_failed_outcome_stale_without_consuming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    attempt = _record_failed(harness, monkeypatch)
    (harness.root / "drift.txt").write_text("drift\n", encoding="utf-8")
    git(harness.root, "add", "drift.txt")
    git(harness.root, "commit", "-m", "test: drift primary")

    with pytest.raises(ParallelMissionWorkflowError, match="STALE_MERGE_OUTCOME"):
        _remediate(harness.restarted(), attempt)
    assert harness.mission_store.load().workflow_generation == 0


def test_forged_blocked_cannot_enter_recovery_but_legitimate_blocked_can(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forged_root = tmp_path / "forged"
    forged_root.mkdir()
    forged_harness = make_harness(forged_root, (make_story("US-0001"),))
    plan, group, context, gate = _unmerged_pass_attempt(forged_harness)
    forged = MergeResult(
        mission_id=plan.mission_id,
        workflow_generation=plan.workflow_generation,
        wave_index=gate.wave_index,
        group_index=gate.group_index,
        baseline_commit=plan.baseline_commit,
        integration_order=gate.integration_order,
        member_commits=tuple(item.result_commit for item in group.member_results),
        integration_commit=None,
        primary_before=plan.baseline_commit,
        primary_after=plan.baseline_commit,
        result=MergeStatus.BLOCKED,
        findings=(),
    )
    forged_attempt = ParallelIntegrationAttempt(plan, group, context, gate, forged)
    with pytest.raises(ParallelMissionWorkflowError, match="UNTRUSTED_MERGE_OUTCOME"):
        forged_harness.restarted().block_for_recovery(
            forged_attempt, updated_at=NOW
        )
    assert forged_harness.mission_store.load().status.value == "ACTIVE"

    real_root = tmp_path / "real"
    real_root.mkdir()
    real_harness = make_harness(real_root, (make_story("US-0001"),))
    real_attempt = _record_blocked(real_harness, monkeypatch)
    assert real_attempt.merge_result is not None
    status_tampered = replace(real_attempt.merge_result, result=MergeStatus.FAILED)
    with pytest.raises(ParallelMissionWorkflowError, match="UNTRUSTED_MERGE_OUTCOME"):
        _remediate(
            real_harness.restarted(),
            replace(real_attempt, merge_result=status_tampered),
        )
    inspection = real_harness.restarted().block_for_recovery(
        real_attempt, updated_at=NOW
    )
    assert inspection.status is ParallelRecoveryStatus.BLOCKED
    assert real_harness.mission_store.load().workflow_generation == 0

    with pytest.raises(ParallelMissionWorkflowError, match="UNTRUSTED_MERGE_OUTCOME"):
        real_harness.restarted().block_for_recovery(real_attempt, updated_at=NOW)


def test_negative_outcome_authority_is_not_publicly_exported() -> None:
    import agentic_engineering_os.application as application
    import agentic_engineering_os.infrastructure as infrastructure

    assert not any("NegativeOutcome" in name for name in application.__all__)
    assert not any("NegativeOutcome" in name for name in infrastructure.__all__)


def test_public_forged_tester_remediation_dossier_is_not_authoritative(
    tmp_path: Path,
) -> None:
    story = make_story("US-0001")
    harness = make_harness(tmp_path, (story,))
    plan = harness.workflow.plan_current()
    _, branch_results, group = implement_group(harness, harness.workflow, plan, 0)
    attempt = harness.workflow.integrate_group(plan, group, updated_at=NOW)
    assert attempt.merge_result is not None
    merged = attempt.merge_result.integration_commit
    assert merged is not None
    workflow = harness.restarted()
    dossier = workflow.accept_integrated_implementer(
        attempt,
        story.id,
        branch_results[story.id],
        integrated_context=make_integrated_context(
            attempt, story.id, branch_results[story.id]
        ),
    )
    negative = _failed_tester(story, merged, 0)
    forged = replace(
        dossier,
        stage=ParallelStoryStage.REMEDIATION_REQUIRED,
        tester_result=negative,
    )

    with pytest.raises(ParallelMissionWorkflowError, match="UNTRUSTED_NEGATIVE_OUTCOME"):
        harness.restarted().remediate_dossier(forged, updated_at=NOW)
    assert harness.mission_store.load().workflow_generation == 0

    authoritative = workflow.accept_tester(dossier, negative)
    remediation = harness.restarted().remediate_dossier(
        authoritative, updated_at=NOW
    )
    assert remediation.triggering_stage is ParallelRemediationStage.TESTER
    assert harness.mission_store.load().workflow_generation == 1


def test_corrupt_negative_outcome_registry_fails_closed_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    attempt = _record_failed(harness, monkeypatch)
    registry = harness.root / ".agentic-engineering-os" / "negative-outcomes.json"
    registry.write_text(
        '{"version":"1.0","version":"1.0","outcomes":[]}\n',
        encoding="utf-8",
    )

    with pytest.raises(
        ParallelMissionWorkflowError, match="MERGE_OUTCOME_AUTHORITY_UNAVAILABLE"
    ):
        _remediate(harness.restarted(), attempt)
    assert harness.mission_store.load().workflow_generation == 0


def test_negative_merge_result_is_not_returned_when_authority_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    _, _, context, gate = _unmerged_pass_attempt(harness)
    coordinator = MergeCoordinator(worktree_manager=harness.manager)
    adapter = harness.manager._integration_git_adapter

    def fail_merge(path: Path, commit: str, *, message: str) -> GitMergeResult:
        return GitMergeResult(False, adapter.current_head(path))

    def fail_write(document) -> None:
        raise PersistenceError("WRITE_FAILED", "simulated authority write failure")

    monkeypatch.setattr(adapter, "merge_no_ff", fail_merge)
    monkeypatch.setattr(coordinator._outcomes, "_write", fail_write)

    with pytest.raises(MergeCoordinationError, match="OUTCOME_PERSISTENCE_FAILED"):
        coordinator.merge(MergeContext(context, gate))
    assert git(harness.root, "status", "--porcelain=v1") == ""
