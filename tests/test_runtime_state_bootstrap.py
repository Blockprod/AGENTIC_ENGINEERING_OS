import json
import subprocess
from dataclasses import replace
from inspect import signature
from pathlib import Path

import pytest

import agentic_engineering_os.infrastructure.project_state_store as state_store_module
from agentic_engineering_os.application import InitializationPlanner
from agentic_engineering_os.domain import (
    InitializationApplyStatus,
    MissionStateGitPolicy,
    ProjectConfiguration,
    RuntimeBootstrapStatus,
    RuntimeStoreDisposition,
    to_dict,
)
from agentic_engineering_os.infrastructure import (
    GitAdapter,
    GitOperationError,
    PersistenceError,
    ProjectConfigurationLoader,
    ProjectConfigurationValidator,
    ProjectStateStore,
    RepositoryInitializer,
    RepositoryReconnaissance,
    RuntimeStateBootstrap,
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


def configuration(
    *, mission_policy: MissionStateGitPolicy = MissionStateGitPolicy.TRACKED
) -> ProjectConfiguration:
    return ProjectConfigurationValidator().validate(
        {
            "config_version": "1.0",
            "project_id": "target",
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
            "mission_state_git_policy": mission_policy.value,
        }
    )


def eligible_repository(
    tmp_path: Path,
    *,
    mission_policy: MissionStateGitPolicy = MissionStateGitPolicy.TRACKED,
):
    root = tmp_path / "target"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "P5.7 Test")
    git(root, "config", "user.email", "p5.7@example.invalid")
    (root / "README.md").write_text("# Target\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    desired = configuration(mission_policy=mission_policy)
    profile = RepositoryReconnaissance().inspect(root)
    plan = InitializationPlanner().plan(profile, desired)
    assert RepositoryInitializer().apply(plan).status is InitializationApplyStatus.APPLIED
    git(root, "add", ".")
    git(root, "commit", "-m", "structural initialization")
    profile = RepositoryReconnaissance().inspect(root)
    assert profile.git.clean.value is True
    return root, desired, profile


def runtime_paths(root: Path) -> set[str]:
    directory = root / ".agentic-engineering-os"
    return {
        path.relative_to(root).as_posix()
        for path in directory.iterdir()
        if path.name != "config.json"
    }


def test_fresh_eligible_repository_bootstraps_only_empty_project_state(
    tmp_path: Path,
) -> None:
    root, config, profile = eligible_repository(tmp_path)
    head = git(root, "rev-parse", "HEAD")
    branch = git(root, "branch", "--show-current")
    index = root / ".git" / "index"
    index_before = (index.read_bytes(), index.stat().st_mtime_ns)

    result = RuntimeStateBootstrap().bootstrap(
        root, config, expected_profile=profile
    )
    state = ProjectStateStore(root).load()

    assert result.status is RuntimeBootstrapStatus.BOOTSTRAPPED
    assert result.expected_profile_fingerprint == InitializationPlanner.fingerprint(profile)
    assert result.created_paths == (".agentic-engineering-os/state.json",)
    assert runtime_paths(root) == {".agentic-engineering-os/state.json"}
    assert to_dict(state) == {
        "schema_version": "1.0",
        "user_stories": [],
        "evidence": [],
        "gates": [],
        "certifications": [],
        "audit_events": [],
    }
    assert git(root, "rev-parse", "HEAD") == head
    assert git(root, "branch", "--show-current") == branch
    assert (index.read_bytes(), index.stat().st_mtime_ns) == index_before


def test_second_bootstrap_is_idempotent_even_before_state_is_committed(
    tmp_path: Path,
) -> None:
    root, config, profile = eligible_repository(tmp_path)
    first = RuntimeStateBootstrap().bootstrap(root, config, expected_profile=profile)
    before = ProjectStateStore(root).state_path.read_bytes()
    current = RepositoryReconnaissance().inspect(root)

    second = RuntimeStateBootstrap().bootstrap(
        root, config, expected_profile=current
    )

    assert first.status is RuntimeBootstrapStatus.BOOTSTRAPPED
    assert second.status is RuntimeBootstrapStatus.ALREADY_BOOTSTRAPPED
    assert second.created_paths == ()
    assert ProjectStateStore(root).state_path.read_bytes() == before


def test_store_dispositions_define_one_required_and_no_fake_mission() -> None:
    assert RuntimeStateBootstrap.STORE_DISPOSITIONS == (
        (
            ".agentic-engineering-os/state.json",
            RuntimeStoreDisposition.REQUIRED_AT_BOOTSTRAP,
        ),
        (
            ".agentic-engineering-os/mission.json",
            RuntimeStoreDisposition.AUTHORIZED_EVENT_ONLY,
        ),
        (
            ".agentic-engineering-os/executions.json",
            RuntimeStoreDisposition.LAZY_INITIALIZED_ON_FIRST_USE,
        ),
        (
            ".agentic-engineering-os/negative-outcomes.json",
            RuntimeStoreDisposition.LAZY_INITIALIZED_ON_FIRST_USE,
        ),
        (
            ".agentic-engineering-os/worktrees.json",
            RuntimeStoreDisposition.LAZY_INITIALIZED_ON_FIRST_USE,
        ),
    )


def test_different_project_configuration_is_refused(tmp_path: Path) -> None:
    root, config, profile = eligible_repository(tmp_path)
    different = replace(config, project_id="different")

    invalid = RuntimeStateBootstrap().bootstrap(
        root, None, expected_profile=profile  # type: ignore[arg-type]
    )

    result = RuntimeStateBootstrap().bootstrap(
        root, different, expected_profile=profile
    )

    assert invalid.status is RuntimeBootstrapStatus.REFUSED
    assert invalid.findings[0].code.startswith("PREFLIGHT_FAILED:")
    assert result.status is RuntimeBootstrapStatus.REFUSED
    assert result.findings[0].code == "PROJECT_CONFIGURATION_MISMATCH"
    assert not ProjectStateStore(root).state_path.exists()


def test_stale_profile_is_refused_before_state_creation(tmp_path: Path) -> None:
    root, config, profile = eligible_repository(tmp_path)
    (root / "changed.txt").write_text("changed\n", encoding="utf-8")

    result = RuntimeStateBootstrap().bootstrap(
        root, config, expected_profile=profile
    )

    assert result.status is RuntimeBootstrapStatus.REFUSED
    assert result.findings[0].code == "STALE_OR_FOREIGN_PROFILE"
    assert not ProjectStateStore(root).state_path.exists()


def test_current_dirty_repository_is_refused_for_first_write(tmp_path: Path) -> None:
    root, config, _ = eligible_repository(tmp_path)
    (root / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    dirty = RepositoryReconnaissance().inspect(root)

    result = RuntimeStateBootstrap().bootstrap(
        root, config, expected_profile=dirty
    )

    assert result.status is RuntimeBootstrapStatus.REFUSED
    assert result.findings[0].code == "DIRTY_REPOSITORY"


@pytest.mark.parametrize("target", ["AGENTS.md", ".gitignore"])
def test_missing_or_tampered_structural_integration_is_refused(
    tmp_path: Path, target: str
) -> None:
    root, config, _ = eligible_repository(tmp_path)
    path = root / target
    if target == "AGENTS.md":
        path.unlink()
    else:
        path.write_text("# tampered\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-m", "break structural integration")
    profile = RepositoryReconnaissance().inspect(root)

    result = RuntimeStateBootstrap().bootstrap(
        root, config, expected_profile=profile
    )

    assert result.status is RuntimeBootstrapStatus.REFUSED
    assert result.findings[0].code == "STRUCTURAL_INITIALIZATION_REQUIRED"


def test_corrupt_existing_state_blocks_without_replacement(tmp_path: Path) -> None:
    root, config, _ = eligible_repository(tmp_path)
    state_path = ProjectStateStore(root).state_path
    state_path.write_bytes(b'{"schema_version":')
    corrupt = state_path.read_bytes()
    profile = RepositoryReconnaissance().inspect(root)

    result = RuntimeStateBootstrap().bootstrap(
        root, config, expected_profile=profile
    )

    assert result.status is RuntimeBootstrapStatus.REFUSED
    assert result.findings[0].code == "RUNTIME_STORE_INVALID"
    assert state_path.read_bytes() == corrupt


def test_unknown_state_version_requires_upgrade_without_replacement(
    tmp_path: Path,
) -> None:
    root, config, _ = eligible_repository(tmp_path)
    state_path = ProjectStateStore(root).state_path
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "999.0",
                "user_stories": [],
                "evidence": [],
                "gates": [],
                "certifications": [],
                "audit_events": [],
            }
        ),
        encoding="utf-8",
    )
    before = state_path.read_bytes()
    profile = RepositoryReconnaissance().inspect(root)

    result = RuntimeStateBootstrap().bootstrap(
        root, config, expected_profile=profile
    )

    assert result.status is RuntimeBootstrapStatus.REFUSED
    assert result.findings[0].code == "UPGRADE_REQUIRED"
    assert state_path.read_bytes() == before


