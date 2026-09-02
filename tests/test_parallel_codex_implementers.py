from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event, Lock

import pytest

from agentic_engineering_os.application import (
    CodexCapabilityStatus,
    ExecutionExecutableIdentity,
    ExecutionStateError,
    ImplementerResult,
    ImplementerVerdict,
    ParallelCodexExecutionError,
    ParallelCodexGroupStatus,
    ParallelCodexImplementerExecutor,
    ParallelCoordinationInput,
    ParallelCoordinationError,
    ParallelImplementerCoordinator,
    PreparedParallelGroup,
    RestartSafeCodexExecutionService,
    SingleRoleCodexExecutor,
    VerificationOutcome,
    VerificationResult,
    record_parallel_probe,
)
from agentic_engineering_os.domain import (
    MissionRole,
    ParallelExecutionPlan,
    ProjectState,
    UserStoryStatus,
    to_dict,
)
from agentic_engineering_os.infrastructure import (
    CodexCapabilityDiscovery,
    CodexRuntimeAdapter,
    CodexRuntimeConfiguration,
    ExecutionGitObserver,
    ExecutionStateStore,
    PersistenceError,
    WorktreeManager,
)
from test_parallel_implementer_coordinator import (
    coordination_input,
    git,
    story,
)


FAKE = Path(__file__).parent / "fixtures" / "fake_codex.py"


class Store:
    def __init__(self, value: object) -> None:
        self.value = value

    def load(self) -> object:
        return self.value


class CountingRuntime:
    def __init__(
        self,
        runtime: CodexRuntimeAdapter,
        calls: dict[str, int],
        finished: list[str],
        key: str,
        lock: Lock,
    ) -> None:
        self._runtime = runtime
        self._calls = calls
        self._finished = finished
        self._key = key
        self._lock = lock

    def execute(self, compiled_prompt, binding, *, cancellation=None):
        with self._lock:
            self._calls[self._key] = self._calls.get(self._key, 0) + 1
        result = self._runtime.execute(
            compiled_prompt, binding, cancellation=cancellation
        )
        with self._lock:
            self._finished.append(self._key)
        return result


class ExecutorFactory:
    def __init__(
        self,
        *,
        manager: WorktreeManager,
        project_store: Store,
        artifact_root: Path,
        results: dict[str, ImplementerResult],
        modes: dict[str, str] | None = None,
        barrier_size: int | None = None,
        delays: dict[str, float] | None = None,
        timeout: float = 5.0,
        timeouts: dict[str, float] | None = None,
        persistence_failure: str | None = None,
        intake_crash: str | None = None,
    ) -> None:
        self.manager = manager
        self.project_store = project_store
        self.artifact_root = artifact_root
        self.results = results
        self.modes = modes or {}
        self.barrier_size = barrier_size
        self.delays = delays or {}
        self.timeout = timeout
        self.timeouts = timeouts or {}
        self.persistence_failure = persistence_failure
        self.intake_crash = intake_crash
        self._intake_crashed: set[str] = set()
        self.calls: dict[str, int] = {}
        self.finished: list[str] = []
        self._lock = Lock()
        self.barrier = artifact_root / "barrier"
        artifact_root.mkdir(parents=True, exist_ok=True)

    def assess_parallel_capability(self):
        executable = Path(sys.executable).resolve()
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        assessment = CodexCapabilityDiscovery().assess(
            executable=str(executable),
            expected_path=str(executable),
            expected_sha256=digest,
            expected_version="fake-codex 1.0",
            launcher_arguments=(str(FAKE), "--fake-mode", "normal"),
            environment=dict(os.environ),
            project_root=str(self.artifact_root),
            test_injection=True,
        )
        assert assessment is not None
        return record_parallel_probe(
            assessment,
            status=CodexCapabilityStatus.SUPPORTED,
            tested_concurrency=8,
            detail="offline subprocess barrier",
        )

    def create(self, context, mission_store) -> SingleRoleCodexExecutor:
        result_file = self.artifact_root / f"{context.assignment_id}.json"
        result_file.write_text(
            json.dumps(to_dict(self.results[context.user_story_id]), ensure_ascii=False),
            encoding="utf-8",
        )
        mode = self.modes.get(
            context.user_story_id,
            "role-result-parallel" if self.barrier_size is not None else "role-result-side-effect",
        )
        arguments = [
            str(FAKE),
            "--fake-mode",
            mode,
            "--fake-result-file",
            str(result_file),
        ]
        if mode == "role-result-parallel":
            arguments.extend(
                (
                    "--fake-parallel-barrier",
                    str(self.barrier),
                    "--fake-parallel-size",
                    str(self.barrier_size),
                    "--fake-delay",
                    str(self.delays.get(context.user_story_id, 0.0)),
                )
            )
        executable = Path(sys.executable).resolve()
        digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        adapter = CodexRuntimeAdapter(
            CodexRuntimeConfiguration(
                executable=str(executable),
                expected_executable_path=str(executable),
                expected_executable_version="fake-codex 1.0",
                expected_executable_sha256=digest,
                launcher_arguments=tuple(arguments),
                test_executable_injection=True,
            )
        )
        runtime = CountingRuntime(
            adapter,
            self.calls,
            self.finished,
            context.user_story_id,
            self._lock,
        )
        state = ExecutionStateStore(context.worktree_path)
        if not state.ledger_path.exists():
            state.initialize()
        if self.persistence_failure == context.user_story_id:
            state._write = lambda ledger: (_ for _ in ()).throw(  # type: ignore[method-assign]
                PersistenceError("WRITE_FAILED", "simulated member persistence failure")
            )
        service = RestartSafeCodexExecutionService(state, runtime, ExecutionGitObserver())
        with self._lock:
            crash_intake = (
                self.intake_crash == context.user_story_id
                and context.user_story_id not in self._intake_crashed
            )
            if crash_intake:
                self._intake_crashed.add(context.user_story_id)
        if crash_intake:
            service.replay_intake = lambda *args, **kwargs: (  # type: ignore[method-assign]
                (_ for _ in ()).throw(
                    ExecutionStateError("CRASH", "simulated crash before intake")
                )
            )
        return SingleRoleCodexExecutor(
            mission_store=mission_store,
            project_store=self.project_store,
            repository=self.manager,
            execution_service=service,
            executable_identity=ExecutionExecutableIdentity(
                str(executable), "fake-codex 1.0", digest
            ),
            timeout_seconds=self.timeouts.get(context.user_story_id, self.timeout),
        )


