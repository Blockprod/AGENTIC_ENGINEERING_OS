"""Fail-closed bootstrap of the minimal authoritative runtime state."""

from __future__ import annotations

import os
import hashlib
import json
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from agentic_engineering_os.domain import (
    AgenticOsInitializationState,
    DocumentStatus,
    InitializationApplyStatus,
    InitializationResult,
    MissionStateGitPolicy,
    OperationApplyStatus,
    ProjectConfiguration,
    ProjectState,
    RepositoryProfile,
    RuntimeBootstrapFinding,
    RuntimeBootstrapResult,
    RuntimeBootstrapStatus,
    RuntimeFileFact,
    RuntimeStoreDisposition,
)

from .git_adapter import GitAdapter, GitOperationError, GitReadOnlyState
from .project_configuration import (
    CONFIG_DIRECTORY,
    CONFIG_FILENAME,
    ProjectConfigurationError,
    ProjectConfigurationLoader,
    ProjectConfigurationValidator,
)
from .project_state_store import (
    PersistenceError,
    ProjectStateStore,
    SCHEMA_VERSION,
    STATE_DIRECTORY,
    STATE_FILENAME,
)
from .repository_reconnaissance import (
    RepositoryReconnaissance,
    RepositoryReconnaissanceError,
)


_STATE_PATH = f"{STATE_DIRECTORY}/{STATE_FILENAME}"
_CONFIG_PATH = f"{CONFIG_DIRECTORY}/{CONFIG_FILENAME}"
_MISSION_PATH = f"{STATE_DIRECTORY}/mission.json"
_LAZY_PATHS = (
    f"{STATE_DIRECTORY}/executions.json",
    f"{STATE_DIRECTORY}/negative-outcomes.json",
    f"{STATE_DIRECTORY}/worktrees.json",
)
_VOLATILE_IGNORE_PROBES = (
    f"{STATE_DIRECTORY}/worktrees.json",
    f"{STATE_DIRECTORY}/.worktrees.bootstrap-check.tmp",
    f"{STATE_DIRECTORY}/negative-outcomes.json",
    f"{STATE_DIRECTORY}/.negative-outcomes.bootstrap-check.tmp",
    f"{STATE_DIRECTORY}/executions.json",
    f"{STATE_DIRECTORY}/.executions.bootstrap-check.tmp",
)
_STRUCTURAL_HANDOFF_PATHS = frozenset(
    {_CONFIG_PATH, "AGENTS.md", ".gitignore"}
)


