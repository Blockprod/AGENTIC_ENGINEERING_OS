import inspect
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    IntegrationGate,
    IntegrationGateClassification,
    MergeContext,
    MergeCoordinationError,
    MergeCoordinator,
    MergeFindingCode,
    MergeStatus,
)
from agentic_engineering_os.infrastructure import GitMergeResult, GitOperationError

from test_integration_gate import completed_group, git, story


def approved_group(tmp_path: Path, count: int = 3):
    letters = "abc"[:count]
    stories = tuple(
        story(f"US-{index:04d}", (f"src/{letter}.py",))
        for index, letter in zip(range(1, count + 1), letters, strict=True)
    )
    changes = {
        item.id: {f"src/{letter}.py": f"value = '{letter}'\n"}
        for item, letter in zip(stories, letters, strict=True)
    }
    declared = {
        item.id: (f"src/{letter}.py",)
        for item, letter in zip(stories, letters, strict=True)
    }
    root, worktrees, baseline, manager, gate_context = completed_group(
        tmp_path,
        stories=stories,
        changes=changes,
        declared=declared,
    )
    gate = IntegrationGate(worktree_manager=manager).evaluate(gate_context)
    assert gate.result is IntegrationGateClassification.PASS
    return root, worktrees, baseline, manager, gate_context, gate


def merge_context(gate_context, gate):
    return MergeContext(gate_context=gate_context, gate_result=gate)


def test_real_git_golden_merge_is_ordered_clean_and_preserves_members(tmp_path: Path) -> None:
    root, _, baseline, manager, gate_context, gate = approved_group(tmp_path)
    member_branches = {
        item.branch_name: git(root, "rev-parse", item.branch_name)
        for item in manager.registry_store.load().assignments
    }

    result = MergeCoordinator(worktree_manager=manager).merge(
        merge_context(gate_context, gate)
    )

    assert result.result is MergeStatus.MERGED
    assert result.primary_before == baseline
    assert result.primary_after == result.integration_commit
    assert git(root, "rev-parse", "HEAD").casefold() == result.integration_commit
    assert git(root, "status", "--porcelain=v1") == ""
    assert tuple(
        git(root, "log", "--reverse", "--first-parent", "--format=%s", f"{baseline}..HEAD").splitlines()
    ) == (
        "merge: integrate US-0001",
        "merge: integrate US-0002",
        "merge: integrate US-0003",
    )
    assert all((root / f"src/{letter}.py").exists() for letter in "abc")
    assert all(git(root, "merge-base", "--is-ancestor", commit, "HEAD") == "" for commit in gate_context.group_result.result_commits)
    assert {
        branch: git(root, "rev-parse", branch)
        for branch in member_branches
    } == member_branches
    assert all(item.status.value == "COMPLETED" for item in manager.registry_store.load().assignments)


@pytest.mark.parametrize(
    "classification", [IntegrationGateClassification.FAIL, IntegrationGateClassification.UNKNOWN]
)
def test_non_pass_gate_never_merges(tmp_path: Path, classification) -> None:
    root, _, baseline, manager, gate_context, gate = approved_group(tmp_path, count=1)
    supplied = replace(gate, result=classification)

    result = MergeCoordinator(worktree_manager=manager).merge(
        merge_context(gate_context, supplied)
    )

    assert result.result is MergeStatus.BLOCKED
    assert result.findings[0].code is MergeFindingCode.GATE_NOT_PASS
    assert git(root, "rev-parse", "HEAD").casefold() == baseline


def test_primary_drift_blocks_without_merge(tmp_path: Path) -> None:
    root, _, baseline, manager, gate_context, gate = approved_group(tmp_path, count=1)
    (root / "external.txt").write_text("drift\n", encoding="utf-8")
    git(root, "add", "external.txt")
    git(root, "commit", "-m", "external: primary drift")
    drift = git(root, "rev-parse", "HEAD").casefold()

    result = MergeCoordinator(worktree_manager=manager).merge(
        merge_context(gate_context, gate)
    )

    assert result.result is MergeStatus.BLOCKED
    assert result.findings[0].code is MergeFindingCode.PRIMARY_DRIFT
    assert git(root, "rev-parse", "HEAD").casefold() == drift
    assert drift != baseline


