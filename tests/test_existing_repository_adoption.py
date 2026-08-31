import inspect
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from agentic_engineering_os.application import ExistingRepositoryAdoption
from agentic_engineering_os.domain import (
    AdoptionStatus,
    HumanOperationConfirmation,
    InitializationApplyFinding,
    InitializationApplyStatus,
    InitializationResult,
    MissionStateGitPolicy,
    ProjectConfiguration,
    RuntimeBootstrapStatus,
)
from agentic_engineering_os.infrastructure import (
    PersistenceError,
    ProjectConfigurationValidator,
    ProjectStateStore,
)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def existing_repository(
    tmp_path: Path,
    *,
    kind: str = "python",
    agents: str | None = None,
) -> Path:
    root = tmp_path / "target"
    root.mkdir(parents=True)
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "P5.8 Test")
    git(root, "config", "user.email", "p5.8@example.invalid")
    (root / "README.md").write_text("# Existing repository\n", encoding="utf-8")
    if kind in {"python", "mixed"}:
        (root / "pyproject.toml").write_text(
            '[project]\nname = "target"\nversion = "0.1.0"\n', encoding="utf-8"
        )
    if kind in {"node", "mixed"}:
        (root / "package.json").write_text(
            '{"name":"target","version":"1.0.0"}\n', encoding="utf-8"
        )
    if agents is not None:
        (root / "AGENTS.md").write_text(agents, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "existing baseline")
    return root


def configuration(project_id: str = "target") -> ProjectConfiguration:
    return ProjectConfigurationValidator().validate(
        {
            "config_version": "1.0",
            "project_id": project_id,
            "repository_root_policy": "CONFIG_PARENT_GIT_ROOT",
            "toolchains": [],
            "verification_commands": [],
            "path_policy": {
                "allowed_paths": [],
                "protected_paths": [],
                "forbidden_paths": [],
            },
            "context_sources": [],
            "codex_constraints": {
                "maximum_sandbox": "read-only",
                "approval_policy": "never",
                "require_clean_git": True,
                "maximum_parallel_executions": 1,
            },
            "mission_state_git_policy": MissionStateGitPolicy.TRACKED.value,
        }
    )


def confirmations(preparation, producer: str = "Human/Alice"):
    assert preparation.initialization_plan is not None
    return tuple(
        HumanOperationConfirmation(
            plan_fingerprint=preparation.initialization_plan.input_fingerprint,
            operation_id=operation.operation_id,
            target_path=operation.target_path,
            expected_current_state=operation.expected_current_state,
            expected_target_fingerprint=operation.expected_target_fingerprint or "",
            confirmed_by=producer,
        )
        for operation in preparation.initialization_plan.operations
        if operation.human_confirmation_required
    )


def adopt(root: Path, config: ProjectConfiguration | None = None):
    service = ExistingRepositoryAdoption()
    preparation = service.prepare_adoption(root, config or configuration())
    result = service.apply_adoption(
        preparation, human_confirmations=confirmations(preparation)
    )
    return service, preparation, result


@pytest.mark.parametrize("kind", ["python", "node", "mixed"])
def test_clean_existing_repository_variants_are_adopted(
    tmp_path: Path, kind: str
) -> None:
    root = existing_repository(tmp_path, kind=kind)
    head = git(root, "rev-parse", "HEAD")

    _, preparation, result = adopt(root)

    assert preparation.status is AdoptionStatus.READY_TO_APPLY
    assert result.status is AdoptionStatus.ADOPTED
    assert result.runtime_bootstrap_result is not None
    assert result.runtime_bootstrap_result.status is RuntimeBootstrapStatus.BOOTSTRAPPED
    assert git(root, "rev-parse", "HEAD") == head
    assert (root / ".agentic-engineering-os" / "state.json").is_file()
    assert not (root / ".agentic-engineering-os" / "mission.json").exists()


def test_user_agents_requires_exact_human_confirmation_and_preserves_content(
    tmp_path: Path,
) -> None:
    user_content = "# Team rules\n\nKeep this text.\n"
    root = existing_repository(tmp_path, agents=user_content)
    service = ExistingRepositoryAdoption()
    preparation = service.prepare_adoption(root, configuration())

    assert preparation.status is AdoptionStatus.NEEDS_HUMAN_CONFIRMATION
    before = (root / "AGENTS.md").read_bytes()
    refused = service.apply_adoption(preparation)
    assert refused.status is AdoptionStatus.BLOCKED
    assert (root / "AGENTS.md").read_bytes() == before

    result = service.apply_adoption(
        preparation, human_confirmations=confirmations(preparation)
    )
    assert result.status is AdoptionStatus.ADOPTED
    assert (root / "AGENTS.md").read_text(encoding="utf-8").startswith(user_content)


