"""Pure, deterministic dry-run planning for repository initialization."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from agentic_engineering_os.domain import (
    AGENTS_MANAGED_SECTION,
    GITIGNORE_MANAGED_SECTION,
    AgenticOsInitializationState,
    DocumentStatus,
    ExpectedFootprintEntry,
    InitializationFinding,
    InitializationOperationType,
    InitializationPlan,
    InitializationRepositoryIdentity,
    ManagedSectionObservation,
    ManagedSectionStatus,
    ObservationClassification,
    PlannedCurrentState,
    PlannedDesiredState,
    PlannedOperation,
    ProjectConfiguration,
    RepositoryProfile,
    RepositorySupportStatus,
)
from agentic_engineering_os.infrastructure.project_configuration import (
    CONFIG_DIRECTORY,
    CONFIG_FILENAME,
    ProjectConfigurationError,
    ProjectConfigurationValidator,
)


_CONFIG_PATH = f"{CONFIG_DIRECTORY}/{CONFIG_FILENAME}"
_RUNTIME_PATH = f"{CONFIG_DIRECTORY}/state.json"
_HEX = frozenset("0123456789abcdef")


class InitializationPlanningError(RuntimeError):
    """The planner input is not a recognized immutable planning contract."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class InitializationPlanner:
    """Transform immutable observations and explicit configuration into a dry run."""

    def __init__(
        self, *, validator: ProjectConfigurationValidator | None = None
    ) -> None:
        self._validator = validator or ProjectConfigurationValidator()

    @staticmethod
    def fingerprint(profile: RepositoryProfile) -> str:
        """Fingerprint every semantic observation in a RepositoryProfile."""

        if not isinstance(profile, RepositoryProfile):
            raise InitializationPlanningError(
                "INVALID_PROFILE", "RepositoryProfile is required"
            )
        return _sha256(_canonical_json(_canonical_value(profile)))

    def plan(
        self,
        profile: RepositoryProfile,
        desired_configuration: ProjectConfiguration | None,
        *,
        current_configuration: ProjectConfiguration | None = None,
        expected_profile_fingerprint: str | None = None,
    ) -> InitializationPlan:
        """Return a complete immutable plan without reading or writing the target."""

        profile_fingerprint = self.fingerprint(profile)
        desired_text, desired_error = self._configuration_text(
            desired_configuration, required=True
        )
        current_text, current_error = self._configuration_text(
            current_configuration, required=False
        )
        desired_hash = _sha256(desired_text) if desired_text is not None else "0" * 64
        current_hash = _sha256(current_text) if current_text is not None else None
        desired_version = (
            desired_configuration.config_version
            if isinstance(desired_configuration, ProjectConfiguration)
            else "UNKNOWN"
        )
        input_fingerprint = _sha256(
            _canonical_json(
                {
                    "current_configuration": current_text,
                    "desired_configuration": desired_text,
                    "profile_fingerprint": profile_fingerprint,
                }
            )
        )
        blockers: list[InitializationFinding] = []
        warnings: list[InitializationFinding] = [
            InitializationFinding(
                f"RECONNAISSANCE_{item.code}",
                item.source,
                item.detail,
            )
            for item in profile.issues
        ]

        self._validate_profile(profile, blockers)
        if expected_profile_fingerprint is not None:
            if not _is_sha256(expected_profile_fingerprint):
                blockers.append(
                    InitializationFinding(
                        "INVALID_EXPECTED_PROFILE_FINGERPRINT",
                        ".",
                        "expected profile fingerprint must be a lowercase SHA-256",
                    )
                )
            elif expected_profile_fingerprint != profile_fingerprint:
                blockers.append(
                    InitializationFinding(
                        "STALE_PROFILE",
                        ".",
                        "observed profile does not match the caller's expected snapshot",
                    )
                )
        if desired_error is not None:
            blockers.append(
                InitializationFinding(
                    "MISSING_OR_INVALID_DESIRED_CONFIGURATION",
                    _CONFIG_PATH,
                    desired_error,
                )
            )
        if current_error is not None:
            blockers.append(
                InitializationFinding(
                    "INVALID_CURRENT_CONFIGURATION_INPUT",
                    _CONFIG_PATH,
                    current_error,
                )
            )
        self._validate_current_configuration(
            profile, current_configuration, current_hash, blockers
        )

        state = profile.agentic_os.state
        if state is AgenticOsInitializationState.PARTIAL_OR_INCONSISTENT:
            blockers.append(
                InitializationFinding(
                    "PARTIAL_OR_INCONSISTENT_STATE",
                    CONFIG_DIRECTORY,
                    "partial initialization is never repaired automatically",
                )
            )
        elif state is AgenticOsInitializationState.UPGRADE_REQUIRED:
            blockers.append(
                InitializationFinding(
                    "UPGRADE_REQUIRED",
                    CONFIG_DIRECTORY,
                    "incompatible formats require an explicit future migration",
                )
            )

        config_status = profile.agentic_os.config_status
        if (
            desired_text is not None
            and current_text is not None
            and config_status is DocumentStatus.VALID
            and desired_text != current_text
        ):
            blockers.append(
                InitializationFinding(
                    "EXISTING_CONFIG_CONFLICT",
                    _CONFIG_PATH,
                    "existing valid configuration differs from the desired configuration",
                )
            )

        self._validate_managed_target(
            profile.agentic_os.agents_managed_section,
            "AGENTS.md",
            "AGENTS_MANAGED_SECTION_CONFLICT",
            blockers,
        )
        self._validate_managed_target(
            profile.agentic_os.gitignore_managed_section,
            ".gitignore",
            "GITIGNORE_MANAGED_SECTION_CONFLICT",
            blockers,
        )
        blockers = _unique_findings(blockers)

        if blockers:
            operations = tuple(
                PlannedOperation(
                    operation_id=f"OP-{index:03d}",
                    operation_type=InitializationOperationType.BLOCKED_CONFLICT,
                    target_path=item.target_path,
                    expected_current_state=_blocked_current_state(state),
                    desired_state=PlannedDesiredState.BLOCKED_UNCHANGED,
                    desired_content=None,
                    desired_content_sha256=None,
                    reason_code=item.code,
                    source="InitializationPlanner",
                    human_confirmation_required=False,
                )
                for index, item in enumerate(blockers, 1)
            )
            confirmations: tuple[str, ...] = ()
            expected = _blocked_footprint()
        else:
            operations, confirmations, plan_warnings = self._planned_operations(
                profile,
                desired_text,
                desired_hash,
            )
            warnings.extend(plan_warnings)
            expected = _expected_footprint()

        repository = InitializationRepositoryIdentity(
            repository_root=profile.requested_root,
            git_head=str(profile.git.head_commit.value or "UNKNOWN"),
            git_branch=(
                str(profile.git.branch.value)
                if profile.git.branch.value is not None
                else None
            ),
        )
        return InitializationPlan(
            repository=repository,
            profile_fingerprint=profile_fingerprint,
            input_fingerprint=input_fingerprint,
            current_initialization_state=state,
            desired_config_version=desired_version,
            desired_configuration=(
                desired_configuration if desired_text is not None else None
            ),
            desired_configuration_sha256=desired_hash,
            operations=operations,
            blockers=tuple(blockers),
            warnings=tuple(_unique_findings(warnings)),
            required_human_confirmations=confirmations,
            expected_footprint=expected,
            ready_for_application=not blockers and not confirmations,
        )

    def _configuration_text(
        self,
        configuration: ProjectConfiguration | None,
        *,
        required: bool,
    ) -> tuple[str | None, str | None]:
        if configuration is None:
            return (
                None,
                "an explicit valid ProjectConfiguration is required"
                if required
                else None,
            )
        try:
            return self._validator.serialize(configuration), None
        except ProjectConfigurationError as error:
            return None, f"{error.code}: {error.message}"

    def _validate_profile(
        self,
        profile: RepositoryProfile,
        blockers: list[InitializationFinding],
    ) -> None:
        git = profile.git
        if profile.support_status is not RepositorySupportStatus.SUPPORTED:
            blockers.append(
                InitializationFinding(
                    "REPOSITORY_NOT_SUPPORTED",
                    ".",
                    f"repository support status is {profile.support_status.value}",
                )
            )
        if not profile.scan_complete:
            blockers.append(
                InitializationFinding(
                    "INCOMPLETE_RECONNAISSANCE",
                    ".",
                    "bounded reconnaissance did not complete",
                )
            )
        branch_value = git.branch.value
        facts_valid = (
            git.is_repository.classification is ObservationClassification.FACT
            and git.is_repository.value is True
            and git.top_level.classification is ObservationClassification.FACT
            and isinstance(git.top_level.value, str)
            and git.head_commit.classification is ObservationClassification.FACT
            and isinstance(git.head_commit.value, str)
            and _is_sha1(git.head_commit.value)
            and git.clean.classification is ObservationClassification.FACT
            and isinstance(git.clean.value, bool)
            and git.branch.classification is ObservationClassification.FACT
            and (branch_value is None or isinstance(branch_value, str))
            and (branch_value is None or bool(branch_value))
            and git.detached.classification is ObservationClassification.FACT
            and isinstance(git.detached.value, bool)
            and git.detached.value == (branch_value is None)
            and not git.errors
        )
        root_matches = (
            isinstance(git.top_level.value, str)
            and Path(profile.requested_root).is_absolute()
            and _path_key(profile.requested_root) == _path_key(git.top_level.value)
        )
        worktree_matches = any(
            _path_key(item.path) == _path_key(profile.requested_root)
            and item.head_commit == git.head_commit.value
            and item.branch_name == branch_value
            for item in git.worktrees
        )
        if not facts_valid or not root_matches or not worktree_matches:
            blockers.append(
                InitializationFinding(
                    "INCONSISTENT_OR_FORGED_PROFILE",
                    ".",
                    "Git identity, root, HEAD, or primary worktree facts are inconsistent",
                )
            )
        if (
            git.clean.value is not True
            and profile.agentic_os.state
            is not AgenticOsInitializationState.INITIALIZED
        ):
            blockers.append(
                InitializationFinding(
                    "DIRTY_REPOSITORY",
                    ".",
                    "initialization planning is not applicable to a dirty repository",
                )
            )
        config = profile.agentic_os
        if (
            config.config_status is DocumentStatus.VALID
            and not _is_sha256(config.config_semantic_fingerprint)
        ) or (
            config.config_status is not DocumentStatus.VALID
            and config.config_semantic_fingerprint is not None
        ):
            blockers.append(
                InitializationFinding(
                    "INCONSISTENT_CONFIG_OBSERVATION",
                    _CONFIG_PATH,
                    "valid configuration lacks its canonical semantic fingerprint",
                )
            )
        uninitialized_consistent = (
            config.config_status is DocumentStatus.ABSENT
            and config.config_semantic_fingerprint is None
            and config.agents_managed_section.status
            in {
                ManagedSectionStatus.FILE_ABSENT,
                ManagedSectionStatus.SECTION_ABSENT,
            }
            and config.gitignore_managed_section.status
            in {
                ManagedSectionStatus.FILE_ABSENT,
                ManagedSectionStatus.SECTION_ABSENT,
            }
            and not config.gitignore_rules
            and all(item.status is DocumentStatus.ABSENT for item in config.runtime_files)
        )
        if (
            config.state is AgenticOsInitializationState.UNINITIALIZED
            and not uninitialized_consistent
        ) or (
            config.state is AgenticOsInitializationState.INITIALIZED
            and (
                config.config_status is not DocumentStatus.VALID
                or config.agents_managed_section.status
                is not ManagedSectionStatus.CURRENT
                or config.gitignore_managed_section.status
                is not ManagedSectionStatus.CURRENT
            )
        ):
            blockers.append(
                InitializationFinding(
                    "INCONSISTENT_INITIALIZATION_CLASSIFICATION",
                    CONFIG_DIRECTORY,
                    "initialization classification contradicts target observations",
                )
            )

    def _validate_current_configuration(
        self,
        profile: RepositoryProfile,
        current_configuration: ProjectConfiguration | None,
        current_hash: str | None,
        blockers: list[InitializationFinding],
    ) -> None:
        status = profile.agentic_os.config_status
        observed_hash = profile.agentic_os.config_semantic_fingerprint
        if status is DocumentStatus.VALID:
            if current_configuration is None or current_hash != observed_hash:
                blockers.append(
                    InitializationFinding(
                        "CURRENT_CONFIGURATION_MISMATCH",
                        _CONFIG_PATH,
                        "current configuration input does not match the observed semantic fingerprint",
                    )
                )
        elif current_configuration is not None:
            blockers.append(
                InitializationFinding(
                    "CURRENT_CONFIGURATION_MISMATCH",
                    _CONFIG_PATH,
                    f"configuration input was supplied while observed status is {status.value}",
                )
            )
        if status in {
            DocumentStatus.INVALID,
            DocumentStatus.TOO_LARGE,
            DocumentStatus.UNSAFE,
        }:
            blockers.append(
                InitializationFinding(
                    "CURRENT_CONFIG_INVALID",
                    _CONFIG_PATH,
                    f"observed configuration is {status.value}",
                )
            )
        elif status is DocumentStatus.UNKNOWN_VERSION:
            blockers.append(
                InitializationFinding(
                    "CURRENT_CONFIG_UPGRADE_REQUIRED",
                    _CONFIG_PATH,
                    "observed configuration version is unsupported",
                )
            )

    def _validate_managed_target(
        self,
        observation: ManagedSectionObservation,
        expected_path: str,
        code: str,
        blockers: list[InitializationFinding],
    ) -> None:
        safe_with_content = {
            ManagedSectionStatus.SECTION_ABSENT,
            ManagedSectionStatus.CURRENT,
            ManagedSectionStatus.TAMPERED,
            ManagedSectionStatus.AMBIGUOUS,
        }
        observation_inconsistent = (
            observation.relative_path != expected_path
            or (
                observation.status in safe_with_content
                and not _is_sha256(observation.content_fingerprint)
            )
            or (
                observation.status is ManagedSectionStatus.FILE_ABSENT
                and observation.content_fingerprint is not None
            )
        )
        if observation_inconsistent:
            blockers.append(
                InitializationFinding(
                    "INCONSISTENT_MANAGED_TARGET_OBSERVATION",
                    expected_path,
                    "managed target path, status, or content fingerprint is inconsistent",
                )
            )
        if observation.status in {
            ManagedSectionStatus.TAMPERED,
            ManagedSectionStatus.AMBIGUOUS,
            ManagedSectionStatus.UNSAFE,
            ManagedSectionStatus.UNKNOWN,
        }:
            blockers.append(
                InitializationFinding(
                    code,
                    observation.relative_path,
                    f"managed target status is {observation.status.value}",
                )
            )

    def _planned_operations(
        self,
        profile: RepositoryProfile,
        desired_text: str | None,
        desired_hash: str,
    ) -> tuple[
        tuple[PlannedOperation, ...],
        tuple[str, ...],
        tuple[InitializationFinding, ...],
    ]:
        if desired_text is None:
            raise InitializationPlanningError(
                "INTERNAL_PLANNING_ERROR", "validated desired configuration disappeared"
            )
        specifications: list[
            tuple[
                InitializationOperationType,
                str,
                PlannedCurrentState,
                PlannedDesiredState,
                str | None,
                str,
                str,
                bool,
            ]
        ] = []
        config_status = profile.agentic_os.config_status
        if config_status is DocumentStatus.ABSENT:
            specifications.append(
                (
                    InitializationOperationType.CREATE_DIRECTORY,
                    CONFIG_DIRECTORY,
                    PlannedCurrentState.ABSENT,
                    PlannedDesiredState.DIRECTORY_PRESENT,
                    None,
                    "CONFIG_DIRECTORY_REQUIRED",
                    "P5.1 footprint",
                    False,
                )
            )
            specifications.append(
                (
                    InitializationOperationType.INITIALIZE_CONFIG,
                    _CONFIG_PATH,
                    PlannedCurrentState.ABSENT,
                    PlannedDesiredState.CANONICAL_CONFIG_PRESENT,
                    desired_text,
                    "EXPLICIT_CONFIGURATION",
                    "ProjectConfigurationValidator",
                    False,
                )
            )
        else:
            specifications.append(
                (
                    InitializationOperationType.NO_OP,
                    _CONFIG_PATH,
                    PlannedCurrentState.PRESENT,
                    PlannedDesiredState.UNCHANGED,
                    None,
                    "CONFIG_ALREADY_IDENTICAL",
                    "observed config semantic fingerprint",
                    False,
                )
            )

        confirmations: list[str] = []
        warnings: list[InitializationFinding] = []
        specifications.extend(
            self._managed_operations(
                profile.agentic_os.agents_managed_section,
                AGENTS_MANAGED_SECTION,
                InitializationOperationType.UPDATE_MANAGED_SECTION,
                confirmations,
                warnings,
            )
        )
        specifications.extend(
            self._managed_operations(
                profile.agentic_os.gitignore_managed_section,
                GITIGNORE_MANAGED_SECTION,
                InitializationOperationType.ADD_GITIGNORE_SECTION,
                confirmations,
                warnings,
            )
        )
        operations = tuple(
            PlannedOperation(
                operation_id=f"OP-{index:03d}",
                operation_type=item[0],
                target_path=item[1],
                expected_current_state=item[2],
                desired_state=item[3],
                desired_content=item[4],
                desired_content_sha256=(
                    _sha256(item[4]) if item[4] is not None else None
                ),
                reason_code=item[5],
                source=item[6],
                human_confirmation_required=item[7],
            )
            for index, item in enumerate(specifications, 1)
        )
        if any(
            item.target_path == _CONFIG_PATH and item.desired_content is not None
            and item.desired_content_sha256 != desired_hash
            for item in operations
        ):
            raise InitializationPlanningError(
                "INTERNAL_PLANNING_ERROR", "planned configuration hash diverged"
            )
        return operations, tuple(confirmations), tuple(warnings)

    def _managed_operations(
        self,
        observation: ManagedSectionObservation,
        canonical_content: str,
        update_type: InitializationOperationType,
        confirmations: list[str],
        warnings: list[InitializationFinding],
    ) -> list[
        tuple[
            InitializationOperationType,
            str,
            PlannedCurrentState,
            PlannedDesiredState,
            str | None,
            str,
            str,
            bool,
        ]
    ]:
        if observation.status is ManagedSectionStatus.FILE_ABSENT:
            return [
                (
                    InitializationOperationType.CREATE_MANAGED_FILE,
                    observation.relative_path,
                    PlannedCurrentState.ABSENT,
                    PlannedDesiredState.CANONICAL_MANAGED_SECTION_PRESENT,
                    canonical_content,
                    "MANAGED_FILE_ABSENT",
                    observation.source,
                    False,
                )
            ]
        if observation.status is ManagedSectionStatus.SECTION_ABSENT:
            confirmation = f"CONFIRM_MANAGED_SECTION:{observation.relative_path}"
            confirmations.append(confirmation)
            warnings.append(
                InitializationFinding(
                    "HUMAN_CONFIRMATION_REQUIRED",
                    observation.relative_path,
                    "existing user file requires confirmation before managed-section insertion",
                )
            )
            return [
                (
                    update_type,
                    observation.relative_path,
                    PlannedCurrentState.SECTION_ABSENT,
                    PlannedDesiredState.CANONICAL_MANAGED_SECTION_PRESENT,
                    canonical_content,
                    "EXISTING_USER_FILE",
                    observation.source,
                    True,
                )
            ]
        return [
            (
                InitializationOperationType.NO_OP,
                observation.relative_path,
                PlannedCurrentState.MANAGED_SECTION_CURRENT,
                PlannedDesiredState.UNCHANGED,
                None,
                "MANAGED_SECTION_ALREADY_CURRENT",
                observation.source,
                False,
            )
        ]


