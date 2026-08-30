"""Local infrastructure adapters."""

from .codex_runtime_adapter import (
    CodexRuntimeAdapter,
    CodexRuntimeConfiguration,
)
from .execution_git_observer import ExecutionGitObserver
from .execution_state_store import ExecutionStateStore
from .mission_state_store import MissionStateStore
from .git_adapter import (
    GitAdapter,
    GitDiffEntry,
    GitMergePreflight,
    GitMergeResult,
    GitOperationError,
    GitPrimaryState,
    GitWorktree,
)
from .project_state_store import PersistenceError, ProjectStateStore
from .project_configuration import (
    CONFIG_DIRECTORY,
    CONFIG_FILENAME,
    CONFIG_VERSION,
    ProjectConfigurationError,
    ProjectConfigurationLoader,
    ProjectConfigurationValidator,
)
from .worktree_manager import (
    WorktreeInspection,
    WorktreeManager,
    WorktreeManagerError,
    WorktreeReconciliation,
)
from .worktree_registry_store import WorktreeRegistryStore

__all__ = [
    "CodexRuntimeAdapter",
    "CodexRuntimeConfiguration",
    "ExecutionGitObserver",
    "ExecutionStateStore",
    "GitAdapter",
    "GitDiffEntry",
    "GitMergePreflight",
    "GitMergeResult",
    "GitOperationError",
    "GitPrimaryState",
    "GitWorktree",
    "MissionStateStore",
    "PersistenceError",
    "CONFIG_DIRECTORY",
    "CONFIG_FILENAME",
    "CONFIG_VERSION",
    "ProjectConfigurationError",
    "ProjectConfigurationLoader",
    "ProjectConfigurationValidator",
    "ProjectStateStore",
    "WorktreeInspection",
    "WorktreeManager",
    "WorktreeManagerError",
    "WorktreeReconciliation",
    "WorktreeRegistryStore",
]
