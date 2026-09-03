import json
from dataclasses import replace
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    MergeContext,
    MergeCoordinationError,
    MergeCoordinator,
    ParallelMissionWorkflow,
    ParallelMissionWorkflowError,
)
from agentic_engineering_os.application.state_transition_service import TransitionContext
from agentic_engineering_os._authoritative_write import _issue_authoritative_write
from agentic_engineering_os.domain import UserStoryStatus, to_dict
from agentic_engineering_os.infrastructure.project_state_store import PersistenceError

from test_negative_merge_outcome_authority import (
    _record_blocked,
    _record_failed,
    _remediate,
)
from test_parallel_mission_workflow import (
    NOW,
    implement_group,
    make_harness,
    make_integrated_context,
    make_story,
    make_tester_result,
)
from test_parallel_remediation_recovery import (
    _failed_tester,
    _remediation_reviewer,
    _unmerged_pass_attempt,
)


class _FailingMissionStore:
    def __init__(self, delegate) -> None:
        self.delegate = delegate

    def load(self):
        return self.delegate.load()

    def save(self, *args, **kwargs):
        raise RuntimeError("mission persistence unavailable")


class _FailingProjectStore:
    def __init__(self, delegate) -> None:
        self.delegate = delegate

    def load(self):
        return self.delegate.load()

    def save(self, *args, **kwargs):
        raise RuntimeError("project persistence unavailable")


def _workflow(harness, *, mission_store=None, project_store=None):
    return ParallelMissionWorkflow(
        mission_store=mission_store or harness.mission_store,
        project_store=project_store or harness.project_store,
        control_loop=harness.control,
        worktree_manager=harness.manager,
    )


def _ledger(harness) -> dict[str, object]:
    path = harness.root / ".agentic-engineering-os" / "negative-outcomes.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _pending(harness) -> dict[str, object]:
    transactions = _ledger(harness)["transactions"]
    assert isinstance(transactions, list)
    pending = [item for item in transactions if item["status"] == "PENDING"]
    assert len(pending) == 1
    return pending[0]


def test_real_failed_merge_recovers_after_project_only_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    attempt = _record_failed(harness, monkeypatch)
    interrupted = ParallelMissionWorkflow(
        mission_store=_FailingMissionStore(harness.mission_store),
        project_store=harness.project_store,
        control_loop=harness.control,
        worktree_manager=harness.manager,
    )

    with pytest.raises(RuntimeError, match="mission persistence unavailable"):
        _remediate(interrupted, attempt)

    assert harness.project_store.load().user_stories[0].status is UserStoryStatus.READY
    assert harness.mission_store.load().workflow_generation == 0

    restarted = harness.restarted()
    inspection = restarted.inspect_recovery()
    assert "PENDING_REMEDIATION_TRANSACTION" in inspection.anomalies
    restarted.resume_recovery(updated_at=NOW)

    assert harness.project_store.load().user_stories[0].status is UserStoryStatus.READY
    assert harness.mission_store.load().workflow_generation == 1
    assert _pending_or_none(harness) is None
    with pytest.raises(ParallelMissionWorkflowError):
        _remediate(harness.restarted(), attempt)


def _pending_or_none(harness):
    transactions = _ledger(harness)["transactions"]
    return next((item for item in transactions if item["status"] == "PENDING"), None)


def test_f0_claim_failure_has_no_business_mutation_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    attempt = _record_failed(harness, monkeypatch)
    before_project = to_dict(harness.project_store.load())
    before_mission = to_dict(harness.mission_store.load())
    workflow = harness.restarted()
    original_write = workflow._negative_outcomes._write

    def fail_claim(document) -> None:
        raise PersistenceError("WRITE_FAILED", "claim unavailable")

    monkeypatch.setattr(workflow._negative_outcomes, "_write", fail_claim)
    with pytest.raises(
        ParallelMissionWorkflowError, match="TRANSACTION_PERSISTENCE_FAILED"
    ):
        _remediate(workflow, attempt)
    assert to_dict(harness.project_store.load()) == before_project
    assert to_dict(harness.mission_store.load()) == before_mission

    monkeypatch.setattr(workflow._negative_outcomes, "_write", original_write)
    remediation = _remediate(harness.restarted(), attempt)
    assert remediation.new_generation == 1


def test_f1_project_failure_is_pending_and_restart_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    attempt = _record_failed(harness, monkeypatch)
    before_project = to_dict(harness.project_store.load())
    interrupted = _workflow(
        harness, project_store=_FailingProjectStore(harness.project_store)
    )

    with pytest.raises(RuntimeError, match="project persistence unavailable"):
        _remediate(interrupted, attempt)
    assert to_dict(harness.project_store.load()) == before_project
    assert harness.mission_store.load().workflow_generation == 0
    assert _pending(harness)["intent"]["source_generation"] == 0

    harness.restarted().resume_recovery(updated_at=NOW)
    assert harness.project_store.load().user_stories[0].status is UserStoryStatus.READY
    assert harness.mission_store.load().workflow_generation == 1