def _blocked_current_state(
    state: AgenticOsInitializationState,
) -> PlannedCurrentState:
    if state is AgenticOsInitializationState.PARTIAL_OR_INCONSISTENT:
        return PlannedCurrentState.PARTIAL_OR_INCONSISTENT
    if state is AgenticOsInitializationState.UPGRADE_REQUIRED:
        return PlannedCurrentState.UPGRADE_REQUIRED
    return PlannedCurrentState.UNKNOWN


def _expected_footprint() -> tuple[ExpectedFootprintEntry, ...]:
    return (
        ExpectedFootprintEntry(
            CONFIG_DIRECTORY, PlannedDesiredState.DIRECTORY_PRESENT, False
        ),
        ExpectedFootprintEntry(
            _CONFIG_PATH, PlannedDesiredState.CANONICAL_CONFIG_PRESENT, False
        ),
        ExpectedFootprintEntry(
            ".gitignore",
            PlannedDesiredState.CANONICAL_MANAGED_SECTION_PRESENT,
            False,
        ),
        ExpectedFootprintEntry(
            "AGENTS.md",
            PlannedDesiredState.CANONICAL_MANAGED_SECTION_PRESENT,
            False,
        ),
        ExpectedFootprintEntry(
            _RUNTIME_PATH,
            PlannedDesiredState.RUNTIME_INITIALIZATION_DEFERRED,
            True,
        ),
    )


def _blocked_footprint() -> tuple[ExpectedFootprintEntry, ...]:
    return tuple(
        ExpectedFootprintEntry(path, PlannedDesiredState.BLOCKED_UNCHANGED, False)
        for path in (CONFIG_DIRECTORY, _CONFIG_PATH, ".gitignore", "AGENTS.md")
    )


def _unique_findings(
    findings: list[InitializationFinding],
) -> list[InitializationFinding]:
    return sorted(
        set(findings), key=lambda item: (item.code, item.target_path, item.detail)
    )


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (str, bool, int)) or value is None:
        return value
    raise InitializationPlanningError(
        "UNSUPPORTED_PROFILE_VALUE",
        f"profile contains unsupported value type: {type(value).__name__}",
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.casefold()
        and all(character in _HEX for character in value)
    )


def _is_sha1(value: str) -> bool:
    return (
        len(value) == 40
        and value == value.casefold()
        and all(character in _HEX for character in value)
    )


def _path_key(value: str) -> str:
    return os.path.normcase(os.path.abspath(value)).casefold()
