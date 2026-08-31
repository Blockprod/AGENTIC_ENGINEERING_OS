"""Application coordinator for explicit adoption of an existing repository."""

from __future__ import annotations

import os
from pathlib import Path

from agentic_engineering_os.domain import (
    AdoptionFinding,
    AdoptionPreparation,
    AdoptionResult,
    AdoptionStatus,
    AgenticOsInitializationState,
    DocumentStatus,
    HumanOperationConfirmation,
    InitializationApplyStatus,
    InitializationResult,
    ManagedSectionStatus,
    ProjectConfiguration,
    RepositoryProfile,
    RepositorySupportStatus,
    RuntimeBootstrapStatus,
)
from agentic_engineering_os.infrastructure.project_configuration import (
    ProjectConfigurationError,
    ProjectConfigurationLoader,
    ProjectConfigurationValidator,
)
from agentic_engineering_os.infrastructure.repository_initializer import (
    RepositoryInitializer,
)
from agentic_engineering_os.infrastructure.repository_reconnaissance import (
    RepositoryReconnaissance,
    RepositoryReconnaissanceError,
)
from agentic_engineering_os.infrastructure.runtime_state_bootstrap import (
    RuntimeStateBootstrap,
)

from .initialization_planner import InitializationPlanner, InitializationPlanningError


_EXPLICIT_CONFIGURATION_REQUIREMENTS = (
    "project_id",
    "toolchains",
    "verification_commands",
    "path_policy",
    "context_sources",
    "codex_constraints",
    "mission_state_git_policy",
)


