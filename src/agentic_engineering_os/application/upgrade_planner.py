"""Deterministic dry-run planning for explicit repository upgrades."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path, PurePosixPath

from agentic_engineering_os import __version__ as _PRODUCT_VERSION
from agentic_engineering_os.domain import (
    DocumentStatus,
    ManagedSectionStatus,
    MigrationArtifact,
    MigrationTargetVersion,
    RepositorySupportStatus,
    UpgradeFinding,
    UpgradePlan,
    UpgradePlanStatus,
    UpgradeStep,
)
from agentic_engineering_os.infrastructure.migration_registry import (
    MigrationRegistryError,
    RepositoryMigrationRegistry,
)
from agentic_engineering_os.infrastructure.agents_integration import (
    AgentsIntegrationService,
)
from agentic_engineering_os.infrastructure.maintenance_state_store import (
    MaintenanceStateStore,
)
from agentic_engineering_os.infrastructure.project_configuration import (
    ProjectConfigurationError,
    ProjectConfigurationLoader,
)
from agentic_engineering_os.infrastructure.project_state_store import PersistenceError
from agentic_engineering_os.infrastructure.repository_reconnaissance import (
    RepositoryReconnaissance,
    RepositoryReconnaissanceError,
)

from .initialization_planner import InitializationPlanner


_ARTIFACT_BY_RUNTIME_PATH = {
    ".agentic-engineering-os/state.json": MigrationArtifact.PROJECT_STATE,
    ".agentic-engineering-os/mission.json": MigrationArtifact.MISSION_STATE,
    ".agentic-engineering-os/worktrees.json": MigrationArtifact.WORKTREE_REGISTRY,
    ".agentic-engineering-os/negative-outcomes.json": MigrationArtifact.NEGATIVE_OUTCOME_LEDGER,
    ".agentic-engineering-os/executions.json": MigrationArtifact.EXECUTION_LEDGER,
    ".agentic-engineering-os/maintenance.json": MigrationArtifact.MAINTENANCE_STATE,
}
_ORDER = {
    MigrationArtifact.AGENTS_MANAGED_SECTION: 10,
    MigrationArtifact.NEGATIVE_OUTCOME_LEDGER: 20,
}


class UpgradePlanner:
    """Plan only closed registry edges against exact repository bytes."""

    def __init__(self) -> None:
        self._reconnaissance = RepositoryReconnaissance()
        self._registry = RepositoryMigrationRegistry()

    def plan(self, repository_root: Path | str) -> UpgradePlan:
        root_text = str(repository_root)
        targets = tuple(
            MigrationTargetVersion(*item) for item in self._registry.target_versions
        )
        try:
            profile = self._reconnaissance.inspect(repository_root)
            root = Path(profile.requested_root)
            profile_fingerprint = InitializationPlanner.fingerprint(profile)
        except (RepositoryReconnaissanceError, OSError) as error:
            return _build_plan(
                root_text,
                None,
                None,
                None,
                targets,
                (),
                (UpgradeFinding("RECONNAISSANCE_FAILED", ".", _error_code(error)),),
            )

        blockers: list[UpgradeFinding] = []
        steps: list[UpgradeStep] = []
        if profile.support_status is not RepositorySupportStatus.SUPPORTED:
            blockers.append(
                UpgradeFinding(
                    "REPOSITORY_NOT_SUPPORTED", ".", profile.support_status.value
                )
            )
        if not profile.scan_complete:
            blockers.append(
                UpgradeFinding(
                    "INCOMPLETE_RECONNAISSANCE",
                    ".",
                    "bounded repository scan did not complete",
                )
            )

        agents = profile.agentic_os.agents_managed_section
        if agents.status is ManagedSectionStatus.UPGRADE_REQUIRED:
            try:
                agents_version = AgentsIntegrationService().inspect(
                    _read_safe(root, "AGENTS.md")
                ).managed_version
            except OSError:
                agents_version = None
            self._append_step(
                root,
                MigrationArtifact.AGENTS_MANAGED_SECTION,
                "AGENTS.md",
                agents_version,
                "2",
                steps,
                blockers,
            )
        elif agents.status in {
            ManagedSectionStatus.TAMPERED,
            ManagedSectionStatus.AMBIGUOUS,
            ManagedSectionStatus.UNSAFE,
            ManagedSectionStatus.UNKNOWN,
        }:
            blockers.append(
                UpgradeFinding(
                    "AGENTS_SOURCE_NOT_MIGRATABLE", "AGENTS.md", agents.status.value
                )
            )

        if profile.agentic_os.config_status is DocumentStatus.UNKNOWN_VERSION:
            blockers.append(
                UpgradeFinding(
                    "UNSUPPORTED_MIGRATION",
                    ".agentic-engineering-os/config.json",
                    f"no historical configuration edge from {profile.agentic_os.config_version}",
                )
            )
        elif profile.agentic_os.config_status in {
            DocumentStatus.INVALID,
            DocumentStatus.TOO_LARGE,
            DocumentStatus.UNSAFE,
        }:
            blockers.append(
                UpgradeFinding(
                    "CORRUPT_SOURCE_ARTIFACT",
                    ".agentic-engineering-os/config.json",
                    profile.agentic_os.config_status.value,
                )
            )

        target_by_artifact = {item.artifact: item.current_version for item in targets}
        for observation in profile.agentic_os.runtime_files:
            artifact = _ARTIFACT_BY_RUNTIME_PATH[observation.relative_path]
            if (
                artifact is MigrationArtifact.MAINTENANCE_STATE
                and observation.status is DocumentStatus.VERSION_OBSERVED
            ):
                maintenance_finding = _maintenance_validation_finding(root)
                if maintenance_finding is not None:
                    blockers.append(maintenance_finding)
            elif observation.status is DocumentStatus.UNKNOWN_VERSION:
                self._append_step(
                    root,
                    artifact,
                    observation.relative_path,
                    observation.schema_version,
                    target_by_artifact[artifact],
                    steps,
                    blockers,
                )
            elif observation.status in {
                DocumentStatus.INVALID,
                DocumentStatus.TOO_LARGE,
                DocumentStatus.UNSAFE,
            }:
                blockers.append(
                    UpgradeFinding(
                        "CORRUPT_SOURCE_ARTIFACT",
                        observation.relative_path,
                        observation.status.value,
                    )
                )

        steps.sort(key=lambda item: (_ORDER.get(item.artifact, 999), item.target_path))
        steps = [
            UpgradeStep(
                f"MIG-{index:03d}",
                item.artifact,
                item.target_path,
                item.source_version,
                item.target_version,
                item.source_fingerprint,
                item.target_fingerprint,
                item.backup_path,
                item.authority_fingerprint_before,
                item.authority_fingerprint_after,
                item.human_confirmation_required,
            )
            for index, item in enumerate(steps, 1)
        ]
        if steps and profile.git.clean.value is not True:
            blockers.append(
                UpgradeFinding(
                    "DIRTY_REPOSITORY",
                    ".",
                    "migration requires an initially clean Git worktree",
                )
            )
        required = tuple(
            item.step_id for item in steps if item.human_confirmation_required
        )
        return _build_plan(
            profile.requested_root,
            profile.git.head_commit.value
            if isinstance(profile.git.head_commit.value, str)
            else None,
            profile.git.branch.value if isinstance(profile.git.branch.value, str) else None,
            profile_fingerprint,
            targets,
            tuple(steps),
            tuple(sorted(set(blockers), key=lambda item: (item.code, item.target_path))),
            required,
        )

    def _append_step(
        self,
        root: Path,
        artifact: MigrationArtifact,
        relative_path: str,
        source_version: str | None,
        target_version: str,
        steps: list[UpgradeStep],
        blockers: list[UpgradeFinding],
    ) -> None:
        if source_version is None:
            blockers.append(
                UpgradeFinding(
                    "CORRUPT_SOURCE_ARTIFACT", relative_path, "source version is absent"
                )
            )
            return
        definition = self._registry.definition(artifact, source_version, target_version)
        if definition is None:
            blockers.append(
                UpgradeFinding(
                    "UNSUPPORTED_MIGRATION",
                    relative_path,
                    f"no explicit edge {source_version}->{target_version}",
                )
            )
            return
        try:
            source = _read_safe(root, relative_path)
            candidate = self._registry.prepare_candidate(
                artifact, source_version, target_version, source
            )
        except (OSError, MigrationRegistryError) as error:
            blockers.append(
                UpgradeFinding(
                    f"SOURCE_NOT_MIGRATABLE:{_error_code(error)}",
                    relative_path,
                    "source failed closed migration validation",
                )
            )
            return
        source_fingerprint = _sha256(source)
        backup_path = _backup_path(
            relative_path, source_version, target_version, source_fingerprint
        )
        if (root / PurePosixPath(backup_path)).exists() or (
            root / PurePosixPath(backup_path)
        ).is_symlink():
            blockers.append(
                UpgradeFinding(
                    "BACKUP_COLLISION",
                    backup_path,
                    "deterministic migration backup already exists",
                )
            )
        steps.append(
            UpgradeStep(
                "PENDING",
                artifact,
                relative_path,
                source_version,
                target_version,
                source_fingerprint,
                _sha256(candidate.content),
                backup_path,
                candidate.authority_fingerprint_before,
                candidate.authority_fingerprint_after,
                definition.human_confirmation_required,
            )
        )


def _build_plan(
    root: str,
    head: str | None,
    branch: str | None,
    profile_fingerprint: str | None,
    targets: tuple[MigrationTargetVersion, ...],
    steps: tuple[UpgradeStep, ...],
    blockers: tuple[UpgradeFinding, ...],
    required: tuple[str, ...] = (),
) -> UpgradePlan:
    status = (
        UpgradePlanStatus.BLOCKED
        if blockers
        else UpgradePlanStatus.ALREADY_CURRENT
        if not steps
        else UpgradePlanStatus.NEEDS_HUMAN_CONFIRMATION
        if required
        else UpgradePlanStatus.READY_TO_APPLY
    )
    payload = {
        "repository_root": root,
        "git_head": head,
        "git_branch": branch,
        "profile_fingerprint": profile_fingerprint,
        "target_product_version": _PRODUCT_VERSION,
        "target_versions": [asdict(item) for item in targets],
        "steps": [asdict(item) for item in steps],
        "required_human_confirmations": list(required),
        "blockers": [asdict(item) for item in blockers],
        "status": status.value,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        ).encode("utf-8")
    ).hexdigest()
    return UpgradePlan(
        root,
        head,
        branch,
        profile_fingerprint,
        _PRODUCT_VERSION,
        targets,
        steps,
        required,
        blockers,
        status,
        fingerprint,
    )


def _read_safe(root: Path, relative: str) -> bytes:
    target = root.joinpath(*PurePosixPath(relative).parts)
    if target.is_symlink() or any(
        parent.is_symlink()
        for parent in target.parents
        if parent != root.parent
    ):
        raise OSError("migration source is a symlink")
    resolved = target.resolve(strict=True)
    if not resolved.is_file() or os.path.commonpath((str(root), str(resolved))) != str(root):
        raise OSError("migration source escaped repository")
    maximum = 256_000 if relative == "AGENTS.md" else 16_000_000
    if resolved.stat().st_size > maximum:
        raise OSError("migration source exceeds policy")
    return resolved.read_bytes()


def _backup_path(relative: str, source: str, target: str, fingerprint: str) -> str:
    safe_source = source.replace(".", "_")
    safe_target = target.replace(".", "_")
    return f"{relative}.agentic-os-backup.v{safe_source}-to-v{safe_target}.{fingerprint}.bak"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _error_code(error: object) -> str:
    return str(getattr(error, "code", type(error).__name__))


def _maintenance_validation_finding(root: Path) -> UpgradeFinding | None:
    target = ".agentic-engineering-os/maintenance.json"
    try:
        record = MaintenanceStateStore(root).load()
        configuration = ProjectConfigurationLoader(root).load()
    except (PersistenceError, ProjectConfigurationError, OSError) as error:
        return UpgradeFinding(
            "CORRUPT_SOURCE_ARTIFACT",
            target,
            f"maintenance state validation failed: {_error_code(error)}",
        )
    expected_root = os.path.normcase(str(root.resolve(strict=True)))
    actual_root = os.path.normcase(
        str(Path(record.scope.repository_root).resolve(strict=False))
    )
    if actual_root != expected_root or record.scope.project_id != configuration.project_id:
        return UpgradeFinding(
            "FOREIGN_RUNTIME_ARTIFACT",
            target,
            "maintenance state is not bound to this repository and project",
        )
    return None


def _json_default(value: object) -> object:
    return getattr(value, "value", str(value))
