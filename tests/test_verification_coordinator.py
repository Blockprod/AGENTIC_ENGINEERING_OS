import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_engineering_os._authoritative_write import _issue_authoritative_write
from agentic_engineering_os.application import (
    CertificationService,
    ContractValidator,
    ControlLoop,
    EvidenceRecorder,
    GateEvaluator,
    StateTransitionService,
    VerificationCoordinationError,
    VerificationCoordinator,
    VerificationProcessResult,
)
from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    EvidenceType,
    GateResult,
    HumanApproval,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
)
from agentic_engineering_os.infrastructure import (
    GitAdapter,
    ProjectConfigurationValidator,
    ProjectStateStore,
)

from test_project_configuration import valid_candidate


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


class FakeRunner:
    def __init__(
        self,
        *,
        exit_code: int | None = 0,
        started: bool = True,
        failure_code: str | None = None,
        mutate: bool = False,
    ) -> None:
        self.exit_code = exit_code
        self.started = started
        self.failure_code = failure_code
        self.mutate = mutate
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def run(self, argv: tuple[str, ...], cwd: Path) -> VerificationProcessResult:
        self.calls.append((argv, cwd))
        if self.mutate:
            (cwd / "unexpected.txt").write_text("mutation", encoding="utf-8")
        return VerificationProcessResult(
            argv=argv,
            cwd=cwd,
            started=self.started,
            exit_code=self.exit_code,
            stdout=b"verified\n",
            stderr=b"" if self.exit_code == 0 else b"failed\n",
            failure_code=self.failure_code,
        )


class RefusingRunner:
    def run(self, argv: tuple[str, ...], cwd: Path) -> VerificationProcessResult:
        raise AssertionError("persisted exact Evidence must be reused without execution")


def run_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


def make_story(identifier: str = "US-0001") -> UserStory:
    return UserStory(
        schema_version="1.0",
        id=identifier,
        title="Trusted verification",
        description="Run configured verification under Control Plane authority.",
        status=UserStoryStatus.PROPOSED,
        priority=1,
        risk=RiskLevel.MEDIUM,
        depends_on=(),
        scope=UserStoryScope(allowed_paths=("src",), forbidden_paths=()),
        acceptance_criteria=(
            AcceptanceCriterion("AC-001", "Verification is observed.", True),
        ),
        required_gates=(f"tests::{identifier}",),
        human_approval=HumanApproval(False, False, None, None),
        metadata=UserStoryMetadata(NOW, "Codex/Architect", NOW),
    )


def make_control_loop(store: ProjectStateStore) -> ControlLoop:
    validator = ContractValidator()
    return ControlLoop(
        state_store=store,
        evidence_recorder_factory=lambda target: EvidenceRecorder(
            target, validator=validator, clock=lambda: NOW
        ),
        gate_evaluator=GateEvaluator(validator=validator, clock=lambda: NOW),
        certification_service=CertificationService(
            validator=validator, clock=lambda: NOW
        ),
        transition_service=StateTransitionService(),
    )


def harness(tmp_path: Path, *, story_id: str = "US-0001"):
    root = tmp_path / "repository"
    root.mkdir()
    run_git(root, "init", "-b", "main")
    run_git(root, "config", "user.name", "Test Operator")
    run_git(root, "config", "user.email", "operator@example.invalid")
    (root / ".gitignore").write_text(".agentic-engineering-os/\n", encoding="utf-8")
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    run_git(root, "add", ".gitignore", "README.md")
    run_git(root, "commit", "-m", "fixture")
    commit = run_git(root, "rev-parse", "HEAD")

    config = ProjectConfigurationValidator().validate(valid_candidate())
    user_story = make_story(story_id)
    store = ProjectStateStore(root)
    current = store.initialize(project_id=config.project_id)
    candidate = replace(current, user_stories=(user_story,))
    operation = "TEST_SETUP_PROJECT_STATE"
    authorization = _issue_authoritative_write(
        store_kind="PROJECT_STATE",
        store=store,
        before_state=current,
        candidate_state=candidate,
        operation=operation,
    )
    store.save(candidate, authorization=authorization, operation=operation)
    return root, commit, config, user_story, store, make_control_loop(store)


def coordinator(root: Path, loop: ControlLoop, runner) -> VerificationCoordinator:
    return VerificationCoordinator(
        root,
        control_loop=loop,
        runner=runner,
        git_observer=GitAdapter(root),
        gate_evaluator=GateEvaluator(clock=lambda: NOW),
        clock=lambda: NOW,
    )


def verify(instance: VerificationCoordinator, config, story, commit: str):
    return instance.verify(
        config,
        story,
        mission_id="M4-R2B",
        workflow_generation=4,
        integrated_commit=commit,
    )


