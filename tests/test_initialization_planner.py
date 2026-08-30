import json
import subprocess
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from agentic_engineering_os.application import InitializationPlanner
from agentic_engineering_os.domain import (
    AGENTS_MANAGED_SECTION,
    GITIGNORE_MANAGED_SECTION,
    AgenticOsInitializationState,
    InitializationOperationType,
    ManagedSectionStatus,
    PlannedDesiredState,
    ProjectConfiguration,
)
from agentic_engineering_os.infrastructure import (
    ProjectConfigurationValidator,
    RepositoryReconnaissance,
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


def repository(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    root = tmp_path / "target"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "P5.4 Test")
    git(root, "config", "user.email", "p5.4@example.invalid")
    for relative, content in {"README.md": "# Target\n", **(files or {})}.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    return root


def configuration_candidate(project_id: str = "target") -> dict[str, object]:
    return {
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
        "mission_state_git_policy": "TRACKED",
    }


def configuration(project_id: str = "target") -> ProjectConfiguration:
    return ProjectConfigurationValidator().validate(configuration_candidate(project_id))


def initialize_fixture(
    root: Path,
    current: ProjectConfiguration,
    *,
    agents: str = AGENTS_MANAGED_SECTION,
    gitignore: str = GITIGNORE_MANAGED_SECTION,
) -> None:
    config = root / ".agentic-engineering-os" / "config.json"
    config.parent.mkdir()
    config.write_text(
        ProjectConfigurationValidator().serialize(current), encoding="utf-8"
    )
    (root / "AGENTS.md").write_text(agents, encoding="utf-8")
    (root / ".gitignore").write_text(gitignore, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "initialized")


def snapshot(root: Path) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold())
        if path.is_file() and ".git" not in path.relative_to(root).parts
    )