def test_f3_finalization_failure_restarts_with_finalize_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    attempt = _record_failed(harness, monkeypatch)
    workflow = harness.restarted()
    original_write = workflow._negative_outcomes._write
    writes = 0

    def fail_second_write(document) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise PersistenceError("WRITE_FAILED", "finalization unavailable")
        original_write(document)

    monkeypatch.setattr(workflow._negative_outcomes, "_write", fail_second_write)
    with pytest.raises(
        ParallelMissionWorkflowError, match="TRANSACTION_FINALIZATION_FAILED"
    ):
        _remediate(workflow, attempt)
    project_after = to_dict(harness.project_store.load())
    assert harness.mission_store.load().workflow_generation == 1
    assert _pending(harness)["status"] == "PENDING"

    for _ in range(2):
        retry = harness.restarted()

        def fail_finalize(fingerprint) -> None:
            raise PersistenceError("WRITE_FAILED", "finalization unavailable")

        monkeypatch.setattr(retry._negative_outcomes, "_finalize", fail_finalize)
        with pytest.raises(
            ParallelMissionWorkflowError, match="TRANSACTION_FINALIZATION_FAILED"
        ):
            retry.resume_recovery(updated_at=NOW)
        assert to_dict(harness.project_store.load()) == project_after
        assert harness.mission_store.load().workflow_generation == 1

    harness.restarted().resume_recovery(updated_at=NOW)
    assert to_dict(harness.project_store.load()) == project_after
    assert harness.mission_store.load().workflow_generation == 1
    assert _pending_or_none(harness) is None


def test_repeated_mission_and_finalization_failures_remain_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    attempt = _record_failed(harness, monkeypatch)
    interrupted = _workflow(
        harness, mission_store=_FailingMissionStore(harness.mission_store)
    )
    with pytest.raises(RuntimeError, match="mission persistence unavailable"):
        _remediate(interrupted, attempt)
    project_after = to_dict(harness.project_store.load())

    for _ in range(2):
        retry = _workflow(
            harness, mission_store=_FailingMissionStore(harness.mission_store)
        )
        with pytest.raises(RuntimeError, match="mission persistence unavailable"):
            retry.resume_recovery(updated_at=NOW)
        assert to_dict(harness.project_store.load()) == project_after
        assert harness.mission_store.load().workflow_generation == 0

    harness.restarted().resume_recovery(updated_at=NOW)
    assert harness.mission_store.load().workflow_generation == 1

    # A completed business transition with a pending ledger is finalize-only,
    # even when finalization itself remains unavailable repeatedly.
    assert to_dict(harness.project_store.load()) == project_after
    assert _pending_or_none(harness) is None


def test_pending_transaction_blocks_duplicates_planning_worktrees_and_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    attempt = _record_failed(harness, monkeypatch)
    interrupted = _workflow(
        harness, project_store=_FailingProjectStore(harness.project_store)
    )
    with pytest.raises(RuntimeError):
        _remediate(interrupted, attempt)

    restarted = harness.restarted()
    with pytest.raises(ParallelMissionWorkflowError, match="RECOVERY_PENDING"):
        _remediate(restarted, attempt)
    with pytest.raises(ParallelMissionWorkflowError, match="RECOVERY_PENDING"):
        restarted.plan_current()
    with pytest.raises(ParallelMissionWorkflowError, match="RECOVERY_PENDING"):
        restarted.prepare_group(attempt.plan, 0)
    with pytest.raises(ParallelMissionWorkflowError, match="RECOVERY_PENDING"):
        restarted.finalize(current_commit=harness.baseline, updated_at=NOW)
    with pytest.raises(MergeCoordinationError, match="RECOVERY_PENDING"):
        MergeCoordinator(worktree_manager=harness.manager).merge(
            MergeContext(attempt.gate_context, attempt.gate_result)
        )


def test_unexpected_project_state_blocks_without_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    attempt = _record_failed(harness, monkeypatch)
    interrupted = _workflow(
        harness, project_store=_FailingProjectStore(harness.project_store)
    )
    with pytest.raises(RuntimeError):
        _remediate(interrupted, attempt)
    harness.control.transition_user_story(
        "US-0001",
        UserStoryStatus.BLOCKED,
        context=TransitionContext(preconditions_proven=True),
    )

    with pytest.raises(ParallelMissionWorkflowError, match="BLOCKED_INCONSISTENT"):
        harness.restarted().resume_recovery(updated_at=NOW)
    assert harness.mission_store.load().workflow_generation == 0
    assert _pending(harness)["status"] == "PENDING"