def test_dry_run_requires_explicit_configuration_and_never_uses_inference(
    tmp_path: Path,
) -> None:
    root = existing_repository(tmp_path, kind="mixed")
    before = {
        path.relative_to(root): path.read_bytes()
        for path in root.iterdir()
        if path.is_file()
    }
    preparation = ExistingRepositoryAdoption().prepare_adoption(root)

    assert preparation.status is AdoptionStatus.NEEDS_CONFIGURATION
    assert preparation.configuration_requirements
    assert preparation.project_configuration is None
    assert {
        path.relative_to(root): path.read_bytes()
        for path in root.iterdir()
        if path.is_file()
    } == before
    assert not (root / ".agentic-engineering-os").exists()


def test_already_adopted_repository_is_idempotent(tmp_path: Path) -> None:
    root = existing_repository(tmp_path)
    service, _, first = adopt(root)
    state_before = ProjectStateStore(root).state_path.read_bytes()

    second_preparation = service.prepare_adoption(root)
    second = service.apply_adoption(second_preparation)

    assert first.status is AdoptionStatus.ADOPTED
    assert second_preparation.status is AdoptionStatus.READY_TO_APPLY
    assert second.status is AdoptionStatus.ADOPTED
    assert second.runtime_bootstrap_result is not None
    assert second.runtime_bootstrap_result.status is RuntimeBootstrapStatus.ALREADY_BOOTSTRAPPED
    assert ProjectStateStore(root).state_path.read_bytes() == state_before


def test_fake_codex_human_and_stale_confirmation_are_refused(tmp_path: Path) -> None:
    root = existing_repository(tmp_path, agents="# User\n")
    service = ExistingRepositoryAdoption()
    preparation = service.prepare_adoption(root, configuration())

    fake = service.apply_adoption(
        preparation,
        human_confirmations=confirmations(preparation, "CoDeX/FakeHuman"),
    )
    assert fake.status is AdoptionStatus.BLOCKED
    assert not (root / ".agentic-engineering-os").exists()

    (root / "AGENTS.md").write_text("# Changed after confirmation\n", encoding="utf-8")
    stale = service.apply_adoption(
        preparation, human_confirmations=confirmations(preparation)
    )
    assert stale.status is AdoptionStatus.BLOCKED
    assert not (root / ".agentic-engineering-os").exists()


def test_stale_plan_and_unrelated_dirty_handoff_are_refused(tmp_path: Path) -> None:
    root = existing_repository(tmp_path)
    service = ExistingRepositoryAdoption()
    preparation = service.prepare_adoption(root, configuration())
    (root / "unexpected.txt").write_text("changed\n", encoding="utf-8")

    stale = service.apply_adoption(preparation)
    assert stale.status is AdoptionStatus.BLOCKED
    assert not (root / ".agentic-engineering-os").exists()

    root2 = existing_repository(tmp_path / "second")
    service2 = ExistingRepositoryAdoption()
    preparation2 = service2.prepare_adoption(root2, configuration())
    original_apply = service2._initializer.apply

    def apply_then_dirty(*args, **kwargs):
        result = original_apply(*args, **kwargs)
        (root2 / "unrelated.txt").write_text("not authorized\n", encoding="utf-8")
        return result

    service2._initializer.apply = apply_then_dirty  # type: ignore[method-assign]
    mismatch = service2.apply_adoption(preparation2)
    assert mismatch.status is AdoptionStatus.PARTIAL_OR_INCONSISTENT
    assert mismatch.runtime_bootstrap_result is not None
    assert (
        mismatch.runtime_bootstrap_result.findings[0].code
        == "INVALID_INITIALIZATION_HANDOFF"
    )
    assert not ProjectStateStore(root2).state_path.exists()


def test_dirty_invalid_configuration_and_unknown_config_are_classified(
    tmp_path: Path,
) -> None:
    root = existing_repository(tmp_path)
    dirty_path = root / "dirty.txt"
    dirty_path.write_text("dirty\n", encoding="utf-8")
    dirty = ExistingRepositoryAdoption().prepare_adoption(root, configuration())
    assert dirty.status is AdoptionStatus.BLOCKED
    dirty_path.unlink()

    invalid = replace(configuration(), project_id="bad/id")
    blocked = ExistingRepositoryAdoption().prepare_adoption(root, invalid)
    assert blocked.status is AdoptionStatus.BLOCKED

    agentic = root / ".agentic-engineering-os"
    agentic.mkdir()
    (agentic / "config.json").write_text('{"config_version":"999.0"}\n', encoding="utf-8")
    upgrade = ExistingRepositoryAdoption().prepare_adoption(root, configuration())
    assert upgrade.status is AdoptionStatus.UPGRADE_REQUIRED


