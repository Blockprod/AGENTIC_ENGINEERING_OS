from __future__ import annotations

import hashlib
import json
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event

import pytest

from agentic_engineering_os.application import (
    CodexRuntimePort,
    ExecutionExecutableIdentity,
    ExecutionStateError,
    RestartSafeCodexExecutionService,
    RoleHandoff,
    SingleRoleArtifacts,
    SingleRoleCodexExecutor,
    SingleRoleExecutionError,
)
from agentic_engineering_os.domain import (
    HumanApproval,
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    ProjectState,
    UserStoryStatus,
    WorktreeRegistry,
    to_dict,
)
from agentic_engineering_os.infrastructure import (
    CodexRuntimeAdapter,
    CodexRuntimeConfiguration,
    ExecutionGitObserver,
    ExecutionStateStore,
    WorktreeManager,
)
from test_codex_result_intake import (
    architect_result,
    certifier_input,
    certifier_result,
    git,
    implementer_result,
    make_tester_result,
    reviewer_result,
    story,
)


FAKE = Path(__file__).parent / "fixtures" / "fake_codex.py"
MISSION = "P4.8"
GENERATION = 8
ROLE_STEP = {
    MissionRole.ARCHITECT: OperatingStep.UNDERSTAND_CONTRACT,
    MissionRole.IMPLEMENTER: OperatingStep.ACT,
    MissionRole.TESTER: OperatingStep.VERIFY,
    MissionRole.REVIEWER: OperatingStep.REPORT,
    MissionRole.CERTIFIER: OperatingStep.CONTROLLED_TRANSITION,
}
ROLE_STATUS = {
    MissionRole.ARCHITECT: UserStoryStatus.PROPOSED,
    MissionRole.IMPLEMENTER: UserStoryStatus.IN_PROGRESS,
    MissionRole.TESTER: UserStoryStatus.TESTING,
    MissionRole.REVIEWER: UserStoryStatus.REVIEW,
    MissionRole.CERTIFIER: UserStoryStatus.CERTIFICATION,
}


class Store:
    def __init__(self, value: object) -> None:
        self.value = value

    def load(self) -> object:
        return self.value


class CountingRuntime(CodexRuntimePort):
    def __init__(self, runtime: CodexRuntimeAdapter) -> None:
        self.runtime = runtime
        self.calls = 0

    def execute(self, compiled_prompt, binding, *, cancellation=None):
        self.calls += 1
        return self.runtime.execute(compiled_prompt, binding, cancellation=cancellation)


@dataclass
class Case:
    root: Path
    manager: WorktreeManager
    mission_store: Store
    project_store: Store
    runtime: CountingRuntime
    service: RestartSafeCodexExecutionService
    executor: SingleRoleCodexExecutor
    handoff: RoleHandoff
    artifacts: SingleRoleArtifacts
    result: object
    result_file: Path


