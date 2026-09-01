import hashlib
import json
import subprocess
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import agentic_engineering_os.infrastructure.repository_upgrade_service as service_module
from agentic_engineering_os.application import MaintenanceGovernanceService, UpgradePlanner
from agentic_engineering_os.domain import (
    AGENTS_MANAGED_SECTION,
    GITIGNORE_MANAGED_SECTION,
    HumanUpgradeConfirmation,
    MaintenanceInitializationRequest,
    MaintenanceScope,
    MigrationArtifact,
    MissionStateGitPolicy,
    ProjectConfiguration,
    UpgradeOperationStatus,
    UpgradePlanStatus,
    UpgradeResultStatus,
)
from agentic_engineering_os.infrastructure import (
    AgentsIntegrationService,
    MaintenanceStateStore,
    ProjectConfigurationValidator,
    ProjectStateStore,
    RepositoryMigrationRegistry,
    RepositoryUpgradeService,
)
from agentic_engineering_os.infrastructure._negative_outcome_store import (
    _NegativeOutcomeStore,
)
from agentic_engineering_os.infrastructure.migration_registry import _AGENTS_V1


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


def configuration() -> ProjectConfiguration:
    return ProjectConfigurationValidator().validate(
        {
            "config_version": "1.0",
            "project_id": "upgrade-target",
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


def repository(
    tmp_path: Path,
    *,
    old_agents: bool = False,
    old_negative: bool = False,
    outcomes: list[dict[str, object]] | None = None,
) -> Path:
    root = tmp_path / "target"
    root.mkdir(parents=True)
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "P5.9 Test")
    git(root, "config", "user.email", "p5.9@example.invalid")
    (root / "README.md").write_text("# Upgrade target\n", encoding="utf-8")
    directory = root / ".agentic-engineering-os"
    directory.mkdir()
    (directory / "config.json").write_text(
        ProjectConfigurationValidator().serialize(configuration()), encoding="utf-8"
    )
    ProjectStateStore(root).initialize()
    agents = "# User rules\n\n" + (_AGENTS_V1 if old_agents else AGENTS_MANAGED_SECTION)
    (root / "AGENTS.md").write_text(agents, encoding="utf-8")
    (root / ".gitignore").write_text(GITIGNORE_MANAGED_SECTION, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "repository baseline")
    if old_negative:
        document = {
            "version": "1.0",
            "outcomes": outcomes if outcomes is not None else [],
        }
        (directory / "negative-outcomes.json").write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    assert git(root, "status", "--porcelain") == ""
    return root


def outcome(result: dict[str, object], *, consumed: bool = False) -> dict[str, object]:
    encoded = json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "fingerprint": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "result": result,
        "consumed": consumed,
    }


def confirmations(plan, producer: str = "Human/Alice"):
    return tuple(
        HumanUpgradeConfirmation(
            plan.plan_fingerprint,
            step.step_id,
            step.artifact,
            step.source_fingerprint,
            step.target_version,
            producer,
        )
        for step in plan.steps
        if step.human_confirmation_required
    )


def initialize_maintenance(
    root: Path, *, project_id: str = "upgrade-target"
) -> bytes:
    head = git(root, "rev-parse", "HEAD")
    service = MaintenanceGovernanceService(MaintenanceStateStore(root))
    service.initialize(
        MaintenanceInitializationRequest(
            MaintenanceScope(project_id, str(root.resolve())),
            head,
            None,
            None,
            datetime(2026, 9, 1, 16, 0, tzinfo=timezone.utc),
            "Human/Alice",
        )
    )
    return (root / ".agentic-engineering-os/maintenance.json").read_bytes()


def test_agents_v1_to_v2_requires_human_and_preserves_user_bytes_and_backup(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path, old_agents=True)
    source = (root / "AGENTS.md").read_bytes()
    state_before = ProjectStateStore(root).state_path.read_bytes()
    plan = UpgradePlanner().plan(root)

    assert plan.status is UpgradePlanStatus.NEEDS_HUMAN_CONFIRMATION
    assert [item.artifact for item in plan.steps] == [
        MigrationArtifact.AGENTS_MANAGED_SECTION
    ]
    refused = RepositoryUpgradeService().apply(plan)
    assert refused.status is UpgradeResultStatus.REFUSED
    assert (root / "AGENTS.md").read_bytes() == source

    result = RepositoryUpgradeService().apply(
        plan, confirmations=confirmations(plan)
    )
    step = plan.steps[0]
    assert result.status is UpgradeResultStatus.MIGRATED
    assert (root / step.backup_path).read_bytes() == source
    assert (root / "AGENTS.md").read_text(encoding="utf-8").startswith("# User rules\n\n")
    assert AgentsIntegrationService().inspect(
        (root / "AGENTS.md").read_bytes()
    ).status.value == "CURRENT"
    assert ProjectStateStore(root).state_path.read_bytes() == state_before


