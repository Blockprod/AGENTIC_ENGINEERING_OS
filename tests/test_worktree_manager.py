import json
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_engineering_os.domain import (
    AcceptanceCriterion,
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
    WorktreeStatus,
)
from agentic_engineering_os.infrastructure import (
    GitOperationError,
    WorktreeManager,
    WorktreeManagerError,
)
import agentic_engineering_os.infrastructure.worktree_registry_store as store_module


NOW = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)


def git(repository: Path, *arguments: str, input_text: str | None = None) -> str:
    process = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        input=input_text,
        check=False,
    )
    if process.returncode != 0:
        raise AssertionError(
            f"git {' '.join(arguments)} failed ({process.returncode}): {process.stderr}"
        )
    return process.stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "repository"
    worktrees = tmp_path / "worktrees"
    root.mkdir()
    worktrees.mkdir()
    git(root, "init", "-b", "trunk")
    git(root, "config", "user.name", "P3.7 Test Operator")
    git(root, "config", "user.email", "p3.7@example.invalid")
    (root / "README.md").write_text("temporary repository\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "test: baseline")
    return root, worktrees, git(root, "rev-parse", "HEAD").casefold()


def mission(*, generation: int = 0) -> MissionState:
    return MissionState(
        schema_version="1.0",
        mission_id="mission-parallel",
        workflow_generation=generation,
        status=MissionStatus.ACTIVE,
        role=MissionRole.ORCHESTRATOR,
        objective="Test isolated worktree management.",
        subject="US-0001",
        operating_step=OperatingStep.ACT,
        next_action="Plan one assignment.",
        observed_commit="0" * 40,
        updated_at=NOW,
        blockers=[],
    )


def story(identifier: str = "US-0001") -> UserStory:
    return UserStory(
        schema_version="1.0",
        id=identifier,
        title=f"Story {identifier}",
        description="Exercise the worktree manager contract.",
        status=UserStoryStatus.IN_PROGRESS,
        priority=1,
        risk=RiskLevel.LOW,
        depends_on=(),
        scope=UserStoryScope(allowed_paths=("src/",), forbidden_paths=()),
        acceptance_criteria=(AcceptanceCriterion("AC-001", "Isolation works.", True),),
        required_gates=(),
        human_approval=HumanApproval(False, False, None, None),
        metadata=UserStoryMetadata(NOW, "human-operator", NOW),
    )


def manager_for(
    root: Path, worktrees: Path, *, initialize: bool = True
) -> WorktreeManager:
    manager = WorktreeManager(repository_root=root, worktree_root=worktrees)
    if initialize:
        manager.initialize_registry()
    return manager


def planned(
    manager: WorktreeManager,
    baseline: str,
    *,
    story_id: str = "US-0001",
    generation: int = 0,
):
    return manager.plan_assignment(
        mission=mission(generation=generation),
        user_story=story(story_id),
        baseline_commit=baseline,
    )


def commit_result(path: Path, filename: str = "result.txt") -> str:
    (path / filename).write_text("isolated result\n", encoding="utf-8")
    git(path, "add", filename)
    git(path, "commit", "-m", "feat: isolated result")
    return git(path, "rev-parse", "HEAD").casefold()


def test_real_git_create_inspect_complete_cleanup_and_branch_retention(
    tmp_path: Path,
) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    assignment = planned(manager, baseline)

    active = manager.activate(assignment.assignment_id, current_generation=0)
    active_path = Path(active.worktree_path)
    inspection = manager.inspect(active.assignment_id, current_generation=0)

    assert active.status is WorktreeStatus.ACTIVE
    assert active_path.exists()
    assert git(active_path, "branch", "--show-current") == active.branch_name
    assert git(active_path, "rev-parse", "HEAD").casefold() == baseline
    assert inspection.resumable
    assert inspection.clean is True

    result_commit = commit_result(active_path)
    completed = manager.complete(active.assignment_id, current_generation=0)
    cleaned = manager.cleanup(
        active.assignment_id,
        integration_in_progress=False,
        confirmed_not_needed=True,
    )

    assert completed.status is WorktreeStatus.COMPLETED
    assert completed.result_commit == result_commit
    assert cleaned.status is WorktreeStatus.CLEANED
    assert not active_path.exists()
    assert git(root, "show-ref", "--verify", f"refs/heads/{active.branch_name}")


def test_active_dirty_is_observable_and_cannot_complete(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    active = manager.activate(planned(manager, baseline).assignment_id, current_generation=0)
    path = Path(active.worktree_path)
    (path / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    inspection = manager.inspect(active.assignment_id, current_generation=0)

    assert inspection.resumable
    assert inspection.clean is False
    with pytest.raises(WorktreeManagerError, match="DIRTY_WORKTREE"):
        manager.complete(active.assignment_id, current_generation=0)
    assert manager.registry_store.load().assignments[0].status is WorktreeStatus.ACTIVE


def test_no_result_commit_means_no_completion(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    active = manager.activate(planned(manager, baseline).assignment_id, current_generation=0)

    with pytest.raises(WorktreeManagerError, match="NO_RESULT_COMMIT"):
        manager.complete(active.assignment_id, current_generation=0)


def test_restart_loads_registry_and_resumes_without_python_object_state(
    tmp_path: Path,
) -> None:
    root, worktrees, baseline = repository(tmp_path)
    first = manager_for(root, worktrees)
    active = first.activate(planned(first, baseline).assignment_id, current_generation=0)

    restarted = manager_for(root, worktrees, initialize=False)
    inspection = restarted.resume(active.assignment_id, current_generation=0)

    assert inspection.resumable
    assert restarted.registry_store.load().assignments[0] == active


def test_two_independent_real_worktrees_share_explicit_baseline(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    first = planned(manager, baseline, story_id="US-0001")
    second = planned(manager, baseline, story_id="US-0002")

    first_active = manager.activate(first.assignment_id, current_generation=0)
    second_active = manager.activate(second.assignment_id, current_generation=0)

    assert first_active.branch_name != second_active.branch_name
    assert first_active.worktree_path != second_active.worktree_path
    assert git(Path(first_active.worktree_path), "rev-parse", "HEAD").casefold() == baseline
    assert git(Path(second_active.worktree_path), "rev-parse", "HEAD").casefold() == baseline


def test_planning_is_deterministic_idempotent_and_has_no_git_mutation(
    tmp_path: Path,
) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)

    first = planned(manager, baseline)
    second = planned(manager, baseline.upper())

    assert first == second
    assert first.status is WorktreeStatus.PLANNED
    assert first.branch_name.startswith("agentic/g0/us-0001-")
    assert Path(first.worktree_path).parent == worktrees.resolve()
    assert len(git(root, "worktree", "list", "--porcelain").split("worktree ")) == 2


def test_non_git_repository_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    worktrees = tmp_path / "worktrees"
    root.mkdir()
    worktrees.mkdir()
    manager = WorktreeManager(repository_root=root, worktree_root=worktrees)

    with pytest.raises(WorktreeManagerError, match="GIT_COMMAND_FAILED"):
        manager.initialize_registry()


def test_linked_worktree_cannot_be_used_as_primary_repository(tmp_path: Path) -> None:
    root, _, baseline = repository(tmp_path)
    linked = tmp_path / "linked"
    linked_worktrees = tmp_path / "linked-worktrees"
    linked_worktrees.mkdir()
    git(root, "worktree", "add", "-b", "linked", str(linked), baseline)
    manager = WorktreeManager(
        repository_root=linked,
        worktree_root=linked_worktrees,
    )

    with pytest.raises(WorktreeManagerError, match="NOT_PRIMARY_WORKTREE"):
        manager.initialize_registry()


def test_unknown_baseline_is_refused_without_registry_mutation(tmp_path: Path) -> None:
    root, worktrees, _ = repository(tmp_path)
    manager = manager_for(root, worktrees)

    with pytest.raises(WorktreeManagerError, match="GIT_COMMAND_FAILED"):
        planned(manager, "f" * 40)

    assert manager.registry_store.load().assignments == ()


def test_ambiguous_identity_inputs_are_refused_before_git_mutation(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    candidates = (
        (replace(mission(), mission_id=" mission-parallel"), story(), baseline),
        (replace(mission(), workflow_generation=True), story(), baseline),
        (mission(), replace(story(), id="../US-0001"), baseline),
        (mission(), story(), "HEAD"),
    )

    for mission_state, user_story, commit in candidates:
        with pytest.raises(WorktreeManagerError, match="INVALID_INPUT"):
            manager.plan_assignment(
                mission=mission_state,
                user_story=user_story,
                baseline_commit=commit,
            )

    assert manager.registry_store.load().assignments == ()
    assert len(git(root, "worktree", "list", "--porcelain").split("worktree ")) == 2


def test_worktree_root_inside_primary_is_refused(tmp_path: Path) -> None:
    root, _, _ = repository(tmp_path)
    nested = root / "worktrees"
    nested.mkdir()

    with pytest.raises(WorktreeManagerError, match="UNSAFE_WORKTREE_ROOT"):
        WorktreeManager(repository_root=root, worktree_root=nested)


def test_dirty_primary_blocks_activation_without_stash(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    assignment = planned(manager, baseline)
    (root / "unexpected.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(WorktreeManagerError, match="DIRTY_PRIMARY"):
        manager.activate(assignment.assignment_id, current_generation=0)

    assert not Path(assignment.worktree_path).exists()
    assert manager.registry_store.load().assignments[0].status is WorktreeStatus.PLANNED


def test_branch_and_path_collisions_are_refused(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    branch_assignment = planned(manager, baseline)
    git(root, "branch", branch_assignment.branch_name, baseline)

    with pytest.raises(WorktreeManagerError, match="BRANCH_COLLISION"):
        manager.activate(branch_assignment.assignment_id, current_generation=0)

    other = planned(manager, baseline, story_id="US-0002")
    Path(other.worktree_path).mkdir()
    with pytest.raises(WorktreeManagerError, match="PATH_COLLISION"):
        manager.activate(other.assignment_id, current_generation=0)


def test_case_variant_branch_collision_is_refused(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    assignment = planned(manager, baseline)
    git(root, "branch", assignment.branch_name.upper(), baseline)

    with pytest.raises(WorktreeManagerError, match="BRANCH_COLLISION"):
        manager.activate(assignment.assignment_id, current_generation=0)


def test_case_variant_path_collision_is_refused(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    assignment = planned(manager, baseline)
    (worktrees / assignment.assignment_id.upper()).mkdir()

    with pytest.raises(WorktreeManagerError, match="PATH_COLLISION"):
        manager.activate(assignment.assignment_id, current_generation=0)


def test_stale_generation_is_not_activated_or_resumed(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    assignment = planned(manager, baseline)

    with pytest.raises(WorktreeManagerError, match="STALE_GENERATION"):
        manager.activate(assignment.assignment_id, current_generation=1)

    active = manager.activate(assignment.assignment_id, current_generation=0)
    inspection = manager.inspect(active.assignment_id, current_generation=1)
    assert not inspection.resumable
    assert "STALE_GENERATION" in inspection.reasons
    with pytest.raises(WorktreeManagerError, match="NOT_RESUMABLE"):
        manager.resume(active.assignment_id, current_generation=1)


def test_registry_git_branch_mismatch_is_fail_closed(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    active = manager.activate(planned(manager, baseline).assignment_id, current_generation=0)
    path = Path(active.worktree_path)
    git(path, "switch", "-c", "local-divergence")

    inspection = manager.inspect(active.assignment_id, current_generation=0)
    reconciliation = manager.inspect_all(current_generation=0)

    assert not inspection.resumable
    assert "BRANCH_MISMATCH" in inspection.reasons
    assert any(item.startswith("REGISTRY_GIT_MISMATCH:") for item in reconciliation.anomalies)


def test_registry_assignment_without_physical_worktree_is_reported(
    tmp_path: Path,
) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    active = manager.activate(planned(manager, baseline).assignment_id, current_generation=0)
    git(root, "worktree", "remove", active.worktree_path)

    inspection = manager.inspect(active.assignment_id, current_generation=0)
    reconciliation = manager.inspect_all(current_generation=0)

    assert not inspection.physical_exists
    assert not inspection.resumable
    assert "WORKTREE_NOT_REGISTERED" in inspection.reasons
    assert any(item.startswith("REGISTRY_GIT_MISMATCH:") for item in reconciliation.anomalies)


def test_orphan_agentic_worktree_is_reported_not_adopted(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    orphan_path = worktrees / "orphan"
    git(root, "worktree", "add", "-b", "agentic/orphan", str(orphan_path), baseline)

    reconciliation = manager.inspect_all(current_generation=0)

    assert not reconciliation.is_consistent
    assert any(item.startswith("ORPHAN_AGENTIC_WORKTREE:") for item in reconciliation.anomalies)
    assert manager.registry_store.load().assignments == ()


def test_complete_wrong_branch_is_refused(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    active = manager.activate(planned(manager, baseline).assignment_id, current_generation=0)
    path = Path(active.worktree_path)
    git(path, "switch", "-c", "wrong-branch")
    commit_result(path)

    with pytest.raises(WorktreeManagerError, match="WORKTREE_MISMATCH"):
        manager.complete(active.assignment_id, current_generation=0)


def test_complete_non_descendant_commit_is_refused(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    active = manager.activate(planned(manager, baseline).assignment_id, current_generation=0)
    path = Path(active.worktree_path)
    tree = git(path, "rev-parse", "HEAD^{tree}")
    unrelated = git(path, "commit-tree", tree, input_text="unrelated root\n")
    git(path, "update-ref", f"refs/heads/{active.branch_name}", unrelated)

    with pytest.raises(WorktreeManagerError, match="NON_DESCENDANT_RESULT"):
        manager.complete(active.assignment_id, current_generation=0)


def test_failed_preserves_dirty_diagnostics_and_cleanup_refuses(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    active = manager.activate(planned(manager, baseline).assignment_id, current_generation=0)
    path = Path(active.worktree_path)
    (path / "diagnostic.txt").write_text("preserve me\n", encoding="utf-8")

    failed = manager.mark_failed(active.assignment_id, current_generation=0)
    inspection = manager.inspect(failed.assignment_id, current_generation=0)

    assert failed.status is WorktreeStatus.FAILED
    assert path.exists()
    assert inspection.clean is False
    with pytest.raises(WorktreeManagerError, match="DIRTY_WORKTREE"):
        manager.cleanup(
            failed.assignment_id,
            integration_in_progress=False,
            confirmed_not_needed=True,
        )
    assert path.exists()


def test_cleanup_refuses_active_or_unconfirmed_or_integrating(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    active = manager.activate(planned(manager, baseline).assignment_id, current_generation=0)

    with pytest.raises(WorktreeManagerError, match="INVALID_STATUS"):
        manager.cleanup(
            active.assignment_id,
            integration_in_progress=False,
            confirmed_not_needed=True,
        )
    failed = manager.mark_failed(active.assignment_id, current_generation=0)
    with pytest.raises(WorktreeManagerError, match="CLEANUP_NOT_CONFIRMED"):
        manager.cleanup(
            failed.assignment_id,
            integration_in_progress=False,
            confirmed_not_needed=False,
        )
    with pytest.raises(WorktreeManagerError, match="INTEGRATION_IN_PROGRESS"):
        manager.cleanup(
            failed.assignment_id,
            integration_in_progress=True,
            confirmed_not_needed=True,
        )


def test_git_failure_preserves_planned_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    assignment = planned(manager, baseline)

    def fail_git(path: Path, branch: str, commit: str) -> None:
        raise GitOperationError("SIMULATED_GIT_FAILURE", "add failed", exit_code=9)

    monkeypatch.setattr(manager._git, "add_worktree", fail_git)
    with pytest.raises(WorktreeManagerError, match="SIMULATED_GIT_FAILURE"):
        manager.activate(assignment.assignment_id, current_generation=0)

    assert manager.registry_store.load().assignments[0] == assignment


def test_failed_post_create_verification_cleans_only_safe_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    assignment = planned(manager, baseline)
    original = manager._git.current_head

    def wrong_head(path: Path) -> str:
        return "f" * 40 if path == Path(assignment.worktree_path) else original(path)

    monkeypatch.setattr(manager._git, "current_head", wrong_head)
    with pytest.raises(WorktreeManagerError, match="POST_CREATE_VERIFICATION_FAILED"):
        manager.activate(assignment.assignment_id, current_generation=0)

    assert not Path(assignment.worktree_path).exists()
    assert manager.registry_store.load().assignments[0].status is WorktreeStatus.PLANNED
    assert git(root, "show-ref", "--verify", f"refs/heads/{assignment.branch_name}")


def test_git_success_registry_failure_is_observable_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    assignment = planned(manager, baseline)

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("simulated registry replace failure")

    monkeypatch.setattr(store_module.os, "replace", fail_replace)
    with pytest.raises(WorktreeManagerError, match="REGISTRY_WRITE_FAILED_AFTER_GIT"):
        manager.activate(assignment.assignment_id, current_generation=0)
    monkeypatch.undo()

    restarted = manager_for(root, worktrees, initialize=False)
    persisted = restarted.registry_store.load().assignments[0]
    reconciliation = restarted.inspect_all(current_generation=0)

    assert persisted.status is WorktreeStatus.PLANNED
    assert Path(assignment.worktree_path).exists()
    assert any(
        item.startswith("UNEXPECTED_PHYSICAL_WORKTREE:")
        for item in reconciliation.anomalies
    )


def test_corrupt_registry_blocks_manager_without_fallback(tmp_path: Path) -> None:
    root, worktrees, _ = repository(tmp_path)
    manager = manager_for(root, worktrees)
    manager.registry_store.registry_path.write_text(
        '{"schema_version":"1.0","assignments":[],"assignments":[]}',
        encoding="utf-8",
    )

    with pytest.raises(WorktreeManagerError, match="INVALID_JSON"):
        manager.inspect_all(current_generation=0)


def test_registry_path_outside_configured_root_is_refused(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    assignment = planned(manager, baseline)
    raw = json.loads(manager.registry_store.registry_path.read_text(encoding="utf-8"))
    raw["assignments"][0]["worktree_path"] = str(
        (tmp_path / "outside" / assignment.assignment_id).resolve()
    )
    manager.registry_store.registry_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(WorktreeManagerError, match="WORKTREE_ROOT_MISMATCH"):
        manager.inspect_all(current_generation=0)


def test_cleanup_failed_clean_worktree_retains_branch(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    active = manager.activate(planned(manager, baseline).assignment_id, current_generation=0)
    failed = manager.mark_failed(active.assignment_id, current_generation=0)

    cleaned = manager.cleanup(
        failed.assignment_id,
        integration_in_progress=False,
        confirmed_not_needed=True,
    )

    assert cleaned.status is WorktreeStatus.CLEANED
    assert not Path(failed.worktree_path).exists()
    assert git(root, "show-ref", "--verify", f"refs/heads/{failed.branch_name}")


def test_cleanup_refuses_completed_branch_advanced_after_recording(
    tmp_path: Path,
) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = manager_for(root, worktrees)
    active = manager.activate(planned(manager, baseline).assignment_id, current_generation=0)
    path = Path(active.worktree_path)
    commit_result(path)
    completed = manager.complete(active.assignment_id, current_generation=0)
    commit_result(path, "later.txt")

    with pytest.raises(WorktreeManagerError, match="RESULT_COMMIT_MISMATCH"):
        manager.cleanup(
            completed.assignment_id,
            integration_in_progress=False,
            confirmed_not_needed=True,
        )

    assert path.exists()


def test_manager_source_has_no_unauthorized_merge_or_parallel_execution_primitives() -> None:
    source = (
        Path(__file__).parents[1]
        / "src"
        / "agentic_engineering_os"
        / "infrastructure"
        / "worktree_manager.py"
    ).read_text(encoding="utf-8")
    adapter = (
        Path(__file__).parents[1]
        / "src"
        / "agentic_engineering_os"
        / "infrastructure"
        / "git_adapter.py"
    ).read_text(encoding="utf-8")
    manager_forbidden = (
        '"merge"',
        '"rebase"',
        '"cherry-pick"',
        "thread",
        "asyncio",
        "codex",
    )
    adapter_forbidden = (
        "reset --hard",
        "clean -fd",
        "remove --force",
        "branch -d",
        "force push",
        '"rebase"',
        '"cherry-pick"',
        "thread",
        "asyncio",
        "codex",
    )
    assert all(item not in source.casefold() for item in manager_forbidden)
    assert all(item not in adapter.casefold() for item in adapter_forbidden)
    assert "shell=false" in adapter.casefold()