def test_uninitialized_repository_produces_complete_safe_create_plan(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    desired = configuration()
    profile = RepositoryReconnaissance().inspect(root)

    plan = InitializationPlanner().plan(profile, desired)

    assert profile.agentic_os.state is AgenticOsInitializationState.UNINITIALIZED
    assert plan.blockers == ()
    assert plan.ready_for_application is True
    assert [item.operation_type for item in plan.operations] == [
        InitializationOperationType.CREATE_DIRECTORY,
        InitializationOperationType.INITIALIZE_CONFIG,
        InitializationOperationType.CREATE_MANAGED_FILE,
        InitializationOperationType.CREATE_MANAGED_FILE,
    ]
    config_operation = next(
        item for item in plan.operations if item.target_path.endswith("config.json")
    )
    assert json.loads(config_operation.desired_content or "") == configuration_candidate()
    assert config_operation.desired_content_sha256 == plan.desired_configuration_sha256


def test_already_initialized_repository_is_idempotent_no_op(tmp_path: Path) -> None:
    root = repository(tmp_path)
    current = configuration()
    initialize_fixture(root, current)
    profile = RepositoryReconnaissance().inspect(root)

    plan = InitializationPlanner().plan(
        profile, current, current_configuration=current
    )

    assert profile.agentic_os.state is AgenticOsInitializationState.INITIALIZED
    assert plan.ready_for_application is True
    assert plan.blockers == ()
    assert plan.desired_configuration == current
    assert {item.operation_type for item in plan.operations} == {
        InitializationOperationType.NO_OP
    }
    assert all(item.desired_content is None for item in plan.operations)


def test_dirty_but_conforming_initialized_repository_can_only_plan_no_op(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    current = configuration()
    initialize_fixture(root, current)
    (root / "unrelated-dirty.txt").write_text("dirty\n", encoding="utf-8")
    profile = RepositoryReconnaissance().inspect(root)

    plan = InitializationPlanner().plan(
        profile, current, current_configuration=current
    )

    assert profile.git.clean.value is False
    assert plan.ready_for_application is True
    assert {item.operation_type for item in plan.operations} == {
        InitializationOperationType.NO_OP
    }


def test_repeated_planning_is_deterministic_and_immutable(tmp_path: Path) -> None:
    root = repository(tmp_path)
    profile = RepositoryReconnaissance().inspect(root)
    planner = InitializationPlanner()

    first = planner.plan(profile, configuration())
    second = planner.plan(profile, configuration())

    assert first == second
    assert first.profile_fingerprint == planner.fingerprint(profile)
    with pytest.raises(FrozenInstanceError):
        first.ready_for_application = False  # type: ignore[misc]


def test_planning_never_mutates_target_or_git_index(tmp_path: Path) -> None:
    root = repository(tmp_path)
    profile = RepositoryReconnaissance().inspect(root)
    before_files = snapshot(root)
    index = root / ".git" / "index"
    before_status = git(root, "status", "--porcelain=v1", "--untracked-files=all")
    before_index = (index.read_bytes(), index.stat().st_mtime_ns)

    InitializationPlanner().plan(profile, configuration())

    assert snapshot(root) == before_files
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before_index
    assert git(root, "status", "--porcelain=v1", "--untracked-files=all") == before_status


def test_existing_user_files_require_human_confirmation(tmp_path: Path) -> None:
    root = repository(
        tmp_path,
        {
            "AGENTS.md": "# User instructions\n",
            ".gitignore": "dist/\n",
        },
    )
    plan = InitializationPlanner().plan(
        RepositoryReconnaissance().inspect(root), configuration()
    )

    assert plan.blockers == ()
    assert plan.ready_for_application is False
    assert plan.required_human_confirmations == (
        "CONFIRM_MANAGED_SECTION:AGENTS.md",
        "CONFIRM_MANAGED_SECTION:.gitignore",
    )
    human_operations = [
        item for item in plan.operations if item.human_confirmation_required
    ]
    assert {item.operation_type for item in human_operations} == {
        InitializationOperationType.UPDATE_MANAGED_SECTION,
        InitializationOperationType.ADD_GITIGNORE_SECTION,
    }


def test_missing_gitignore_is_planned_as_safe_managed_file_creation(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path, {"AGENTS.md": "# User instructions\n"})
    plan = InitializationPlanner().plan(
        RepositoryReconnaissance().inspect(root), configuration()
    )

    operation = next(item for item in plan.operations if item.target_path == ".gitignore")
    assert operation.operation_type is InitializationOperationType.CREATE_MANAGED_FILE
    assert operation.human_confirmation_required is False


def test_expected_profile_fingerprint_rejects_stale_snapshot(tmp_path: Path) -> None:
    root = repository(tmp_path)
    profile = RepositoryReconnaissance().inspect(root)

    plan = InitializationPlanner().plan(
        profile,
        configuration(),
        expected_profile_fingerprint="0" * 64,
    )

    assert "STALE_PROFILE" in {item.code for item in plan.blockers}
    assert plan.ready_for_application is False


def test_profile_fingerprint_changes_with_relevant_repository_fact(tmp_path: Path) -> None:
    root = repository(tmp_path)
    planner = InitializationPlanner()
    before = RepositoryReconnaissance().inspect(root)
    (root / "README.md").write_text("changed\n", encoding="utf-8")
    after = RepositoryReconnaissance().inspect(root)

    assert planner.fingerprint(before) != planner.fingerprint(after)


def test_dirty_repository_is_a_hard_blocker(tmp_path: Path) -> None:
    root = repository(tmp_path)
    (root / "dirty.txt").write_text("dirty", encoding="utf-8")

    plan = InitializationPlanner().plan(
        RepositoryReconnaissance().inspect(root), configuration()
    )

    assert "DIRTY_REPOSITORY" in {item.code for item in plan.blockers}
    assert all(
        item.operation_type is InitializationOperationType.BLOCKED_CONFLICT
        for item in plan.operations
    )


@pytest.mark.parametrize(
    ("config_text", "expected_code"),
    [
        ("{", "CURRENT_CONFIG_INVALID"),
        ('{"config_version":"9.0"}', "CURRENT_CONFIG_UPGRADE_REQUIRED"),
    ],
)
def test_invalid_or_unknown_current_configuration_blocks_without_repair(
    tmp_path: Path, config_text: str, expected_code: str
) -> None:
    root = repository(tmp_path)
    path = root / ".agentic-engineering-os" / "config.json"
    path.parent.mkdir()
    path.write_text(config_text, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "bad config")

    plan = InitializationPlanner().plan(
        RepositoryReconnaissance().inspect(root), configuration()
    )

    assert expected_code in {item.code for item in plan.blockers}
    assert plan.ready_for_application is False


def test_existing_different_valid_configuration_is_never_overwritten(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    current = configuration("current")
    initialize_fixture(root, current)

    plan = InitializationPlanner().plan(
        RepositoryReconnaissance().inspect(root),
        configuration("desired"),
        current_configuration=current,
    )

    assert "EXISTING_CONFIG_CONFLICT" in {item.code for item in plan.blockers}
    assert not any(item.desired_content for item in plan.operations)


@pytest.mark.parametrize(
    ("target", "tampered"),
    [
        (
            "AGENTS.md",
            AGENTS_MANAGED_SECTION.replace("Control Plane", "altered Control Plane"),
        ),
        (
            ".gitignore",
            GITIGNORE_MANAGED_SECTION.replace("executions.json", "other.json", 1),
        ),
    ],
)
def test_managed_section_tampering_is_a_hard_blocker(
    tmp_path: Path, target: str, tampered: str
) -> None:
    root = repository(tmp_path)
    current = configuration()
    initialize_fixture(
        root,
        current,
        agents=tampered if target == "AGENTS.md" else AGENTS_MANAGED_SECTION,
        gitignore=tampered if target == ".gitignore" else GITIGNORE_MANAGED_SECTION,
    )
    profile = RepositoryReconnaissance().inspect(root)

    plan = InitializationPlanner().plan(
        profile, current, current_configuration=current
    )

    observation = (
        profile.agentic_os.agents_managed_section
        if target == "AGENTS.md"
        else profile.agentic_os.gitignore_managed_section
    )
    assert observation.status is ManagedSectionStatus.TAMPERED
    assert any(item.target_path == target for item in plan.blockers)


def test_duplicate_managed_markers_are_ambiguous_and_blocked(tmp_path: Path) -> None:
    root = repository(
        tmp_path,
        {"AGENTS.md": AGENTS_MANAGED_SECTION + AGENTS_MANAGED_SECTION},
    )
    profile = RepositoryReconnaissance().inspect(root)

    plan = InitializationPlanner().plan(profile, configuration())

    assert profile.agentic_os.agents_managed_section.status is ManagedSectionStatus.AMBIGUOUS
    assert "AGENTS_MANAGED_SECTION_CONFLICT" in {item.code for item in plan.blockers}


def test_partial_agentic_footprint_is_not_completed(tmp_path: Path) -> None:
    root = repository(tmp_path)
    state = root / ".agentic-engineering-os" / "state.json"
    state.parent.mkdir()
    state.write_text('{"schema_version":"1.0"}', encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "partial")

    plan = InitializationPlanner().plan(
        RepositoryReconnaissance().inspect(root), configuration()
    )

    assert "PARTIAL_OR_INCONSISTENT_STATE" in {item.code for item in plan.blockers}
    assert all(item.desired_content is None for item in plan.operations)


def test_symlink_managed_target_is_never_planned_for_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository(tmp_path, {"AGENTS.md": "placeholder\n"})
    agents = root / "AGENTS.md"
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == agents or original_is_symlink(path),
    )

    plan = InitializationPlanner().plan(
        RepositoryReconnaissance().inspect(root), configuration()
    )

    assert "AGENTS_MANAGED_SECTION_CONFLICT" in {item.code for item in plan.blockers}
    assert all(item.desired_content is None for item in plan.operations)


def test_forged_repository_root_mismatch_is_rejected(tmp_path: Path) -> None:
    root = repository(tmp_path)
    profile = RepositoryReconnaissance().inspect(root)
    forged = replace(profile, requested_root=str(tmp_path / "outside"))

    plan = InitializationPlanner().plan(forged, configuration())

    assert "INCONSISTENT_OR_FORGED_PROFILE" in {item.code for item in plan.blockers}


def test_forged_managed_target_outside_repository_is_rejected(tmp_path: Path) -> None:
    root = repository(tmp_path)
    profile = RepositoryReconnaissance().inspect(root)
    forged_agents = replace(
        profile.agentic_os.agents_managed_section,
        relative_path="../outside/AGENTS.md",
    )
    forged_state = replace(profile.agentic_os, agents_managed_section=forged_agents)
    forged = replace(profile, agentic_os=forged_state)

    plan = InitializationPlanner().plan(forged, configuration())

    assert "INCONSISTENT_MANAGED_TARGET_OBSERVATION" in {
        item.code for item in plan.blockers
    }


def test_forged_current_configuration_mismatch_is_rejected(tmp_path: Path) -> None:
    root = repository(tmp_path)
    current = configuration("current")
    initialize_fixture(root, current)

    plan = InitializationPlanner().plan(
        RepositoryReconnaissance().inspect(root),
        current,
        current_configuration=configuration("forged"),
    )

    assert "CURRENT_CONFIGURATION_MISMATCH" in {item.code for item in plan.blockers}


def test_inference_never_replaces_explicit_desired_configuration(
    tmp_path: Path,
) -> None:
    root = repository(
        tmp_path,
        {"pyproject.toml": "[project]\nname='demo'\nversion='1'\n"},
    )
    profile = RepositoryReconnaissance().inspect(root)

    plan = InitializationPlanner().plan(profile, None)

    assert [item.identity for item in profile.toolchains] == ["python"]
    assert "MISSING_OR_INVALID_DESIRED_CONFIGURATION" in {
        item.code for item in plan.blockers
    }


def test_forged_invalid_desired_domain_object_is_rejected(tmp_path: Path) -> None:
    root = repository(tmp_path)
    desired = replace(configuration(), config_version="9.0")

    plan = InitializationPlanner().plan(
        RepositoryReconnaissance().inspect(root), desired
    )

    assert "MISSING_OR_INVALID_DESIRED_CONFIGURATION" in {
        item.code for item in plan.blockers
    }


def test_expected_footprint_marks_runtime_as_deferred_not_created(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    plan = InitializationPlanner().plan(
        RepositoryReconnaissance().inspect(root), configuration()
    )

    runtime = next(
        item for item in plan.expected_footprint if item.relative_path.endswith("state.json")
    )
    assert runtime.deferred is True
    assert runtime.expected_state is PlannedDesiredState.RUNTIME_INITIALIZATION_DEFERRED
    assert not any(item.target_path.endswith("state.json") for item in plan.operations)


def test_operation_catalog_has_no_arbitrary_command_primitive() -> None:
    assert "RUN_ARBITRARY_COMMAND" not in {
        item.value for item in InitializationOperationType
    }