def copy_contracts(root: Path) -> None:
    source = Path(__file__).parents[1]
    paths = (
        "AGENTS.md",
        "docs/02-invariants.md",
        "docs/03-fail-closed-policy.md",
        "docs/04-authority-model.md",
        "docs/12-codex-operating-contract.md",
        "docs/16-architect.md",
        "docs/17-implementer.md",
        "docs/18-tester.md",
        "docs/19-reviewer.md",
        "docs/20-certifier.md",
        "docs/35-codex-execution-contract.md",
        "docs/PHASE-3-CERTIFICATION.md",
        "roles/architect.md",
        "roles/implementer.md",
        "roles/tester.md",
        "roles/reviewer.md",
        "roles/certifier.md",
        "schemas/architect-result.schema.json",
        "schemas/implementer-result.schema.json",
        "schemas/tester-result.schema.json",
        "schemas/reviewer-result.schema.json",
        "schemas/certifier-result.schema.json",
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
    (root / "src").mkdir(exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "src" / "feature.py").write_text("BASELINE = True\n", encoding="utf-8")
    (root / "tests" / "test_feature.py").write_text("def test_baseline():\n    assert True\n", encoding="utf-8")


def bound_results(commit: str):
    return (
        replace(
            architect_result(commit),
            mission_id=MISSION,
            workflow_generation=GENERATION,
        ),
        replace(
            implementer_result(commit),
            mission_id=MISSION,
            workflow_generation=GENERATION,
        ),
        replace(
            make_tester_result(commit),
            mission_id=MISSION,
            workflow_generation=GENERATION,
        ),
        replace(
            reviewer_result(commit),
            mission_id=MISSION,
            workflow_generation=GENERATION,
        ),
        replace(
            certifier_result(commit),
            mission_id=MISSION,
            workflow_generation=GENERATION,
        ),
    )


def make_case(
    tmp_path: Path,
    role: MissionRole,
    *,
    mode: str | None = None,
    payload: object | None = None,
    timeout: float = 5.0,
) -> Case:
    root = tmp_path / "repository"
    root.mkdir(parents=True)
    copy_contracts(root)
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "P4.8 Test Operator")
    git(root, "config", "user.email", "p4.8@example.invalid")
    git(root, "add", ".")
    git(root, "commit", "-m", "test: single role baseline")
    commit = git(root, "rev-parse", "HEAD").casefold()
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    manager = WorktreeManager(repository_root=root, worktree_root=worktree_root)
    manager.initialize_registry()

    subject = "architecture" if role is MissionRole.ARCHITECT else "US-0001"
    mission = MissionState(
        "1.0",
        MISSION,
        GENERATION,
        MissionStatus.ACTIVE,
        role,
        "Execute one bounded Codex role.",
        subject,
        ROLE_STEP[role],
        "Return the canonical role result.",
        commit,
        story(UserStoryStatus.PROPOSED).metadata.updated_at,
        [],
    )
    selected_story = story(ROLE_STATUS[role])
    architect, implementer, tester, reviewer, certifier = bound_results(commit)
    dossier = certifier_input(commit)
    project = ProjectState(
        "1.0",
        [selected_story],
        list(dossier.evidence) if role is MissionRole.CERTIFIER else [],
        list(dossier.gates) if role is MissionRole.CERTIFIER else [],
    )
    if role is MissionRole.IMPLEMENTER:
        assignment = manager.plan_assignment(
            mission=mission, user_story=selected_story, baseline_commit=commit
        )
        manager.activate(assignment.assignment_id, current_generation=GENERATION)

    handoff = RoleHandoff(
        MissionRole.ORCHESTRATOR,
        role,
        MISSION,
        GENERATION,
        subject,
        "Execute one bounded Codex role.",
        commit,
        ROLE_STEP[role],
        (),
        "Return only the schema-valid RoleResult.",
    )
    results = {
        MissionRole.ARCHITECT: replace(architect, subject=subject),
        MissionRole.IMPLEMENTER: implementer,
        MissionRole.TESTER: tester,
        MissionRole.REVIEWER: reviewer,
        MissionRole.CERTIFIER: certifier,
    }
    artifacts = {
        MissionRole.ARCHITECT: SingleRoleArtifacts(),
        MissionRole.IMPLEMENTER: SingleRoleArtifacts(),
        MissionRole.TESTER: SingleRoleArtifacts(implementer_result=implementer),
        MissionRole.REVIEWER: SingleRoleArtifacts(
            implementer_result=implementer, tester_result=tester
        ),
        MissionRole.CERTIFIER: SingleRoleArtifacts(
            architect_result=architect,
            implementer_result=implementer,
            tester_result=tester,
            reviewer_result=reviewer,
        ),
    }[role]
    result = results[role] if payload is None else payload
    result_file = tmp_path / "role-result.json"
    result_file.write_text(
        result if isinstance(result, str) else json.dumps(to_dict(result), ensure_ascii=False),
        encoding="utf-8",
    )
    selected_mode = mode or (
        "role-result-side-effect" if role in {MissionRole.IMPLEMENTER, MissionRole.TESTER} else "role-result"
    )
    executable = Path(sys.executable).resolve()
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    adapter = CodexRuntimeAdapter(
        CodexRuntimeConfiguration(
            executable=str(executable),
            expected_executable_path=str(executable),
            expected_executable_version="fake-codex 1.0",
            expected_executable_sha256=digest,
            launcher_arguments=(
                str(FAKE),
                "--fake-mode",
                selected_mode,
                "--fake-result-file",
                str(result_file),
            ),
        )
    )
    runtime = CountingRuntime(adapter)
    ledger = ExecutionStateStore(root)
    ledger.initialize()
    service = RestartSafeCodexExecutionService(ledger, runtime, ExecutionGitObserver())
    mission_store = Store(mission)
    project_store = Store(project)
    executor = SingleRoleCodexExecutor(
        mission_store=mission_store,
        project_store=project_store,
        repository=manager,
        execution_service=service,
        executable_identity=ExecutionExecutableIdentity(
            str(executable), "fake-codex 1.0", digest
        ),
        timeout_seconds=timeout,
    )
    return Case(
        root,
        manager,
        mission_store,
        project_store,
        runtime,
        service,
        executor,
        handoff,
        artifacts,
        result,
        result_file,
    )