def test_configured_argv_produces_tool_evidence_and_passing_gate(tmp_path: Path) -> None:
    root, commit, config, story, store, loop = harness(tmp_path)
    runner = FakeRunner()

    result = verify(coordinator(root, loop, runner), config, story, commit)

    assert runner.calls == [(('python', '-m', 'pytest', 'tests'), root.resolve())]
    assert len(result.evidence) == 1
    assert result.evidence[0].evidence_type is EvidenceType.COMMAND_RESULT
    assert result.evidence[0].source == "agentic-engineering-os/verification"
    assert result.evidence[0].producer == "ControlPlane/VerificationCoordinator"
    assert result.evidence[0].command == '["python","-m","pytest","tests"]'
    assert result.evidence[0].result["command_id"] == "tests"
    assert result.gates[0].gate.gate_id == "tests::US-0001"
    assert result.gates[0].result is GateResult.PASS
    assert store.load().gates == [result.gates[0].gate]


def test_same_policy_isolated_for_two_story_instances(tmp_path: Path) -> None:
    root, commit, config, first, store, loop = harness(tmp_path)
    current = store.load()
    second = make_story("US-0002")
    candidate = replace(current, user_stories=(*current.user_stories, second))
    operation = "TEST_ADD_SECOND_STORY"
    authorization = _issue_authoritative_write(
        store_kind="PROJECT_STATE",
        store=store,
        before_state=current,
        candidate_state=candidate,
        operation=operation,
    )
    store.save(candidate, authorization=authorization, operation=operation)

    first_result = verify(coordinator(root, loop, FakeRunner()), config, first, commit)
    second_result = verify(coordinator(root, loop, FakeRunner()), config, second, commit)

    assert first_result.gates[0].gate_id == "tests::US-0001"
    assert second_result.gates[0].gate_id == "tests::US-0002"


def test_failed_command_produces_failing_gate(tmp_path: Path) -> None:
    root, commit, config, story, _store, loop = harness(tmp_path)

    result = verify(coordinator(root, loop, FakeRunner(exit_code=7)), config, story, commit)

    assert result.evidence[0].exit_code == 7
    assert result.evidence[0].result["passed"] is False
    assert result.gates[0].result is GateResult.FAIL


@pytest.mark.parametrize(
    ("started", "failure_code"),
    [(False, "COMMAND_NOT_FOUND"), (True, "COMMAND_TIMEOUT")],
)
def test_unfinished_command_produces_no_evidence_and_unknown_gate(
    tmp_path: Path, started: bool, failure_code: str
) -> None:
    root, commit, config, story, store, loop = harness(tmp_path)

    result = verify(
        coordinator(
            root,
            loop,
            FakeRunner(
                exit_code=None,
                started=started,
                failure_code=failure_code,
            ),
        ),
        config,
        story,
        commit,
    )

    assert result.evidence == ()
    assert result.blockers == (failure_code,)
    assert result.gates[0].result is GateResult.UNKNOWN
    assert store.load().evidence == []


def test_exact_evidence_and_gate_are_reused_after_restart(tmp_path: Path) -> None:
    root, commit, config, story, _store, loop = harness(tmp_path)
    initial = verify(coordinator(root, loop, FakeRunner()), config, story, commit)

    resumed = verify(coordinator(root, loop, RefusingRunner()), config, story, commit)

    assert resumed.evidence == initial.evidence
    assert resumed.gates[0].gate == initial.gates[0].gate


def test_divergent_deterministic_evidence_collision_is_refused(tmp_path: Path) -> None:
    root, commit, config, story, store, loop = harness(tmp_path)
    verify(coordinator(root, loop, FakeRunner()), config, story, commit)
    current = store.load()
    forged = replace(current.evidence[0], producer="pytest")
    candidate = replace(current, evidence=(forged,))
    operation = "TEST_FORGE_EVIDENCE"
    authorization = _issue_authoritative_write(
        store_kind="PROJECT_STATE",
        store=store,
        before_state=current,
        candidate_state=candidate,
        operation=operation,
    )
    store.save(candidate, authorization=authorization, operation=operation)

    with pytest.raises(VerificationCoordinationError) as captured:
        verify(coordinator(root, loop, RefusingRunner()), config, story, commit)

    assert captured.value.code == "EVIDENCE_COLLISION"


