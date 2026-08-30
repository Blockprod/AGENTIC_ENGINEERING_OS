import subprocess
from dataclasses import replace
from inspect import signature
from pathlib import Path

import pytest

import agentic_engineering_os.infrastructure.repository_initializer as initializer_module
from agentic_engineering_os.application import InitializationPlanner
from agentic_engineering_os.domain import (
    AGENTS_MANAGED_SECTION,
    GITIGNORE_MANAGED_SECTION,
    AgenticOsInitializationState,
    HumanOperationConfirmation,
    InitializationApplyStatus,
    InitializationOperationType,
    OperationApplyStatus,
    PlannedCurrentState,
    ProjectConfiguration,
)
from agentic_engineering_os.infrastructure import (
    ProjectConfigurationLoader,
    ProjectConfigurationValidator,
    RepositoryInitializer,
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
    git(root, "config", "user.name", "P5.5 Test")
    git(root, "config", "user.email", "p5.5@example.invalid")
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


def plan_for(root: Path, desired: ProjectConfiguration | None = None):
    desired = desired or configuration()
    profile = RepositoryReconnaissance().inspect(root)
    current = (
        ProjectConfigurationLoader(root).load()
        if profile.agentic_os.config_status.value == "VALID"
        else None
    )
    return InitializationPlanner().plan(
        profile, desired, current_configuration=current
    )


def confirmation(plan, operation, *, actor: str = "Human/Alice"):
    return HumanOperationConfirmation(
        plan_fingerprint=plan.input_fingerprint,
        operation_id=operation.operation_id,
        target_path=operation.target_path,
        expected_current_state=operation.expected_current_state,
        confirmed_by=actor,
    )


def non_git_snapshot(root: Path) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold())
        if path.is_file() and ".git" not in path.relative_to(root).parts
    )