def test_unexpected_mission_state_blocks_without_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    attempt = _record_failed(harness, monkeypatch)
    interrupted = _workflow(
        harness, project_store=_FailingProjectStore(harness.project_store)
    )
    with pytest.raises(RuntimeError):
        _remediate(interrupted, attempt)
    current = harness.mission_store.load()
    unexpected = replace(current, next_action="Unexpected concurrent mutation.")
    authorization = _issue_authoritative_write(
        store_kind="MISSION_STATE",
        store=harness.mission_store,
        before_state=current,
        candidate_state=unexpected,
        operation="TEST_UNEXPECTED_STATE",
    )
    harness.mission_store.save(
        unexpected,
        authorization=authorization,
        operation="TEST_UNEXPECTED_STATE",
    )

    with pytest.raises(ParallelMissionWorkflowError, match="BLOCKED_INCONSISTENT"):
        harness.restarted().resume_recovery(updated_at=NOW)
    assert harness.mission_store.load().workflow_generation == 0
    assert _pending(harness)["status"] == "PENDING"


def test_mission_only_applied_recovery_applies_project_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    attempt = _record_failed(harness, monkeypatch)
    interrupted = _workflow(
        harness, project_store=_FailingProjectStore(harness.project_store)
    )
    with pytest.raises(RuntimeError):
        _remediate(interrupted, attempt)
    record = _pending(harness)
    intent = record["intent"]
    current = harness.mission_store.load()
    candidate = harness.restarted()._mission_candidate_from_transaction(
        current, intent
    )
    authorization = _issue_authoritative_write(
        store_kind="MISSION_STATE",
        store=harness.mission_store,
        before_state=current,
        candidate_state=candidate,
        operation="BEGIN_PARALLEL_REMEDIATION",
    )
    harness.mission_store.save(
        candidate,
        authorization=authorization,
        operation="BEGIN_PARALLEL_REMEDIATION",
    )
    assert harness.mission_store.load().workflow_generation == 1
    assert harness.project_store.load().user_stories[0].status is not UserStoryStatus.READY

    harness.restarted().resume_recovery(updated_at=NOW)
    assert harness.project_store.load().user_stories[0].status is UserStoryStatus.READY
    assert harness.mission_store.load().workflow_generation == 1
    assert _pending_or_none(harness) is None


def test_same_generation_blocked_recovery_is_restart_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    attempt = _record_blocked(harness, monkeypatch)
    interrupted = _workflow(
        harness, mission_store=_FailingMissionStore(harness.mission_store)
    )
    with pytest.raises(RuntimeError, match="mission persistence unavailable"):
        interrupted.block_for_recovery(attempt, updated_at=NOW)
    intent = _pending(harness)["intent"]
    assert intent["source_generation"] == intent["target_generation"] == 0

    inspection = harness.restarted().resume_recovery(updated_at=NOW)
    assert inspection.active_generation == 0
    assert harness.mission_store.load().status.value == "BLOCKED"
    assert _pending_or_none(harness) is None
    resumed = harness.restarted().resume_recovery(updated_at=NOW)
    assert resumed.active_generation == 0
    assert harness.mission_store.load().status.value == "ACTIVE"