def test_dirty_primary_blocks_without_stash_or_merge(tmp_path: Path) -> None:
    root, _, baseline, manager, gate_context, gate = approved_group(tmp_path, count=1)
    (root / "uncommitted.txt").write_text("preserve me\n", encoding="utf-8")

    result = MergeCoordinator(worktree_manager=manager).merge(
        merge_context(gate_context, gate)
    )

    assert result.result is MergeStatus.BLOCKED
    assert result.findings[0].code is MergeFindingCode.PRIMARY_DIRTY
    assert git(root, "rev-parse", "HEAD").casefold() == baseline
    assert (root / "uncommitted.txt").read_text(encoding="utf-8") == "preserve me\n"


def test_member_branch_drift_makes_gate_stale(tmp_path: Path) -> None:
    root, _, baseline, manager, gate_context, gate = approved_group(tmp_path, count=1)
    assignment = manager.registry_store.load().assignments[0]
    member = Path(assignment.worktree_path)
    (member / "after-gate.txt").write_text("drift\n", encoding="utf-8")
    git(member, "add", "after-gate.txt")
    git(member, "commit", "-m", "external: member drift")

    result = MergeCoordinator(worktree_manager=manager).merge(
        merge_context(gate_context, gate)
    )

    assert result.result is MergeStatus.BLOCKED
    assert result.findings[0].code is MergeFindingCode.STALE_INTEGRATION_GATE
    assert git(root, "rev-parse", "HEAD").casefold() == baseline


def test_forged_pass_or_missing_member_is_rejected(tmp_path: Path) -> None:
    _, _, _, manager, gate_context, gate = approved_group(tmp_path)
    forged = replace(
        gate,
        integration_order=gate.integration_order[:-1],
        member_commits=gate.member_commits[:-1],
    )

    with pytest.raises(MergeCoordinationError, match="exactly bound"):
        MergeCoordinator(worktree_manager=manager).merge(
            merge_context(gate_context, forged)
        )


def test_gate_pass_with_modified_member_commit_is_rejected(tmp_path: Path) -> None:
    root, _, baseline, manager, gate_context, gate = approved_group(tmp_path, count=1)
    member = gate.member_commits[0]
    forged = replace(
        gate,
        member_commits=(replace(member, result_commit=baseline),),
    )

    with pytest.raises(MergeCoordinationError, match="exactly bound"):
        MergeCoordinator(worktree_manager=manager).merge(
            merge_context(gate_context, forged)
        )
    assert git(root, "rev-parse", "HEAD").casefold() == baseline


def test_actual_git_conflict_is_aborted_and_primary_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, worktrees, baseline, manager, gate_context, gate = approved_group(tmp_path, count=2)
    intruder_path = worktrees / "intruder"
    git(root, "worktree", "add", "-b", "test/intruder", str(intruder_path), baseline)
    (intruder_path / "src/a.py").write_text("value = 'conflict'\n", encoding="utf-8")
    git(intruder_path, "add", "src/a.py")
    git(intruder_path, "commit", "-m", "test: conflicting commit")
    intruder = git(intruder_path, "rev-parse", "HEAD")
    adapter = manager._integration_git_adapter
    original = adapter.merge_no_ff
    calls = 0

    def conflict_on_second(path: Path, commit: str, *, message: str) -> GitMergeResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(path, commit, message=message)
        process = subprocess.run(
            ["git", "-C", str(path), "merge", "--no-ff", "--no-edit", intruder],
            shell=False,
            capture_output=True,
            text=True,
            check=False,
        )
        assert process.returncode != 0
        return GitMergeResult(False, adapter.current_head(path))

    monkeypatch.setattr(adapter, "merge_no_ff", conflict_on_second)
    result = MergeCoordinator(worktree_manager=manager).merge(
        merge_context(gate_context, gate)
    )

    assert result.result is MergeStatus.FAILED
    assert result.findings[0].code is MergeFindingCode.GIT_MERGE_CONFLICT
    assert git(root, "rev-parse", "HEAD").casefold() == baseline
    assert git(root, "status", "--porcelain=v1") == ""
    integration = next(
        item for item in adapter.list_worktrees()
        if item.branch_name and item.branch_name.startswith("agentic/integration/")
    )
    assert not adapter.merge_in_progress(integration.path)
    assert adapter.is_clean(integration.path)