class ExistingRepositoryAdoption:
    """Compose P5.2 through P5.7 without adding another write boundary."""

    def __init__(self) -> None:
        self._reconnaissance = RepositoryReconnaissance()
        self._validator = ProjectConfigurationValidator()
        self._planner = InitializationPlanner()
        self._initializer = RepositoryInitializer()
        self._runtime_bootstrap = RuntimeStateBootstrap()

    def prepare_adoption(
        self,
        repository_root: Path | str,
        project_configuration: ProjectConfiguration | None = None,
    ) -> AdoptionPreparation:
        """Return a deterministic dry-run and never mutate the target repository."""

        root_text = str(repository_root)
        try:
            profile = self._reconnaissance.inspect(repository_root)
        except (RepositoryReconnaissanceError, OSError) as error:
            return _preparation_blocked(
                root_text,
                "RECONNAISSANCE_FAILED",
                _error_detail(error),
            )

        state = profile.agentic_os.state
        if state is AgenticOsInitializationState.UPGRADE_REQUIRED:
            return _preparation_from_profile(
                profile,
                AdoptionStatus.UPGRADE_REQUIRED,
                "UPGRADE_REQUIRED",
                "existing Agentic OS data requires an explicit migration",
            )
        if state is AgenticOsInitializationState.PARTIAL_OR_INCONSISTENT:
            return _preparation_from_profile(
                profile,
                AdoptionStatus.PARTIAL_OR_INCONSISTENT,
                "PARTIAL_OR_INCONSISTENT",
                "existing Agentic OS footprint is not safe to complete implicitly",
            )
        runtime_observations = {
            item.relative_path: item for item in profile.agentic_os.runtime_files
        }
        project_state = runtime_observations.get(
            ".agentic-engineering-os/state.json"
        )
        if (
            project_state is not None
            and project_state.status is DocumentStatus.ABSENT
            and any(
                item.relative_path != ".agentic-engineering-os/state.json"
                and item.status is not DocumentStatus.ABSENT
                for item in profile.agentic_os.runtime_files
            )
        ):
            return _preparation_from_profile(
                profile,
                AdoptionStatus.PARTIAL_OR_INCONSISTENT,
                "PARTIAL_RUNTIME_FOOTPRINT",
                "runtime files exist without the required authoritative project state",
            )

        current_configuration: ProjectConfiguration | None = None
        if profile.agentic_os.config_status is DocumentStatus.VALID:
            try:
                current_configuration = ProjectConfigurationLoader(
                    Path(profile.requested_root)
                ).load()
            except ProjectConfigurationError as error:
                return _preparation_from_profile(
                    profile,
                    AdoptionStatus.BLOCKED,
                    f"CONFIGURATION_LOAD_FAILED:{error.code}",
                    error.message,
                )

        if project_configuration is None:
            if current_configuration is None:
                return AdoptionPreparation(
                    repository_root=profile.requested_root,
                    status=AdoptionStatus.NEEDS_CONFIGURATION,
                    repository_profile=profile,
                    project_configuration=None,
                    configuration_requirements=_EXPLICIT_CONFIGURATION_REQUIREMENTS,
                    initialization_plan=None,
                    required_human_confirmations=(),
                    findings=(
                        AdoptionFinding(
                            "EXPLICIT_CONFIGURATION_REQUIRED",
                            ".agentic-engineering-os/config.json",
                            "repository inference is not configuration authority",
                        ),
                    ),
                )
            desired = current_configuration
        else:
            try:
                self._validator.serialize(project_configuration)
            except ProjectConfigurationError as error:
                return _preparation_from_profile(
                    profile,
                    AdoptionStatus.BLOCKED,
                    f"INVALID_PROJECT_CONFIGURATION:{error.code}",
                    error.message,
                )
            desired = project_configuration

        try:
            plan = self._planner.plan(
                profile,
                desired,
                current_configuration=current_configuration,
                expected_profile_fingerprint=self._planner.fingerprint(profile),
            )
        except InitializationPlanningError as error:
            return _preparation_from_profile(
                profile,
                AdoptionStatus.BLOCKED,
                f"PLANNING_FAILED:{error.code}",
                error.message,
            )
        if plan.blockers:
            status = _status_for_blocked_plan(profile)
            return AdoptionPreparation(
                repository_root=profile.requested_root,
                status=status,
                repository_profile=profile,
                project_configuration=desired,
                configuration_requirements=(),
                initialization_plan=plan,
                required_human_confirmations=plan.required_human_confirmations,
                findings=tuple(
                    AdoptionFinding(item.code, item.target_path, item.detail)
                    for item in plan.blockers
                ),
            )
        status = (
            AdoptionStatus.NEEDS_HUMAN_CONFIRMATION
            if plan.required_human_confirmations
            else AdoptionStatus.READY_TO_APPLY
        )
        return AdoptionPreparation(
            repository_root=profile.requested_root,
            status=status,
            repository_profile=profile,
            project_configuration=desired,
            configuration_requirements=(),
            initialization_plan=plan,
            required_human_confirmations=plan.required_human_confirmations,
            findings=(),
        )

    def apply_adoption(
        self,
        preparation: AdoptionPreparation,
        *,
        human_confirmations: tuple[HumanOperationConfirmation, ...] = (),
    ) -> AdoptionResult:
        """Apply one exact preparation through initializer then runtime bootstrap."""

        if not isinstance(preparation, AdoptionPreparation):
            raise TypeError("apply_adoption requires AdoptionPreparation")
        plan = preparation.initialization_plan
        configuration = preparation.project_configuration
        if (
            preparation.status
            not in {
                AdoptionStatus.READY_TO_APPLY,
                AdoptionStatus.NEEDS_HUMAN_CONFIRMATION,
            }
            or plan is None
            or configuration is None
            or plan.desired_configuration != configuration
        ):
            return _result_without_application(
                preparation,
                "PREPARATION_NOT_APPLICABLE",
                "prepare_adoption must yield an applicable exact plan",
            )

        initialization = self._initializer.apply(
            plan, human_confirmations=human_confirmations
        )
        if initialization.status not in {
            InitializationApplyStatus.APPLIED,
            InitializationApplyStatus.NO_OP,
        }:
            final = _inspect_optional(self._reconnaissance, preparation.repository_root)
            status = _failure_status(initialization.status, final)
            return AdoptionResult(
                repository_root=preparation.repository_root,
                status=status,
                preparation=preparation,
                initialization_result=initialization,
                applied_operations=initialization.operation_results,
                runtime_bootstrap_result=None,
                final_repository_profile=final,
                findings=tuple(
                    AdoptionFinding(item.code, item.target_path, item.detail)
                    for item in initialization.findings
                ),
            )

        after_initialization = _inspect_optional(
            self._reconnaissance, preparation.repository_root
        )
        if after_initialization is None:
            return _failed_after_initialization(
                preparation,
                initialization,
                None,
                "POST_INITIALIZATION_RECONNAISSANCE_FAILED",
            )
        runtime = self._runtime_bootstrap.bootstrap(
            preparation.repository_root,
            configuration,
            expected_profile=after_initialization,
            initialization_result=(
                initialization
                if initialization.status is InitializationApplyStatus.APPLIED
                else None
            ),
        )
        final = _inspect_optional(self._reconnaissance, preparation.repository_root)
        if runtime.status not in {
            RuntimeBootstrapStatus.BOOTSTRAPPED,
            RuntimeBootstrapStatus.ALREADY_BOOTSTRAPPED,
        }:
            return AdoptionResult(
                repository_root=preparation.repository_root,
                status=(
                    AdoptionStatus.PARTIAL_OR_INCONSISTENT
                    if runtime.status is RuntimeBootstrapStatus.PARTIAL_FAILURE
                    or initialization.status is InitializationApplyStatus.APPLIED
                    else AdoptionStatus.BLOCKED
                ),
                preparation=preparation,
                initialization_result=initialization,
                applied_operations=initialization.operation_results,
                runtime_bootstrap_result=runtime,
                final_repository_profile=final,
                findings=tuple(
                    AdoptionFinding(item.code, item.target_path, item.detail)
                    for item in runtime.findings
                ),
            )
        readiness_error = _readiness_error(
            preparation.repository_profile, final, configuration
        )
        if readiness_error is not None:
            return AdoptionResult(
                repository_root=preparation.repository_root,
                status=AdoptionStatus.BLOCKED,
                preparation=preparation,
                initialization_result=initialization,
                applied_operations=initialization.operation_results,
                runtime_bootstrap_result=runtime,
                final_repository_profile=final,
                findings=(
                    AdoptionFinding(
                        readiness_error,
                        ".",
                        "final reconnaissance did not prove adoption readiness",
                    ),
                ),
            )
        return AdoptionResult(
            repository_root=preparation.repository_root,
            status=AdoptionStatus.ADOPTED,
            preparation=preparation,
            initialization_result=initialization,
            applied_operations=initialization.operation_results,
            runtime_bootstrap_result=runtime,
            final_repository_profile=final,
            findings=(),
        )