@pytest.mark.parametrize(
    "role",
    (
        MissionRole.ARCHITECT,
        MissionRole.IMPLEMENTER,
        MissionRole.TESTER,
        MissionRole.REVIEWER,
        MissionRole.CERTIFIER,
    ),
)
def test_five_roles_execute_full_offline_subprocess_pipeline(tmp_path: Path, role: MissionRole) -> None:
    case = make_case(tmp_path, role)

    outcome = case.executor.execute(
        case.handoff, request_id=f"request-{role.value.casefold()}", artifacts=case.artifacts
    )

    assert outcome.validated
    assert outcome.validated_result is not None
    assert outcome.validated_result.role is role
    assert outcome.status.value == "VALIDATED"
    assert case.runtime.calls == 1
    if role is MissionRole.IMPLEMENTER:
        assignment = case.manager.registry_store.load().assignments[0]
        assert Path(assignment.worktree_path, "src", "feature.py").read_text(encoding="utf-8") == "fake Codex side effect\n"
        assert not (case.root / "src" / "feature.py").read_text(encoding="utf-8").startswith("fake")


def test_completed_execution_is_revalidated_without_runtime_replay(tmp_path: Path) -> None:
    case = make_case(tmp_path, MissionRole.ARCHITECT)
    first = case.executor.execute(case.handoff, request_id="request-completed", artifacts=case.artifacts)
    second = case.executor.execute(case.handoff, request_id="request-completed", artifacts=case.artifacts)

    assert first.validated and second.validated
    assert second.completed_reused
    assert case.runtime.calls == 1


def test_observation_restart_replays_only_intake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case = make_case(tmp_path, MissionRole.ARCHITECT)
    original = case.service.replay_intake
    monkeypatch.setattr(
        case.service,
        "replay_intake",
        lambda *args, **kwargs: (_ for _ in ()).throw(ExecutionStateError("CRASH", "after observation")),
    )
    with pytest.raises(ExecutionStateError, match="CRASH"):
        case.executor.execute(case.handoff, request_id="request-observed", artifacts=case.artifacts)
    assert case.runtime.calls == 1
    monkeypatch.setattr(case.service, "replay_intake", original)

    resumed = case.executor.execute(case.handoff, request_id="request-observed", artifacts=case.artifacts)

    assert resumed.validated and resumed.intake_replayed
    assert case.runtime.calls == 1


