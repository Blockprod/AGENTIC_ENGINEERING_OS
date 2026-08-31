import subprocess
from dataclasses import replace
from inspect import signature
from pathlib import Path

import pytest

import agentic_engineering_os.infrastructure.agents_integration as integration_module
from agentic_engineering_os.application import InitializationPlanner
from agentic_engineering_os.domain import (
    AGENTS_MANAGED_SECTION,
    AGENTS_MANAGED_SECTION_VERSION,
    AGENTS_SECTION_END,
    AGENTS_SECTION_START,
    HumanOperationConfirmation,
    InitializationApplyStatus,
    InitializationOperationType,
    ManagedSectionStatus,
    ProjectConfiguration,
)
from agentic_engineering_os.infrastructure import (
    AgentsIntegrationError,
    AgentsIntegrationService,
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


def repository(tmp_path: Path, *, agents: bytes | None = None) -> Path:
    root = tmp_path / "target"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "P5.6 Test")
    git(root, "config", "user.email", "p5.6@example.invalid")
    (root / "README.md").write_text("# Target\n", encoding="utf-8")
    if agents is not None:
        (root / "AGENTS.md").write_bytes(agents)
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    return root


def configuration() -> ProjectConfiguration:
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
            "mission_state_git_policy": "TRACKED",
        }
    )


def plan_for(root: Path):
    desired = configuration()
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
    assert operation.expected_target_fingerprint is not None
    return HumanOperationConfirmation(
        plan_fingerprint=plan.input_fingerprint,
        operation_id=operation.operation_id,
        target_path=operation.target_path,
        expected_current_state=operation.expected_current_state,
        expected_target_fingerprint=operation.expected_target_fingerprint,
        confirmed_by=actor,
    )


def test_inspection_handles_absent_user_and_current_states() -> None:
    service = AgentsIntegrationService()

    assert service.inspect(None).status is ManagedSectionStatus.FILE_ABSENT
    assert service.inspect(b"# User instructions\n").status is ManagedSectionStatus.SECTION_ABSENT
    current = service.inspect(AGENTS_MANAGED_SECTION.encode("utf-8"))
    assert current.status is ManagedSectionStatus.CURRENT
    assert current.managed_version == AGENTS_MANAGED_SECTION_VERSION


@pytest.mark.parametrize(
    "content",
    [
        AGENTS_SECTION_START + "\ncontent\n",
        "content\n" + AGENTS_SECTION_END + "\n",
        AGENTS_MANAGED_SECTION + AGENTS_MANAGED_SECTION,
        AGENTS_SECTION_START
        + "\n<!-- BEGIN AGENTIC_ENGINEERING_OS MANAGED SECTION v1 -->\n"
        + AGENTS_SECTION_END,
        "<!--  BEGIN AGENTIC_ENGINEERING_OS MANAGED SECTION v2 -->\n",
        "<!-- begin agentic_engineering_os managed section v2 -->\n",
    ],
)
def test_partial_duplicate_nested_or_spoofed_markers_are_ambiguous(
    content: str,
) -> None:
    assert (
        AgentsIntegrationService().inspect(content.encode("utf-8")).status
        is ManagedSectionStatus.AMBIGUOUS
    )


@pytest.mark.parametrize("version", ["1", "3", "999"])
def test_older_and_future_versions_require_upgrade(version: str) -> None:
    content = (
        f"<!-- BEGIN AGENTIC_ENGINEERING_OS MANAGED SECTION v{version} -->\n"
        "versioned content\n"
        f"<!-- END AGENTIC_ENGINEERING_OS MANAGED SECTION v{version} -->\n"
    )

    inspection = AgentsIntegrationService().inspect(content.encode("utf-8"))

    assert inspection.status is ManagedSectionStatus.UPGRADE_REQUIRED
    assert inspection.managed_version == version


def test_reconnaissance_and_planner_block_unknown_agents_version(
    tmp_path: Path,
) -> None:
    future = (
        b"<!-- BEGIN AGENTIC_ENGINEERING_OS MANAGED SECTION v999 -->\n"
        b"future\n"
        b"<!-- END AGENTIC_ENGINEERING_OS MANAGED SECTION v999 -->\n"
    )
    root = repository(tmp_path, agents=future)
    profile = RepositoryReconnaissance().inspect(root)

    plan = InitializationPlanner().plan(profile, configuration())

    assert (
        profile.agentic_os.agents_managed_section.status
        is ManagedSectionStatus.UPGRADE_REQUIRED
    )
    assert "UPGRADE_REQUIRED" in {finding.code for finding in plan.blockers}
    assert all(
        operation.operation_type is InitializationOperationType.BLOCKED_CONFLICT
        for operation in plan.operations
    )


def test_same_version_altered_content_is_tampered() -> None:
    altered = AGENTS_MANAGED_SECTION.replace(
        "Repository and Git truth", "Repository conversation"
    )
    assert altered != AGENTS_MANAGED_SECTION
    assert (
        AgentsIntegrationService().inspect(altered.encode("utf-8")).status
        is ManagedSectionStatus.TAMPERED
    )


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_existing_user_bytes_and_newline_convention_are_preserved(
    tmp_path: Path, newline: bytes
) -> None:
    root = tmp_path / "target"
    root.mkdir()
    original = "# Règles utilisateur\n\nTexte 日本語\n".encode("utf-8").replace(
        b"\n", newline
    )
    target = root / "AGENTS.md"
    target.write_bytes(original)
    fingerprint = AgentsIntegrationService().inspect(original).content_fingerprint
    assert fingerprint is not None

    AgentsIntegrationService().integrate_from_plan(
        root,
        expected_fingerprint=fingerprint,
        planned_content=AGENTS_MANAGED_SECTION,
    )

    actual = target.read_bytes()
    assert actual.startswith(original)
    assert actual[: len(original)] == original
    assert AGENTS_MANAGED_SECTION.encode("utf-8").replace(b"\n", newline) in actual
    assert AgentsIntegrationService().inspect(actual).status is ManagedSectionStatus.CURRENT


