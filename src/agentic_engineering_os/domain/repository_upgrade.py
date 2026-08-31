"""Immutable contracts for explicit repository-format upgrades."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .repository_reconnaissance import RepositoryProfile


class MigrationArtifact(str, Enum):
    AGENTS_MANAGED_SECTION = "AGENTS_MANAGED_SECTION"
    PROJECT_CONFIGURATION = "PROJECT_CONFIGURATION"
    PROJECT_STATE = "PROJECT_STATE"
    MISSION_STATE = "MISSION_STATE"
    WORKTREE_REGISTRY = "WORKTREE_REGISTRY"
    NEGATIVE_OUTCOME_LEDGER = "NEGATIVE_OUTCOME_LEDGER"
    EXECUTION_LEDGER = "EXECUTION_LEDGER"


class UpgradePlanStatus(str, Enum):
    ALREADY_CURRENT = "ALREADY_CURRENT"
    NEEDS_HUMAN_CONFIRMATION = "NEEDS_HUMAN_CONFIRMATION"
    READY_TO_APPLY = "READY_TO_APPLY"
    BLOCKED = "BLOCKED"


class UpgradeResultStatus(str, Enum):
    MIGRATED = "MIGRATED"
    ALREADY_CURRENT = "ALREADY_CURRENT"
    REFUSED = "REFUSED"
    FAILED = "FAILED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"


class UpgradeOperationStatus(str, Enum):
    MIGRATED = "MIGRATED"
    FAILED = "FAILED"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


@dataclass(frozen=True, slots=True)
class MigrationTargetVersion:
    artifact: MigrationArtifact
    current_version: str
    versioned_in_git: bool | None
    volatile: bool


@dataclass(frozen=True, slots=True)
class UpgradeFinding:
    code: str
    target_path: str
    detail: str


@dataclass(frozen=True, slots=True)
class UpgradeStep:
    step_id: str
    artifact: MigrationArtifact
    target_path: str
    source_version: str
    target_version: str
    source_fingerprint: str
    target_fingerprint: str
    backup_path: str
    authority_fingerprint_before: str | None
    authority_fingerprint_after: str | None
    human_confirmation_required: bool


@dataclass(frozen=True, slots=True)
class UpgradePlan:
    repository_root: str
    git_head: str | None
    git_branch: str | None
    profile_fingerprint: str | None
    target_product_version: str
    target_versions: tuple[MigrationTargetVersion, ...]
    steps: tuple[UpgradeStep, ...]
    required_human_confirmations: tuple[str, ...]
    blockers: tuple[UpgradeFinding, ...]
    status: UpgradePlanStatus
    plan_fingerprint: str


@dataclass(frozen=True, slots=True)
class HumanUpgradeConfirmation:
    plan_fingerprint: str
    step_id: str
    artifact: MigrationArtifact
    source_fingerprint: str
    target_version: str
    confirmed_by: str


@dataclass(frozen=True, slots=True)
class UpgradeOperationResult:
    step_id: str
    artifact: MigrationArtifact
    target_path: str
    backup_path: str
    status: UpgradeOperationStatus
    detail: str


@dataclass(frozen=True, slots=True)
class UpgradeResult:
    repository_root: str
    status: UpgradeResultStatus
    plan_fingerprint: str
    operation_results: tuple[UpgradeOperationResult, ...]
    findings: tuple[UpgradeFinding, ...]
    final_repository_profile: RepositoryProfile | None
