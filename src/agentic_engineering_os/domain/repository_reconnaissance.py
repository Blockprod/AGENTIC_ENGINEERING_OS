"""Immutable observations produced by deterministic repository reconnaissance."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .project_configuration import MissionStateGitPolicy, VerificationKind


class ObservationClassification(str, Enum):
    FACT = "FACT"
    INFERENCE = "INFERENCE"
    UNKNOWN = "UNKNOWN"


class RepositorySupportStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class AgenticOsInitializationState(str, Enum):
    UNINITIALIZED = "UNINITIALIZED"
    INITIALIZED = "INITIALIZED"
    PARTIAL_OR_INCONSISTENT = "PARTIAL_OR_INCONSISTENT"
    UPGRADE_REQUIRED = "UPGRADE_REQUIRED"


class DocumentStatus(str, Enum):
    ABSENT = "ABSENT"
    VALID = "VALID"
    INVALID = "INVALID"
    UNKNOWN_VERSION = "UNKNOWN_VERSION"
    TOO_LARGE = "TOO_LARGE"
    UNSAFE = "UNSAFE"
    VERSION_OBSERVED = "VERSION_OBSERVED"


AGENTS_MANAGED_SECTION_VERSION = "2"
AGENTS_SECTION_START = (
    "<!-- BEGIN AGENTIC_ENGINEERING_OS MANAGED SECTION "
    f"v{AGENTS_MANAGED_SECTION_VERSION} -->"
)
AGENTS_SECTION_END = (
    "<!-- END AGENTIC_ENGINEERING_OS MANAGED SECTION "
    f"v{AGENTS_MANAGED_SECTION_VERSION} -->"
)
AGENTS_MANAGED_SECTION = "\n".join(
    (
        AGENTS_SECTION_START,
        "## Agentic Engineering OS",
        "",
        f"Managed contract version: {AGENTS_MANAGED_SECTION_VERSION}",
        "",
        "- AGENTIC_ENGINEERING_OS is the repository control and runtime layer.",
        "- Repository and Git truth prevail over agent declarations.",
        "- Stay within the explicit mission scope and preserve Human Authority.",
        "- Mutate authoritative state only through the designated controlled components.",
        "- Never edit `.agentic-engineering-os` runtime files directly.",
        "- Use the required role contracts, handoffs, and RoleResult outputs.",
        AGENTS_SECTION_END,
        "",
    )
)

GITIGNORE_MANAGED_SECTION_VERSION = "2"
GITIGNORE_SECTION_START = (
    "# BEGIN AGENTIC_ENGINEERING_OS MANAGED SECTION "
    f"v{GITIGNORE_MANAGED_SECTION_VERSION}"
)
GITIGNORE_SECTION_END = (
    "# END AGENTIC_ENGINEERING_OS MANAGED SECTION "
    f"v{GITIGNORE_MANAGED_SECTION_VERSION}"
)
GITIGNORE_V1_SECTION_START = "# BEGIN AGENTIC_ENGINEERING_OS MANAGED SECTION v1"
GITIGNORE_V1_SECTION_END = "# END AGENTIC_ENGINEERING_OS MANAGED SECTION v1"
GITIGNORE_MISSION_STATE_RULE = ".agentic-engineering-os/mission.json"
GITIGNORE_ORCHESTRATION_RECORD_RULE = ".agentic-engineering-os/orchestration.json"
GITIGNORE_ORCHESTRATION_TEMP_RULE = ".agentic-engineering-os/.orchestration.*.tmp"
_GITIGNORE_BASE_RULES = (
        GITIGNORE_SECTION_START,
        ".agentic-engineering-os/worktrees.json",
        ".agentic-engineering-os/.worktrees.*.tmp",
        ".agentic-engineering-os/negative-outcomes.json",
        ".agentic-engineering-os/.negative-outcomes.*.tmp",
        ".agentic-engineering-os/executions.json",
        ".agentic-engineering-os/.executions.*.tmp",
        ".agentic-engineering-os/maintenance.json",
        ".agentic-engineering-os/.maintenance.*.tmp",
        ".agentic-engineering-os/.maintenance.lock",
        ".agentic-engineering-os/operational-events/",
        GITIGNORE_ORCHESTRATION_RECORD_RULE,
        GITIGNORE_ORCHESTRATION_TEMP_RULE,
)

_GITIGNORE_V1_BASE_RULES = (
    GITIGNORE_V1_SECTION_START,
    *_GITIGNORE_BASE_RULES[1:-2],
)


def gitignore_managed_section(policy: MissionStateGitPolicy) -> str:
    """Return the bounded canonical section for an explicit mission Git policy."""

    if not isinstance(policy, MissionStateGitPolicy):
        raise TypeError("MissionStateGitPolicy is required")
    rules = _GITIGNORE_BASE_RULES
    if policy is MissionStateGitPolicy.IGNORED:
        rules += (GITIGNORE_MISSION_STATE_RULE,)
    return "\n".join(
        (
            *rules,
        GITIGNORE_SECTION_END,
        "",
        )
    )


# Backward-compatible canonical TRACKED section.
GITIGNORE_MANAGED_SECTION = gitignore_managed_section(MissionStateGitPolicy.TRACKED)


def gitignore_managed_section_v1(policy: MissionStateGitPolicy) -> str:
    """Return the recognized historical v1 section for explicit migration."""

    if not isinstance(policy, MissionStateGitPolicy):
        raise TypeError("MissionStateGitPolicy is required")
    rules = _GITIGNORE_V1_BASE_RULES
    if policy is MissionStateGitPolicy.IGNORED:
        rules += (GITIGNORE_MISSION_STATE_RULE,)
    return "\n".join((*rules, GITIGNORE_V1_SECTION_END, ""))


class ManagedSectionStatus(str, Enum):
    FILE_ABSENT = "FILE_ABSENT"
    SECTION_ABSENT = "SECTION_ABSENT"
    CURRENT = "CURRENT"
    TAMPERED = "TAMPERED"
    AMBIGUOUS = "AMBIGUOUS"
    UPGRADE_REQUIRED = "UPGRADE_REQUIRED"
    UNSAFE = "UNSAFE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ObservedValue:
    classification: ObservationClassification
    value: str | bool | int | None
    source: str
    detail: str


@dataclass(frozen=True, slots=True)
class GitWorktreeObservation:
    path: str
    head_commit: str
    branch_name: str | None


@dataclass(frozen=True, slots=True)
class GitRepositoryObservation:
    is_repository: ObservedValue
    top_level: ObservedValue
    branch: ObservedValue
    detached: ObservedValue
    head_commit: ObservedValue
    clean: ObservedValue
    worktrees: tuple[GitWorktreeObservation, ...]
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PathObservation:
    relative_path: str
    kind: str
    classification: ObservationClassification
    source: str


@dataclass(frozen=True, slots=True)
class SymlinkObservation:
    relative_path: str
    target_scope: str
    classification: ObservationClassification
    source: str


@dataclass(frozen=True, slots=True)
class ManifestObservation:
    relative_path: str
    format: str
    status: DocumentStatus
    classification: ObservationClassification
    detail: str


@dataclass(frozen=True, slots=True)
class ToolchainObservation:
    identity: str
    classification: ObservationClassification
    evidence_paths: tuple[str, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class CandidateCommandObservation:
    command_id: str
    kind: VerificationKind
    executable: str
    args: tuple[str, ...]
    classification: ObservationClassification
    source: str
    detail: str


@dataclass(frozen=True, slots=True)
class RuntimeFileObservation:
    relative_path: str
    status: DocumentStatus
    schema_version: str | None
    classification: ObservationClassification
    detail: str


@dataclass(frozen=True, slots=True)
class ManagedSectionObservation:
    relative_path: str
    status: ManagedSectionStatus
    content_fingerprint: str | None
    source: str
    detail: str


@dataclass(frozen=True, slots=True)
class AgenticOsStateObservation:
    state: AgenticOsInitializationState
    classification: ObservationClassification
    config_status: DocumentStatus
    config_version: str | None
    agents_reference: ObservedValue
    gitignore_rules: tuple[str, ...]
    agents_managed_section: ManagedSectionObservation
    gitignore_managed_section: ManagedSectionObservation
    config_semantic_fingerprint: str | None
    runtime_files: tuple[RuntimeFileObservation, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class ReconnaissanceIssue:
    code: str
    classification: ObservationClassification
    source: str
    detail: str


@dataclass(frozen=True, slots=True)
class RepositoryProfile:
    requested_root: str
    support_status: RepositorySupportStatus
    git: GitRepositoryObservation
    top_level_entries: tuple[PathObservation, ...]
    manifests: tuple[ManifestObservation, ...]
    toolchains: tuple[ToolchainObservation, ...]
    candidate_commands: tuple[CandidateCommandObservation, ...]
    context_sources: tuple[PathObservation, ...]
    sensitive_paths: tuple[PathObservation, ...]
    symlinks: tuple[SymlinkObservation, ...]
    agentic_os: AgenticOsStateObservation
    codex_availability: ObservedValue
    scan_complete: bool
    issues: tuple[ReconnaissanceIssue, ...]