def test_partial_runtime_and_tampered_agents_are_fail_closed(tmp_path: Path) -> None:
    root = existing_repository(tmp_path)
    _, _, adopted = adopt(root)
    assert adopted.status is AdoptionStatus.ADOPTED
    state_path = ProjectStateStore(root).state_path
    canonical_state = state_path.read_bytes()
    state_path.write_text(
        '{"schema_version":"999.0","user_stories":[],"evidence":[],'
        '"gates":[],"certifications":[],"audit_events":[]}\n',
        encoding="utf-8",
    )
    upgrade = ExistingRepositoryAdoption().prepare_adoption(root)
    assert upgrade.status is AdoptionStatus.UPGRADE_REQUIRED
    state_path.write_bytes(canonical_state)

    state_path.unlink()
    (state_path.parent / "executions.json").write_text(
        '{"records":[],"schema_version":"1.1"}\n', encoding="utf-8"
    )
    partial = ExistingRepositoryAdoption().prepare_adoption(root)
    assert partial.status is AdoptionStatus.PARTIAL_OR_INCONSISTENT

    (state_path.parent / "executions.json").unlink()
    agents = root / "AGENTS.md"
    agents.write_text(
        agents.read_text(encoding="utf-8").replace(
            "Repository and Git truth", "Altered truth"
        ),
        encoding="utf-8",
    )
    tampered = ExistingRepositoryAdoption().prepare_adoption(root)
    assert tampered.status is AdoptionStatus.PARTIAL_OR_INCONSISTENT


def test_symlink_managed_target_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = existing_repository(tmp_path, agents="# User rules\n")
    agents = root / "AGENTS.md"
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == agents or original_is_symlink(path),
    )

    preparation = ExistingRepositoryAdoption().prepare_adoption(
        root, configuration()
    )

    assert preparation.status in {
        AdoptionStatus.PARTIAL_OR_INCONSISTENT,
        AdoptionStatus.BLOCKED,
    }
    assert preparation.initialization_plan is None or preparation.initialization_plan.blockers


def test_runtime_failure_remains_observable_without_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = existing_repository(tmp_path)
    service = ExistingRepositoryAdoption()
    preparation = service.prepare_adoption(root, configuration())

    def fail_initialize(store: ProjectStateStore):
        raise PersistenceError("WRITE_FAILED", "simulated")

    monkeypatch.setattr(ProjectStateStore, "initialize", fail_initialize)
    result = service.apply_adoption(preparation)

    assert result.status is AdoptionStatus.PARTIAL_OR_INCONSISTENT
    assert result.initialization_result is not None
    assert result.initialization_result.status is InitializationApplyStatus.APPLIED
    assert result.runtime_bootstrap_result is not None
    assert result.runtime_bootstrap_result.status is RuntimeBootstrapStatus.FAILED
    assert (root / ".agentic-engineering-os" / "config.json").is_file()
    assert not ProjectStateStore(root).state_path.exists()


def test_initializer_failure_stops_before_runtime(tmp_path: Path) -> None:
    root = existing_repository(tmp_path)
    service = ExistingRepositoryAdoption()
    preparation = service.prepare_adoption(root, configuration())
    plan = preparation.initialization_plan
    assert plan is not None

    def fail_initializer(*args, **kwargs):
        return InitializationResult(
            plan_fingerprint=plan.input_fingerprint,
            repository_root=str(root),
            status=InitializationApplyStatus.FAILED,
            operation_results=(),
            findings=(
                InitializationApplyFinding("SIMULATED", None, ".", "failure"),
            ),
            profile_fingerprint_before=plan.profile_fingerprint,
            profile_fingerprint_after=plan.profile_fingerprint,
            git_head_before=plan.repository.git_head,
            git_head_after=plan.repository.git_head,
            initialization_state_after=None,
        )

    service._initializer.apply = fail_initializer  # type: ignore[method-assign]

    result = service.apply_adoption(preparation)
    assert result.status is AdoptionStatus.BLOCKED
    assert result.runtime_bootstrap_result is None
    assert not (root / ".agentic-engineering-os").exists()


def test_detached_head_and_multiple_worktrees_are_honestly_classified(
    tmp_path: Path,
) -> None:
    detached_root = existing_repository(tmp_path / "detached")
    git(detached_root, "checkout", "--detach")
    detached = ExistingRepositoryAdoption().prepare_adoption(
        detached_root, configuration()
    )
    assert detached.status is AdoptionStatus.READY_TO_APPLY
    assert detached.repository_profile is not None
    assert detached.repository_profile.git.detached.value is True

    root = existing_repository(tmp_path / "multiple")
    sibling = tmp_path / "secondary-worktree"
    git(root, "worktree", "add", "-b", "secondary", str(sibling))
    try:
        multiple = ExistingRepositoryAdoption().prepare_adoption(root, configuration())
        assert multiple.status is AdoptionStatus.READY_TO_APPLY
        assert multiple.repository_profile is not None
        assert len(multiple.repository_profile.git.worktrees) == 2
    finally:
        git(root, "worktree", "remove", "--force", str(sibling))


def test_coordinator_has_no_direct_write_or_mission_creation_api() -> None:
    public = {
        name
        for name, member in inspect.getmembers(
            ExistingRepositoryAdoption, inspect.isfunction
        )
        if not name.startswith("_")
    }
    source = inspect.getsource(ExistingRepositoryAdoption)

    assert public == {"prepare_adoption", "apply_adoption"}
    assert "write_text" not in source
    assert "write_bytes" not in source
    assert "mission.json" not in source
