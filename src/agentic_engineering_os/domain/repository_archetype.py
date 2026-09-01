"""Closed repository archetype and execution-readiness contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .platform import ExecutableDiscoveryMethod
from .project_configuration import VerificationKind


class RepositoryArchetype(str, Enum):
    PYTHON = "PYTHON"
    NODE = "NODE"
    RUST = "RUST"
    UNKNOWN = "UNKNOWN"


class ArchetypeSupportLevel(str, Enum):
    UNSUPPORTED = "UNSUPPORTED"
    RECOGNIZED = "RECOGNIZED"
    ADOPTABLE = "ADOPTABLE"
    EXECUTION_READY = "EXECUTION_READY"
    AMBIGUOUS = "AMBIGUOUS"


class ToolchainAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class VerificationCommandContract:
    command_id: str
    kind: VerificationKind
    executable: str
    args: tuple[str, ...]
    cwd: str
    required: bool
    owner_component_id: str | None
    owner_archetype: RepositoryArchetype | None


@dataclass(frozen=True, slots=True)
class ArchetypeComponent:
    component_id: str
    archetype: RepositoryArchetype
    root: str
    detected_toolchains: tuple[str, ...]
    manifests: tuple[str, ...]
    lockfiles: tuple[str, ...]
    package_manager: str | None
    workspace: bool
    declared_scripts: tuple[str, ...]
    configured_command_ids: tuple[str, ...]
    source_scopes: tuple[str, ...]
    test_scopes: tuple[str, ...]
    build_scopes: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RepositoryArchetypeProfile:
    repository_root: str
    project_id: str
    configuration_fingerprint: str
    components: tuple[ArchetypeComponent, ...]
    command_contracts: tuple[VerificationCommandContract, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolchainMachineFact:
    archetype: RepositoryArchetype
    requested_executable: str
    availability: ToolchainAvailability
    resolved_path: str | None
    version: str | None
    discovery_method: ExecutableDiscoveryMethod
    observed_size: int | None
    observed_mtime_ns: int | None
    observed_sha256: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.requested_executable, str) or not self.requested_executable:
            raise ValueError("toolchain executable identity must be explicit")
        if self.availability is ToolchainAvailability.AVAILABLE and (
            self.resolved_path is None
            or self.version is None
            or self.observed_size is None
            or self.observed_mtime_ns is None
            or self.observed_sha256 is None
        ):
            raise ValueError("available toolchain fact requires complete observation")
        if self.availability is ToolchainAvailability.UNAVAILABLE and (
            self.resolved_path is not None
            or self.version is not None
            or self.observed_size is not None
            or self.observed_mtime_ns is not None
            or self.observed_sha256 is not None
        ):
            raise ValueError("unavailable toolchain fact cannot claim observations")
        if self.observed_sha256 is not None and (
            len(self.observed_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.observed_sha256)
        ):
            raise ValueError("toolchain digest must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class ArchetypeAssessment:
    repository_root: str
    project_id: str
    detected_archetypes: tuple[RepositoryArchetype, ...]
    configured_commands: tuple[VerificationCommandContract, ...]
    executable_toolchains: tuple[ToolchainMachineFact, ...]
    blockers: tuple[str, ...]
    support_level: ArchetypeSupportLevel
