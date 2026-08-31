"""Safe, fail-closed application of canonical initialization plans."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path, PurePosixPath

from agentic_engineering_os.domain import (
    AGENTS_MANAGED_SECTION,
    GITIGNORE_MANAGED_SECTION,
    AgenticOsInitializationState,
    DocumentStatus,
    HumanOperationConfirmation,
    InitializationApplyFinding,
    InitializationApplyStatus,
    InitializationOperationResult,
    InitializationOperationType,
    InitializationPlan,
    InitializationResult,
    ManagedSectionStatus,
    OperationApplyStatus,
    PlannedCurrentState,
    PlannedOperation,
    ProjectConfiguration,
    RepositoryProfile,
)

from .git_adapter import GitAdapter, GitOperationError, GitReadOnlyState
from .agents_integration import AgentsIntegrationError, AgentsIntegrationService
from .project_configuration import (
    CONFIG_DIRECTORY,
    CONFIG_FILENAME,
    ProjectConfigurationError,
    ProjectConfigurationLoader,
    ProjectConfigurationValidator,
)
from .repository_reconnaissance import (
    RepositoryReconnaissance,
    RepositoryReconnaissanceError,
)


_CONFIG_PATH = f"{CONFIG_DIRECTORY}/{CONFIG_FILENAME}"
_MAX_MANAGED_FILE_BYTES = 256_000
_ALLOWED_OPERATION_TARGETS = {
    InitializationOperationType.CREATE_DIRECTORY: frozenset({CONFIG_DIRECTORY}),
    InitializationOperationType.INITIALIZE_CONFIG: frozenset({_CONFIG_PATH}),
    InitializationOperationType.CREATE_MANAGED_FILE: frozenset(
        {"AGENTS.md", ".gitignore"}
    ),
    InitializationOperationType.ADD_GITIGNORE_SECTION: frozenset({".gitignore"}),
    InitializationOperationType.UPDATE_MANAGED_SECTION: frozenset({"AGENTS.md"}),
    InitializationOperationType.NO_OP: frozenset(
        {_CONFIG_PATH, "AGENTS.md", ".gitignore"}
    ),
}


class _ApplyFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class RepositoryInitializer:
    """Apply only the safe P5.5 subset of a freshly reconstructed P5.4 plan."""

    def __init__(self) -> None:
        from agentic_engineering_os.application.initialization_planner import (
            InitializationPlanner,
        )

        self._reconnaissance = RepositoryReconnaissance()
        self._planner = InitializationPlanner()
        self._validator = ProjectConfigurationValidator()
        self._agents_integration = AgentsIntegrationService()

    def apply(
        self,
        plan: InitializationPlan,
        *,
        human_confirmations: tuple[HumanOperationConfirmation, ...] = (),
    ) -> InitializationResult:
        """Reconstruct trust, apply ordered operations, and report exact outcomes."""

        if not isinstance(plan, InitializationPlan):
            raise TypeError("RepositoryInitializer.apply requires InitializationPlan")
        plan_fingerprint = plan.input_fingerprint
        root, root_error = _resolve_plan_root(plan)
        if root_error is not None or root is None:
            return _refused(plan, root_error or "INVALID_REPOSITORY_ROOT", None)

        try:
            profile_before = self._reconnaissance.inspect(root)
            current_configuration = _load_current_configuration(profile_before, root)
            reconstructed = self._planner.plan(
                profile_before,
                plan.desired_configuration,
                current_configuration=current_configuration,
            )
        except (
            RepositoryReconnaissanceError,
            ProjectConfigurationError,
            OSError,
        ) as error:
            return _refused(
                plan,
                f"TRUST_RECONSTRUCTION_FAILED:{type(error).__name__}",
                None,
            )

        before_fingerprint = self._planner.fingerprint(profile_before)
        if reconstructed != plan:
            return _refused(
                plan,
                "UNTRUSTED_OR_STALE_PLAN",
                profile_before,
                before_fingerprint=before_fingerprint,
            )
        if plan.blockers:
            return _refused(
                plan,
                "PLAN_HAS_BLOCKERS",
                profile_before,
                before_fingerprint=before_fingerprint,
            )

        confirmation_error = _validate_confirmations(plan, human_confirmations)
        if confirmation_error is not None:
            return _refused(
                plan,
                confirmation_error,
                profile_before,
                before_fingerprint=before_fingerprint,
            )
        catalog_error = _validate_operation_catalog(plan)
        if catalog_error is not None:
            return _refused(
                plan,
                catalog_error,
                profile_before,
                before_fingerprint=before_fingerprint,
            )

        try:
            baseline_git = GitAdapter(root).observe_read_only()
        except GitOperationError as error:
            return _refused(
                plan,
                f"GIT_REVALIDATION_FAILED:{error.code}",
                profile_before,
                before_fingerprint=before_fingerprint,
            )

        operation_results: list[InitializationOperationResult] = []
        failure: InitializationApplyFinding | None = None
        for operation in plan.operations:
            try:
                self._revalidate_git(root, baseline_git)
                self._apply_operation(
                    root,
                    operation,
                    plan.desired_configuration,
                    profile_before,
                )
                status = (
                    OperationApplyStatus.NO_OP
                    if operation.operation_type is InitializationOperationType.NO_OP
                    else OperationApplyStatus.APPLIED
                )
                operation_results.append(
                    InitializationOperationResult(
                        operation.operation_id,
                        operation.operation_type,
                        operation.target_path,
                        status,
                        "operation verified",
                    )
                )
            except (
                _ApplyFailure,
                OSError,
                ProjectConfigurationError,
                RepositoryReconnaissanceError,
                AgentsIntegrationError,
            ) as error:
                if isinstance(error, _ApplyFailure):
                    code = error.code
                    detail = error.message
                elif isinstance(error, ProjectConfigurationError):
                    code = f"CONFIGURATION_VERIFICATION_FAILED:{error.code}"
                    detail = "configuration verification failed"
                elif isinstance(error, RepositoryReconnaissanceError):
                    code = f"RECONNAISSANCE_FAILED:{error.code}"
                    detail = "repository verification failed"
                elif isinstance(error, AgentsIntegrationError):
                    code = f"AGENTS_INTEGRATION_FAILED:{error.code}"
                    detail = error.message
                else:
                    code = "WRITE_FAILED"
                    detail = f"filesystem operation failed: {type(error).__name__}"
                operation_results.append(
                    InitializationOperationResult(
                        operation.operation_id,
                        operation.operation_type,
                        operation.target_path,
                        OperationApplyStatus.FAILED,
                        detail,
                    )
                )
                failure = InitializationApplyFinding(
                    code,
                    operation.operation_id,
                    operation.target_path,
                    detail,
                )
                break

        attempted = len(operation_results)
        operation_results.extend(
            InitializationOperationResult(
                item.operation_id,
                item.operation_type,
                item.target_path,
                OperationApplyStatus.NOT_ATTEMPTED,
                "stopped after prior failure",
            )
            for item in plan.operations[attempted:]
        )

        profile_after, after_error = self._observe_after(root)
        after_fingerprint = (
            self._planner.fingerprint(profile_after) if profile_after is not None else None
        )
        findings = [failure] if failure is not None else []
        if after_error is not None:
            findings.append(
                InitializationApplyFinding(
                    "FINAL_RECONNAISSANCE_FAILED", None, ".", after_error
                )
            )
        if profile_after is not None and not _same_git_identity(
            baseline_git, profile_after
        ):
            findings.append(
                InitializationApplyFinding(
                    "GIT_IDENTITY_CHANGED",
                    None,
                    ".",
                    "Git root, HEAD, branch, or worktrees changed during apply",
                )
            )

        applied_count = sum(
            item.status is OperationApplyStatus.APPLIED for item in operation_results
        )
        if findings:
            result_status = (
                InitializationApplyStatus.PARTIAL_FAILURE
                if applied_count
                else InitializationApplyStatus.FAILED
            )
        elif applied_count:
            if (
                profile_after is None
                or profile_after.agentic_os.state
                is not AgenticOsInitializationState.INITIALIZED
            ):
                findings.append(
                    InitializationApplyFinding(
                        "RESULTING_FOOTPRINT_NOT_INITIALIZED",
                        None,
                        CONFIG_DIRECTORY,
                        "post-write reconnaissance did not prove INITIALIZED",
                    )
                )
                result_status = InitializationApplyStatus.PARTIAL_FAILURE
            else:
                result_status = InitializationApplyStatus.APPLIED
        else:
            result_status = InitializationApplyStatus.NO_OP

        return InitializationResult(
            plan_fingerprint=plan_fingerprint,
            repository_root=str(root),
            status=result_status,
            operation_results=tuple(operation_results),
            findings=tuple(item for item in findings if item is not None),
            profile_fingerprint_before=before_fingerprint,
            profile_fingerprint_after=after_fingerprint,
            git_head_before=baseline_git.head_commit,
            git_head_after=(
                str(profile_after.git.head_commit.value)
                if profile_after is not None
                and isinstance(profile_after.git.head_commit.value, str)
                else None
            ),
            initialization_state_after=(
                profile_after.agentic_os.state if profile_after is not None else None
            ),
        )

    def _revalidate_git(self, root: Path, baseline: GitReadOnlyState) -> None:
        try:
            current = GitAdapter(root).observe_read_only()
        except GitOperationError as error:
            raise _ApplyFailure(
                "GIT_REVALIDATION_FAILED", f"Git observation failed: {error.code}"
            ) from error
        if not _same_read_only_git(baseline, current):
            raise _ApplyFailure(
                "GIT_IDENTITY_CHANGED",
                "Git root, HEAD, branch, or worktrees changed before operation",
            )

    def _apply_operation(
        self,
        root: Path,
        operation: PlannedOperation,
        desired_configuration: ProjectConfiguration | None,
        profile_before: RepositoryProfile,
    ) -> None:
        target = _safe_target(root, operation.target_path)
        if operation.operation_type is InitializationOperationType.CREATE_DIRECTORY:
            _require_absent(target)
            target.mkdir(parents=False, exist_ok=False)
            if target.is_symlink() or not target.is_dir():
                raise _ApplyFailure("WRITE_VERIFICATION_FAILED", "directory was not created safely")
            return
        if operation.operation_type is InitializationOperationType.INITIALIZE_CONFIG:
            if desired_configuration is None:
                raise _ApplyFailure("INVALID_PLAN", "desired configuration is absent")
            canonical = self._validator.serialize(desired_configuration)
            _require_planned_content(operation, canonical)
            _require_absent(target)
            _exclusive_create(target, canonical.encode("utf-8"))
            loaded = ProjectConfigurationLoader(root, validator=self._validator).load()
            if loaded != desired_configuration or target.read_bytes() != canonical.encode("utf-8"):
                raise _ApplyFailure(
                    "WRITE_VERIFICATION_FAILED", "created configuration is not canonical"
                )
            return
        if operation.operation_type is InitializationOperationType.CREATE_MANAGED_FILE:
            canonical = _managed_content(operation.target_path)
            _require_planned_content(operation, canonical)
            _require_absent(target)
            if operation.target_path == "AGENTS.md":
                self._agents_integration.create_from_plan(
                    root, planned_content=canonical
                )
            else:
                _exclusive_create(target, canonical.encode("utf-8"))
            if target.read_bytes() != canonical.encode("utf-8"):
                raise _ApplyFailure(
                    "WRITE_VERIFICATION_FAILED", "managed file bytes differ from plan"
                )
            return
        if operation.operation_type is InitializationOperationType.UPDATE_MANAGED_SECTION:
            canonical = AGENTS_MANAGED_SECTION
            _require_planned_content(operation, canonical)
            expected = profile_before.agentic_os.agents_managed_section
            if (
                expected.status is not ManagedSectionStatus.SECTION_ABSENT
                or expected.content_fingerprint != operation.expected_target_fingerprint
                or expected.content_fingerprint is None
            ):
                raise _ApplyFailure(
                    "EXPECTED_STATE_MISMATCH",
                    "initial AGENTS.md fingerprint or section state diverged",
                )
            self._agents_integration.integrate_from_plan(
                root,
                expected_fingerprint=expected.content_fingerprint,
                planned_content=canonical,
            )
            observed = self._reconnaissance.inspect(
                root
            ).agentic_os.agents_managed_section
            if observed.status is not ManagedSectionStatus.CURRENT:
                raise _ApplyFailure(
                    "WRITE_VERIFICATION_FAILED",
                    "AGENTS.md managed section is not canonical",
                )
            return
        if operation.operation_type is InitializationOperationType.ADD_GITIGNORE_SECTION:
            canonical = GITIGNORE_MANAGED_SECTION
            _require_planned_content(operation, canonical)
            expected = profile_before.agentic_os.gitignore_managed_section
            if expected.status is not ManagedSectionStatus.SECTION_ABSENT:
                raise _ApplyFailure(
                    "EXPECTED_STATE_MISMATCH", "initial gitignore section was not absent"
                )
            _append_gitignore_section(root, target, expected.content_fingerprint, canonical)
            observed = self._reconnaissance.inspect(root).agentic_os.gitignore_managed_section
            if observed.status is not ManagedSectionStatus.CURRENT:
                raise _ApplyFailure(
                    "WRITE_VERIFICATION_FAILED", "gitignore managed section is not canonical"
                )
            return
        if operation.operation_type is InitializationOperationType.NO_OP:
            self._verify_no_op(root, operation, desired_configuration)
            return
        raise _ApplyFailure(
            "UNSUPPORTED_OPERATION", f"operation type {operation.operation_type.value} is not supported"
        )

    def _verify_no_op(
        self,
        root: Path,
        operation: PlannedOperation,
        desired_configuration: ProjectConfiguration | None,
    ) -> None:
        if operation.target_path == _CONFIG_PATH:
            if desired_configuration is None:
                raise _ApplyFailure("INVALID_PLAN", "desired configuration is absent")
            current = ProjectConfigurationLoader(root, validator=self._validator).load()
            if current != desired_configuration:
                raise _ApplyFailure("EXPECTED_STATE_MISMATCH", "configuration is not identical")
            return
        profile = self._reconnaissance.inspect(root)
        observation = (
            profile.agentic_os.agents_managed_section
            if operation.target_path == "AGENTS.md"
            else profile.agentic_os.gitignore_managed_section
        )
        if observation.status is not ManagedSectionStatus.CURRENT:
            raise _ApplyFailure(
                "EXPECTED_STATE_MISMATCH", "managed section is no longer canonical"
            )

    def _observe_after(
        self, root: Path
    ) -> tuple[RepositoryProfile | None, str | None]:
        try:
            return self._reconnaissance.inspect(root), None
        except (RepositoryReconnaissanceError, OSError) as error:
            return None, f"post-apply observation failed: {type(error).__name__}"


def _resolve_plan_root(
    plan: InitializationPlan,
) -> tuple[Path | None, str | None]:
    candidate = Path(plan.repository.repository_root)
    if (
        not candidate.is_absolute()
        or candidate.is_symlink()
        or _path_has_symlink_component(candidate)
    ):
        return None, "INVALID_REPOSITORY_ROOT"
    try:
        root = candidate.resolve(strict=True)
    except OSError:
        return None, "INVALID_REPOSITORY_ROOT"
    if not root.is_dir() or _path_key(root) != _path_key(candidate):
        return None, "INVALID_REPOSITORY_ROOT"
    return root, None


def _load_current_configuration(
    profile: RepositoryProfile, root: Path
) -> ProjectConfiguration | None:
    if profile.agentic_os.config_status is DocumentStatus.VALID:
        return ProjectConfigurationLoader(root).load()
    return None


def _validate_confirmations(
    plan: InitializationPlan,
    confirmations: tuple[HumanOperationConfirmation, ...],
) -> str | None:
    from agentic_engineering_os.application._identity import (
        is_attributable_human_identity,
    )

    if not isinstance(confirmations, tuple) or any(
        not isinstance(item, HumanOperationConfirmation) for item in confirmations
    ):
        return "INVALID_HUMAN_CONFIRMATION_COLLECTION"
    required = {
        item.operation_id: item
        for item in plan.operations
        if item.human_confirmation_required
    }
    if len(required) != sum(
        item.human_confirmation_required for item in plan.operations
    ):
        return "AMBIGUOUS_HUMAN_CONFIRMATION_REQUIREMENTS"
    supplied: dict[str, HumanOperationConfirmation] = {}
    for confirmation in confirmations:
        if confirmation.operation_id in supplied:
            return "DUPLICATE_HUMAN_CONFIRMATION"
        supplied[confirmation.operation_id] = confirmation
        operation = required.get(confirmation.operation_id)
        if operation is None:
            return "UNEXPECTED_HUMAN_CONFIRMATION"
        if not is_attributable_human_identity(confirmation.confirmed_by):
            return "INVALID_HUMAN_IDENTITY"
        if (
            confirmation.plan_fingerprint != plan.input_fingerprint
            or confirmation.target_path != operation.target_path
            or confirmation.expected_current_state
            is not operation.expected_current_state
            or confirmation.expected_target_fingerprint
            != operation.expected_target_fingerprint
        ):
            return "HUMAN_CONFIRMATION_BINDING_MISMATCH"
    if set(supplied) != set(required):
        return "MISSING_HUMAN_CONFIRMATION"
    return None


def _validate_operation_catalog(plan: InitializationPlan) -> str | None:
    seen_ids: set[str] = set()
    for index, operation in enumerate(plan.operations, 1):
        if operation.operation_id != f"OP-{index:03d}" or operation.operation_id in seen_ids:
            return "INVALID_OPERATION_ORDER"
        seen_ids.add(operation.operation_id)
        if operation.human_confirmation_required and operation.operation_type not in {
            InitializationOperationType.ADD_GITIGNORE_SECTION,
            InitializationOperationType.UPDATE_MANAGED_SECTION,
        }:
            return "UNSUPPORTED_HUMAN_OPERATION"
        if operation.human_confirmation_required and not _is_sha256(
            operation.expected_target_fingerprint
        ):
            return "INVALID_EXPECTED_TARGET_FINGERPRINT"
        targets = _ALLOWED_OPERATION_TARGETS.get(operation.operation_type)
        if targets is None or operation.target_path not in targets:
            return "UNSUPPORTED_OPERATION"
        if not _safe_relative_path(operation.target_path):
            return "UNSAFE_OPERATION_TARGET"
    return None


def _safe_target(root: Path, relative: str) -> Path:
    if not _safe_relative_path(relative):
        raise _ApplyFailure("UNSAFE_OPERATION_TARGET", "target path is not canonical")
    target = root.joinpath(*PurePosixPath(relative).parts)
    cursor = root
    for part in PurePosixPath(relative).parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise _ApplyFailure("SYMLINK_ESCAPE", "target parent is a symlink")
    try:
        parent = target.parent.resolve(strict=True)
    except OSError as error:
        raise _ApplyFailure("UNSAFE_OPERATION_TARGET", "target parent is absent") from error
    if not _contains(root, parent):
        raise _ApplyFailure("UNSAFE_OPERATION_TARGET", "target parent escapes repository")
    if target.is_symlink():
        raise _ApplyFailure("SYMLINK_ESCAPE", "target is a symlink")
    return target


def _safe_relative_path(value: str) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        return False
    path = PurePosixPath(value)
    return str(path) == value and all(part not in {"", ".", ".."} for part in path.parts)


def _require_absent(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise _ApplyFailure(
            "EXPECTED_ABSENT_TARGET_EXISTS", "planned create target is no longer absent"
        )


def _require_planned_content(operation: PlannedOperation, expected: str) -> None:
    expected_hash = _sha256(expected.encode("utf-8"))
    if (
        operation.desired_content != expected
        or operation.desired_content_sha256 != expected_hash
    ):
        raise _ApplyFailure("INVALID_PLANNED_CONTENT", "planned content is not canonical")


def _managed_content(target_path: str) -> str:
    if target_path == "AGENTS.md":
        return AGENTS_MANAGED_SECTION
    if target_path == ".gitignore":
        return GITIGNORE_MANAGED_SECTION
    raise _ApplyFailure("UNSUPPORTED_OPERATION", "managed-file target is unsupported")


def _exclusive_create(path: Path, content: bytes) -> None:
    temporary = _write_temporary_file(path.parent, content)
    linked = False
    try:
        if path.exists() or path.is_symlink():
            raise _ApplyFailure(
                "EXPECTED_ABSENT_TARGET_EXISTS", "target appeared after temporary write"
            )
        try:
            os.link(temporary, path)
            linked = True
        except FileExistsError as error:
            raise _ApplyFailure(
                "EXPECTED_ABSENT_TARGET_EXISTS", "target appeared during exclusive create"
            ) from error
        if path.is_symlink() or path.read_bytes() != content:
            raise _ApplyFailure(
                "WRITE_VERIFICATION_FAILED", "exclusive-created bytes differ"
            )
        _fsync_directory(path.parent)
    finally:
        _cleanup_temporary(temporary)
    if not linked:
        raise _ApplyFailure("WRITE_FAILED", "exclusive create did not install target")


def _append_gitignore_section(
    root: Path,
    path: Path,
    expected_fingerprint: str | None,
    canonical_section: str,
) -> None:
    if expected_fingerprint is None:
        raise _ApplyFailure("EXPECTED_STATE_MISMATCH", "gitignore fingerprint is absent")
    original = _read_bounded_regular_file(root, path)
    if _sha256(original) != expected_fingerprint:
        raise _ApplyFailure("EXPECTED_STATE_MISMATCH", "gitignore changed after planning")
    try:
        original.decode("utf-8")
    except UnicodeError as error:
        raise _ApplyFailure("EXPECTED_STATE_MISMATCH", "gitignore is not UTF-8") from error
    separator = b"" if not original else (b"\n" if original.endswith(b"\n") else b"\n\n")
    replacement = original + separator + canonical_section.encode("utf-8")
    temporary = _write_temporary_file(path.parent, replacement)
    try:
        current = _read_bounded_regular_file(root, path)
        if _sha256(current) != expected_fingerprint:
            raise _ApplyFailure("EXPECTED_STATE_MISMATCH", "gitignore changed before replace")
        os.replace(temporary, path)
        if path.is_symlink() or path.read_bytes() != replacement:
            raise _ApplyFailure(
                "WRITE_VERIFICATION_FAILED", "gitignore replacement bytes differ"
            )
        _fsync_directory(path.parent)
    finally:
        _cleanup_temporary(temporary)


def _read_bounded_regular_file(root: Path, path: Path) -> bytes:
    if path.is_symlink():
        raise _ApplyFailure("SYMLINK_ESCAPE", "managed target is a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise _ApplyFailure("EXPECTED_STATE_MISMATCH", "managed target is absent") from error
    if not resolved.is_file() or not _contains(root, resolved):
        raise _ApplyFailure("UNSAFE_OPERATION_TARGET", "managed target is unsafe")
    try:
        if resolved.stat().st_size > _MAX_MANAGED_FILE_BYTES:
            raise _ApplyFailure("EXPECTED_STATE_MISMATCH", "managed target is too large")
        return resolved.read_bytes()
    except OSError as error:
        raise _ApplyFailure("READ_FAILED", "managed target cannot be read") from error


def _write_temporary_file(directory: Path, content: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(
        dir=directory,
        prefix=".agentic-init.",
        suffix=".tmp",
    )
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        _cleanup_temporary(path)
        raise
    return path


def _cleanup_temporary(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _same_read_only_git(left: GitReadOnlyState, right: GitReadOnlyState) -> bool:
    return (
        _path_key(left.top_level) == _path_key(right.top_level)
        and left.head_commit == right.head_commit
        and left.branch_name == right.branch_name
        and left.detached == right.detached
        and left.worktrees == right.worktrees
    )


def _same_git_identity(baseline: GitReadOnlyState, profile: RepositoryProfile) -> bool:
    return (
        isinstance(profile.git.top_level.value, str)
        and _path_key(baseline.top_level) == _path_key(Path(profile.git.top_level.value))
        and profile.git.head_commit.value == baseline.head_commit
        and profile.git.branch.value == baseline.branch_name
        and tuple(
            (item.path, item.head_commit, item.branch_name) for item in profile.git.worktrees
        )
        == tuple(
            (str(item.path), item.head_commit, item.branch_name)
            for item in sorted(baseline.worktrees, key=lambda value: _path_key(value.path))
        )
    )


def _refused(
    plan: InitializationPlan,
    code: str,
    profile: RepositoryProfile | None,
    *,
    before_fingerprint: str | None = None,
) -> InitializationResult:
    return InitializationResult(
        plan_fingerprint=plan.input_fingerprint,
        repository_root=plan.repository.repository_root,
        status=InitializationApplyStatus.REFUSED,
        operation_results=tuple(
            InitializationOperationResult(
                item.operation_id,
                item.operation_type,
                item.target_path,
                OperationApplyStatus.NOT_ATTEMPTED,
                "application refused before operation execution",
            )
            for item in plan.operations
        ),
        findings=(InitializationApplyFinding(code, None, ".", "plan application refused"),),
        profile_fingerprint_before=before_fingerprint,
        profile_fingerprint_after=before_fingerprint,
        git_head_before=(
            str(profile.git.head_commit.value)
            if profile is not None and isinstance(profile.git.head_commit.value, str)
            else None
        ),
        git_head_after=(
            str(profile.git.head_commit.value)
            if profile is not None and isinstance(profile.git.head_commit.value, str)
            else None
        ),
        initialization_state_after=(profile.agentic_os.state if profile is not None else None),
    )


def _contains(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False))).casefold()


def _path_has_symlink_component(path: Path) -> bool:
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor = cursor / part
        if cursor.is_symlink():
            return True
    return False


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _is_sha256(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
