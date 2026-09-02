import subprocess
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    DAGValidator,
    ExecutionConflictAnalyzer,
    ImplementerInput,
    ImplementerResult,
    ImplementerVerdict,
    ParallelCoordinationError,
    ParallelCoordinationInput,
    ParallelGroupStatus,
    ParallelImplementerCoordinator,
    ReadinessEngine,
    VerificationOutcome,
    VerificationResult,
    WavePlanner,
)
from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    ConflictAnalysis,
    ConflictClassification,
    ConflictReason,
    ExecutionConflict,
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
    WorktreeStatus,
)
from agentic_engineering_os.infrastructure import WorktreeManager, WorktreeManagerError
from tests._validated_execution_ledger import write_validated_implementer_execution


NOW = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)


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


def repository(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "repository"
    worktrees = tmp_path / "worktrees"
    root.mkdir()
    worktrees.mkdir()
    git(root, "init", "-b", "trunk")
    git(root, "config", "user.name", "P3.8 Test Operator")
    git(root, "config", "user.email", "p3.8@example.invalid")
    (root / "README.md").write_text("baseline\n", encoding="utf-8")
    (root / ".gitignore").write_text(".agentic-engineering-os/\n", encoding="utf-8")
    git(root, "add", "README.md", ".gitignore")
    git(root, "commit", "-m", "test: baseline")
    return root, worktrees, git(root, "rev-parse", "HEAD").casefold()


def story(
    identifier: str,
    *,
    allowed: tuple[str, ...] | None = None,
    priority: int = 1,
    status: UserStoryStatus = UserStoryStatus.PLANNED,
) -> UserStory:
    return UserStory(
        schema_version="1.0",
        id=identifier,
        title=f"Story {identifier}",
        description=f"Implement isolated behavior for {identifier}.",
        status=status,
        priority=priority,
        risk=RiskLevel.LOW,
        depends_on=(),
        scope=UserStoryScope(
            allowed_paths=allowed if allowed is not None else (f"changes/{identifier.casefold()}/",),
            forbidden_paths=(),
        ),
        acceptance_criteria=(AcceptanceCriterion("AC-001", "Result is observable.", True),),
        required_gates=(),
        human_approval=HumanApproval(False, False, None, None),
        metadata=UserStoryMetadata(NOW, "human-operator", NOW),
    )


def mission(baseline: str, *, generation: int = 0, role: MissionRole = MissionRole.ORCHESTRATOR) -> MissionState:
    return MissionState(
        schema_version="1.0",
        mission_id="mission-parallel",
        workflow_generation=generation,
        status=MissionStatus.ACTIVE,
        role=role,
        objective="Coordinate isolated Implementers.",
        subject="mission-parallel",
        operating_step=OperatingStep.ACT,
        next_action="Prepare the current Wave.",
        observed_commit=baseline,
        updated_at=NOW,
        blockers=[],
    )


def coordination_input(
    baseline: str,
    *stories: UserStory,
    generation: int = 0,
    conflict_override: ConflictAnalysis | None = None,
) -> ParallelCoordinationInput:
    state = ProjectState(schema_version="1.0", user_stories=list(stories))
    dag = DAGValidator().build(state)
    readiness = ReadinessEngine().evaluate(dag, state)
    waves = WavePlanner().plan(dag, readiness, state)
    conflicts = ExecutionConflictAnalyzer().analyze(waves, state)
    return ParallelCoordinationInput(
        mission_id="mission-parallel",
        workflow_generation=generation,
        wave_index=0,
        wave_plan=waves,
        conflict_analysis=conflict_override or conflicts,
        project_state=state,
        mission_state=mission(baseline, generation=generation),
        baseline_commit=baseline,
    )


def coordinator(root: Path, worktrees: Path) -> tuple[ParallelImplementerCoordinator, WorktreeManager]:
    manager = WorktreeManager(repository_root=root, worktree_root=worktrees)
    manager.initialize_registry()
    return ParallelImplementerCoordinator(worktree_manager=manager), manager


def commit_result(path: Path, identifier: str) -> str:
    target = path / "changes" / identifier.casefold() / "result.txt"
    target.parent.mkdir(parents=True)
    target.write_text("isolated result\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", f"feat: {identifier} result")
    return git(path, "rev-parse", "HEAD").casefold()


def dirty_result(path: Path, identifier: str) -> str:
    relative = f"changes/{identifier.casefold()}/result.txt"
    target = path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("isolated result\n", encoding="utf-8")
    return relative


def implementer_input(context, assigned_story: UserStory) -> ImplementerInput:
    in_progress = replace(assigned_story, status=UserStoryStatus.IN_PROGRESS)
    return ImplementerInput.from_handoff(context.handoff, in_progress)


def implementer_result(context, changed: str) -> ImplementerResult:
    command = "python -m pytest"
    return ImplementerResult(
        mission_id=context.handoff.mission_id,
        workflow_generation=context.workflow_generation,
        subject=context.user_story_id,
        user_story_id=context.user_story_id,
        observed_commit=context.baseline_commit,
        summary="Implemented the isolated assignment.",
        files_changed=(changed,),
        tests_added_or_modified=(),
        verification_commands=(command,),
        verification_results=(
            VerificationResult(command, True, VerificationOutcome.PASS, 0, "Observed pass"),
        ),
        assumptions=(),
        findings=(),
        blockers=(),
        recommended_next_role=MissionRole.TESTER,
        verdict=ImplementerVerdict.READY_FOR_TEST,
    )


def test_empty_wave_and_one_member_are_deterministic(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    service, _ = coordinator(root, worktrees)

    empty = service.plan(coordination_input(baseline))
    one = service.plan(coordination_input(baseline, story("US-0001")))

    assert empty.groups == ()
    assert one.groups[0].user_story_ids == ("US-0001",)
    assert service.plan(coordination_input(baseline, story("US-0001"))) == one
    with pytest.raises(FrozenInstanceError):
        one.wave_index = 1  # type: ignore[misc]


def test_all_safe_stories_form_one_ordered_group(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    service, _ = coordinator(root, worktrees)
    value = coordination_input(
        baseline,
        story("US-0003", priority=2),
        story("US-0002", priority=1),
        story("US-0001", priority=1),
    )

    result = service.plan(value)

    assert tuple(group.group_index for group in result.groups) == (0,)
    assert result.groups[0].user_story_ids == ("US-0001", "US-0002", "US-0003")


def test_conflict_and_unknown_split_with_first_fit_greedy(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    service, _ = coordinator(root, worktrees)
    conflict = coordination_input(
        baseline,
        story("US-0001", allowed=("src/shared.py",)),
        story("US-0002", allowed=("src/shared.py",)),
        story("US-0003", allowed=("src/other.py",)),
    )
    unknown = coordination_input(
        baseline,
        story("US-0001", allowed=()),
        story("US-0002", allowed=("src/other.py",)),
    )

    assert tuple(group.user_story_ids for group in service.plan(conflict).groups) == (
        ("US-0001", "US-0003"),
        ("US-0002",),
    )
    assert tuple(group.user_story_ids for group in service.plan(unknown).groups) == (
        ("US-0001",),
        ("US-0002",),
    )


def test_forged_wave_conflicts_generation_and_baseline_are_rejected(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    service, _ = coordinator(root, worktrees)
    value = coordination_input(baseline, story("US-0001"), story("US-0002"))
    pair = value.conflict_analysis.pairs[0]
    forged_conflict = replace(
        value,
        conflict_analysis=ConflictAnalysis(
            (replace(pair, classification=ConflictClassification.CONFLICT, reasons=(ConflictReason.PATH_OVERLAP,)),)
        ),
    )
    candidates = (
        replace(value, wave_plan=replace(value.wave_plan, waves=())),
        forged_conflict,
        replace(value, workflow_generation=1),
        replace(value, baseline_commit="0" * 40),
    )

    for candidate in candidates:
        with pytest.raises(ParallelCoordinationError):
            service.plan(candidate)


def test_forged_duplicate_omitted_and_unsafe_groups_do_not_mutate_registry(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    service, manager = coordinator(root, worktrees)
    value = coordination_input(
        baseline,
        story("US-0001", allowed=("src/shared.py",)),
        story("US-0002", allowed=("src/shared.py",)),
    )
    plan = service.plan(value)
    combined = replace(
        plan.groups[0],
        user_story_ids=("US-0001", "US-0002"),
    )
    candidates = (
        replace(plan, groups=(combined,)),
        replace(plan, groups=(replace(plan.groups[0], user_story_ids=("US-0001", "US-0001")),)),
        replace(plan, groups=(plan.groups[0],)),
    )
    for candidate in candidates:
        with pytest.raises(ParallelCoordinationError, match="PLAN_STALE"):
            service.prepare_group(candidate, 0, coordination_input=value)
        assert manager.registry_store.load().assignments == ()


def test_real_git_three_safe_assignments_are_distinct_and_primary_unchanged(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    service, manager = coordinator(root, worktrees)
    stories = (story("US-0001"), story("US-0002"), story("US-0003"))
    value = coordination_input(baseline, *stories)

    prepared = service.prepare_group(service.plan(value), 0, coordination_input=value)

    assert len(set(prepared.assignment_ids)) == 3
    assert len(set(prepared.worktree_paths)) == 3
    assert len(set(prepared.branch_names)) == 3
    assert all(Path(path).is_dir() for path in prepared.worktree_paths)
    assert all(git(Path(path), "rev-parse", "HEAD").casefold() == baseline for path in prepared.worktree_paths)
    assert all(item.status is WorktreeStatus.ACTIVE for item in manager.registry_store.load().assignments)
    assert git(root, "rev-parse", "HEAD").casefold() == baseline
    primary_status = git(root, "status", "--porcelain=v1")
    assert primary_status in {"", "?? .agentic-engineering-os/"}


def test_prepare_is_restart_safe_for_active_assignments(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    service, _ = coordinator(root, worktrees)
    value = coordination_input(baseline, story("US-0001"), story("US-0002"))
    plan = service.plan(value)
    first = service.prepare_group(plan, 0, coordination_input=value)

    restarted = ParallelImplementerCoordinator(
        worktree_manager=WorktreeManager(repository_root=root, worktree_root=worktrees)
    )
    resumed = restarted.prepare_group(
        restarted.plan(value), 0, coordination_input=value
    )

    assert resumed.assignment_ids == first.assignment_ids
    assert resumed.worktree_paths == first.worktree_paths


def test_stale_plan_and_advanced_primary_are_rejected_without_assignment(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    service, manager = coordinator(root, worktrees)
    value = coordination_input(baseline, story("US-0001"))
    plan = service.plan(value)
    stale_state = ProjectState(schema_version="1.0", user_stories=[story("US-0001", priority=2)])
    stale = coordination_input(baseline, *stale_state.user_stories)
    with pytest.raises(ParallelCoordinationError, match="PLAN_STALE"):
        service.prepare_group(plan, 0, coordination_input=stale)
    assert manager.registry_store.load().assignments == ()

    (root / "advance.txt").write_text("advance\n", encoding="utf-8")
    git(root, "add", "advance.txt")
    git(root, "commit", "-m", "test: advance primary")
    with pytest.raises(ParallelCoordinationError, match="PLAN_STALE"):
        service.prepare_group(plan, 0, coordination_input=value)
    assert manager.registry_store.load().assignments == ()


def test_result_submission_observes_commit_and_completes_group(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    service, manager = coordinator(root, worktrees)
    assigned = story("US-0001")
    value = coordination_input(baseline, assigned)
    prepared = service.prepare_group(service.plan(value), 0, coordination_input=value)
    context = prepared.contexts[0]
    input_value = implementer_input(context, assigned)
    changed = dirty_result(Path(context.worktree_path), context.user_story_id)
    result = implementer_result(context, changed)
    execution_id = write_validated_implementer_execution(context, result, observed_at=NOW)

    member = service.submit_result(
        prepared,
        context.assignment_id,
        result,
        execution_id=execution_id,
        implementer_input=input_value,
        current_mission=mission(baseline, role=MissionRole.IMPLEMENTER),
    )
    restarted = ParallelImplementerCoordinator(
        worktree_manager=WorktreeManager(repository_root=root, worktree_root=worktrees)
    )
    completed = restarted.complete_group(prepared, (member,))

    assert member.result_commit == git(Path(context.worktree_path), "rev-parse", "HEAD").casefold()
    assert member.result_commit != baseline
    assert git(Path(context.worktree_path), "status", "--porcelain=v1") == ""
    assert completed.status is ParallelGroupStatus.COMPLETED
    assert manager.registry_store.load().assignments[0].status is WorktreeStatus.COMPLETED


def test_submission_rejects_extra_dirty_path_forged_result_and_wrong_execution(
    tmp_path: Path,
) -> None:
    root, worktrees, baseline = repository(tmp_path)
    service, manager = coordinator(root, worktrees)
    assigned = story("US-0001")
    value = coordination_input(baseline, assigned)
    prepared = service.prepare_group(service.plan(value), 0, coordination_input=value)
    context = prepared.contexts[0]
    changed = dirty_result(Path(context.worktree_path), context.user_story_id)
    result = implementer_result(context, changed)
    execution_id = write_validated_implementer_execution(context, result, observed_at=NOW)
    submission = {
        "execution_id": execution_id,
        "implementer_input": implementer_input(context, assigned),
        "current_mission": mission(baseline, role=MissionRole.IMPLEMENTER),
    }

    with pytest.raises(ParallelCoordinationError, match="EXECUTION_BINDING_MISMATCH"):
        service.submit_result(
            prepared,
            context.assignment_id,
            replace(result, summary="forged summary"),
            **submission,
        )
    with pytest.raises(ParallelCoordinationError, match="EXECUTION_BINDING_MISMATCH"):
        service.submit_result(
            prepared,
            context.assignment_id,
            result,
            **{**submission, "execution_id": "cx-" + "9" * 24},
        )

    extra = Path(context.worktree_path) / "changes" / "us-0001" / "extra.txt"
    extra.write_text("extra\n", encoding="utf-8")
    with pytest.raises(ParallelCoordinationError, match="GROUP_INCOMPLETE"):
        service.submit_result(
            prepared,
            context.assignment_id,
            result,
            **submission,
        )
    assert manager.registry_store.load().assignments[0].status is WorktreeStatus.ACTIVE
    assert git(Path(context.worktree_path), "rev-parse", "HEAD").casefold() == baseline


def test_registry_persistence_failure_after_commit_resumes_exactly_once(
    tmp_path: Path,
) -> None:
    root, worktrees, baseline = repository(tmp_path)
    service, manager = coordinator(root, worktrees)
    assigned = story("US-0001")
    value = coordination_input(baseline, assigned)
    prepared = service.prepare_group(service.plan(value), 0, coordination_input=value)
    context = prepared.contexts[0]
    changed = dirty_result(Path(context.worktree_path), context.user_story_id)
    result = implementer_result(context, changed)
    execution_id = write_validated_implementer_execution(context, result, observed_at=NOW)
    original_persist = manager._persist

    def fail_complete(before, candidate, operation):
        if operation == "COMPLETE":
            raise WorktreeManagerError("INJECTED_PERSISTENCE_FAILURE", "registry unavailable")
        return original_persist(before, candidate, operation)

    manager._persist = fail_complete
    with pytest.raises(ParallelCoordinationError, match="GROUP_INCOMPLETE"):
        service.submit_result(
            prepared,
            context.assignment_id,
            result,
            execution_id=execution_id,
            implementer_input=implementer_input(context, assigned),
            current_mission=mission(baseline, role=MissionRole.IMPLEMENTER),
        )
    manager._persist = original_persist
    committed = git(Path(context.worktree_path), "rev-parse", "HEAD").casefold()
    assert committed != baseline
    assert manager.registry_store.load().assignments[0].status is WorktreeStatus.ACTIVE

    resumed = service.submit_result(
        prepared,
        context.assignment_id,
        result,
        execution_id=execution_id,
        implementer_input=implementer_input(context, assigned),
        current_mission=mission(baseline, role=MissionRole.IMPLEMENTER),
    )

    assert resumed.result_commit == committed
    assert git(Path(context.worktree_path), "rev-list", "--count", f"{baseline}..HEAD") == "1"
    assert manager.registry_store.load().assignments[0].status is WorktreeStatus.COMPLETED


def test_submission_rejects_cross_story_stale_and_missing_commit(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    service, manager = coordinator(root, worktrees)
    stories = (story("US-0001"), story("US-0002"))
    value = coordination_input(baseline, *stories)
    prepared = service.prepare_group(service.plan(value), 0, coordination_input=value)
    left, right = prepared.contexts
    wrong = implementer_result(right, "changes/us-0002/result.txt")
    with pytest.raises(ParallelCoordinationError, match="ASSIGNMENT_MISMATCH"):
        service.submit_result(
            prepared,
            left.assignment_id,
            wrong,
            execution_id="cx-" + "0" * 24,
            implementer_input=implementer_input(right, stories[1]),
            current_mission=mission(baseline, role=MissionRole.IMPLEMENTER),
        )
    stale = replace(wrong, workflow_generation=1)
    with pytest.raises(ParallelCoordinationError):
        service.submit_result(
            prepared,
            right.assignment_id,
            stale,
            execution_id="cx-" + "0" * 24,
            implementer_input=implementer_input(right, stories[1]),
            current_mission=mission(baseline, role=MissionRole.IMPLEMENTER),
        )
    assert all(item.status is WorktreeStatus.ACTIVE for item in manager.registry_store.load().assignments)
    with pytest.raises(ParallelCoordinationError, match="EXECUTION_LEDGER_UNAVAILABLE"):
        service.submit_result(
            prepared,
            left.assignment_id,
            implementer_result(left, "changes/us-0001/result.txt"),
            execution_id="cx-" + "0" * 24,
            implementer_input=implementer_input(left, stories[0]),
            current_mission=mission(baseline, role=MissionRole.IMPLEMENTER),
        )


def test_blocked_result_cannot_complete_assignment(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    service, manager = coordinator(root, worktrees)
    assigned = story("US-0001")
    value = coordination_input(baseline, assigned)
    prepared = service.prepare_group(service.plan(value), 0, coordination_input=value)
    context = prepared.contexts[0]
    commit_result(Path(context.worktree_path), context.user_story_id)
    blocked = replace(
        implementer_result(context, "changes/us-0001/result.txt"),
        files_changed=(),
        verification_commands=(),
        verification_results=(),
        blockers=("Implementation blocked.",),
        recommended_next_role=MissionRole.ORCHESTRATOR,
        verdict=ImplementerVerdict.BLOCKED,
    )
    with pytest.raises(ParallelCoordinationError, match="INVALID_IMPLEMENTER_RESULT"):
        service.submit_result(
            prepared,
            context.assignment_id,
            blocked,
            execution_id="cx-" + "0" * 24,
            implementer_input=implementer_input(context, assigned),
            current_mission=mission(baseline, role=MissionRole.IMPLEMENTER),
        )
    assert manager.registry_store.load().assignments[0].status is WorktreeStatus.ACTIVE


def test_dirty_worktree_and_failed_member_block_completion(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    service, manager = coordinator(root, worktrees)
    assigned = story("US-0001")
    value = coordination_input(baseline, assigned)
    prepared = service.prepare_group(service.plan(value), 0, coordination_input=value)
    context = prepared.contexts[0]
    target = Path(context.worktree_path) / "changes" / "us-0001" / "dirty.txt"
    target.parent.mkdir(parents=True)
    target.write_text("dirty\n", encoding="utf-8")
    with pytest.raises(ParallelCoordinationError, match="EXECUTION_LEDGER_UNAVAILABLE"):
        service.submit_result(
            prepared,
            context.assignment_id,
            implementer_result(context, "changes/us-0001/dirty.txt"),
            execution_id="cx-" + "0" * 24,
            implementer_input=implementer_input(context, assigned),
            current_mission=mission(baseline, role=MissionRole.IMPLEMENTER),
        )
    failed = service.fail_member(
        prepared,
        context.assignment_id,
        current_mission=mission(baseline, role=MissionRole.IMPLEMENTER),
    )
    assert failed.status is ParallelGroupStatus.FAILED
    assert manager.registry_store.load().assignments[0].status is WorktreeStatus.FAILED


def test_registry_git_mismatch_blocks_submission_without_registry_mutation(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    service, manager = coordinator(root, worktrees)
    assigned = story("US-0001")
    value = coordination_input(baseline, assigned)
    prepared = service.prepare_group(service.plan(value), 0, coordination_input=value)
    context = prepared.contexts[0]
    git(root, "worktree", "remove", context.worktree_path)

    with pytest.raises(ParallelCoordinationError, match="ASSIGNMENT_MISMATCH"):
        service.submit_result(
            prepared,
            context.assignment_id,
            implementer_result(context, "changes/us-0001/result.txt"),
            execution_id="cx-" + "0" * 24,
            implementer_input=implementer_input(context, assigned),
            current_mission=mission(baseline, role=MissionRole.IMPLEMENTER),
        )
    assert manager.registry_store.load().assignments[0].status is WorktreeStatus.ACTIVE


def test_second_conflict_group_waits_for_first_completion(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    service, manager = coordinator(root, worktrees)
    value = coordination_input(
        baseline,
        story("US-0001", allowed=("src/shared.py",)),
        story("US-0002", allowed=("src/shared.py",)),
    )
    plan = service.plan(value)
    with pytest.raises(ParallelCoordinationError, match="GROUP_INCOMPLETE"):
        service.prepare_group(plan, 1, coordination_input=value)
    assert manager.registry_store.load().assignments == ()


class FailingSecondActivationManager:
    def __init__(self, delegate: WorktreeManager) -> None:
        self.delegate = delegate
        self.activations = 0

    @property
    def registry_store(self):
        return self.delegate.registry_store

    def current_primary_commit(self) -> str:
        return self.delegate.current_primary_commit()

    def plan_assignment(self, **kwargs):
        return self.delegate.plan_assignment(**kwargs)

    def activate(self, assignment_id: str, *, current_generation: int):
        self.activations += 1
        if self.activations == 2:
            raise RuntimeError("injected activation failure")
        return self.delegate.activate(assignment_id, current_generation=current_generation)

    def resume(self, assignment_id: str, *, current_generation: int):
        return self.delegate.resume(assignment_id, current_generation=current_generation)

    def complete(self, assignment_id: str, *, current_generation: int):
        return self.delegate.complete(assignment_id, current_generation=current_generation)

    def mark_failed(self, assignment_id: str, *, current_generation: int):
        return self.delegate.mark_failed(assignment_id, current_generation=current_generation)


class CollisionManager(FailingSecondActivationManager):
    def __init__(self, delegate: WorktreeManager, code: str) -> None:
        super().__init__(delegate)
        self.code = code

    def activate(self, assignment_id: str, *, current_generation: int):
        raise WorktreeManagerError(self.code, "injected collision")


@pytest.mark.parametrize("code", ["BRANCH_COLLISION", "PATH_COLLISION"])
def test_worktree_collision_is_propagated_without_false_preparation(tmp_path: Path, code: str) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = WorktreeManager(repository_root=root, worktree_root=worktrees)
    manager.initialize_registry()
    service = ParallelImplementerCoordinator(
        worktree_manager=CollisionManager(manager, code)
    )
    value = coordination_input(baseline, story("US-0001"))

    with pytest.raises(ParallelCoordinationError) as captured:
        service.prepare_group(service.plan(value), 0, coordination_input=value)
    assert captured.value.code == "WORKTREE_PREPARATION_FAILED"
    assert code in captured.value.message
    assert manager.registry_store.load().assignments[0].status is WorktreeStatus.PLANNED


def test_partial_preparation_is_error_and_remains_observable_after_restart(tmp_path: Path) -> None:
    root, worktrees, baseline = repository(tmp_path)
    manager = WorktreeManager(repository_root=root, worktree_root=worktrees)
    manager.initialize_registry()
    service = ParallelImplementerCoordinator(
        worktree_manager=FailingSecondActivationManager(manager)
    )
    value = coordination_input(baseline, story("US-0001"), story("US-0002"))

    with pytest.raises(ParallelCoordinationError) as captured:
        service.prepare_group(service.plan(value), 0, coordination_input=value)
    assert captured.value.code == "WORKTREE_PREPARATION_FAILED"
    assert len(captured.value.prepared_assignment_ids) == 1
    statuses = tuple(item.status for item in manager.registry_store.load().assignments)
    assert statuses.count(WorktreeStatus.ACTIVE) == 1
    assert statuses.count(WorktreeStatus.PLANNED) == 1

    restarted = ParallelImplementerCoordinator(
        worktree_manager=WorktreeManager(repository_root=root, worktree_root=worktrees)
    )
    resumed = restarted.prepare_group(restarted.plan(value), 0, coordination_input=value)
    assert len(resumed.assignment_ids) == 2
