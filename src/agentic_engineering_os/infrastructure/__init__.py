"""Local infrastructure adapters."""

from .agents_integration import AgentsIntegrationError, AgentsIntegrationService
from .codex_runtime_adapter import (
    CodexRuntimeAdapter,
    CodexRuntimeConfiguration,
)
from .codex_capability_discovery import CodexCapabilityDiscovery
from .execution_git_observer import ExecutionGitObserver
from .execution_state_store import ExecutionStateStore
from .mission_state_store import MissionStateStore
from .maintenance_state_store import (
    MAINTENANCE_FILENAME,
    MAINTENANCE_LOCK_FILENAME,
    MAX_MAINTENANCE_BYTES,
    MaintenanceStateStore,
)
from .operational_event_store import (
    DEFAULT_MAX_SEGMENT_BYTES,
    DEFAULT_MAX_SEGMENTS,
    MAX_STORE_RECORD_BYTES,
    OPERATIONAL_EVENT_DIRECTORY,
    OPERATIONAL_EVENT_STORE_VERSION,
    OperationalEventAppendReceipt,
    OperationalEventQuery,
    OperationalEventStore,
    OperationalEventStoreError,
    StructuredEventLogger,
)
from .git_adapter import (
    GitAdapter,
    GitDiffEntry,
    GitMergePreflight,
    GitMergeResult,
    GitOperationError,
    GitPrimaryState,
    GitReadOnlyState,
    GitWorktree,
)
from .repository_reconnaissance import (
    RepositoryReconnaissance,
    RepositoryReconnaissanceError,
)
from .repository_initializer import RepositoryInitializer
from .runtime_state_bootstrap import RuntimeStateBootstrap
from .migration_registry import MigrationRegistryError, RepositoryMigrationRegistry
from .repository_upgrade_service import RepositoryUpgradeService
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
from .incident_event_journal import (
    IncidentEventJournal,
    IncidentEventJournalError,
    incident_record_from_operational_event,
    incident_record_to_operational_event,
)
from .platform_environment import (
    RUNTIME_ENVIRONMENT_ALLOWLIST,
    PlatformDiscoveryError,
    PlatformEnvironmentProbe,
    build_bounded_environment,
    discover_executable,
    environment_value,
    windows_contract_path_key,
)
from .repository_archetype import (
    RepositoryArchetypeError,
    RepositoryArchetypeEvaluator,
    RepositoryArchetypeProfiler,
    RepositoryToolchainProbe,
)

__all__ = [
    "CodexCapabilityDiscovery",
    "RepositoryArchetypeError",
    "RepositoryArchetypeEvaluator",
    "RepositoryArchetypeProfiler",
    "RepositoryToolchainProbe",
    "RUNTIME_ENVIRONMENT_ALLOWLIST",
    "PlatformDiscoveryError",
    "PlatformEnvironmentProbe",
    "build_bounded_environment",
    "discover_executable",
    "environment_value",
    "windows_contract_path_key",
    "MAINTENANCE_FILENAME",
    "MAINTENANCE_LOCK_FILENAME",
    "MAX_MAINTENANCE_BYTES",
    "MaintenanceStateStore",
    "AgentsIntegrationError",
    "AgentsIntegrationService",
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
    "GitReadOnlyState",
    "GitWorktree",
    "MissionStateStore",
    "DEFAULT_MAX_SEGMENT_BYTES",
    "DEFAULT_MAX_SEGMENTS",
    "MAX_STORE_RECORD_BYTES",
    "OPERATIONAL_EVENT_DIRECTORY",
    "OPERATIONAL_EVENT_STORE_VERSION",
    "OperationalEventAppendReceipt",
    "OperationalEventQuery",
    "OperationalEventStore",
    "OperationalEventStoreError",
    "PersistenceError",
    "CONFIG_DIRECTORY",
    "CONFIG_FILENAME",
    "CONFIG_VERSION",
    "ProjectConfigurationError",
    "ProjectConfigurationLoader",
    "ProjectConfigurationValidator",
    "ProjectStateStore",
    "RepositoryReconnaissance",
    "RepositoryReconnaissanceError",
    "RepositoryInitializer",
    "RuntimeStateBootstrap",
    "StructuredEventLogger",
    "MigrationRegistryError",
    "RepositoryMigrationRegistry",
    "RepositoryUpgradeService",
    "WorktreeInspection",
    "WorktreeManager",
    "WorktreeManagerError",
    "WorktreeReconciliation",
    "WorktreeRegistryStore",
    "IncidentEventJournal",
    "IncidentEventJournalError",
    "incident_record_from_operational_event",
    "incident_record_to_operational_event",
]
