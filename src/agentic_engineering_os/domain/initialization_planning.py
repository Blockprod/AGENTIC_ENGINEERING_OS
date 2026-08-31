"""Immutable contract for repository initialization dry-run planning."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .project_configuration import ProjectConfiguration
from .repository_reconnaissance import AgenticOsInitializationState


class InitializationOperationType(str, Enum):
    CREATE_DIRECTORY = "CREATE_DIRECTORY"
    INITIALIZE_CONFIG = "INITIALIZE_CONFIG"
    CREATE_MANAGED_FILE = "CREATE_MANAGED_FILE"
    UPDATE_MANAGED_SECTION = "UPDATE_MANAGED_SECTION"
    ADD_GITIGNORE_SECTION = "ADD_GITIGNORE_SECTION"
    NO_OP = "NO_OP"
    BLOCKED_CONFLICT = "BLOCKED_CONFLICT"


class PlannedCurrentState(str, Enum):
    ABSENT = "ABSENT"
    PRESENT = "PRESENT"
    SECTION_ABSENT = "SECTION_ABSENT"
    MANAGED_SECTION_CURRENT = "MANAGED_SECTION_CURRENT"
    PARTIAL_OR_INCONSISTENT = "PARTIAL_OR_INCONSISTENT"
    UPGRADE_REQUIRED = "UPGRADE_REQUIRED"
    UNKNOWN = "UNKNOWN"


class PlannedDesiredState(str, Enum):
    DIRECTORY_PRESENT = "DIRECTORY_PRESENT"
    CANONICAL_CONFIG_PRESENT = "CANONICAL_CONFIG_PRESENT"
    CANONICAL_MANAGED_SECTION_PRESENT = "CANONICAL_MANAGED_SECTION_PRESENT"
    RUNTIME_INITIALIZATION_DEFERRED = "RUNTIME_INITIALIZATION_DEFERRED"
    UNCHANGED = "UNCHANGED"
    BLOCKED_UNCHANGED = "BLOCKED_UNCHANGED"


@dataclass(frozen=True, slots=True)
class InitializationRepositoryIdentity:
    repository_root: str
    git_head: str
    git_branch: str | None


@dataclass(frozen=True, slots=True)
class PlannedOperation:
    operation_id: str
    operation_type: InitializationOperationType
    target_path: str
    expected_current_state: PlannedCurrentState
    desired_state: PlannedDesiredState
    desired_content: str | None
    desired_content_sha256: str | None
    expected_target_fingerprint: str | None
    reason_code: str
    source: str
    human_confirmation_required: bool


@dataclass(frozen=True, slots=True)
class InitializationFinding:
    code: str
    target_path: str
    detail: str


@dataclass(frozen=True, slots=True)
class ExpectedFootprintEntry:
    relative_path: str
    expected_state: PlannedDesiredState
    deferred: bool


@dataclass(frozen=True, slots=True)
class InitializationPlan:
    repository: InitializationRepositoryIdentity
    profile_fingerprint: str
    input_fingerprint: str
    current_initialization_state: AgenticOsInitializationState
    desired_config_version: str
    desired_configuration: ProjectConfiguration | None
    desired_configuration_sha256: str
    operations: tuple[PlannedOperation, ...]
    blockers: tuple[InitializationFinding, ...]
    warnings: tuple[InitializationFinding, ...]
    required_human_confirmations: tuple[str, ...]
    expected_footprint: tuple[ExpectedFootprintEntry, ...]
    ready_for_application: bool