def test_gate_failure_mission_write_failure_recovers_exactly_once(
    tmp_path: Path
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    plan, group, _, _ = _unmerged_pass_attempt(harness)
    member = group.member_results[0]
    forged_declaration = replace(
        group,
        member_results=(
            replace(
                member,
                implementer_result=replace(
                    member.implementer_result, files_changed=()
                ),
            ),
        ),
    )
    attempt = harness.workflow.integrate_group(plan, forged_declaration, updated_at=NOW)
    assert attempt.gate_result.result.value == "FAIL"
    interrupted = _workflow(
        harness, mission_store=_FailingMissionStore(harness.mission_store)
    )
    with pytest.raises(RuntimeError, match="mission persistence unavailable"):
        interrupted.remediate_integration(
            attempt,
            affected_user_story_ids=("US-0001",),
            updated_at=NOW,
        )
    harness.restarted().resume_recovery(updated_at=NOW)
    assert harness.mission_store.load().workflow_generation == 1


def test_tester_failure_mission_write_failure_recovers_exactly_once(
    tmp_path: Path
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    plan = harness.workflow.plan_current()
    _, branches, group = implement_group(harness, harness.workflow, plan, 0)
    attempt = harness.workflow.integrate_group(plan, group, updated_at=NOW)
    merged = attempt.merge_result.integration_commit
    assert merged is not None
    story = harness.project_store.load().user_stories[0]
    workflow = harness.restarted()
    dossier = workflow.accept_integrated_implementer(
        attempt,
        story.id,
        branches[story.id],
        integrated_context=make_integrated_context(
            attempt, story.id, branches[story.id]
        ),
    )
    negative = workflow.accept_tester(dossier, _failed_tester(story, merged, 0))
    interrupted = _workflow(
        harness, mission_store=_FailingMissionStore(harness.mission_store)
    )
    with pytest.raises(RuntimeError, match="mission persistence unavailable"):
        interrupted.remediate_dossier(negative, updated_at=NOW)
    harness.restarted().resume_recovery(updated_at=NOW)
    assert harness.mission_store.load().workflow_generation == 1


def test_reviewer_failure_mission_write_failure_recovers_exactly_once(
    tmp_path: Path
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    plan = harness.workflow.plan_current()
    _, branches, group = implement_group(harness, harness.workflow, plan, 0)
    attempt = harness.workflow.integrate_group(plan, group, updated_at=NOW)
    merged = attempt.merge_result.integration_commit
    assert merged is not None
    story = harness.project_store.load().user_stories[0]
    workflow = harness.restarted()
    dossier = workflow.accept_integrated_implementer(
        attempt,
        story.id,
        branches[story.id],
        integrated_context=make_integrated_context(
            attempt, story.id, branches[story.id]
        ),
    )
    tested = workflow.accept_tester(dossier, make_tester_result(story, merged))
    negative = workflow.accept_reviewer(
        tested,
        _remediation_reviewer(
            story, merged, 0, branches[story.id].files_changed
        ),
    )
    interrupted = _workflow(
        harness, mission_store=_FailingMissionStore(harness.mission_store)
    )
    with pytest.raises(RuntimeError, match="mission persistence unavailable"):
        interrupted.remediate_dossier(negative, updated_at=NOW)
    harness.restarted().resume_recovery(updated_at=NOW)
    assert harness.mission_store.load().workflow_generation == 1


def test_new_legitimate_failure_in_next_generation_can_open_n_plus_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    attempt = _record_failed(harness, monkeypatch)
    assert _remediate(harness.restarted(), attempt).new_generation == 1
    workflow = harness.restarted()
    plan = workflow.plan_current()
    prepared = workflow.prepare_group(plan, 0)
    failed = workflow.fail_member(prepared, prepared.assignment_ids[0])
    remediation = workflow.remediate_failed_group(
        plan,
        failed,
        affected_user_story_ids=("US-0001",),
        updated_at=NOW,
    )
    assert remediation.previous_generation == 1
    assert remediation.new_generation == 2
    assert harness.mission_store.load().workflow_generation == 2


def test_transaction_json_is_strict_and_primitives_are_not_exported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agentic_engineering_os.application as application
    import agentic_engineering_os.infrastructure as infrastructure

    assert not any("Transaction" in name for name in application.__all__)
    assert not any("Transaction" in name for name in infrastructure.__all__)

    harness = make_harness(tmp_path, (make_story("US-0001"),))
    attempt = _record_failed(harness, monkeypatch)
    interrupted = _workflow(
        harness, project_store=_FailingProjectStore(harness.project_store)
    )
    with pytest.raises(RuntimeError):
        _remediate(interrupted, attempt)
    path = harness.root / ".agentic-engineering-os" / "negative-outcomes.json"
    text = path.read_text(encoding="utf-8")
    original = json.loads(text)
    tampering = (
        ("mission_id", "FORGED"),
        ("source_generation", 99),
        ("target_generation", 99),
        ("triggering_stage", "FORGED"),
        ("authority_fingerprint", "0" * 64),
        ("affected_user_story_ids", ["FORGED"]),
        ("baseline_commit", "0" * 40),
        ("project_before_fingerprint", "0" * 64),
        ("mission_after_fingerprint", "0" * 64),
    )
    for field, value in tampering:
        candidate = json.loads(json.dumps(original))
        candidate["transactions"][0]["intent"][field] = value
        path.write_text(json.dumps(candidate), encoding="utf-8")
        with pytest.raises(
            ParallelMissionWorkflowError, match="TRANSACTION_AUTHORITY_UNAVAILABLE"
        ):
            harness.restarted().inspect_recovery()

    path.write_text(
        text.replace('"transactions":', '"transactions":[],"transactions":'),
        encoding="utf-8",
    )

    with pytest.raises(
        ParallelMissionWorkflowError, match="TRANSACTION_AUTHORITY_UNAVAILABLE"
    ):
        harness.restarted().inspect_recovery()