def test_negative_outcome_v1_to_v2_preserves_authority_and_creates_backup(
    tmp_path: Path,
) -> None:
    original_outcome = outcome({"status": "FAILED", "subject": "US-1"})
    root = repository(tmp_path, old_negative=True, outcomes=[original_outcome])
    source = (root / ".agentic-engineering-os/negative-outcomes.json").read_bytes()
    plan = UpgradePlanner().plan(root)

    assert plan.status is UpgradePlanStatus.READY_TO_APPLY
    step = plan.steps[0]
    assert step.artifact is MigrationArtifact.NEGATIVE_OUTCOME_LEDGER
    assert step.authority_fingerprint_before == step.authority_fingerprint_after
    result = RepositoryUpgradeService().apply(plan)

    assert result.status is UpgradeResultStatus.MIGRATED
    assert (root / step.backup_path).read_bytes() == source
    current = _NegativeOutcomeStore(root)._load()
    assert current == {
        "version": "2.0",
        "outcomes": [original_outcome],
        "transactions": [],
    }


def test_multi_artifact_order_and_partial_failure_preserve_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository(tmp_path, old_agents=True, old_negative=True)
    plan = UpgradePlanner().plan(root)
    assert [item.artifact for item in plan.steps] == [
        MigrationArtifact.AGENTS_MANAGED_SECTION,
        MigrationArtifact.NEGATIVE_OUTCOME_LEDGER,
    ]
    original_replace = service_module._replace_candidate

    def fail_second(root_path: Path, relative: str, content: bytes) -> None:
        if relative.endswith("negative-outcomes.json"):
            raise OSError("simulated second write failure")
        original_replace(root_path, relative, content)

    monkeypatch.setattr(service_module, "_replace_candidate", fail_second)
    result = RepositoryUpgradeService().apply(
        plan, confirmations=confirmations(plan)
    )

    assert result.status is UpgradeResultStatus.PARTIAL_FAILURE
    assert [item.status for item in result.operation_results] == [
        UpgradeOperationStatus.MIGRATED,
        UpgradeOperationStatus.FAILED,
    ]
    assert all((root / item.backup_path).is_file() for item in plan.steps)
    assert AgentsIntegrationService().inspect(
        (root / "AGENTS.md").read_bytes()
    ).status.value == "CURRENT"
    assert json.loads(
        (root / ".agentic-engineering-os/negative-outcomes.json").read_text(
            encoding="utf-8"
        )
    )["version"] == "1.0"


def test_already_current_is_noop_and_package_import_does_not_mutate(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    before = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    plan = UpgradePlanner().plan(root)
    result = RepositoryUpgradeService().apply(plan)

    assert plan.status is UpgradePlanStatus.ALREADY_CURRENT
    assert result.status is UpgradeResultStatus.ALREADY_CURRENT
    assert {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    } == before


def test_absent_or_current_maintenance_state_is_known_and_never_mutated(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)

    absent = UpgradePlanner().plan(root)
    assert absent.status is UpgradePlanStatus.ALREADY_CURRENT
    assert not absent.steps

    maintenance_before = initialize_maintenance(root)
    current = UpgradePlanner().plan(root)
    result = RepositoryUpgradeService().apply(current)

    target = next(
        item
        for item in current.target_versions
        if item.artifact is MigrationArtifact.MAINTENANCE_STATE
    )
    assert (target.current_version, target.versioned_in_git, target.volatile) == (
        "1.0",
        False,
        True,
    )
    assert current.status is UpgradePlanStatus.ALREADY_CURRENT
    assert not current.steps
    assert result.status is UpgradeResultStatus.ALREADY_CURRENT
    assert (
        root / ".agentic-engineering-os/maintenance.json"
    ).read_bytes() == maintenance_before


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        ('{"schema_version":"99.0"}', "UNSUPPORTED_MIGRATION"),
        ('{"schema_version":"1.0",', "CORRUPT_SOURCE_ARTIFACT"),
    ],
)
def test_unknown_or_corrupt_maintenance_state_blocks_without_replacement(
    tmp_path: Path,
    content: str,
    expected_code: str,
) -> None:
    root = repository(tmp_path)
    path = root / ".agentic-engineering-os/maintenance.json"
    path.write_text(content, encoding="utf-8")
    before = path.read_bytes()

    plan = UpgradePlanner().plan(root)

    assert plan.status is UpgradePlanStatus.BLOCKED
    assert any(item.code == expected_code for item in plan.blockers)
    assert not any(
        item.artifact is MigrationArtifact.MAINTENANCE_STATE for item in plan.steps
    )
    assert path.read_bytes() == before