class RuntimeStateBootstrap:
    """Create only canonical empty ProjectState after exact repository preflight."""

    STORE_DISPOSITIONS = (
        (_STATE_PATH, RuntimeStoreDisposition.REQUIRED_AT_BOOTSTRAP),
        (_MISSION_PATH, RuntimeStoreDisposition.AUTHORIZED_EVENT_ONLY),
        *(
            (path, RuntimeStoreDisposition.LAZY_INITIALIZED_ON_FIRST_USE)
            for path in _LAZY_PATHS
        ),
    )

    def __init__(self) -> None:
        self._reconnaissance = RepositoryReconnaissance()
        self._configuration_validator = ProjectConfigurationValidator()

    def bootstrap(
        self,
        repository_root: Path | str,
        project_configuration: ProjectConfiguration,
        *,
        expected_profile: RepositoryProfile,
        initialization_result: InitializationResult | None = None,
    ) -> RuntimeBootstrapResult:
        """Validate freshness and initialize state through ProjectStateStore only."""

        root, root_error = _resolve_root(repository_root)
        if root is None:
            return _result_without_profile(
                str(repository_root),
                RuntimeBootstrapStatus.REFUSED,
                root_error or "INVALID_REPOSITORY_ROOT",
            )
        if not isinstance(expected_profile, RepositoryProfile):
            return _result_without_profile(
                str(root), RuntimeBootstrapStatus.REFUSED, "INVALID_EXPECTED_PROFILE"
            )
        expected_fingerprint = self._safe_fingerprint(expected_profile)
        if expected_fingerprint is None:
            return _result_without_profile(
                str(root), RuntimeBootstrapStatus.REFUSED, "INVALID_EXPECTED_PROFILE"
            )

        try:
            self._configuration_validator.serialize(project_configuration)
            current_configuration = ProjectConfigurationLoader(root).load()
            current = self._reconnaissance.inspect(root)
            baseline_git = GitAdapter(root).observe_read_only()
        except (
            ProjectConfigurationError,
            RepositoryReconnaissanceError,
            GitOperationError,
            OSError,
        ) as error:
            return _result_without_profile(
                str(root),
                RuntimeBootstrapStatus.REFUSED,
                f"PREFLIGHT_FAILED:{_error_code(error)}",
                expected_fingerprint=expected_fingerprint,
            )

        before_fingerprint = self._safe_fingerprint(current)
        before_facts = _runtime_facts(current)
        if (
            current != expected_profile
            or before_fingerprint != expected_fingerprint
            or _path_key(Path(expected_profile.requested_root)) != _path_key(root)
        ):
            return _refused(
                root,
                current,
                expected_fingerprint,
                before_fingerprint,
                before_facts,
                "STALE_OR_FOREIGN_PROFILE",
            )
        if current_configuration != project_configuration:
            return _refused(
                root,
                current,
                expected_fingerprint,
                before_fingerprint,
                before_facts,
                "PROJECT_CONFIGURATION_MISMATCH",
            )

        observations = {item.relative_path: item for item in current.agentic_os.runtime_files}
        state_observation = observations.get(_STATE_PATH)
        if state_observation is None:
            return _refused(
                root,
                current,
                expected_fingerprint,
                before_fingerprint,
                before_facts,
                "STATE_OBSERVATION_MISSING",
            )
        incompatible = tuple(
            item.relative_path
            for item in current.agentic_os.runtime_files
            if item.status is DocumentStatus.UNKNOWN_VERSION
        )
        if incompatible:
            return _refused(
                root,
                current,
                expected_fingerprint,
                before_fingerprint,
                before_facts,
                "UPGRADE_REQUIRED",
                detail=f"unsupported runtime versions: {', '.join(incompatible)}",
            )
        invalid = tuple(
            item.relative_path
            for item in current.agentic_os.runtime_files
            if item.status
            in {
                DocumentStatus.INVALID,
                DocumentStatus.TOO_LARGE,
                DocumentStatus.UNSAFE,
            }
        )
        if invalid:
            return _refused(
                root,
                current,
                expected_fingerprint,
                before_fingerprint,
                before_facts,
                "RUNTIME_STORE_INVALID",
                detail=f"invalid runtime files: {', '.join(invalid)}",
            )
        if current.agentic_os.state is not AgenticOsInitializationState.INITIALIZED:
            return _refused(
                root,
                current,
                expected_fingerprint,
                before_fingerprint,
                before_facts,
                "STRUCTURAL_INITIALIZATION_REQUIRED",
            )
        if state_observation.status is DocumentStatus.ABSENT and any(
            item.relative_path != _STATE_PATH
            and item.status is not DocumentStatus.ABSENT
            for item in current.agentic_os.runtime_files
        ):
            return _refused(
                root,
                current,
                expected_fingerprint,
                before_fingerprint,
                before_facts,
                "PARTIAL_RUNTIME_FOOTPRINT",
            )

        policy_error = self._git_policy_error(root, project_configuration)
        if policy_error is not None:
            return _refused(
                root,
                current,
                expected_fingerprint,
                before_fingerprint,
                before_facts,
                policy_error,
            )

        store = ProjectStateStore(root)
        if state_observation.status is DocumentStatus.VERSION_OBSERVED:
            try:
                loaded = store.load()
            except PersistenceError as error:
                return _refused(
                    root,
                    current,
                    expected_fingerprint,
                    before_fingerprint,
                    before_facts,
                    f"STATE_VALIDATION_FAILED:{error.code}",
                )
            if loaded.project_id != project_configuration.project_id:
                return _refused(
                    root,
                    current,
                    expected_fingerprint,
                    before_fingerprint,
                    before_facts,
                    "STATE_PROJECT_BINDING_MISMATCH",
                )
            return _success_result(
                root,
                RuntimeBootstrapStatus.ALREADY_BOOTSTRAPPED,
                current,
                current,
                expected_fingerprint,
                before_fingerprint,
                before_facts,
                (),
            )
        if state_observation.status is not DocumentStatus.ABSENT:
            return _refused(
                root,
                current,
                expected_fingerprint,
                before_fingerprint,
                before_facts,
                "STATE_STATUS_NOT_BOOTSTRAPPABLE",
            )
        if project_configuration.codex_constraints.require_clean_git and not (
            current.git.clean.value is True and baseline_git.clean
        ):
            handoff_error = _structural_handoff_error(
                root,
                current,
                before_fingerprint,
                baseline_git,
                initialization_result,
            )
            if handoff_error is not None:
                return _refused(
                    root,
                    current,
                    expected_fingerprint,
                    before_fingerprint,
                    before_facts,
                    handoff_error,
                )

        try:
            immediate = self._reconnaissance.inspect(root)
            immediate_git = GitAdapter(root).observe_read_only()
        except (RepositoryReconnaissanceError, GitOperationError, OSError) as error:
            return _refused(
                root,
                current,
                expected_fingerprint,
                before_fingerprint,
                before_facts,
                f"FRESHNESS_REVALIDATION_FAILED:{_error_code(error)}",
            )
        if immediate != current or not _same_git_identity(baseline_git, immediate_git):
            return _refused(
                root,
                current,
                expected_fingerprint,
                before_fingerprint,
                before_facts,
                "REPOSITORY_CHANGED_BEFORE_BOOTSTRAP",
            )

        created = False
        try:
            initialized = store.initialize(project_id=project_configuration.project_id)
            created = True
            loaded = store.load()
            if not _canonical_empty(
                initialized, project_configuration.project_id
            ) or not _canonical_empty(loaded, project_configuration.project_id):
                raise PersistenceError(
                    "NON_EMPTY_BOOTSTRAP_STATE",
                    "ProjectStateStore did not return canonical empty state",
                )
            after = self._reconnaissance.inspect(root)
            after_git = GitAdapter(root).observe_read_only()
            after_state = next(
                item for item in after.agentic_os.runtime_files if item.relative_path == _STATE_PATH
            )
            if (
                after_state.status is not DocumentStatus.VERSION_OBSERVED
                or not _same_git_identity(baseline_git, after_git)
                or any((root / Path(path)).exists() for path in (*_LAZY_PATHS, _MISSION_PATH))
            ):
                raise PersistenceError(
                    "BOOTSTRAP_VERIFICATION_FAILED",
                    "post-bootstrap runtime footprint or Git identity is incoherent",
                )
        except (
            PersistenceError,
            RepositoryReconnaissanceError,
            GitOperationError,
            OSError,
            StopIteration,
        ) as error:
            after = _observe_optional(self._reconnaissance, root)
            after_facts = _runtime_facts(after) if after is not None else ()
            state_present = any(
                item.relative_path == _STATE_PATH
                and item.status is not DocumentStatus.ABSENT
                for item in after_facts
            )
            status = (
                RuntimeBootstrapStatus.PARTIAL_FAILURE
                if state_present
                else RuntimeBootstrapStatus.FAILED
            )
            return RuntimeBootstrapResult(
                repository_root=str(root),
                status=status,
                expected_profile_fingerprint=expected_fingerprint,
                profile_fingerprint_before=before_fingerprint,
                profile_fingerprint_after=(
                    self._safe_fingerprint(after) if after is not None else None
                ),
                git_head_before=baseline_git.head_commit,
                git_head_after=_profile_head(after),
                runtime_files_before=before_facts,
                runtime_files_after=after_facts,
                created_paths=(_STATE_PATH,) if created else (),
                findings=(
                    RuntimeBootstrapFinding(
                        f"BOOTSTRAP_FAILED:{_error_code(error)}",
                        _STATE_PATH,
                        "runtime bootstrap stopped without rollback",
                    ),
                ),
            )

        return _success_result(
            root,
            RuntimeBootstrapStatus.BOOTSTRAPPED,
            current,
            after,
            expected_fingerprint,
            before_fingerprint,
            before_facts,
            (_STATE_PATH,),
        )

    def _safe_fingerprint(self, profile: RepositoryProfile | None) -> str | None:
        if profile is None:
            return None
        try:
            return _profile_fingerprint(profile)
        except Exception:
            return None

    def _git_policy_error(
        self, root: Path, configuration: ProjectConfiguration
    ) -> str | None:
        adapter = GitAdapter(root)
        try:
            if any(adapter.is_ignored(path) for path in (_CONFIG_PATH, _STATE_PATH)):
                return "VERSIONED_STATE_IS_IGNORED"
            if not all(adapter.is_ignored(path) for path in _VOLATILE_IGNORE_PROBES):
                return "VOLATILE_RUNTIME_NOT_IGNORED"
            mission_ignored = adapter.is_ignored(_MISSION_PATH)
        except GitOperationError:
            return "GITIGNORE_POLICY_UNKNOWN"
        if (
            configuration.mission_state_git_policy is MissionStateGitPolicy.TRACKED
            and mission_ignored
        ) or (
            configuration.mission_state_git_policy is MissionStateGitPolicy.IGNORED
            and not mission_ignored
        ):
            return "MISSION_GIT_POLICY_MISMATCH"
        return None