def test_symlink_state_observation_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config, profile = eligible_repository(tmp_path)
    state_path = ProjectStateStore(root).state_path
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == state_path or original_is_symlink(path),
    )

    result = RuntimeStateBootstrap().bootstrap(
        root, config, expected_profile=profile
    )

    assert result.status is RuntimeBootstrapStatus.REFUSED
    assert not state_path.exists()


def test_state_appearing_during_store_initialize_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config, profile = eligible_repository(tmp_path)
    original_write = state_store_module._write_temporary
    racer = b"racer-owned-state\n"

    def race(directory: Path, text: str) -> Path:
        temporary = original_write(directory, text)
        (directory / "state.json").write_bytes(racer)
        return temporary

    monkeypatch.setattr(state_store_module, "_write_temporary", race)
    result = RuntimeStateBootstrap().bootstrap(
        root, config, expected_profile=profile
    )

    assert result.status is RuntimeBootstrapStatus.PARTIAL_FAILURE
    assert ProjectStateStore(root).state_path.read_bytes() == racer
    assert result.created_paths == ()
    assert not list(root.rglob(".state.*.tmp"))

    replay_profile = RepositoryReconnaissance().inspect(root)
    replay = RuntimeStateBootstrap().bootstrap(
        root, config, expected_profile=replay_profile
    )
    assert replay.status is RuntimeBootstrapStatus.REFUSED
    assert replay.findings[0].code == "RUNTIME_STORE_INVALID"
    assert ProjectStateStore(root).state_path.read_bytes() == racer