@pytest.mark.parametrize("mode", ("malformed", "role-result-tool-failure"))
def test_malformed_or_tool_failed_transport_never_returns_role_result(tmp_path: Path, mode: str) -> None:
    case = make_case(tmp_path, MissionRole.ARCHITECT, mode=mode)
    outcome = case.executor.execute(case.handoff, request_id=f"request-{mode}", artifacts=case.artifacts)
    assert not outcome.validated
    assert outcome.validated_result is None


def test_wrong_role_result_and_certifier_self_certification_are_refused(tmp_path: Path) -> None:
    wrong = make_case(tmp_path / "wrong", MissionRole.ARCHITECT)
    _, _, _, reviewer, _ = bound_results(wrong.handoff.observed_commit)
    wrong.result_file.write_text(json.dumps(to_dict(reviewer)), encoding="utf-8")
    wrong_outcome = wrong.executor.execute(wrong.handoff, request_id="request-wrong-role", artifacts=wrong.artifacts)

    certifier = make_case(tmp_path / "certifier", MissionRole.CERTIFIER)
    payload = to_dict(certifier.result)
    payload["verdict"] = "CERTIFIED"
    certifier.result_file.write_text(json.dumps(payload), encoding="utf-8")
    certifier_outcome = certifier.executor.execute(
        certifier.handoff, request_id="request-self-certify", artifacts=certifier.artifacts
    )

    assert not wrong_outcome.validated
    assert not certifier_outcome.validated


def test_read_only_side_effect_and_implementer_invalid_result_remain_failed(tmp_path: Path) -> None:
    read_only = make_case(
        tmp_path / "read-only", MissionRole.REVIEWER, mode="role-result-forbidden-side-effect"
    )
    read_only_outcome = read_only.executor.execute(
        read_only.handoff, request_id="request-read-only-drift", artifacts=read_only.artifacts
    )
    implementer = make_case(
        tmp_path / "implementer",
        MissionRole.IMPLEMENTER,
        mode="role-result-invalid-side-effect",
        payload="not-json",
    )
    implementer_outcome = implementer.executor.execute(
        implementer.handoff,
        request_id="request-invalid-after-side-effect",
        artifacts=implementer.artifacts,
    )

    assert not read_only_outcome.validated
    assert not implementer_outcome.validated
    assert implementer_outcome.validated_result is None


def test_declared_paths_must_equal_physical_git_side_effects(tmp_path: Path) -> None:
    case = make_case(tmp_path, MissionRole.IMPLEMENTER, mode="role-result-invalid-side-effect")
    outcome = case.executor.execute(case.handoff, request_id="request-path-mismatch", artifacts=case.artifacts)
    assert not outcome.validated
    assert any("declared file changes differ" in blocker for blocker in outcome.blockers)


@pytest.mark.parametrize("mode", ("sleep", "timeout-side-effect"))
def test_timeout_never_retries_and_dirty_timeout_requires_recovery(tmp_path: Path, mode: str) -> None:
    case = make_case(tmp_path, MissionRole.IMPLEMENTER, mode=mode, timeout=0.1)
    outcome = case.executor.execute(case.handoff, request_id=f"request-{mode}", artifacts=case.artifacts)
    assert not outcome.validated
    assert case.runtime.calls == 1
    if mode == "timeout-side-effect":
        with pytest.raises(RuntimeError, match="WORKTREE_MISMATCH"):
            case.executor.execute(
                case.handoff, request_id=f"request-{mode}", artifacts=case.artifacts
            )
    else:
        replay = case.executor.execute(
            case.handoff, request_id=f"request-{mode}", artifacts=case.artifacts
        )
        assert not replay.validated
    assert case.runtime.calls == 1


def test_pre_cancelled_execution_never_produces_role_result(tmp_path: Path) -> None:
    case = make_case(tmp_path, MissionRole.ARCHITECT, mode="sleep", timeout=1.0)
    cancellation = Event()
    cancellation.set()
    outcome = case.executor.execute(
        case.handoff,
        request_id="request-cancelled",
        artifacts=case.artifacts,
        cancellation=cancellation,
    )
    assert not outcome.validated


