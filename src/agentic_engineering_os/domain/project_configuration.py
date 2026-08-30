"""Repository-local project configuration data contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RepositoryRootPolicy(str, Enum):
    """How the configured repository root is established at runtime."""

    CONFIG_PARENT_GIT_ROOT = "CONFIG_PARENT_GIT_ROOT"


class VerificationKind(str, Enum):
    TEST = "TEST"
    BUILD = "BUILD"
    LINT = "LINT"
    TYPECHECK = "TYPECHECK"
    OTHER = "OTHER"


class WorkingDirectoryPolicy(str, Enum):
    REPOSITORY_RELATIVE = "REPOSITORY_RELATIVE"


class CodexSandboxConstraint(str, Enum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


class CodexApprovalConstraint(str, Enum):
    NEVER = "never"


class MissionStateGitPolicy(str, Enum):
    TRACKED = "TRACKED"
    IGNORED = "IGNORED"


@dataclass(frozen=True, slots=True)
class ToolchainDeclaration:
    identity: str
    version_constraint: str | None


@dataclass(frozen=True, slots=True)
class VerificationCommand:
    command_id: str
    kind: VerificationKind
    executable: str
    args: tuple[str, ...]
    cwd: str
    cwd_policy: WorkingDirectoryPolicy
    required: bool


@dataclass(frozen=True, slots=True)
class ProjectPathPolicy:
    allowed_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CodexProjectConstraints:
    maximum_sandbox: CodexSandboxConstraint
    approval_policy: CodexApprovalConstraint
    require_clean_git: bool
    maximum_parallel_executions: int


@dataclass(frozen=True, slots=True)
class ProjectConfiguration:
    """Versioned configuration owned by one target repository."""

    config_version: str
    project_id: str
    repository_root_policy: RepositoryRootPolicy
    toolchains: tuple[ToolchainDeclaration, ...]
    verification_commands: tuple[VerificationCommand, ...]
    path_policy: ProjectPathPolicy
    context_sources: tuple[str, ...]
    codex_constraints: CodexProjectConstraints
    mission_state_git_policy: MissionStateGitPolicy