def test_fresh_repository_apply_is_verified_and_has_no_git_mutation(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    desired = configuration()
    plan = plan_for(root, desired)
    head = git(root, "rev-parse", "HEAD")
    branch = git(root, "branch", "--show-current")
    index = root / ".git" / "index"
    index_before = (index.read_bytes(), index.stat().st_mtime_ns)

    result = RepositoryInitializer().apply(plan)

    assert result.status is InitializationApplyStatus.APPLIED
    assert all(
        item.status is OperationApplyStatus.APPLIED
        for item in result.operation_results
    )
    assert ProjectConfigurationLoader(root).load() == desired
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == AGENTS_MANAGED_SECTION
    assert (root / ".gitignore").read_text(encoding="utf-8") == GITIGNORE_MANAGED_SECTION
    assert not (root / ".agentic-engineering-os" / "state.json").exists()
    assert git(root, "rev-parse", "HEAD") == head
    assert git(root, "branch", "--show-current") == branch
    assert (index.read_bytes(), index.stat().st_mtime_ns) == index_before


def test_reconnaissance_and_replan_after_apply_are_idempotent(tmp_path: Path) -> None:
    root = repository(tmp_path)
    desired = configuration()
    original = plan_for(root, desired)
    assert RepositoryInitializer().apply(original).status is InitializationApplyStatus.APPLIED

    profile = RepositoryReconnaissance().inspect(root)
    replay_plan = InitializationPlanner().plan(
        profile,
        desired,
        current_configuration=ProjectConfigurationLoader(root).load(),
    )
    replay_result = RepositoryInitializer().apply(replay_plan)

    assert profile.agentic_os.state is AgenticOsInitializationState.INITIALIZED
    assert {item.operation_type for item in replay_plan.operations} == {
        InitializationOperationType.NO_OP
    }
    assert replay_result.status is InitializationApplyStatus.NO_OP
    assert all(
        item.status is OperationApplyStatus.NO_OP
        for item in replay_result.operation_results
    )


def test_same_applied_create_plan_replay_is_refused_without_write(tmp_path: Path) -> None:
    root = repository(tmp_path)
    plan = plan_for(root)
    initializer = RepositoryInitializer()
    assert initializer.apply(plan).status is InitializationApplyStatus.APPLIED
    before = non_git_snapshot(root)

    replay = initializer.apply(plan)

    assert replay.status is InitializationApplyStatus.REFUSED
    assert replay.findings[0].code == "UNTRUSTED_OR_STALE_PLAN"
    assert non_git_snapshot(root) == before


def test_existing_gitignore_is_appended_only_with_exact_human_confirmation(
    tmp_path: Path,
) -> None:
    original = b"dist/\r\n# user bytes\r\n"
    root = repository(tmp_path, {".gitignore": original.decode("utf-8")})
    (root / ".gitignore").write_bytes(original)
    git(root, "add", ".gitignore")
    git(root, "commit", "-m", "preserve crlf")
    plan = plan_for(root)
    operation = next(item for item in plan.operations if item.human_confirmation_required)

    result = RepositoryInitializer().apply(
        plan, human_confirmations=(confirmation(plan, operation),)
    )

    actual = (root / ".gitignore").read_bytes()
    assert result.status is InitializationApplyStatus.APPLIED
    assert actual.startswith(original)
    assert actual.count(GITIGNORE_MANAGED_SECTION.encode("utf-8")) == 1
    assert RepositoryReconnaissance().inspect(root).agentic_os.state is AgenticOsInitializationState.INITIALIZED


def test_missing_human_confirmation_refuses_before_any_write(tmp_path: Path) -> None:
    root = repository(tmp_path, {".gitignore": "dist/\n"})
    plan = plan_for(root)
    before = non_git_snapshot(root)

    result = RepositoryInitializer().apply(plan)

    assert result.status is InitializationApplyStatus.REFUSED
    assert result.findings[0].code == "MISSING_HUMAN_CONFIRMATION"
    assert non_git_snapshot(root) == before


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: replace(value, plan_fingerprint="0" * 64),
        lambda value: replace(value, operation_id="OP-999"),
        lambda value: replace(value, target_path="AGENTS.md"),
        lambda value: replace(value, expected_current_state=PlannedCurrentState.UNKNOWN),
    ],
)
def test_wrong_confirmation_binding_is_refused(
    tmp_path: Path, mutator
) -> None:
    root = repository(tmp_path, {".gitignore": "dist/\n"})
    plan = plan_for(root)
    operation = next(item for item in plan.operations if item.human_confirmation_required)

    result = RepositoryInitializer().apply(
        plan, human_confirmations=(mutator(confirmation(plan, operation)),)
    )

    assert result.status is InitializationApplyStatus.REFUSED
    assert result.findings[0].code in {
        "HUMAN_CONFIRMATION_BINDING_MISMATCH",
        "UNEXPECTED_HUMAN_CONFIRMATION",
    }


@pytest.mark.parametrize(
    "actor",
    ["Codex/FakeHuman", "codex/FakeHuman", "CODEX/FakeHuman", "CoDeX/FakeHuman"],
)
def test_codex_identity_cannot_confirm_human_operation(
    tmp_path: Path, actor: str
) -> None:
    root = repository(tmp_path, {".gitignore": "dist/\n"})
    plan = plan_for(root)
    operation = next(item for item in plan.operations if item.human_confirmation_required)

    result = RepositoryInitializer().apply(
        plan,
        human_confirmations=(confirmation(plan, operation, actor=actor),),
    )

    assert result.status is InitializationApplyStatus.REFUSED
    assert result.findings[0].code == "INVALID_HUMAN_IDENTITY"


def test_existing_agents_update_remains_deferred_even_when_confirmed(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path, {"AGENTS.md": "# User-owned instructions\n"})
    plan = plan_for(root)
    operation = next(item for item in plan.operations if item.human_confirmation_required)
    before = non_git_snapshot(root)

    result = RepositoryInitializer().apply(
        plan, human_confirmations=(confirmation(plan, operation),)
    )

    assert result.status is InitializationApplyStatus.REFUSED
    assert result.findings[0].code == "UNSUPPORTED_HUMAN_OPERATION"
    assert non_git_snapshot(root) == before


