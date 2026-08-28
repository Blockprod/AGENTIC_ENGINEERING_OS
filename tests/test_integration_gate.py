import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    DAGValidator,
    ExecutionConflictAnalyzer,
    ImplementerInput,
    ImplementerResult,
    ImplementerVerdict,
    IntegrationFindingCode,
    IntegrationGate,
    IntegrationGateClassification,
    IntegrationGateContext,
    ParallelCoordinationInput,
    ParallelGroupResult,
    ParallelGroupStatus,
    ParallelImplementerCoordinator,
    ParallelMemberResult,
    ReadinessEngine,
    VerificationOutcome,
    VerificationResult,
    WavePlanner,
)
from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    HumanApproval,
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    ProjectState,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
    to_dict,
)
from agentic_engineering_os.infrastructure import WorktreeManager


NOW = datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc)
COMMAND = "python -m pytest"


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
    git(root, "config", "user.name", "P3.9 Test Operator")
    git(root, "config", "user.email", "p3.9@example.invalid")
    (root / ".gitignore").write_text(".agentic-engineering-os/\n", encoding="utf-8")
    source = root / "src"
    source.mkdir()
    (source / "shared.py").write_text(
        "line_one = 'base'\nline_two = 'base'\n", encoding="utf-8"
    )
    (source / "delete.py").write_text("delete_me = True\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "test: baseline")
    return root, worktrees, git(root, "rev-parse", "HEAD").casefold()


def story(identifier: str, allowed: tuple[str, ...]) -> UserStory:
    return UserStory(
        schema_version="1.0",
        id=identifier,
        title=f"Story {identifier}",
        description=f"Produce an isolated result for {identifier}.",
        status=UserStoryStatus.PLANNED,
        priority=1,
        risk=RiskLevel.LOW,
        depends_on=(),
        scope=UserStoryScope(allowed_paths=allowed, forbidden_paths=()),
        acceptance_criteria=(AcceptanceCriterion("AC-001", "Result is observable.", True),),
        required_gates=(),
        human_approval=HumanApproval(False, False, None, None),
        metadata=UserStoryMetadata(NOW, "human-operator", NOW),
    )


def mission(
    baseline: str,
    *,
    generation: int = 0,
    role: MissionRole = MissionRole.ORCHESTRATOR,
) -> MissionState:
    return MissionState(
        schema_version="1.0",
        mission_id="mission-integration",
        workflow_generation=generation,
        status=MissionStatus.ACTIVE,
        role=role,
        objective="Evaluate parallel integration eligibility.",
        subject="mission-integration",
        operating_step=OperatingStep.ACT,
        next_action="Evaluate the completed group.",
        observed_commit=baseline,
        updated_at=NOW,
        blockers=[],
    )


def coordination(baseline: str, stories: tuple[UserStory, ...]) -> ParallelCoordinationInput:
    state = ProjectState(schema_version="1.0", user_stories=list(stories))
    dag = DAGValidator().build(state)
    readiness = ReadinessEngine().evaluate(dag, state)
    waves = WavePlanner().plan(dag, readiness, state)
    conflicts = ExecutionConflictAnalyzer().analyze(waves, state)
    return ParallelCoordinationInput(
        mission_id="mission-integration",
        workflow_generation=0,
        wave_index=0,
        wave_plan=waves,
        conflict_analysis=conflicts,
        project_state=state,
        mission_state=mission(baseline),
        baseline_commit=baseline,
    )


def implementer_input(prepared_context, assigned: UserStory) -> ImplementerInput:
    return ImplementerInput.from_handoff(
        prepared_context.handoff,
        replace(assigned, status=UserStoryStatus.IN_PROGRESS),
    )


def implementer_result(prepared_context, declared: tuple[str, ...]) -> ImplementerResult:
    return ImplementerResult(
        mission_id=prepared_context.handoff.mission_id,
        workflow_generation=prepared_context.workflow_generation,
        subject=prepared_context.user_story_id,
        user_story_id=prepared_context.user_story_id,
        observed_commit=prepared_context.baseline_commit,
        summary="Produced the isolated result.",
        files_changed=declared,
        tests_added_or_modified=(),
        verification_commands=(COMMAND,),
        verification_results=(
            VerificationResult(
                COMMAND,
                True,
                VerificationOutcome.PASS,
                0,
                "Observed pass",
            ),
        ),
        assumptions=(),
        findings=(),
        blockers=(),
        recommended_next_role=MissionRole.TESTER,
        verdict=ImplementerVerdict.READY_FOR_TEST,
    )


def apply_changes(
    worktree: Path,
    changes: dict[str, str | bytes | None],
    story_id: str,
) -> str:
    for relative, content in changes.items():
        target = worktree / relative
        if content is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-m", f"feat: result for {story_id}")
    return git(worktree, "rev-parse", "HEAD").casefold()


def completed_group(
    tmp_path: Path,
    *,
    stories: tuple[UserStory, ...],
    changes: dict[str, dict[str, str | bytes | None]],
    declared: dict[str, tuple[str, ...]],
) -> tuple[
    Path,
    Path,
    str,
    WorktreeManager,
    IntegrationGateContext,
]:
    root, worktrees, baseline = repository(tmp_path)
    manager = WorktreeManager(repository_root=root, worktree_root=worktrees)
    manager.initialize_registry()
    coordinator = ParallelImplementerCoordinator(worktree_manager=manager)
    coordination_input = coordination(baseline, stories)
    plan = coordinator.plan(coordination_input)
    prepared = coordinator.prepare_group(plan, 0, coordination_input=coordination_input)
    story_by_id = {item.id: item for item in stories}
    members: list[ParallelMemberResult] = []
    for prepared_context in prepared.contexts:
        story_id = prepared_context.user_story_id
        apply_changes(Path(prepared_context.worktree_path), changes[story_id], story_id)
        input_value = implementer_input(prepared_context, story_by_id[story_id])
        result = implementer_result(prepared_context, declared[story_id])
        completed = manager.complete(
            prepared_context.assignment_id,
            current_generation=prepared.workflow_generation,
        )
        assert completed.result_commit is not None
        members.append(
            ParallelMemberResult(
                assignment_id=completed.assignment_id,
                user_story_id=story_id,
                result_commit=completed.result_commit,
                implementer_input=input_value,
                implementer_result=result,
            )
        )
    member_tuple = tuple(members)
    group = ParallelGroupResult(
        group_index=0,
        status=ParallelGroupStatus.COMPLETED,
        member_results=member_tuple,
        assignment_ids=tuple(item.assignment_id for item in member_tuple),
        result_commits=tuple(item.result_commit for item in member_tuple),
    )
    context = IntegrationGateContext(
        coordination_input=coordination_input,
        parallel_plan=plan,
        group_result=group,
        current_mission_state=mission(baseline, role=MissionRole.IMPLEMENTER),
    )
    return root, worktrees, baseline, manager, context


def finding_codes(result) -> set[IntegrationFindingCode]:
    return {item.code for item in result.findings}


def primary_snapshot(root: Path) -> tuple[str, str, str, str, str]:
    return (
        git(root, "branch", "--show-current"),
        git(root, "rev-parse", "HEAD").casefold(),
        git(root, "status", "--porcelain=v1"),
        git(root, "count-objects", "-v"),
        git(root, "for-each-ref", "--format=%(refname):%(objectname)", "refs/heads"),
    )


def test_real_git_disjoint_group_passes_without_primary_or_object_mutation(tmp_path: Path) -> None:
    stories = (
        story("US-0001", ("src/a.py",)),
        story("US-0002", ("src/b.py",)),
        story("US-0003", ("src/c.py",)),
    )
    root, _, baseline, manager, context = completed_group(
        tmp_path,
        stories=stories,
        changes={
            "US-0001": {"src/a.py": "a = 1\n"},
            "US-0002": {"src/b.py": "b = 2\n"},
            "US-0003": {"src/c.py": "c = 3\n"},
        },
        declared={
            "US-0001": ("src/a.py",),
            "US-0002": ("src/b.py",),
            "US-0003": ("src/c.py",),
        },
    )
    before = primary_snapshot(root)
    domain_before = to_dict(context)

    result = IntegrationGate(worktree_manager=manager).evaluate(context)

    assert result.result is IntegrationGateClassification.PASS
    assert result.findings == ()
    assert result.integration_order == ("US-0001", "US-0002", "US-0003")
    assert tuple(item.changed_files for item in result.member_commits) == (
        ("src/a.py",),
        ("src/b.py",),
        ("src/c.py",),
    )
    assert result.baseline_commit == baseline
    assert primary_snapshot(root) == before
    assert to_dict(context) == domain_before


@pytest.mark.parametrize(
    ("allowed", "changes", "declared", "expected_files"),
    [
        (
            ("assets/result.bin",),
            {"assets/result.bin": b"\x00\x01\xff\x00"},
            ("assets/result.bin",),
            ("assets/result.bin",),
        ),
        (
            ("src/shared.py", "src/renamed.py"),
            {
                "src/shared.py": None,
                "src/renamed.py": "line_one = 'base'\nline_two = 'base'\n",
            },
            ("src/shared.py", "src/renamed.py"),
            ("src/renamed.py", "src/shared.py"),
        ),
    ],
)
def test_binary_and_conservative_rename_in_scope_are_supported(
    tmp_path: Path,
    allowed: tuple[str, ...],
    changes: dict[str, str | bytes | None],
    declared: tuple[str, ...],
    expected_files: tuple[str, ...],
) -> None:
    stories = (story("US-0001", allowed),)
    _, _, _, manager, context = completed_group(
        tmp_path,
        stories=stories,
        changes={"US-0001": changes},
        declared={"US-0001": declared},
    )

    result = IntegrationGate(worktree_manager=manager).evaluate(context)

    assert result.result is IntegrationGateClassification.PASS
    assert result.member_commits[0].changed_files == expected_files


def test_same_file_collision_and_real_merge_conflict_fail(tmp_path: Path) -> None:
    stories = (
        story("US-0001", ("src/a.py",)),
        story("US-0002", ("src/b.py",)),
    )
    root, _, _, manager, context = completed_group(
        tmp_path,
        stories=stories,
        changes={
            "US-0001": {"src/shared.py": "line_one = 'A'\nline_two = 'base'\n"},
            "US-0002": {"src/shared.py": "line_one = 'B'\nline_two = 'base'\n"},
        },
        declared={"US-0001": ("src/a.py",), "US-0002": ("src/b.py",)},
    )
    before = primary_snapshot(root)

    result = IntegrationGate(worktree_manager=manager).evaluate(context)

    assert result.result is IntegrationGateClassification.FAIL
    assert IntegrationFindingCode.CROSS_BRANCH_PATH_COLLISION in finding_codes(result)
    assert IntegrationFindingCode.GIT_MERGE_CONFLICT in finding_codes(result)
    assert IntegrationFindingCode.SCOPE_VIOLATION in finding_codes(result)
    assert primary_snapshot(root) == before


@pytest.mark.parametrize(
    "changes",
    [
        {
            "US-0001": {"src/delete.py": None},
            "US-0002": {"src/delete.py": "delete_me = False\n"},
        },
        {
            "US-0001": {"src/new.py": "value = 'A'\n"},
            "US-0002": {"src/new.py": "value = 'B'\n"},
        },
    ],
)
def test_add_delete_and_add_add_same_path_are_conservative_failures(
    tmp_path: Path,
    changes: dict[str, dict[str, str | bytes | None]],
) -> None:
    stories = (
        story("US-0001", ("src/a.py",)),
        story("US-0002", ("src/b.py",)),
    )
    _, _, _, manager, context = completed_group(
        tmp_path,
        stories=stories,
        changes=changes,
        declared={"US-0001": ("src/a.py",), "US-0002": ("src/b.py",)},
    )

    result = IntegrationGate(worktree_manager=manager).evaluate(context)

    assert result.result is IntegrationGateClassification.FAIL
    assert IntegrationFindingCode.CROSS_BRANCH_PATH_COLLISION in finding_codes(result)


def test_declared_diff_mismatch_and_actual_scope_violation_fail(tmp_path: Path) -> None:
    stories = (story("US-0001", ("src/allowed.py",)),)
    _, _, _, manager, context = completed_group(
        tmp_path,
        stories=stories,
        changes={"US-0001": {"src/outside.py": "outside = True\n"}},
        declared={"US-0001": ("src/allowed.py",)},
    )

    result = IntegrationGate(worktree_manager=manager).evaluate(context)

    assert result.result is IntegrationGateClassification.FAIL
    assert IntegrationFindingCode.DECLARED_DIFF_MISMATCH in finding_codes(result)
    assert IntegrationFindingCode.SCOPE_VIOLATION in finding_codes(result)


def test_branch_tip_drift_after_completion_is_non_pass(tmp_path: Path) -> None:
    stories = (story("US-0001", ("src/a.py", "src/drift.py")),)
    root, _, _, manager, context = completed_group(
        tmp_path,
        stories=stories,
        changes={"US-0001": {"src/a.py": "a = 1\n"}},
        declared={"US-0001": ("src/a.py",)},
    )
    assignment = manager.registry_store.load().assignments[0]
    apply_changes(Path(assignment.worktree_path), {"src/drift.py": "drift = True\n"}, "drift")
    before = primary_snapshot(root)

    result = IntegrationGate(worktree_manager=manager).evaluate(context)

    assert result.result is IntegrationGateClassification.FAIL
    assert IntegrationFindingCode.ASSIGNMENT_MISMATCH in finding_codes(result)
    assert primary_snapshot(root) == before


def test_stale_generation_and_stale_primary_baseline_cannot_pass(tmp_path: Path) -> None:
    stories = (story("US-0001", ("src/a.py",)),)
    root, _, baseline, manager, context = completed_group(
        tmp_path,
        stories=stories,
        changes={"US-0001": {"src/a.py": "a = 1\n"}},
        declared={"US-0001": ("src/a.py",)},
    )
    stale_generation = replace(
        context,
        current_mission_state=mission(
            baseline,
            generation=1,
            role=MissionRole.IMPLEMENTER,
        ),
    )
    generation_result = IntegrationGate(worktree_manager=manager).evaluate(stale_generation)
    assert generation_result.result is IntegrationGateClassification.FAIL
    assert IntegrationFindingCode.GENERATION_MISMATCH in finding_codes(generation_result)

    (root / "primary.txt").write_text("advance\n", encoding="utf-8")
    git(root, "add", "primary.txt")
    git(root, "commit", "-m", "test: advance primary")
    primary_result = IntegrationGate(worktree_manager=manager).evaluate(context)
    assert primary_result.result is IntegrationGateClassification.FAIL
    assert IntegrationFindingCode.BASELINE_MISMATCH in finding_codes(primary_result)


def test_incomplete_group_and_registry_git_mismatch_cannot_pass(tmp_path: Path) -> None:
    stories = (story("US-0001", ("src/a.py",)),)
    root, _, _, manager, context = completed_group(
        tmp_path,
        stories=stories,
        changes={"US-0001": {"src/a.py": "a = 1\n"}},
        declared={"US-0001": ("src/a.py",)},
    )
    for status in (
        ParallelGroupStatus.PREPARED,
        ParallelGroupStatus.BLOCKED,
        ParallelGroupStatus.FAILED,
    ):
        incomplete = replace(
            context,
            group_result=replace(context.group_result, status=status),
        )
        incomplete_result = IntegrationGate(worktree_manager=manager).evaluate(incomplete)
        assert incomplete_result.result is IntegrationGateClassification.FAIL
        assert IntegrationFindingCode.INCOMPLETE_MEMBER in finding_codes(incomplete_result)

    assignment = manager.registry_store.load().assignments[0]
    git(root, "worktree", "remove", assignment.worktree_path)
    mismatch_result = IntegrationGate(worktree_manager=manager).evaluate(context)
    assert mismatch_result.result is not IntegrationGateClassification.PASS
    assert IntegrationFindingCode.GIT_STATE_UNKNOWN in finding_codes(mismatch_result)


@pytest.mark.parametrize(
    "replacement_stories",
    [
        (
            story("US-0001", ("src/shared.py",)),
            story("US-0002", ("src/shared.py",)),
        ),
        (
            story("US-0001", ()),
            story("US-0002", ("src/b.py",)),
        ),
    ],
)
def test_conflict_or_unknown_pair_cannot_become_integration_admissible(
    tmp_path: Path,
    replacement_stories: tuple[UserStory, ...],
) -> None:
    original = (
        story("US-0001", ("src/a.py",)),
        story("US-0002", ("src/b.py",)),
    )
    _, _, baseline, manager, context = completed_group(
        tmp_path,
        stories=original,
        changes={
            "US-0001": {"src/a.py": "a = 1\n"},
            "US-0002": {"src/b.py": "b = 2\n"},
        },
        declared={"US-0001": ("src/a.py",), "US-0002": ("src/b.py",)},
    )
    changed_coordination = replace(
        coordination(baseline, replacement_stories),
        conflict_analysis=context.coordination_input.conflict_analysis,
    )
    changed_context = replace(context, coordination_input=changed_coordination)

    result = IntegrationGate(worktree_manager=manager).evaluate(changed_context)

    assert result.result is IntegrationGateClassification.FAIL
    assert IntegrationFindingCode.CONFLICT_ANALYSIS_MISMATCH in finding_codes(result)


def test_restart_and_repeated_evaluation_are_deterministic(tmp_path: Path) -> None:
    stories = (
        story("US-0001", ("src/a.py",)),
        story("US-0002", ("src/b.py",)),
    )
    root, worktrees, _, manager, context = completed_group(
        tmp_path,
        stories=stories,
        changes={
            "US-0001": {"src/a.py": "a = 1\n"},
            "US-0002": {"src/b.py": "b = 2\n"},
        },
        declared={"US-0001": ("src/a.py",), "US-0002": ("src/b.py",)},
    )
    first = IntegrationGate(worktree_manager=manager).evaluate(context)
    restarted_manager = WorktreeManager(repository_root=root, worktree_root=worktrees)
    restarted_gate = IntegrationGate(worktree_manager=restarted_manager)

    assert restarted_gate.evaluate(context) == first
    assert restarted_gate.evaluate(context) == first


class MergeUnavailableManager:
    def __init__(self, delegate: WorktreeManager) -> None:
        self.delegate = delegate

    @property
    def registry_store(self):
        return self.delegate.registry_store

    def inspect_primary(self):
        return self.delegate.inspect_primary()

    def inspect(self, assignment_id: str, *, current_generation: int):
        return self.delegate.inspect(assignment_id, current_generation=current_generation)

    def inspect_all(self, *, current_generation: int):
        return self.delegate.inspect_all(current_generation=current_generation)

    def diff_name_status(self, baseline_commit: str, result_commit: str):
        return self.delegate.diff_name_status(baseline_commit, result_commit)

    def merge_preflight(self, baseline_commit: str, left_commit: str, right_commit: str):
        raise RuntimeError("merge-tree unavailable")


def test_unavailable_merge_preflight_is_unknown_never_pass(tmp_path: Path) -> None:
    stories = (
        story("US-0001", ("src/a.py",)),
        story("US-0002", ("src/b.py",)),
    )
    _, _, _, manager, context = completed_group(
        tmp_path,
        stories=stories,
        changes={
            "US-0001": {"src/a.py": "a = 1\n"},
            "US-0002": {"src/b.py": "b = 2\n"},
        },
        declared={"US-0001": ("src/a.py",), "US-0002": ("src/b.py",)},
    )

    result = IntegrationGate(
        worktree_manager=MergeUnavailableManager(manager)
    ).evaluate(context)

    assert result.result is IntegrationGateClassification.UNKNOWN
    assert IntegrationFindingCode.GIT_STATE_UNKNOWN in finding_codes(result)
