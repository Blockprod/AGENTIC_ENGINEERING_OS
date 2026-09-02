"""P7.10-R1 product tests for mission-state Git policy adoption."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

import pytest

from agentic_engineering_os.application import ExistingRepositoryAdoption
from agentic_engineering_os.domain import (
    AGENTS_MANAGED_SECTION,
    GITIGNORE_MANAGED_SECTION,
    GITIGNORE_MISSION_STATE_RULE,
    GITIGNORE_ORCHESTRATION_RECORD_RULE,
    GITIGNORE_ORCHESTRATION_TEMP_RULE,
    GITIGNORE_SECTION_START,
    AdoptionStatus,
    HumanOperationConfirmation,
    InitializationOperationType,
    MissionRole,
    MissionState,
    MissionStateGitPolicy,
    MissionStatus,
    OperatingStep,
    RuntimeBootstrapStatus,
    gitignore_managed_section,
)
from agentic_engineering_os.infrastructure import (
    MissionStateStore,
    ProjectConfigurationValidator,
    RepositoryReconnaissance,
)


def _git(root: Path, *args: str) -> str:
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


def _fresh_repository(
    tmp_path: Path, *, gitignore: str | None = None
) -> Path:
    root = tmp_path / "target"
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "P7.10-R1 Test")
    _git(root, "config", "user.email", "p7.10-r1@example.invalid")
    (root / "README.md").write_text("# Target\n", encoding="utf-8")
    if gitignore is not None:
        (root / ".gitignore").write_text(gitignore, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "target baseline")
    return root


def _configuration(policy: MissionStateGitPolicy):
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
            "mission_state_git_policy": policy.value,
        }
    )


def _confirmations(preparation):
    assert preparation.initialization_plan is not None
    return tuple(
        HumanOperationConfirmation(
            plan_fingerprint=preparation.initialization_plan.input_fingerprint,
            operation_id=operation.operation_id,
            target_path=operation.target_path,
            expected_current_state=operation.expected_current_state,
            expected_target_fingerprint=operation.expected_target_fingerprint or "",
            confirmed_by="Human/Alice",
        )
        for operation in preparation.initialization_plan.operations
        if operation.human_confirmation_required
    )


def _adopt(root: Path, policy: MissionStateGitPolicy):
    service = ExistingRepositoryAdoption()
    preparation = service.prepare_adoption(root, _configuration(policy))
    result = service.apply_adoption(
        preparation, human_confirmations=_confirmations(preparation)
    )
    return service, preparation, result


def _commit_adoption(root: Path) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "adopt Agentic Engineering OS")
    assert _git(root, "status", "--porcelain") == ""


def _mission(root: Path) -> MissionState:
    return MissionState(
        schema_version="1.0",
        mission_id="P7.10-R1-test",
        workflow_generation=0,
        status=MissionStatus.ACTIVE,
        role=MissionRole.IMPLEMENTER,
        objective="Prove mission Git policy.",
        subject="Target repository",
        operating_step=OperatingStep.ACT,
        next_action="Verify Git status.",
        observed_commit=_git(root, "rev-parse", "HEAD"),
        updated_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )


def test_fresh_ignored_adoption_materializes_policy_before_bootstrap(
    tmp_path: Path,
) -> None:
    root = _fresh_repository(tmp_path)
    service = ExistingRepositoryAdoption()
    before = tuple(
        (path.relative_to(root), path.read_bytes())
        for path in root.iterdir()
        if path.is_file()
    )

    preparation = service.prepare_adoption(
        root, _configuration(MissionStateGitPolicy.IGNORED)
    )
    assert tuple(
        (path.relative_to(root), path.read_bytes())
        for path in root.iterdir()
        if path.is_file()
    ) == before
    result = service.apply_adoption(preparation)

    assert preparation.status is AdoptionStatus.READY_TO_APPLY
    assert result.status is AdoptionStatus.ADOPTED
    assert GITIGNORE_MISSION_STATE_RULE in (
        root / ".gitignore"
    ).read_text(encoding="utf-8").splitlines()
    installed = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert GITIGNORE_SECTION_START in installed
    assert GITIGNORE_ORCHESTRATION_RECORD_RULE in installed
    assert GITIGNORE_ORCHESTRATION_TEMP_RULE in installed
    assert result.runtime_bootstrap_result is not None
    assert result.runtime_bootstrap_result.status is RuntimeBootstrapStatus.BOOTSTRAPPED
    assert not (root / ".agentic-engineering-os" / "mission.json").exists()

    (root / ".agentic-engineering-os" / "orchestration.json").write_text(
        "{}\n", encoding="utf-8"
    )
    (root / ".agentic-engineering-os" / ".orchestration.probe.tmp").write_text(
        "temporary\n", encoding="utf-8"
    )
    assert _git(root, "check-ignore", GITIGNORE_ORCHESTRATION_RECORD_RULE) == (
        GITIGNORE_ORCHESTRATION_RECORD_RULE
    )
    assert _git(
        root, "check-ignore", ".agentic-engineering-os/.orchestration.probe.tmp"
    ) == ".agentic-engineering-os/.orchestration.probe.tmp"

    for authoritative in (
        ".agentic-engineering-os/config.json",
        ".agentic-engineering-os/state.json",
    ):
        observed = subprocess.run(
            ["git", "-C", str(root), "check-ignore", authoritative],
            shell=False,
            capture_output=True,
            text=True,
            check=False,
        )
        assert observed.returncode == 1


def test_existing_gitignore_requires_human_and_preserves_user_bytes(
    tmp_path: Path,
) -> None:
    user_content = "# User rules\n.env\n"
    root = _fresh_repository(tmp_path, gitignore=user_content)
    service = ExistingRepositoryAdoption()
    preparation = service.prepare_adoption(
        root, _configuration(MissionStateGitPolicy.IGNORED)
    )

    assert preparation.status is AdoptionStatus.NEEDS_HUMAN_CONFIRMATION
    refused = service.apply_adoption(preparation)
    assert refused.status is AdoptionStatus.BLOCKED

    adopted = service.apply_adoption(
        preparation, human_confirmations=_confirmations(preparation)
    )
    assert adopted.status is AdoptionStatus.ADOPTED
    content = (root / ".gitignore").read_text(encoding="utf-8")
    assert content.startswith(user_content)
    assert content.count(GITIGNORE_MISSION_STATE_RULE) == 1


def test_tracked_policy_retains_original_canonical_section(tmp_path: Path) -> None:
    root = _fresh_repository(tmp_path)
    _, _, result = _adopt(root, MissionStateGitPolicy.TRACKED)

    assert result.status is AdoptionStatus.ADOPTED
    assert (root / ".gitignore").read_text(encoding="utf-8") == GITIGNORE_MANAGED_SECTION
    assert GITIGNORE_MISSION_STATE_RULE not in GITIGNORE_MANAGED_SECTION.splitlines()


def test_canonical_ignored_section_is_idempotent_no_op(tmp_path: Path) -> None:
    root = _fresh_repository(tmp_path)
    service, _, first = _adopt(root, MissionStateGitPolicy.IGNORED)
    assert first.status is AdoptionStatus.ADOPTED
    _commit_adoption(root)

    preparation = service.prepare_adoption(root)
    assert preparation.initialization_plan is not None
    gitignore_operation = next(
        operation
        for operation in preparation.initialization_plan.operations
        if operation.target_path == ".gitignore"
    )

    assert gitignore_operation.operation_type is InitializationOperationType.NO_OP
    second = service.apply_adoption(preparation)
    assert second.status is AdoptionStatus.ADOPTED
    assert _git(root, "status", "--porcelain") == ""


def test_ignored_mission_creation_keeps_committed_repository_clean(
    tmp_path: Path,
) -> None:
    root = _fresh_repository(tmp_path)
    _, _, result = _adopt(root, MissionStateGitPolicy.IGNORED)
    assert result.status is AdoptionStatus.ADOPTED
    _commit_adoption(root)

    MissionStateStore(root).initialize(_mission(root))

    assert (
        _git(root, "check-ignore", GITIGNORE_MISSION_STATE_RULE)
        == GITIGNORE_MISSION_STATE_RULE
    )
    assert _git(root, "status", "--porcelain") == ""


def test_tracked_mission_creation_remains_versionable(tmp_path: Path) -> None:
    root = _fresh_repository(tmp_path)
    _, _, result = _adopt(root, MissionStateGitPolicy.TRACKED)
    assert result.status is AdoptionStatus.ADOPTED
    _commit_adoption(root)

    MissionStateStore(root).initialize(_mission(root))

    assert GITIGNORE_MISSION_STATE_RULE in _git(root, "status", "--porcelain")


def test_stale_plan_after_gitignore_change_is_refused(tmp_path: Path) -> None:
    root = _fresh_repository(tmp_path, gitignore="# User\n")
    service = ExistingRepositoryAdoption()
    preparation = service.prepare_adoption(
        root, _configuration(MissionStateGitPolicy.IGNORED)
    )
    (root / ".gitignore").write_text("# Changed\n", encoding="utf-8")

    result = service.apply_adoption(
        preparation, human_confirmations=_confirmations(preparation)
    )

    assert result.status is AdoptionStatus.BLOCKED
    assert not (root / ".agentic-engineering-os").exists()


def test_policy_change_after_planning_is_refused(tmp_path: Path) -> None:
    root = _fresh_repository(tmp_path)
    service = ExistingRepositoryAdoption()
    preparation = service.prepare_adoption(
        root, _configuration(MissionStateGitPolicy.TRACKED)
    )
    forged = replace(
        preparation,
        project_configuration=_configuration(MissionStateGitPolicy.IGNORED),
    )

    result = service.apply_adoption(forged)

    assert result.status is AdoptionStatus.BLOCKED
    assert result.findings[0].code == "PREPARATION_NOT_APPLICABLE"


def test_preparation_cannot_cross_repository_or_project_binding(
    tmp_path: Path,
) -> None:
    first = _fresh_repository(tmp_path / "first")
    second = _fresh_repository(tmp_path / "second")
    service = ExistingRepositoryAdoption()
    preparation = service.prepare_adoption(
        first, _configuration(MissionStateGitPolicy.IGNORED)
    )

    result = service.apply_adoption(replace(preparation, repository_root=str(second)))

    assert result.status is AdoptionStatus.BLOCKED
    assert result.findings[0].code == "PREPARATION_BINDING_MISMATCH"
    assert not (first / ".agentic-engineering-os").exists()
    assert not (second / ".agentic-engineering-os").exists()


def test_policy_incompatible_managed_section_is_not_current(tmp_path: Path) -> None:
    root = _fresh_repository(tmp_path, gitignore=GITIGNORE_MANAGED_SECTION)
    config_path = root / ".agentic-engineering-os" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text(
        ProjectConfigurationValidator().serialize(
            _configuration(MissionStateGitPolicy.IGNORED)
        ),
        encoding="utf-8",
    )
    (root / "AGENTS.md").write_text(AGENTS_MANAGED_SECTION, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "incompatible footprint")

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.agentic_os.gitignore_managed_section.status.value == "TAMPERED"
    assert profile.agentic_os.state.value == "PARTIAL_OR_INCONSISTENT"


def test_duplicate_managed_section_fails_closed(tmp_path: Path) -> None:
    section = gitignore_managed_section(MissionStateGitPolicy.IGNORED)
    root = _fresh_repository(tmp_path, gitignore=section + section)

    preparation = ExistingRepositoryAdoption().prepare_adoption(
        root, _configuration(MissionStateGitPolicy.IGNORED)
    )

    assert preparation.status is AdoptionStatus.PARTIAL_OR_INCONSISTENT


def test_forged_arbitrary_ignore_entry_is_refused(tmp_path: Path) -> None:
    root = _fresh_repository(tmp_path)
    service = ExistingRepositoryAdoption()
    preparation = service.prepare_adoption(
        root, _configuration(MissionStateGitPolicy.IGNORED)
    )
    assert preparation.initialization_plan is not None
    operation = next(
        item
        for item in preparation.initialization_plan.operations
        if item.operation_type is InitializationOperationType.CREATE_MANAGED_FILE
        and item.target_path == ".gitignore"
    )
    injected = (operation.desired_content or "") + "arbitrary-secret.txt\n"
    forged_operation = replace(
        operation,
        desired_content=injected,
        desired_content_sha256=sha256(injected.encode("utf-8")).hexdigest(),
    )
    forged_plan = replace(
        preparation.initialization_plan,
        operations=tuple(
            forged_operation if item is operation else item
            for item in preparation.initialization_plan.operations
        ),
    )
    forged = replace(preparation, initialization_plan=forged_plan)

    result = service.apply_adoption(forged)

    assert result.status is AdoptionStatus.BLOCKED
    assert result.findings[0].code == "UNTRUSTED_OR_STALE_PLAN"


def test_unrelated_wildcard_does_not_satisfy_managed_ignored_contract(
    tmp_path: Path,
) -> None:
    root = _fresh_repository(
        tmp_path, gitignore=GITIGNORE_MANAGED_SECTION + "*.json\n"
    )
    config_path = root / ".agentic-engineering-os" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text(
        ProjectConfigurationValidator().serialize(
            _configuration(MissionStateGitPolicy.IGNORED)
        ),
        encoding="utf-8",
    )
    (root / "AGENTS.md").write_text(AGENTS_MANAGED_SECTION, encoding="utf-8")
    _git(
        root,
        "add",
        "-f",
        ".agentic-engineering-os/config.json",
        "AGENTS.md",
        ".gitignore",
    )
    _git(root, "commit", "-m", "wildcard is not authority")

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.agentic_os.gitignore_managed_section.status.value == "TAMPERED"
    assert profile.agentic_os.state.value == "PARTIAL_OR_INCONSISTENT"


def test_symlink_gitignore_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fresh_repository(tmp_path, gitignore="# User\n")
    target = root / ".gitignore"
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == target or original_is_symlink(path),
    )

    preparation = ExistingRepositoryAdoption().prepare_adoption(
        root, _configuration(MissionStateGitPolicy.IGNORED)
    )

    assert preparation.status is AdoptionStatus.BLOCKED
    assert any(
        finding.code == "GITIGNORE_MANAGED_SECTION_CONFLICT"
        for finding in preparation.findings
    )