@dataclass
class Case:
    root: Path
    manager: WorktreeManager
    coordinator: ParallelImplementerCoordinator
    coordination: ParallelCoordinationInput
    plan: ParallelExecutionPlan
    prepared: PreparedParallelGroup
    mission_store: Store
    project_store: Store
    results: dict[str, ImplementerResult]
    artifact_root: Path


def _copy_contracts(root: Path) -> None:
    source = Path(__file__).parents[1]
    paths = (
        "AGENTS.md",
        "docs/02-invariants.md",
        "docs/03-fail-closed-policy.md",
        "docs/04-authority-model.md",
        "docs/12-codex-operating-contract.md",
        "docs/17-implementer.md",
        "docs/35-codex-execution-contract.md",
        "docs/PHASE-3-CERTIFICATION.md",
        "roles/implementer.md",
        "schemas/implementer-result.schema.json",
    )
    for relative in paths:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    (root / ".gitignore").write_text(
        ".agentic-engineering-os/worktrees.json\n"
        ".agentic-engineering-os/executions.json\n"
        ".agentic-engineering-os/.executions.*.tmp\n",
        encoding="utf-8",
    )


def _result(identifier: str, baseline: str) -> ImplementerResult:
    changed = f"changes/{identifier.casefold()}/result.txt"
    command = f"python -m pytest tests/{identifier.casefold()}"
    return ImplementerResult(
        mission_id="mission-parallel",
        workflow_generation=0,
        subject=identifier,
        user_story_id=identifier,
        observed_commit=baseline,
        summary=f"Implemented {identifier} in its isolated worktree.",
        files_changed=(changed,),
        tests_added_or_modified=(),
        verification_commands=(command,),
        verification_results=(
            VerificationResult(command, True, VerificationOutcome.PASS, 0, "fake pass"),
        ),
        assumptions=(),
        findings=(),
        blockers=(),
        recommended_next_role=MissionRole.TESTER,
        verdict=ImplementerVerdict.READY_FOR_TEST,
    )


def make_case(tmp_path: Path, count: int = 2) -> Case:
    root = tmp_path / "repository"
    worktrees = tmp_path / "worktrees"
    root.mkdir(parents=True)
    worktrees.mkdir(parents=True)
    _copy_contracts(root)
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "P4.9 Test Operator")
    git(root, "config", "user.email", "p4.9@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-m", "test: parallel Codex baseline")
    baseline = git(root, "rev-parse", "HEAD").casefold()
    stories = tuple(story(f"US-{index:04d}") for index in range(1, count + 1))
    coordination = coordination_input(baseline, *stories)
    manager = WorktreeManager(repository_root=root, worktree_root=worktrees)
    manager.initialize_registry()
    coordinator = ParallelImplementerCoordinator(worktree_manager=manager)
    plan = coordinator.plan(coordination)
    prepared = coordinator.prepare_group(plan, 0, coordination_input=coordination)
    current_project = ProjectState(
        "1.0",
        [replace(item, status=UserStoryStatus.IN_PROGRESS) for item in stories],
    )
    return Case(
        root,
        manager,
        coordinator,
        coordination,
        plan,
        prepared,
        Store(coordination.mission_state),
        Store(current_project),
        {item.id: _result(item.id, baseline) for item in stories},
        tmp_path / "runtime-artifacts",
    )