def _resolve_root(value: Path | str) -> tuple[Path | None, str | None]:
    candidate = Path(value)
    if not candidate.is_absolute() or candidate.is_symlink():
        return None, "INVALID_REPOSITORY_ROOT"
    cursor = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        cursor = cursor / part
        if cursor.is_symlink():
            return None, "SYMLINK_REPOSITORY_ROOT"
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None, "INVALID_REPOSITORY_ROOT"
    if not resolved.is_dir() or _path_key(resolved) != _path_key(candidate):
        return None, "INVALID_REPOSITORY_ROOT"
    return resolved, None


def _canonical_empty(state: ProjectState, project_id: str) -> bool:
    return (
        isinstance(state, ProjectState)
        and state.schema_version == SCHEMA_VERSION
        and state.project_id == project_id
        and not state.user_stories
        and not state.evidence
        and not state.gates
        and not state.certifications
        and not state.audit_events
    )


def _structural_handoff_error(
    root: Path,
    profile: RepositoryProfile,
    profile_fingerprint: str | None,
    git_state: GitReadOnlyState,
    result: InitializationResult | None,
) -> str | None:
    """Authorize only the exact dirty footprint produced by a successful initializer."""

    if not isinstance(result, InitializationResult):
        return "DIRTY_REPOSITORY"
    if (
        result.status is not InitializationApplyStatus.APPLIED
        or result.findings
        or _path_key(Path(result.repository_root)) != _path_key(root)
        or result.profile_fingerprint_after != profile_fingerprint
        or result.git_head_before != git_state.head_commit
        or result.git_head_after != git_state.head_commit
        or result.initialization_state_after
        is not AgenticOsInitializationState.INITIALIZED
    ):
        return "INVALID_INITIALIZATION_HANDOFF"
    applied_paths = {
        item.target_path
        for item in result.operation_results
        if item.status is OperationApplyStatus.APPLIED
        and item.target_path in _STRUCTURAL_HANDOFF_PATHS
    }
    if not applied_paths or any(
        item.status not in {OperationApplyStatus.APPLIED, OperationApplyStatus.NO_OP}
        for item in result.operation_results
    ):
        return "INVALID_INITIALIZATION_HANDOFF"
    try:
        changed_paths = set(GitAdapter(root).worktree_changed_paths(root))
    except GitOperationError:
        return "INITIALIZATION_HANDOFF_GIT_STATE_UNKNOWN"
    if changed_paths != applied_paths:
        return "INITIALIZATION_HANDOFF_DIRTY_PATH_MISMATCH"
    return None