def test_stale_evidence_replay_is_refused(tmp_path: Path) -> None:
    root, commit, config, story, store, loop = harness(tmp_path)
    verify(coordinator(root, loop, FakeRunner()), config, story, commit)
    current = store.load()
    stale = replace(current.evidence[0], commit="a" * 40)
    candidate = replace(current, evidence=(stale,))
    operation = "TEST_REPLAY_STALE_EVIDENCE"
    authorization = _issue_authoritative_write(
        store_kind="PROJECT_STATE",
        store=store,
        before_state=current,
        candidate_state=candidate,
        operation=operation,
    )
    store.save(candidate, authorization=authorization, operation=operation)

    with pytest.raises(VerificationCoordinationError) as captured:
        verify(coordinator(root, loop, RefusingRunner()), config, story, commit)

    assert captured.value.code == "EVIDENCE_COLLISION"


def test_divergent_preexisting_gate_collision_is_refused(tmp_path: Path) -> None:
    root, commit, config, story, store, loop = harness(tmp_path)
    verify(coordinator(root, loop, FakeRunner()), config, story, commit)
    current = store.load()
    divergent = replace(current.gates[0], result=GateResult.FAIL)
    candidate = replace(current, gates=(divergent,))
    operation = "TEST_FORGE_GATE"
    authorization = _issue_authoritative_write(
        store_kind="PROJECT_STATE",
        store=store,
        before_state=current,
        candidate_state=candidate,
        operation=operation,
    )
    store.save(candidate, authorization=authorization, operation=operation)

    with pytest.raises(VerificationCoordinationError) as captured:
        verify(coordinator(root, loop, RefusingRunner()), config, story, commit)

    assert captured.value.code == "GATE_COLLISION"


def test_required_story_gate_without_policy_blocks_before_execution(tmp_path: Path) -> None:
    root, commit, config, story, _store, loop = harness(tmp_path)
    runner = FakeRunner()

    with pytest.raises(VerificationCoordinationError) as captured:
        verify(
            coordinator(root, loop, runner),
            replace(config, gate_policies=()),
            story,
            commit,
        )

    assert captured.value.code == "GATE_POLICY_MISSING"
    assert runner.calls == []


def test_runner_cannot_substitute_configured_argv(tmp_path: Path) -> None:
    root, commit, config, story, _store, loop = harness(tmp_path)

    class SubstitutingRunner(FakeRunner):
        def run(self, argv, cwd):
            observed = super().run(argv, cwd)
            return replace(observed, argv=("other",))

    with pytest.raises(VerificationCoordinationError) as captured:
        verify(coordinator(root, loop, SubstitutingRunner()), config, story, commit)

    assert captured.value.code == "COMMAND_BINDING_MISMATCH"


def test_cwd_escape_is_refused_even_for_manually_forged_configuration(
    tmp_path: Path,
) -> None:
    root, commit, config, story, _store, loop = harness(tmp_path)
    escaped = replace(config.verification_commands[1], cwd="..")
    forged = replace(
        config,
        verification_commands=(config.verification_commands[0], escaped),
        gate_policies=(
            replace(config.gate_policies[0], verification_command_ids=("tests",)),
        ),
    )

    with pytest.raises(VerificationCoordinationError) as captured:
        verify(coordinator(root, loop, FakeRunner()), forged, story, commit)

    assert captured.value.code == "COMMAND_CWD_ESCAPE"


def test_cwd_symlink_escape_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, commit, config, story, _store, loop = harness(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "linked-cwd"
    link.mkdir()
    original_resolve = Path.resolve

    def resolve_symlink(candidate: Path, strict: bool = False) -> Path:
        if candidate == link:
            return outside
        return original_resolve(candidate, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve_symlink)
    escaped = replace(config.verification_commands[1], cwd="linked-cwd")
    forged = replace(
        config,
        verification_commands=(config.verification_commands[0], escaped),
    )

    with pytest.raises(VerificationCoordinationError) as captured:
        verify(coordinator(root, loop, FakeRunner()), forged, story, commit)

    assert captured.value.code == "COMMAND_CWD_ESCAPE"


def test_dirty_file_created_by_verification_blocks_before_evidence(tmp_path: Path) -> None:
    root, commit, config, story, store, loop = harness(tmp_path)

    with pytest.raises(VerificationCoordinationError) as captured:
        verify(coordinator(root, loop, FakeRunner(mutate=True)), config, story, commit)

    assert captured.value.code == "VERIFICATION_MUTATED_REPOSITORY"
    assert store.load().evidence == []
    assert (root / "unexpected.txt").exists()


def test_head_divergence_blocks_before_command_execution(tmp_path: Path) -> None:
    root, commit, config, story, _store, loop = harness(tmp_path)
    (root / "README.md").write_text("advanced\n", encoding="utf-8")
    run_git(root, "add", "README.md")
    run_git(root, "commit", "-m", "advance")
    runner = FakeRunner()

    with pytest.raises(VerificationCoordinationError) as captured:
        verify(coordinator(root, loop, runner), config, story, commit)

    assert captured.value.code == "HEAD_DIVERGED"
    assert runner.calls == []