def test_wrong_handoff_generation_story_commit_upstream_and_human_authority_fail_closed(tmp_path: Path) -> None:
    case = make_case(tmp_path, MissionRole.TESTER)
    mutations = (
        replace(case.handoff, to_role=MissionRole.ORCHESTRATOR),
        replace(case.handoff, workflow_generation=GENERATION + 1),
        replace(case.handoff, subject="US-WRONG"),
        replace(case.handoff, observed_commit="f" * 40),
    )
    for index, handoff in enumerate(mutations):
        with pytest.raises((SingleRoleExecutionError, ValueError, RuntimeError)):
            case.executor.execute(
                handoff,
                request_id=f"request-stale-{index}",
                artifacts=case.artifacts,
            )
    architect = make_case(tmp_path / "wrong-source", MissionRole.ARCHITECT)
    with pytest.raises(SingleRoleExecutionError, match="INVALID_HANDOFF"):
        architect.executor.execute(
            replace(architect.handoff, from_role=MissionRole.REVIEWER),
            request_id="request-wrong-source",
            artifacts=architect.artifacts,
        )
    forged = replace(case.artifacts.implementer_result, user_story_id="US-WRONG")
    with pytest.raises((ValueError, RuntimeError)):
        case.executor.execute(
            case.handoff,
            request_id="request-forged-upstream",
            artifacts=SingleRoleArtifacts(implementer_result=forged),
        )

    human = make_case(tmp_path / "human", MissionRole.IMPLEMENTER)
    human_story = human.project_store.value.user_stories[0]
    human.project_store.value.user_stories[0] = replace(
        human_story,
        human_approval=HumanApproval(True, False, None, None),
    )
    with pytest.raises(ValueError):
        human.executor.execute(
            human.handoff,
            request_id="request-human-required",
            artifacts=human.artifacts,
        )


def test_missing_or_wrong_worktree_and_extra_artifacts_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    implementer = make_case(tmp_path / "worktree", MissionRole.IMPLEMENTER)
    monkeypatch.setattr(
        implementer.manager.registry_store,
        "load",
        lambda: WorktreeRegistry("1.0", ()),
    )
    with pytest.raises((SingleRoleExecutionError, RuntimeError)):
        implementer.executor.execute(
            implementer.handoff, request_id="request-no-worktree", artifacts=implementer.artifacts
        )

    wrong = make_case(tmp_path / "wrong-worktree", MissionRole.IMPLEMENTER)
    assignment = wrong.manager.registry_store.load().assignments[0]
    monkeypatch.setattr(
        wrong.manager.registry_store,
        "load",
        lambda: WorktreeRegistry(
            "1.0",
            (replace(assignment, worktree_path=str(wrong.root)),),
        ),
    )
    with pytest.raises(RuntimeError, match="WORKTREE_MISMATCH"):
        wrong.executor.execute(
            wrong.handoff,
            request_id="request-wrong-worktree",
            artifacts=wrong.artifacts,
        )

    architect = make_case(tmp_path / "extra", MissionRole.ARCHITECT)
    _, extra, _, _, _ = bound_results(architect.handoff.observed_commit)
    with pytest.raises(SingleRoleExecutionError, match="UPSTREAM_SET_MISMATCH"):
        architect.executor.execute(
            architect.handoff,
            request_id="request-extra-artifact",
            artifacts=SingleRoleArtifacts(implementer_result=extra),
        )


def test_coordinator_exposes_no_control_plane_progression_api(tmp_path: Path) -> None:
    case = make_case(tmp_path, MissionRole.ARCHITECT)
    for forbidden in (
        "record_evidence",
        "evaluate_gate",
        "certify",
        "approve_human",
        "transition",
        "control_loop",
    ):
        assert not hasattr(case.executor, forbidden)