def test_forged_operation_and_path_traversal_are_refused(tmp_path: Path) -> None:
    root = repository(tmp_path)
    plan = plan_for(root)
    forged_operation = replace(plan.operations[0], target_path="../outside")
    forged = replace(plan, operations=(forged_operation, *plan.operations[1:]))

    result = RepositoryInitializer().apply(forged)

    assert result.status is InitializationApplyStatus.REFUSED
    assert result.findings[0].code == "UNTRUSTED_OR_STALE_PLAN"
    assert not (tmp_path / "outside").exists()


def test_stale_head_is_refused_before_write(tmp_path: Path) -> None:
    root = repository(tmp_path)
    plan = plan_for(root)
    (root / "new.txt").write_text("new\n", encoding="utf-8")
    git(root, "add", "new.txt")
    git(root, "commit", "-m", "advance")

    result = RepositoryInitializer().apply(plan)

    assert result.status is InitializationApplyStatus.REFUSED
    assert result.findings[0].code == "UNTRUSTED_OR_STALE_PLAN"
    assert not (root / ".agentic-engineering-os").exists()


def test_dirty_repository_after_planning_is_refused(tmp_path: Path) -> None:
    root = repository(tmp_path)
    plan = plan_for(root)
    (root / "dirty.txt").write_text("dirty", encoding="utf-8")

    result = RepositoryInitializer().apply(plan)

    assert result.status is InitializationApplyStatus.REFUSED
    assert not (root / ".agentic-engineering-os").exists()


def test_file_appearing_after_plan_is_never_overwritten(tmp_path: Path) -> None:
    root = repository(tmp_path)
    plan = plan_for(root)
    config = root / ".agentic-engineering-os" / "config.json"
    config.parent.mkdir()
    config.write_text("user-created\n", encoding="utf-8")

    result = RepositoryInitializer().apply(plan)

    assert result.status is InitializationApplyStatus.REFUSED
    assert config.read_text(encoding="utf-8") == "user-created\n"


def test_changed_config_after_noop_plan_is_refused_without_overwrite(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    desired = configuration()
    assert RepositoryInitializer().apply(plan_for(root, desired)).status is InitializationApplyStatus.APPLIED
    noop = plan_for(root, desired)
    different = ProjectConfigurationValidator().serialize(configuration("different"))
    config = root / ".agentic-engineering-os" / "config.json"
    config.write_text(different, encoding="utf-8")

    result = RepositoryInitializer().apply(noop)

    assert result.status is InitializationApplyStatus.REFUSED
    assert config.read_text(encoding="utf-8") == different


def test_symlink_target_observation_refuses_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository(tmp_path)
    plan = plan_for(root)
    agents = root / "AGENTS.md"
    agents.write_text("outside-like\n", encoding="utf-8")
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == agents or original_is_symlink(path),
    )

    result = RepositoryInitializer().apply(plan)

    assert result.status is InitializationApplyStatus.REFUSED
    assert result.findings[0].code == "UNTRUSTED_OR_STALE_PLAN"


@pytest.mark.parametrize("tampered", ["duplicate", "edited"])
def test_duplicate_or_tampered_section_after_plan_is_refused(
    tmp_path: Path, tampered: str
) -> None:
    root = repository(tmp_path)
    desired = configuration()
    assert RepositoryInitializer().apply(plan_for(root, desired)).status is InitializationApplyStatus.APPLIED
    noop = plan_for(root, desired)
    agents = root / "AGENTS.md"
    agents.write_text(
        AGENTS_MANAGED_SECTION + AGENTS_MANAGED_SECTION
        if tampered == "duplicate"
        else AGENTS_MANAGED_SECTION.replace("Control Plane", "changed"),
        encoding="utf-8",
    )

    result = RepositoryInitializer().apply(noop)

    assert result.status is InitializationApplyStatus.REFUSED


