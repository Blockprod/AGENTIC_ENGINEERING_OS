"""Read-only platform facts; never project authority or runtime policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PlatformFamily(str, Enum):
    WINDOWS = "WINDOWS"
    LINUX = "LINUX"
    MACOS = "MACOS"
    UNKNOWN = "UNKNOWN"


class PlatformCertification(str, Enum):
    WINDOWS_V1_TARGET = "WINDOWS_V1_TARGET"
    NOT_CERTIFIED = "NOT_CERTIFIED"


class CapabilityState(str, Enum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


class PathSemantics(str, Enum):
    WINDOWS_LOCAL = "WINDOWS_LOCAL"
    POSIX_UNCERTIFIED = "POSIX_UNCERTIFIED"
    UNKNOWN = "UNKNOWN"


class FilesystemScope(str, Enum):
    LOCAL = "LOCAL"
    NETWORK_OR_UNC = "NETWORK_OR_UNC"
    UNKNOWN = "UNKNOWN"


class CaseSemantics(str, Enum):
    WINDOWS_CASEFOLD_POLICY = "WINDOWS_CASEFOLD_POLICY"
    CASE_SENSITIVE = "CASE_SENSITIVE"
    UNKNOWN = "UNKNOWN"


class CaseSensitivityObservation(str, Enum):
    INSENSITIVE = "INSENSITIVE"
    SENSITIVE = "SENSITIVE"
    UNKNOWN = "UNKNOWN"


class ProcessTerminationSemantics(str, Enum):
    WINDOWS_PROCESS_TREE_FORCE_KILL = "WINDOWS_PROCESS_TREE_FORCE_KILL"
    DIRECT_CHILD_FORCE_KILL = "DIRECT_CHILD_FORCE_KILL"
    POSIX_UNCERTIFIED = "POSIX_UNCERTIFIED"
    UNKNOWN = "UNKNOWN"


class ExecutableDiscoveryMethod(str, Enum):
    EXPLICIT_PATH = "EXPLICIT_PATH"
    PATH_LOOKUP = "PATH_LOOKUP"
    CURRENT_PROCESS = "CURRENT_PROCESS"
    NOT_REQUESTED = "NOT_REQUESTED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ExecutableFact:
    identity: str
    state: CapabilityState
    path: str | None
    version: str | None
    sha256: str | None
    discovery_method: ExecutableDiscoveryMethod

    def __post_init__(self) -> None:
        if not isinstance(self.identity, str) or not self.identity.strip():
            raise ValueError("executable identity must be explicit")
        if self.state is CapabilityState.SUPPORTED and not self.path:
            raise ValueError("supported executable fact requires a path")
        if self.sha256 is not None and (
            len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise ValueError("executable digest must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class PlatformFacts:
    family: PlatformFamily
    certification: PlatformCertification
    path_semantics: PathSemantics
    executable_suffixes: tuple[str, ...]
    process_termination: ProcessTerminationSemantics
    core_shell_required: bool

    def __post_init__(self) -> None:
        if self.certification is PlatformCertification.WINDOWS_V1_TARGET and (
            self.family is not PlatformFamily.WINDOWS
            or self.path_semantics is not PathSemantics.WINDOWS_LOCAL
        ):
            raise ValueError("Windows V1 certification requires Windows path semantics")
        if self.core_shell_required:
            raise ValueError("the V1 core runtime must remain shell-free")


@dataclass(frozen=True, slots=True)
class MachineFacts:
    temporary_root: str | None
    temporary_root_writable: CapabilityState
    symlink_semantics: CapabilityState
    junction_semantics: CapabilityState
    case_sensitivity: CaseSensitivityObservation
    powershell: CapabilityState
    git: ExecutableFact
    codex: ExecutableFact
    python: ExecutableFact


@dataclass(frozen=True, slots=True)
class ProjectPlatformBinding:
    repository_root: str
    filesystem_scope: FilesystemScope
    case_semantics: CaseSemantics
    reparse_point: CapabilityState

    def __post_init__(self) -> None:
        if not isinstance(self.repository_root, str) or not self.repository_root:
            raise ValueError("repository binding requires an absolute observed root")


@dataclass(frozen=True, slots=True)
class PlatformCapabilities:
    platform: PlatformFacts
    machine: MachineFacts
    project: ProjectPlatformBinding

    def require_windows_v1_local_safety(self, *, require_codex: bool = False) -> None:
        """Fail closed when the observed binding is outside the certified V1 envelope."""

        if self.platform.certification is not PlatformCertification.WINDOWS_V1_TARGET:
            raise ValueError("UNSUPPORTED_PLATFORM")
        if self.project.filesystem_scope is not FilesystemScope.LOCAL:
            raise ValueError("UNSUPPORTED_FILESYSTEM")
        if self.project.reparse_point is not CapabilityState.UNSUPPORTED:
            raise ValueError("UNKNOWN_REPARSE_SEMANTICS")
        if self.machine.temporary_root_writable is not CapabilityState.SUPPORTED:
            raise ValueError("TEMPORARY_ROOT_UNAVAILABLE")
        if self.machine.git.state is not CapabilityState.SUPPORTED:
            raise ValueError("GIT_UNAVAILABLE")
        if require_codex and self.machine.codex.state is not CapabilityState.SUPPORTED:
            raise ValueError("CODEX_UNAVAILABLE")