def _readiness_error(
    initial: RepositoryProfile | None,
    final: RepositoryProfile | None,
    configuration: ProjectConfiguration,
) -> str | None:
    if initial is None or final is None:
        return "FINAL_RECONNAISSANCE_UNAVAILABLE"
    if (
        final.support_status is not RepositorySupportStatus.SUPPORTED
        or not final.scan_complete
        or final.agentic_os.state is not AgenticOsInitializationState.INITIALIZED
        or final.agentic_os.config_status is not DocumentStatus.VALID
        or final.agentic_os.agents_managed_section.status
        is not ManagedSectionStatus.CURRENT
        or final.agentic_os.gitignore_managed_section.status
        is not ManagedSectionStatus.CURRENT
    ):
        return "FINAL_STRUCTURE_NOT_READY"
    state = next(
        (
            item
            for item in final.agentic_os.runtime_files
            if item.relative_path == ".agentic-engineering-os/state.json"
        ),
        None,
    )
    if state is None or state.status is not DocumentStatus.VERSION_OBSERVED:
        return "FINAL_RUNTIME_NOT_READY"
    if not _same_git_identity(initial, final):
        return "GIT_IDENTITY_CHANGED"
    try:
        loaded = ProjectConfigurationLoader(Path(final.requested_root)).load()
    except ProjectConfigurationError:
        return "FINAL_CONFIGURATION_INVALID"
    if loaded != configuration:
        return "FINAL_CONFIGURATION_MISMATCH"
    return None