def _runtime_facts(profile: RepositoryProfile | None) -> tuple[RuntimeFileFact, ...]:
    if profile is None:
        return ()
    return tuple(
        RuntimeFileFact(item.relative_path, item.status)
        for item in profile.agentic_os.runtime_files
    )


def _observe_optional(
    reconnaissance: RepositoryReconnaissance, root: Path
) -> RepositoryProfile | None:
    try:
        return reconnaissance.inspect(root)
    except (RepositoryReconnaissanceError, OSError):
        return None


def _refused(
    root: Path,
    profile: RepositoryProfile,
    expected_fingerprint: str,
    before_fingerprint: str | None,
    before_facts: tuple[RuntimeFileFact, ...],
    code: str,
    *,
    detail: str = "runtime bootstrap refused before mutation",
) -> RuntimeBootstrapResult:
    return RuntimeBootstrapResult(
        repository_root=str(root),
        status=RuntimeBootstrapStatus.REFUSED,
        expected_profile_fingerprint=expected_fingerprint,
        profile_fingerprint_before=before_fingerprint,
        profile_fingerprint_after=before_fingerprint,
        git_head_before=_profile_head(profile),
        git_head_after=_profile_head(profile),
        runtime_files_before=before_facts,
        runtime_files_after=before_facts,
        created_paths=(),
        findings=(RuntimeBootstrapFinding(code, _STATE_PATH, detail),),
    )