def test_temporary_write_failure_stops_and_leaves_partial_state_observable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository(tmp_path)
    plan = plan_for(root)

    def fail_write(directory: Path, content: bytes) -> Path:
        raise OSError("simulated pre-install failure")

    monkeypatch.setattr(initializer_module, "_write_temporary_file", fail_write)
    result = RepositoryInitializer().apply(plan)

    assert result.status is InitializationApplyStatus.PARTIAL_FAILURE
    assert result.operation_results[0].status is OperationApplyStatus.APPLIED
    assert result.operation_results[1].status is OperationApplyStatus.FAILED
    assert all(
        item.status is OperationApplyStatus.NOT_ATTEMPTED
        for item in result.operation_results[2:]
    )
    assert RepositoryReconnaissance().inspect(root).agentic_os.state is AgenticOsInitializationState.PARTIAL_OR_INCONSISTENT
    assert not list(root.rglob(".agentic-init.*.tmp"))


def test_later_operation_failure_preserves_earlier_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository(tmp_path)
    plan = plan_for(root)
    original_create = initializer_module._exclusive_create

    def fail_agents(path: Path, content: bytes) -> None:
        if path.name == "AGENTS.md":
            raise OSError("simulated later failure")
        original_create(path, content)

    monkeypatch.setattr(initializer_module, "_exclusive_create", fail_agents)
    result = RepositoryInitializer().apply(plan)

    assert result.status is InitializationApplyStatus.PARTIAL_FAILURE
    assert ProjectConfigurationLoader(root).load() == configuration()
    assert not (root / "AGENTS.md").exists()
    assert result.operation_results[2].status is OperationApplyStatus.FAILED


def test_git_head_change_between_operations_stops_partial_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository(tmp_path)
    plan = plan_for(root)
    original_create = initializer_module._exclusive_create
    advanced = False

    def advance_after_config(path: Path, content: bytes) -> None:
        nonlocal advanced
        original_create(path, content)
        if path.name == "config.json" and not advanced:
            advanced = True
            (root / "external.txt").write_text("external\n", encoding="utf-8")
            git(root, "add", "external.txt")
            git(root, "commit", "-m", "external advance")

    monkeypatch.setattr(initializer_module, "_exclusive_create", advance_after_config)
    result = RepositoryInitializer().apply(plan)

    assert result.status is InitializationApplyStatus.PARTIAL_FAILURE
    assert result.operation_results[2].status is OperationApplyStatus.FAILED
    assert result.operation_results[2].detail.startswith("Git root, HEAD")
    assert not (root / "AGENTS.md").exists()


def test_replace_failure_preserves_original_gitignore_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository(tmp_path, {".gitignore": "dist/\n"})
    plan = plan_for(root)
    operation = next(item for item in plan.operations if item.human_confirmation_required)
    original = (root / ".gitignore").read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(initializer_module.os, "replace", fail_replace)
    result = RepositoryInitializer().apply(
        plan, human_confirmations=(confirmation(plan, operation),)
    )

    assert result.status is InitializationApplyStatus.PARTIAL_FAILURE
    assert (root / ".gitignore").read_bytes() == original
    assert not list(root.rglob(".agentic-init.*.tmp"))


def test_race_creating_config_after_temp_write_is_refused_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository(tmp_path)
    plan = plan_for(root)
    original_write = initializer_module._write_temporary_file
    raced = False

    def race(directory: Path, content: bytes) -> Path:
        nonlocal raced
        temporary = original_write(directory, content)
        if directory.name == ".agentic-engineering-os" and not raced:
            raced = True
            (directory / "config.json").write_text("racer\n", encoding="utf-8")
        return temporary

    monkeypatch.setattr(initializer_module, "_write_temporary_file", race)
    result = RepositoryInitializer().apply(plan)

    assert result.status is InitializationApplyStatus.PARTIAL_FAILURE
    assert (root / ".agentic-engineering-os" / "config.json").read_text(
        encoding="utf-8"
    ) == "racer\n"
    assert not list(root.rglob(".agentic-init.*.tmp"))


def test_initializer_source_has_no_git_mutation_or_arbitrary_write_api() -> None:
    source = Path(initializer_module.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "RUN_ARBITRARY_COMMAND" not in source
    assert all(
        command not in source
        for command in ('"commit"', '"checkout"', '"reset"', '"stash"')
    )
    assert list(signature(RepositoryInitializer).parameters) == []