def test_structurally_invalid_maintenance_state_blocks_without_replacement(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    path = root / ".agentic-engineering-os/maintenance.json"
    path.write_text('{"schema_version":"1.0"}', encoding="utf-8")
    before = path.read_bytes()

    plan = UpgradePlanner().plan(root)

    assert plan.status is UpgradePlanStatus.BLOCKED
    assert any(item.code == "CORRUPT_SOURCE_ARTIFACT" for item in plan.blockers)
    assert path.read_bytes() == before


@pytest.mark.parametrize("binding", ["project", "repository"])
def test_foreign_maintenance_binding_is_refused_without_replacement(
    tmp_path: Path, binding: str
) -> None:
    root = repository(tmp_path)
    path = root / ".agentic-engineering-os/maintenance.json"
    if binding == "project":
        initialize_maintenance(root, project_id="foreign-project")
    else:
        other = repository(tmp_path / "other")
        foreign = initialize_maintenance(other)
        path.write_bytes(foreign)
    before = path.read_bytes()

    plan = UpgradePlanner().plan(root)

    assert plan.status is UpgradePlanStatus.BLOCKED
    assert any(item.code == "FOREIGN_RUNTIME_ARTIFACT" for item in plan.blockers)
    assert path.read_bytes() == before


def test_maintenance_state_symlink_substitution_blocks_upgrade_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository(tmp_path)
    path = root / ".agentic-engineering-os/maintenance.json"
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda candidate: candidate == path or original_is_symlink(candidate),
    )

    plan = UpgradePlanner().plan(root)

    assert plan.status is UpgradePlanStatus.BLOCKED
    assert any(
        item.code == "CORRUPT_SOURCE_ARTIFACT"
        and item.target_path == ".agentic-engineering-os/maintenance.json"
        for item in plan.blockers
    )


def test_replan_after_success_is_current_and_old_plan_replay_is_refused(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path, old_negative=True)
    old_plan = UpgradePlanner().plan(root)
    first = RepositoryUpgradeService().apply(old_plan)
    current = UpgradePlanner().plan(root)
    replay = RepositoryUpgradeService().apply(old_plan)

    assert first.status is UpgradeResultStatus.MIGRATED
    assert current.status is UpgradePlanStatus.ALREADY_CURRENT
    assert not current.steps
    assert replay.status is UpgradeResultStatus.REFUSED
    assert replay.findings[0].code == "STALE_OR_FOREIGN_PLAN"


@pytest.mark.parametrize(
    ("filename", "version_field", "version", "artifact"),
    [
        (
            "executions.json",
            "schema_version",
            "1.0",
            MigrationArtifact.EXECUTION_LEDGER,
        ),
        (
            "executions.json",
            "schema_version",
            "99.0",
            MigrationArtifact.EXECUTION_LEDGER,
        ),
        ("mission.json", "schema_version", "0.9", MigrationArtifact.MISSION_STATE),
    ],
)
def test_unknown_future_and_unsupported_edges_are_blocked(
    tmp_path: Path,
    filename: str,
    version_field: str,
    version: str,
    artifact: MigrationArtifact,
) -> None:
    root = repository(tmp_path)
    path = root / ".agentic-engineering-os" / filename
    path.write_text(
        json.dumps({version_field: version, "records": []}), encoding="utf-8"
    )
    plan = UpgradePlanner().plan(root)

    assert plan.status is UpgradePlanStatus.BLOCKED
    assert any(item.code == "UNSUPPORTED_MIGRATION" for item in plan.blockers)
    assert not any(item.artifact is artifact for item in plan.steps)