def _result_without_profile(
    root: str,
    status: RuntimeBootstrapStatus,
    code: str,
    *,
    expected_fingerprint: str | None = None,
) -> RuntimeBootstrapResult:
    return RuntimeBootstrapResult(
        repository_root=root,
        status=status,
        expected_profile_fingerprint=expected_fingerprint,
        profile_fingerprint_before=None,
        profile_fingerprint_after=None,
        git_head_before=None,
        git_head_after=None,
        runtime_files_before=(),
        runtime_files_after=(),
        created_paths=(),
        findings=(RuntimeBootstrapFinding(code, _STATE_PATH, "preflight refused"),),
    )


def _success_result(
    root: Path,
    status: RuntimeBootstrapStatus,
    before: RepositoryProfile,
    after: RepositoryProfile,
    expected_fingerprint: str,
    before_fingerprint: str | None,
    before_facts: tuple[RuntimeFileFact, ...],
    created_paths: tuple[str, ...],
) -> RuntimeBootstrapResult:
    return RuntimeBootstrapResult(
        repository_root=str(root),
        status=status,
        expected_profile_fingerprint=expected_fingerprint,
        profile_fingerprint_before=before_fingerprint,
        profile_fingerprint_after=_profile_fingerprint(after),
        git_head_before=_profile_head(before),
        git_head_after=_profile_head(after),
        runtime_files_before=before_facts,
        runtime_files_after=_runtime_facts(after),
        created_paths=created_paths,
        findings=(),
    )


def _same_git_identity(left: GitReadOnlyState, right: GitReadOnlyState) -> bool:
    return (
        _path_key(left.top_level) == _path_key(right.top_level)
        and left.head_commit == right.head_commit
        and left.branch_name == right.branch_name
        and left.detached == right.detached
        and left.worktrees == right.worktrees
    )


def _profile_head(profile: RepositoryProfile | None) -> str | None:
    if profile is None or not isinstance(profile.git.head_commit.value, str):
        return None
    return profile.git.head_commit.value


def _error_code(error: object) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) else type(error).__name__


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False))).casefold()


def _profile_fingerprint(profile: RepositoryProfile) -> str:
    """Match the canonical P5.4 profile fingerprint without an application import."""

    canonical = json.dumps(
        _canonical_value(profile),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    raise TypeError(f"unsupported profile value: {type(value).__name__}")