def test_partial_temp_integration_blocks_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, baseline, manager, gate_context, gate = approved_group(tmp_path, count=2)
    adapter = manager._integration_git_adapter
    original = adapter.merge_no_ff
    calls = 0

    def stop_after_first(path: Path, commit: str, *, message: str) -> GitMergeResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(path, commit, message=message)
        raise GitOperationError("SIMULATED_INTERRUPTION", "test interruption")

    monkeypatch.setattr(adapter, "merge_no_ff", stop_after_first)
    first = MergeCoordinator(worktree_manager=manager).merge(
        merge_context(gate_context, gate)
    )
    assert first.result is MergeStatus.FAILED
    monkeypatch.setattr(adapter, "merge_no_ff", original)

    restarted = MergeCoordinator(worktree_manager=manager).merge(
        merge_context(gate_context, gate)
    )

    assert restarted.result is MergeStatus.BLOCKED
    assert restarted.findings[0].code in {
        MergeFindingCode.STALE_INTEGRATION_GATE,
        MergeFindingCode.TEMP_RESOURCE_DIVERGENCE,
    }
    assert git(root, "rev-parse", "HEAD").casefold() == baseline


def test_repeat_after_success_is_idempotent(tmp_path: Path) -> None:
    root, _, _, manager, gate_context, gate = approved_group(tmp_path, count=2)
    coordinator = MergeCoordinator(worktree_manager=manager)
    first = coordinator.merge(merge_context(gate_context, gate))
    refs_before = git(root, "for-each-ref", "--format=%(refname):%(objectname)", "refs/heads")

    second = MergeCoordinator(worktree_manager=manager).merge(
        merge_context(gate_context, gate)
    )

    assert first.result is MergeStatus.MERGED
    assert second.result is MergeStatus.MERGED
    assert second.integration_commit == first.integration_commit
    assert second.findings[0].code is MergeFindingCode.ALREADY_MERGED
    assert git(root, "for-each-ref", "--format=%(refname):%(objectname)", "refs/heads") == refs_before


def test_primary_drift_at_final_promotion_cannot_integrate_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, baseline, manager, gate_context, gate = approved_group(tmp_path, count=1)
    adapter = manager._integration_git_adapter
    original = adapter.fast_forward

    def drift_then_promote(path: Path, expected_old: str, commit: str) -> str:
        (root / "late-drift.txt").write_text("external\n", encoding="utf-8")
        git(root, "add", "late-drift.txt")
        git(root, "commit", "-m", "external: late primary drift")
        return original(path, expected_old, commit)

    monkeypatch.setattr(adapter, "fast_forward", drift_then_promote)
    result = MergeCoordinator(worktree_manager=manager).merge(
        merge_context(gate_context, gate)
    )

    assert result.result is MergeStatus.BLOCKED
    assert result.findings[0].code is MergeFindingCode.PROMOTION_FAILED
    assert git(root, "rev-parse", "HEAD").casefold() != baseline
    assert git(root, "status", "--porcelain=v1") == ""
    assert not adapter.is_ancestor(gate.member_commits[0].result_commit, "HEAD")


def test_no_destructive_or_forced_git_primitives_are_present() -> None:
    from agentic_engineering_os.infrastructure import git_adapter
    from agentic_engineering_os.application import merge_coordinator

    source = inspect.getsource(git_adapter) + inspect.getsource(merge_coordinator)
    forbidden = (
        "reset --hard",
        "clean -fd",
        "branch -D",
        "worktree remove --force",
        "checkout --ours",
        "checkout --theirs",
        "force push",
        "force=True",
    )
    assert all(item not in source for item in forbidden)