def _same_git_identity(left: RepositoryProfile, right: RepositoryProfile) -> bool:
    return (
        _path_key(left.requested_root) == _path_key(right.requested_root)
        and left.git.top_level.value == right.git.top_level.value
        and left.git.head_commit.value == right.git.head_commit.value
        and left.git.branch.value == right.git.branch.value
        and left.git.detached.value == right.git.detached.value
        and left.git.worktrees == right.git.worktrees
    )


def _status_for_blocked_plan(profile: RepositoryProfile) -> AdoptionStatus:
    if profile.agentic_os.state is AgenticOsInitializationState.UPGRADE_REQUIRED:
        return AdoptionStatus.UPGRADE_REQUIRED
    if profile.agentic_os.state is AgenticOsInitializationState.PARTIAL_OR_INCONSISTENT:
        return AdoptionStatus.PARTIAL_OR_INCONSISTENT
    return AdoptionStatus.BLOCKED


def _failure_status(
    status: InitializationApplyStatus, profile: RepositoryProfile | None
) -> AdoptionStatus:
    if status is InitializationApplyStatus.PARTIAL_FAILURE or (
        profile is not None
        and profile.agentic_os.state
        is AgenticOsInitializationState.PARTIAL_OR_INCONSISTENT
    ):
        return AdoptionStatus.PARTIAL_OR_INCONSISTENT
    return AdoptionStatus.BLOCKED


def _preparation_from_profile(
    profile: RepositoryProfile,
    status: AdoptionStatus,
    code: str,
    detail: str,
) -> AdoptionPreparation:
    return AdoptionPreparation(
        repository_root=profile.requested_root,
        status=status,
        repository_profile=profile,
        project_configuration=None,
        configuration_requirements=(),
        initialization_plan=None,
        required_human_confirmations=(),
        findings=(AdoptionFinding(code, ".agentic-engineering-os", detail),),
    )


def _preparation_blocked(root: str, code: str, detail: str) -> AdoptionPreparation:
    return AdoptionPreparation(
        repository_root=root,
        status=AdoptionStatus.BLOCKED,
        repository_profile=None,
        project_configuration=None,
        configuration_requirements=(),
        initialization_plan=None,
        required_human_confirmations=(),
        findings=(AdoptionFinding(code, ".", detail),),
    )


def _result_without_application(
    preparation: AdoptionPreparation, code: str, detail: str
) -> AdoptionResult:
    return AdoptionResult(
        repository_root=preparation.repository_root,
        status=AdoptionStatus.BLOCKED,
        preparation=preparation,
        initialization_result=None,
        applied_operations=(),
        runtime_bootstrap_result=None,
        final_repository_profile=preparation.repository_profile,
        findings=(AdoptionFinding(code, ".", detail),),
    )


def _failed_after_initialization(
    preparation: AdoptionPreparation,
    initialization: InitializationResult,
    final: RepositoryProfile | None,
    code: str,
) -> AdoptionResult:
    return AdoptionResult(
        repository_root=preparation.repository_root,
        status=AdoptionStatus.BLOCKED,
        preparation=preparation,
        initialization_result=initialization,
        applied_operations=initialization.operation_results,
        runtime_bootstrap_result=None,
        final_repository_profile=final,
        findings=(AdoptionFinding(code, ".", "adoption stopped without rollback"),),
    )


def _inspect_optional(
    reconnaissance: RepositoryReconnaissance, root: Path | str
) -> RepositoryProfile | None:
    try:
        return reconnaissance.inspect(root)
    except (RepositoryReconnaissanceError, OSError):
        return None


def _error_detail(error: object) -> str:
    code = getattr(error, "code", type(error).__name__)
    return str(code)


def _path_key(value: str) -> str:
    return os.path.normcase(os.path.abspath(value)).casefold()
