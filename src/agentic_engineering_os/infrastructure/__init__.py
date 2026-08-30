"""Local infrastructure adapters."""

from .codex_runtime_adapter import (
    CodexRuntimeAdapter,
    CodexRuntimeConfiguration,
)
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
    "GitAdapter",
    "GitDiffEntry",
    "GitMergePreflight",
    "GitMergeResult",
    "GitOperationError",
    "GitPrimaryState",
    "GitWorktree",
    "MissionStateStore",
    "PersistenceError",
    "ProjectStateStore",
    "WorktreeInspection",
    "WorktreeManager",
    "WorktreeManagerError",
    "WorktreeReconciliation",
    "WorktreeRegistryStore",
]