def test_absent_agents_file_is_created_safely_without_human_confirmation(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    plan = plan_for(root)
    assert not any(
        operation.human_confirmation_required
        and operation.target_path == "AGENTS.md"
        for operation in plan.operations
    )

    result = RepositoryInitializer().apply(plan)

    assert result.status is InitializationApplyStatus.APPLIED
    assert (root / "AGENTS.md").read_bytes() == AGENTS_MANAGED_SECTION.encode("utf-8")


def test_existing_agents_integration_is_confirmed_preserved_and_idempotent(
    tmp_path: Path,
) -> None:
    original = b"# User before\r\n\r\nUnicode: \xe2\x98\x83\r\n"
    root = repository(tmp_path, agents=original)
    plan = plan_for(root)
    operation = next(
        item
        for item in plan.operations
        if item.operation_type is InitializationOperationType.UPDATE_MANAGED_SECTION
    )
    head_before = git(root, "rev-parse", "HEAD")
    branch_before = git(root, "branch", "--show-current")

    result = RepositoryInitializer().apply(
        plan, human_confirmations=(confirmation(plan, operation),)
    )
    current = (root / "AGENTS.md").read_bytes()
    replanned = plan_for(root)
    replay = RepositoryInitializer().apply(replanned)

    assert result.status is InitializationApplyStatus.APPLIED
    assert current.startswith(original)
    assert replay.status is InitializationApplyStatus.NO_OP
    assert git(root, "rev-parse", "HEAD") == head_before
    assert git(root, "branch", "--show-current") == branch_before


def test_file_changed_after_confirmation_is_refused_without_overwrite(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path, agents=b"# User\n")
    plan = plan_for(root)
    operation = next(item for item in plan.operations if item.human_confirmation_required)
    approval = confirmation(plan, operation)
    changed = b"# User changed after confirmation\n"
    (root / "AGENTS.md").write_bytes(changed)

    result = RepositoryInitializer().apply(
        plan, human_confirmations=(approval,)
    )

    assert result.status is InitializationApplyStatus.REFUSED
    assert (root / "AGENTS.md").read_bytes() == changed


def test_confirmation_with_wrong_file_fingerprint_is_refused(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path, agents=b"# User\n")
    plan = plan_for(root)
    operation = next(item for item in plan.operations if item.human_confirmation_required)
    approval = replace(
        confirmation(plan, operation), expected_target_fingerprint="0" * 64
    )

    result = RepositoryInitializer().apply(
        plan, human_confirmations=(approval,)
    )

    assert result.status is InitializationApplyStatus.REFUSED
    assert result.findings[0].code == "HUMAN_CONFIRMATION_BINDING_MISMATCH"


@pytest.mark.parametrize(
    "actor",
    [
        "Codex/FakeHuman",
        "codex/FakeHuman",
        "CODEX/FakeHuman",
        "CoDeX/FakeHuman",
        "Ｃｏｄｅｘ/FakeHuman",
        "Codex\u200b/FakeHuman",
    ],
)
def test_codex_identity_cannot_confirm_agents_integration(
    tmp_path: Path, actor: str
) -> None:
    root = repository(tmp_path, agents=b"# User\n")
    plan = plan_for(root)
    operation = next(item for item in plan.operations if item.human_confirmation_required)

    result = RepositoryInitializer().apply(
        plan,
        human_confirmations=(confirmation(plan, operation, actor=actor),),
    )

    assert result.status is InitializationApplyStatus.REFUSED
    assert result.findings[0].code == "INVALID_HUMAN_IDENTITY"


def test_symlink_agents_observation_blocks_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository(tmp_path, agents=b"# User\n")
    plan = plan_for(root)
    target = root / "AGENTS.md"
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == target or original_is_symlink(path),
    )

    result = RepositoryInitializer().apply(plan)

    assert result.status is InitializationApplyStatus.REFUSED
    assert target.read_bytes() == b"# User\n"


def test_replace_failure_preserves_user_file_and_cleans_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = b"# User\n"
    root = repository(tmp_path, agents=original)
    plan = plan_for(root)
    operation = next(item for item in plan.operations if item.human_confirmation_required)

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("simulated AGENTS.md replace failure")

    monkeypatch.setattr(integration_module.os, "replace", fail_replace)
    result = RepositoryInitializer().apply(
        plan, human_confirmations=(confirmation(plan, operation),)
    )

    assert result.status is InitializationApplyStatus.PARTIAL_FAILURE
    assert (root / "AGENTS.md").read_bytes() == original
    assert not list(root.rglob(".agentic-agents.*.tmp"))


def test_service_rejects_noncanonical_content_and_has_no_target_parameter(
    tmp_path: Path,
) -> None:
    root = tmp_path / "target"
    root.mkdir()

    with pytest.raises(AgentsIntegrationError) as caught:
        AgentsIntegrationService().create_from_plan(
            root, planned_content="# arbitrary caller content\n"
        )

    assert caught.value.code == "INVALID_PLANNED_CONTENT"
    assert "target" not in signature(
        AgentsIntegrationService.integrate_from_plan
    ).parameters
    assert not (root / "AGENTS.md").exists()