def group_executor(case: Case, factory: ExecutorFactory, *, limit: int = 4):
    return ParallelCodexImplementerExecutor(
        parallel_coordinator=case.coordinator,
        mission_store=case.mission_store,
        project_store=case.project_store,
        executor_factory=factory,
        max_concurrency=limit,
    )


@pytest.mark.parametrize("count", (2, 3))
def test_safe_members_run_as_real_parallel_isolated_subprocesses(
    tmp_path: Path, count: int
) -> None:
    case = make_case(tmp_path, count)
    order = tuple(reversed(case.prepared.user_story_ids))
    factory = ExecutorFactory(
        manager=case.manager,
        project_store=case.project_store,
        artifact_root=case.artifact_root,
        results=case.results,
        barrier_size=count,
        delays={identifier: index * 0.5 for index, identifier in enumerate(order)},
    )

    outcome = group_executor(case, factory).execute_group(
        case.plan,
        case.prepared,
        coordination_input=case.coordination,
        request_id_prefix=f"parallel-{count}",
    )

    assert outcome.successful
    assert outcome.status is ParallelCodexGroupStatus.READY_FOR_P3_HANDOFF
    assert outcome.max_concurrency == count
    assert tuple(item.user_story_id for item in outcome.members) == case.prepared.user_story_ids
    assert len({item.request_id for item in outcome.members}) == count
    assert len({item.execution_id for item in outcome.members}) == count
    assert tuple(factory.finished) == order
    markers = tuple(factory.barrier.glob("*.started"))
    assert len(markers) == count
    assert len({item.read_text(encoding="utf-8").casefold() for item in markers}) == count
    for member, context in zip(outcome.members, case.prepared.contexts, strict=True):
        assert member.assignment_id == context.assignment_id
        assert member.implementer_result == case.results[context.user_story_id]
        ledger = ExecutionStateStore(context.worktree_path).load()
        assert len(ledger.records) == 1
        assert ledger.records[0].request_id == member.request_id
        assert ledger.records[0].validated_result_json is not None
        assert context.user_story_id in ledger.records[0].validated_result_json
        assert all(
            other not in ledger.records[0].validated_result_json
            for other in case.prepared.user_story_ids
            if other != context.user_story_id
        )


def test_concurrency_limit_never_exceeds_group_size(tmp_path: Path) -> None:
    case = make_case(tmp_path, 2)
    factory = ExecutorFactory(
        manager=case.manager,
        project_store=case.project_store,
        artifact_root=case.artifact_root,
        results=case.results,
    )
    outcome = group_executor(case, factory, limit=8).execute_group(
        case.plan,
        case.prepared,
        coordination_input=case.coordination,
        request_id_prefix="bounded",
    )
    assert outcome.successful and outcome.max_concurrency == 2
    with pytest.raises(ValueError):
        group_executor(case, factory, limit=9)


