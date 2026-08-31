"""Immutable preparation and result contracts for existing-repository adoption."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .initialization_planning import InitializationPlan
from .project_configuration import ProjectConfiguration
from .repository_initialization import (
    InitializationOperationResult,
    InitializationResult,
)
from .repository_reconnaissance import RepositoryProfile
from .runtime_bootstrap import RuntimeBootstrapResult


class AdoptionStatus(str, Enum):
    NEEDS_CONFIGURATION = "NEEDS_CONFIGURATION"
    NEEDS_HUMAN_CONFIRMATION = "NEEDS_HUMAN_CONFIRMATION"
    READY_TO_APPLY = "READY_TO_APPLY"
    PARTIAL_OR_INCONSISTENT = "PARTIAL_OR_INCONSISTENT"
    UPGRADE_REQUIRED = "UPGRADE_REQUIRED"
    ADOPTED = "ADOPTED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class AdoptionFinding:
    code: str
    target_path: str
    detail: str


@dataclass(frozen=True, slots=True)
class AdoptionPreparation:
    repository_root: str
    status: AdoptionStatus
    repository_profile: RepositoryProfile | None
    project_configuration: ProjectConfiguration | None
    configuration_requirements: tuple[str, ...]
    initialization_plan: InitializationPlan | None
    required_human_confirmations: tuple[str, ...]
    findings: tuple[AdoptionFinding, ...]


@dataclass(frozen=True, slots=True)
class AdoptionResult:
    repository_root: str
    status: AdoptionStatus
    preparation: AdoptionPreparation
    initialization_result: InitializationResult | None
    applied_operations: tuple[InitializationOperationResult, ...]
    runtime_bootstrap_result: RuntimeBootstrapResult | None
    final_repository_profile: RepositoryProfile | None
    findings: tuple[AdoptionFinding, ...]