def test_failure_before_store_write_is_failed_without_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config, profile = eligible_repository(tmp_path)

    def fail_initialize(store: ProjectStateStore):
        raise PersistenceError("WRITE_FAILED", "simulated initialization failure")

    monkeypatch.setattr(ProjectStateStore, "initialize", fail_initialize)
    result = RuntimeStateBootstrap().bootstrap(
        root, config, expected_profile=profile
    )

    assert result.status is RuntimeBootstrapStatus.FAILED
    assert not ProjectStateStore(root).state_path.exists()


def test_failure_after_store_write_is_partial_and_not_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, config, profile = eligible_repository(tmp_path)

    def fail_load(store: ProjectStateStore):
        raise PersistenceError("READ_FAILED", "simulated verification failure")

    monkeypatch.setattr(ProjectStateStore, "load", fail_load)
    result = RuntimeStateBootstrap().bootstrap(
        root, config, expected_profile=profile
    )

    assert result.status is RuntimeBootstrapStatus.PARTIAL_FAILURE
    assert result.created_paths == (".agentic-engineering-os/state.json",)
    assert ProjectStateStore(root).state_path.exists()


def test_partial_lazy_footprint_before_state_blocks(tmp_path: Path) -> None:
    root, config, _ = eligible_repository(tmp_path)
    execution = root / ".agentic-engineering-os" / "executions.json"
    execution.write_text(
        '{"records":[],"schema_version":"1.1"}\n', encoding="utf-8"
    )
    git(root, "add", "-f", execution.relative_to(root).as_posix())
    git(root, "commit", "-m", "partial runtime")
    profile = RepositoryReconnaissance().inspect(root)

    result = RuntimeStateBootstrap().bootstrap(
        root, config, expected_profile=profile
    )

    assert result.status is RuntimeBootstrapStatus.REFUSED
    assert result.findings[0].code == "PARTIAL_RUNTIME_FOOTPRINT"


def test_actual_gitignore_semantics_must_cover_volatile_runtime(
    tmp_path: Path,
) -> None:
    root, config, _ = eligible_repository(tmp_path)
    with (root / ".gitignore").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("!.agentic-engineering-os/executions.json\n")
    git(root, "add", ".gitignore")
    git(root, "commit", "-m", "break volatile ignore policy")
    profile = RepositoryReconnaissance().inspect(root)

    result = RuntimeStateBootstrap().bootstrap(
        root, config, expected_profile=profile
    )

    assert result.status is RuntimeBootstrapStatus.REFUSED
    assert result.findings[0].code == "VOLATILE_RUNTIME_NOT_IGNORED"


def test_versioned_state_must_not_be_ignored(tmp_path: Path) -> None:
    root, config, _ = eligible_repository(tmp_path)
    with (root / ".gitignore").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(".agentic-engineering-os/*\n")
    git(root, "add", ".gitignore")
    git(root, "commit", "-m", "ignore authoritative state")
    profile = RepositoryReconnaissance().inspect(root)

    result = RuntimeStateBootstrap().bootstrap(
        root, config, expected_profile=profile
    )

    assert result.status is RuntimeBootstrapStatus.REFUSED
    assert result.findings[0].code == "VERSIONED_STATE_IS_IGNORED"


def test_ignored_mission_policy_requires_matching_gitignore_plan(
    tmp_path: Path,
) -> None:
    root, config, profile = eligible_repository(
        tmp_path, mission_policy=MissionStateGitPolicy.IGNORED
    )

    result = RuntimeStateBootstrap().bootstrap(
        root, config, expected_profile=profile
    )

    assert result.status is RuntimeBootstrapStatus.REFUSED
    assert result.findings[0].code == "MISSION_GIT_POLICY_MISMATCH"


def test_no_caller_state_or_mission_injection_api(tmp_path: Path) -> None:
    root, config, profile = eligible_repository(tmp_path)
    parameters = signature(RuntimeStateBootstrap.bootstrap).parameters

    assert "state" not in parameters
    assert "mission" not in parameters
    with pytest.raises(TypeError):
        RuntimeStateBootstrap().bootstrap(
            root,
            config,
            expected_profile=profile,
            state=object(),
        )


@pytest.mark.parametrize("path", ["../outside", "/absolute", "bad\\path", "./state"])
def test_git_ignore_observation_rejects_noncanonical_paths(
    tmp_path: Path, path: str
) -> None:
    root, _, _ = eligible_repository(tmp_path)

    with pytest.raises(GitOperationError) as caught:
        GitAdapter(root).is_ignored(path)

    assert caught.value.code == "INVALID_RELATIVE_PATH"