def test_restart_revalidates_completed_members_without_subprocess_replay(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    factory = ExecutorFactory(
        manager=case.manager,
        project_store=case.project_store,
        artifact_root=case.artifact_root,
        results=case.results,
        barrier_size=2,
    )
    executor = group_executor(case, factory)
    first = executor.execute_group(
        case.plan,
        case.prepared,
        coordination_input=case.coordination,
        request_id_prefix="restart",
    )
    second = executor.execute_group(
        case.plan,
        case.prepared,
        coordination_input=case.coordination,
        request_id_prefix="restart",
    )
    assert first.successful and second.successful
    assert all(item.execution_outcome.completed_reused for item in second.members)
    assert factory.calls == {identifier: 1 for identifier in case.prepared.user_story_ids}


def test_restart_replays_only_intake_for_observed_member(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    interrupted = case.prepared.user_story_ids[1]
    factory = ExecutorFactory(
        manager=case.manager,
        project_store=case.project_store,
        artifact_root=case.artifact_root,
        results=case.results,
        intake_crash=interrupted,
    )
    executor = group_executor(case, factory)
    first = executor.execute_group(
        case.plan,
        case.prepared,
        coordination_input=case.coordination,
        request_id_prefix="intake-replay",
    )
    second = executor.execute_group(
        case.plan,
        case.prepared,
        coordination_input=case.coordination,
        request_id_prefix="intake-replay",
    )
    assert not first.successful and second.successful
    assert second.members[1].execution_outcome.intake_replayed
    assert factory.calls == {identifier: 1 for identifier in case.prepared.user_story_ids}


def test_one_member_can_be_interrupted_without_cancelling_others(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    interrupted = case.prepared.contexts[1]
    cancellation = Event()
    cancellation.set()
    factory = ExecutorFactory(
        manager=case.manager,
        project_store=case.project_store,
        artifact_root=case.artifact_root,
        results=case.results,
        modes={interrupted.user_story_id: "sleep"},
    )
    outcome = group_executor(case, factory).execute_group(
        case.plan,
        case.prepared,
        coordination_input=case.coordination,
        request_id_prefix="member-cancel",
        member_cancellations={interrupted.assignment_id: cancellation},
    )
    assert outcome.members[0].ready_for_test
    assert not outcome.members[1].ready_for_test
    assert not outcome.successful


@pytest.mark.parametrize(
    ("mode", "timeout"),
    (("malformed", 5.0), ("role-result-tool-failure", 5.0), ("sleep", 0.1)),
)
def test_one_member_failure_is_isolated_and_group_stays_incomplete(
    tmp_path: Path, mode: str, timeout: float
) -> None:
    case = make_case(tmp_path)
    failed = case.prepared.user_story_ids[1]
    factory = ExecutorFactory(
        manager=case.manager,
        project_store=case.project_store,
        artifact_root=case.artifact_root,
        results=case.results,
        modes={failed: mode},
        timeouts={failed: timeout},
    )
    outcome = group_executor(case, factory).execute_group(
        case.plan,
        case.prepared,
        coordination_input=case.coordination,
        request_id_prefix=f"partial-{mode}",
    )
    assert not outcome.successful
    assert outcome.status is ParallelCodexGroupStatus.INCOMPLETE
    assert outcome.members[0].ready_for_test
    assert not outcome.members[1].ready_for_test
    assert factory.calls == {identifier: 1 for identifier in case.prepared.user_story_ids}


def test_dirty_member_and_cross_member_result_are_refused_without_contamination(
    tmp_path: Path,
) -> None:
    dirty = make_case(tmp_path / "dirty")
    dirty_path = Path(dirty.prepared.contexts[1].worktree_path) / "unexpected.txt"
    dirty_path.write_text("unexpected", encoding="utf-8")
    factory = ExecutorFactory(
        manager=dirty.manager,
        project_store=dirty.project_store,
        artifact_root=dirty.artifact_root,
        results=dirty.results,
    )
    dirty_outcome = group_executor(dirty, factory).execute_group(
        dirty.plan,
        dirty.prepared,
        coordination_input=dirty.coordination,
        request_id_prefix="dirty",
    )
    assert dirty_outcome.members[0].ready_for_test
    assert not dirty_outcome.members[1].ready_for_test
    assert factory.calls == {dirty.prepared.user_story_ids[0]: 1}

    crossed = make_case(tmp_path / "crossed")
    left, right = crossed.prepared.user_story_ids
    crossed.results = {left: crossed.results[right], right: crossed.results[left]}
    crossed_factory = ExecutorFactory(
        manager=crossed.manager,
        project_store=crossed.project_store,
        artifact_root=crossed.artifact_root,
        results=crossed.results,
    )
    crossed_outcome = group_executor(crossed, crossed_factory).execute_group(
        crossed.plan,
        crossed.prepared,
        coordination_input=crossed.coordination,
        request_id_prefix="crossed",
    )
    assert not crossed_outcome.successful
    assert not any(item.ready_for_test for item in crossed_outcome.members)


def test_execution_state_persistence_failure_is_member_local(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    failed = case.prepared.user_story_ids[1]
    factory = ExecutorFactory(
        manager=case.manager,
        project_store=case.project_store,
        artifact_root=case.artifact_root,
        results=case.results,
        persistence_failure=failed,
    )
    outcome = group_executor(case, factory).execute_group(
        case.plan,
        case.prepared,
        coordination_input=case.coordination,
        request_id_prefix="persistence",
    )
    assert outcome.members[0].ready_for_test
    assert not outcome.members[1].ready_for_test
    assert any("WRITE_FAILED" in item for item in outcome.members[1].blockers)


def test_conflict_unknown_and_changed_current_scope_cannot_reuse_safe_group(
    tmp_path: Path,
) -> None:
    case = make_case(tmp_path)
    baseline = case.coordination.baseline_commit
    conflict = coordination_input(
        baseline,
        story("US-0001", allowed=("changes/shared/",)),
        story("US-0002", allowed=("changes/shared/",)),
    )
    unknown = coordination_input(
        baseline,
        story("US-0001", allowed=()),
        story("US-0002"),
    )
    factory = ExecutorFactory(
        manager=case.manager,
        project_store=case.project_store,
        artifact_root=case.artifact_root,
        results=case.results,
    )
    executor = group_executor(case, factory)
    for candidate in (conflict, unknown):
        with pytest.raises(ParallelCoordinationError):
            executor.execute_group(
                case.plan,
                case.prepared,
                coordination_input=candidate,
                request_id_prefix="unsafe",
            )
    current = case.project_store.value.user_stories[0]
    case.project_store.value.user_stories[0] = replace(
        current, scope=story("US-0001", allowed=("changes/shared/",)).scope
    )
    with pytest.raises(ParallelCodexExecutionError, match="PROJECT_STATE_MISMATCH"):
        executor.execute_group(
            case.plan,
            case.prepared,
            coordination_input=case.coordination,
            request_id_prefix="changed-scope",
        )
    assert factory.calls == {}


def test_forged_stale_duplicate_and_swapped_group_bindings_fail_before_launch(
    tmp_path: Path,
) -> None:
    case = make_case(tmp_path)
    left, right = case.prepared.contexts
    candidates = (
        replace(case.prepared, group_index=1),
        replace(case.prepared, workflow_generation=1),
        replace(
            case.prepared,
            user_story_ids=(left.user_story_id, left.user_story_id),
        ),
        replace(
            case.prepared,
            worktree_paths=(right.worktree_path, left.worktree_path),
            contexts=(
                replace(left, worktree_path=right.worktree_path),
                replace(right, worktree_path=left.worktree_path),
            ),
        ),
        replace(
            case.prepared,
            worktree_paths=(left.worktree_path, left.worktree_path),
            contexts=(left, replace(right, worktree_path=left.worktree_path)),
        ),
        replace(
            case.prepared,
            baseline_commit="f" * 40,
            contexts=tuple(
                replace(item, baseline_commit="f" * 40)
                for item in case.prepared.contexts
            ),
        ),
    )
    factory = ExecutorFactory(
        manager=case.manager,
        project_store=case.project_store,
        artifact_root=case.artifact_root,
        results=case.results,
    )
    executor = group_executor(case, factory)
    for index, candidate in enumerate(candidates):
        with pytest.raises(ParallelCoordinationError):
            executor.execute_group(
                case.plan,
                candidate,
                coordination_input=case.coordination,
                request_id_prefix=f"forged-{index}",
            )
    assert factory.calls == {}


def test_non_active_assignment_and_stale_mission_fail_before_launch(tmp_path: Path) -> None:
    failed = make_case(tmp_path / "failed")
    failed.manager.mark_failed(
        failed.prepared.assignment_ids[0], current_generation=0
    )
    factory = ExecutorFactory(
        manager=failed.manager,
        project_store=failed.project_store,
        artifact_root=failed.artifact_root,
        results=failed.results,
    )
    with pytest.raises(ParallelCoordinationError):
        group_executor(failed, factory).execute_group(
            failed.plan,
            failed.prepared,
            coordination_input=failed.coordination,
            request_id_prefix="failed-assignment",
        )

    stale = make_case(tmp_path / "stale")
    stale.mission_store.value = replace(
        stale.mission_store.value, workflow_generation=1
    )
    stale_factory = ExecutorFactory(
        manager=stale.manager,
        project_store=stale.project_store,
        artifact_root=stale.artifact_root,
        results=stale.results,
    )
    with pytest.raises(ParallelCodexExecutionError, match="MISSION_STATE_MISMATCH"):
        group_executor(stale, stale_factory).execute_group(
            stale.plan,
            stale.prepared,
            coordination_input=stale.coordination,
            request_id_prefix="stale-mission",
        )
    assert factory.calls == {} and stale_factory.calls == {}


def test_parallel_executor_exposes_no_integration_or_control_plane_api(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    factory = ExecutorFactory(
        manager=case.manager,
        project_store=case.project_store,
        artifact_root=case.artifact_root,
        results=case.results,
    )
    executor = group_executor(case, factory)
    for forbidden in (
        "submit_result",
        "complete_group",
        "integration_gate",
        "merge",
        "run_tester",
        "transition",
        "certify",
    ):
        assert not hasattr(executor, forbidden)