def test_stale_source_head_and_foreign_repository_are_refused(tmp_path: Path) -> None:
    root = repository(tmp_path, old_negative=True)
    plan = UpgradePlanner().plan(root)
    source = root / ".agentic-engineering-os/negative-outcomes.json"
    source.write_bytes(source.read_bytes() + b"\n")
    stale_source = RepositoryUpgradeService().apply(plan)
    assert stale_source.status is UpgradeResultStatus.REFUSED

    root2 = repository(tmp_path / "head", old_negative=True)
    head_plan = UpgradePlanner().plan(root2)
    (root2 / "changed.txt").write_text("changed\n", encoding="utf-8")
    git(root2, "add", "changed.txt")
    git(root2, "commit", "-m", "move head")
    stale_head = RepositoryUpgradeService().apply(head_plan)
    assert stale_head.status is UpgradeResultStatus.REFUSED

    root3 = repository(tmp_path / "foreign", old_negative=True)
    foreign = replace(plan, repository_root=str(root3))
    wrong_repository = RepositoryUpgradeService().apply(foreign)
    assert wrong_repository.status is UpgradeResultStatus.REFUSED


def test_missing_fake_and_wrong_human_confirmations_are_refused(tmp_path: Path) -> None:
    root = repository(tmp_path, old_agents=True)
    plan = UpgradePlanner().plan(root)

    missing = RepositoryUpgradeService().apply(plan)
    fake = RepositoryUpgradeService().apply(
        plan, confirmations=confirmations(plan, "cOdEx/FakeHuman")
    )
    wrong = replace(confirmations(plan)[0], target_version="99")
    mismatched = RepositoryUpgradeService().apply(plan, confirmations=(wrong,))

    assert [missing.status, fake.status, mismatched.status] == [
        UpgradeResultStatus.REFUSED,
        UpgradeResultStatus.REFUSED,
        UpgradeResultStatus.REFUSED,
    ]
    assert _AGENTS_V1 in (root / "AGENTS.md").read_text(encoding="utf-8")


def test_backup_collision_and_symlink_source_are_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository(tmp_path, old_negative=True)
    initial = UpgradePlanner().plan(root)
    backup = root / initial.steps[0].backup_path
    backup.write_text("operator-owned backup\n", encoding="utf-8")
    collision = UpgradePlanner().plan(root)
    assert collision.status is UpgradePlanStatus.BLOCKED
    assert any(item.code == "BACKUP_COLLISION" for item in collision.blockers)

    root2 = repository(tmp_path / "symlink", old_agents=True)
    agents = root2 / "AGENTS.md"
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == agents or original_is_symlink(path),
    )
    unsafe = UpgradePlanner().plan(root2)
    assert unsafe.status is UpgradePlanStatus.BLOCKED


def test_write_failure_before_replace_keeps_source_and_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository(tmp_path, old_negative=True)
    plan = UpgradePlanner().plan(root)
    source_path = root / plan.steps[0].target_path
    source = source_path.read_bytes()

    monkeypatch.setattr(
        service_module,
        "_replace_candidate",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("simulated")),
    )
    result = RepositoryUpgradeService().apply(plan)

    assert result.status is UpgradeResultStatus.PARTIAL_FAILURE
    assert source_path.read_bytes() == source
    assert (root / plan.steps[0].backup_path).read_bytes() == source


def test_corrupt_old_ledger_and_authority_escalation_attempt_are_blocked(
    tmp_path: Path,
) -> None:
    malicious = outcome({"status": "CERTIFIED", "human_approval": True})
    malicious["fingerprint"] = "0" * 64
    root = repository(tmp_path, old_negative=True, outcomes=[malicious])
    state_before = ProjectStateStore(root).state_path.read_bytes()
    plan = UpgradePlanner().plan(root)

    assert plan.status is UpgradePlanStatus.BLOCKED
    assert any("SOURCE_NOT_MIGRATABLE" in item.code for item in plan.blockers)
    assert ProjectStateStore(root).state_path.read_bytes() == state_before


def test_closed_registry_has_only_real_edges_and_no_generic_migrate_api() -> None:
    registry = RepositoryMigrationRegistry()
    assert registry.supported_edges == (
        (MigrationArtifact.AGENTS_MANAGED_SECTION, "1", "2"),
        (MigrationArtifact.NEGATIVE_OUTCOME_LEDGER, "1.0", "2.0"),
    )
    assert (
        registry.definition(MigrationArtifact.EXECUTION_LEDGER, "1.0", "1.1")
        is None
    )
    assert (
        registry.definition(MigrationArtifact.MAINTENANCE_STATE, "0.9", "1.0")
        is None
    )
    assert not hasattr(registry, "migrate")
