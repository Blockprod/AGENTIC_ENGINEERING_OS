"""Fail-closed orchestration of isolated Git worktree resources."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from agentic_engineering_os._worktree_registry_write import _issue_registry_write
from agentic_engineering_os.domain import (
    MissionState,
    UserStory,
    WorktreeAssignment,
    WorktreeRegistry,
    WorktreeStatus,
)

from ._worktree_identity import (
    derive_assignment_id,
    derive_branch_name,
    validate_identity_inputs,
)
from .git_adapter import GitAdapter, GitOperationError, GitWorktree
from .project_state_store import PersistenceError, STATE_DIRECTORY
from .worktree_registry_store import (
    WORKTREE_REGISTRY_FILENAME,
    WorktreeRegistryStore,
)


class WorktreeManagerError(RuntimeError):
    """A worktree operation could not be proven safe and complete."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        reasons: tuple[str, ...] = (),
    ) -> None:
        self.code = code
        self.message = message
        self.reasons = reasons
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class WorktreeInspection:
    assignment_id: str
    registry_status: WorktreeStatus
    physical_exists: bool
    branch_matches: bool
    head_commit: str | None
    clean: bool | None
    resumable: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorktreeReconciliation:
    inspections: tuple[WorktreeInspection, ...]
    anomalies: tuple[str, ...]

    @property
    def is_consistent(self) -> bool:
        return not self.anomalies


class WorktreeManager:
    """Own validated Git mutations and exact registry transitions."""

    def __init__(
        self,
        *,
        repository_root: Path | str,
        worktree_root: Path | str,
        registry_store: WorktreeRegistryStore | None = None,
        git_adapter: GitAdapter | None = None,
    ) -> None:
        repository = Path(repository_root)
        root = Path(worktree_root)
        if root.is_symlink():
            raise WorktreeManagerError(
                "UNSAFE_WORKTREE_ROOT", "worktree root cannot be a symlink"
            )
        try:
            self._repository_root = repository.resolve(strict=True)
            self._worktree_root = root.resolve(strict=True)
        except OSError as error:
            raise WorktreeManagerError(
                "INVALID_ROOT", "repository and worktree roots must already exist"
            ) from error
        if not self._repository_root.is_dir() or not self._worktree_root.is_dir():
            raise WorktreeManagerError("INVALID_ROOT", "manager roots must be directories")
        if _contains(self._repository_root, self._worktree_root) or _contains(
            self._worktree_root, self._repository_root
        ):
            raise WorktreeManagerError(
                "UNSAFE_WORKTREE_ROOT",
                "worktree root must be disjoint from the primary worktree",
            )
        self._store = registry_store or WorktreeRegistryStore(self._repository_root)
        self._git = git_adapter or GitAdapter(self._repository_root)
        expected_registry = (
            self._repository_root / STATE_DIRECTORY / WORKTREE_REGISTRY_FILENAME
        )
        if _path_key(self._store.registry_path) != _path_key(expected_registry):
            raise WorktreeManagerError(
                "REGISTRY_ROOT_MISMATCH", "registry store does not belong to repository root"
            )
        if _path_key(self._git.repository_root) != _path_key(self._repository_root):
            raise WorktreeManagerError(
                "GIT_ROOT_MISMATCH", "Git adapter does not belong to repository root"
            )

    @property
    def repository_root(self) -> Path:
        return self._repository_root

    @property
    def worktree_root(self) -> Path:
        return self._worktree_root

    @property
    def registry_store(self) -> WorktreeRegistryStore:
        return self._store

    def initialize_registry(self) -> WorktreeRegistry:
        self._verify_repository()
        try:
            return self._store.initialize()
        except PersistenceError as error:
            raise _registry_error(error) from error

    def plan_assignment(
        self,
        *,
        mission: MissionState,
        user_story: UserStory,
        baseline_commit: str,
    ) -> WorktreeAssignment:
        """Persist one deterministic PLANNED assignment without Git mutation."""

        if not isinstance(mission, MissionState):
            raise WorktreeManagerError("INVALID_INPUT", "mission must be MissionState")
        if not isinstance(user_story, UserStory):
            raise WorktreeManagerError("INVALID_INPUT", "user_story must be UserStory")
        try:
            mission_id, story_id, generation, requested_commit = validate_identity_inputs(
                mission.mission_id,
                user_story.id,
                mission.workflow_generation,
                baseline_commit,
            )
        except ValueError as error:
            raise WorktreeManagerError("INVALID_INPUT", str(error)) from error
        self._verify_repository()
        resolved_commit = self._resolve_commit(requested_commit)
        if resolved_commit != requested_commit:
            raise WorktreeManagerError(
                "BASELINE_MISMATCH", "baseline must be the exact resolved commit SHA"
            )
        assignment_id = derive_assignment_id(
            mission_id, story_id, generation, resolved_commit
        )
        branch_name = derive_branch_name(story_id, generation, assignment_id)
        try:
            self._git.validate_branch_name(branch_name)
        except GitOperationError as error:
            raise _git_error(error) from error
        worktree_path = (self._worktree_root / assignment_id).resolve(strict=False)
        if worktree_path.parent != self._worktree_root:
            raise WorktreeManagerError("UNSAFE_WORKTREE_PATH", "derived path escapes root")
        if len(str(worktree_path)) > 240:
            raise WorktreeManagerError(
                "WORKTREE_PATH_TOO_LONG", "derived worktree path exceeds V1 safe length"
            )
        assignment = WorktreeAssignment(
            assignment_id=assignment_id,
            mission_id=mission_id,
            user_story_id=story_id,
            workflow_generation=generation,
            baseline_commit=resolved_commit,
            branch_name=branch_name,
            worktree_path=str(worktree_path),
            status=WorktreeStatus.PLANNED,
            result_commit=None,
        )
        registry = self._load_registry()
        existing = _assignment_or_none(registry, assignment_id)
        if existing is not None:
            if existing == assignment:
                return existing
            raise WorktreeManagerError(
                "ASSIGNMENT_COLLISION", "deterministic assignment ID already has other state"
            )
        candidate = _registry_with(registry, assignment)
        self._persist(registry, candidate, "PLAN")
        return assignment

    def activate(
        self, assignment_id: str, *, current_generation: int
    ) -> WorktreeAssignment:
        registry = self._load_registry()
        assignment = _require_assignment(registry, assignment_id)
        _require_generation(assignment, current_generation)
        if assignment.status is not WorktreeStatus.PLANNED:
            raise WorktreeManagerError(
                "INVALID_STATUS", "only a PLANNED assignment can be activated"
            )
        self._verify_structural_operation(assignment)
        path = Path(assignment.worktree_path)
        worktrees = self._list_worktrees()
        sibling_collision = any(
            child.name.casefold() == path.name.casefold()
            for child in self._worktree_root.iterdir()
        )
        if (
            path.exists()
            or sibling_collision
            or any(_path_key(item.path) == _path_key(path) for item in worktrees)
        ):
            raise WorktreeManagerError("PATH_COLLISION", "worktree path already exists")
        if self._branch_exists(assignment.branch_name) or any(
            _branch_key(item.branch_name) == _branch_key(assignment.branch_name)
            for item in worktrees
            if item.branch_name is not None
        ):
            raise WorktreeManagerError("BRANCH_COLLISION", "assignment branch already exists")
        try:
            self._git.add_worktree(
                path, assignment.branch_name, assignment.baseline_commit
            )
        except GitOperationError as error:
            cleanup = self._cleanup_partial_creation(assignment)
            converted = _git_error(error)
            raise WorktreeManagerError(
                converted.code,
                f"{converted.message}; {cleanup}",
                reasons=converted.reasons,
            ) from error
        try:
            self._verify_new_worktree(assignment)
        except WorktreeManagerError as error:
            cleanup = self._cleanup_partial_creation(assignment)
            raise WorktreeManagerError(
                "POST_CREATE_VERIFICATION_FAILED",
                f"created worktree failed verification; {cleanup}",
                reasons=(error.code, *error.reasons),
            ) from error
        candidate_assignment = replace(assignment, status=WorktreeStatus.ACTIVE)
        candidate = _registry_with(registry, candidate_assignment)
        try:
            self._persist(registry, candidate, "ACTIVATE")
        except WorktreeManagerError as error:
            raise WorktreeManagerError(
                "REGISTRY_WRITE_FAILED_AFTER_GIT",
                "Git worktree exists but ACTIVE registry write failed",
                reasons=(error.code,),
            ) from error
        return candidate_assignment

    def inspect(
        self, assignment_id: str, *, current_generation: int
    ) -> WorktreeInspection:
        registry = self._load_registry()
        assignment = _require_assignment(registry, assignment_id)
        return self._inspect_assignment(assignment, current_generation=current_generation)

    def resume(
        self, assignment_id: str, *, current_generation: int
    ) -> WorktreeInspection:
        inspection = self.inspect(
            assignment_id, current_generation=current_generation
        )
        if not inspection.resumable:
            raise WorktreeManagerError(
                "NOT_RESUMABLE",
                "assignment does not exactly match current Git and mission state",
                reasons=inspection.reasons,
            )
        return inspection

    def complete(
        self, assignment_id: str, *, current_generation: int
    ) -> WorktreeAssignment:
        registry = self._load_registry()
        assignment = _require_assignment(registry, assignment_id)
        _require_generation(assignment, current_generation)
        if assignment.status is not WorktreeStatus.ACTIVE:
            raise WorktreeManagerError(
                "INVALID_STATUS", "only an ACTIVE assignment can be completed"
            )
        inspection = self._inspect_assignment(
            assignment, current_generation=current_generation
        )
        if not inspection.physical_exists or not inspection.branch_matches:
            raise WorktreeManagerError(
                "WORKTREE_MISMATCH", "worktree path or branch does not match registry"
            )
        if inspection.clean is not True:
            raise WorktreeManagerError(
                "DIRTY_WORKTREE", "dirty worktree cannot be completed"
            )
        head = inspection.head_commit
        if head is None:
            raise WorktreeManagerError("HEAD_UNAVAILABLE", "result HEAD is unavailable")
        if head == assignment.baseline_commit:
            raise WorktreeManagerError(
                "NO_RESULT_COMMIT", "completion requires a commit after baseline"
            )
        if "BASELINE_NOT_ANCESTOR" in inspection.reasons:
            raise WorktreeManagerError(
                "NON_DESCENDANT_RESULT", "result commit is not descended from baseline"
            )
        if "BRANCH_TIP_MISMATCH" in inspection.reasons:
            raise WorktreeManagerError(
                "BRANCH_TIP_MISMATCH", "worktree HEAD is not the dedicated branch tip"
            )
        if "BRANCH_REF_MISSING" in inspection.reasons:
            raise WorktreeManagerError(
                "BRANCH_REF_MISSING", "dedicated branch ref is unavailable"
            )
        if inspection.reasons:
            raise WorktreeManagerError(
                "WORKTREE_MISMATCH",
                "worktree inspection contains unresolved inconsistencies",
                reasons=inspection.reasons,
            )
        candidate_assignment = replace(
            assignment,
            status=WorktreeStatus.COMPLETED,
            result_commit=head,
        )
        self._persist(
            registry,
            _registry_with(registry, candidate_assignment),
            "COMPLETE",
        )
        return candidate_assignment

    def mark_failed(
        self, assignment_id: str, *, current_generation: int
    ) -> WorktreeAssignment:
        registry = self._load_registry()
        assignment = _require_assignment(registry, assignment_id)
        _require_generation(assignment, current_generation)
        if assignment.status is not WorktreeStatus.ACTIVE:
            raise WorktreeManagerError(
                "INVALID_STATUS", "only an ACTIVE assignment can be marked FAILED"
            )
        candidate_assignment = replace(assignment, status=WorktreeStatus.FAILED)
        self._persist(
            registry,
            _registry_with(registry, candidate_assignment),
            "FAIL",
        )
        return candidate_assignment

    def cleanup(
        self,
        assignment_id: str,
        *,
        integration_in_progress: bool,
        confirmed_not_needed: bool,
    ) -> WorktreeAssignment:
        if not isinstance(integration_in_progress, bool) or not isinstance(
            confirmed_not_needed, bool
        ):
            raise WorktreeManagerError("INVALID_INPUT", "cleanup decisions must be booleans")
        if integration_in_progress:
            raise WorktreeManagerError(
                "INTEGRATION_IN_PROGRESS", "worktree cannot be removed during integration"
            )
        if not confirmed_not_needed:
            raise WorktreeManagerError(
                "CLEANUP_NOT_CONFIRMED", "explicit cleanup confirmation is required"
            )
        registry = self._load_registry()
        assignment = _require_assignment(registry, assignment_id)
        if assignment.status not in {
            WorktreeStatus.COMPLETED,
            WorktreeStatus.FAILED,
        }:
            raise WorktreeManagerError(
                "INVALID_STATUS", "cleanup requires COMPLETED or FAILED"
            )
        if (
            assignment.status is WorktreeStatus.COMPLETED
            and assignment.result_commit is None
        ):
            raise WorktreeManagerError(
                "RESULT_COMMIT_REQUIRED", "COMPLETED cleanup requires result_commit"
            )
        self._verify_repository()
        self._require_clean_primary()
        path = Path(assignment.worktree_path)
        worktree = _find_worktree(self._list_worktrees(), path)
        if worktree is None:
            if path.exists() or assignment.status is WorktreeStatus.COMPLETED:
                raise WorktreeManagerError(
                    "WORKTREE_MISMATCH", "terminal registry record lacks expected worktree"
                )
        else:
            if worktree.branch_name != assignment.branch_name:
                raise WorktreeManagerError(
                    "BRANCH_MISMATCH", "worktree branch differs from registry"
                )
            if not self._is_clean(path):
                raise WorktreeManagerError(
                    "DIRTY_WORKTREE", "dirty worktree cannot be removed"
                )
            if assignment.status is WorktreeStatus.COMPLETED:
                head = self._current_head(path)
                try:
                    branch_tip = self._git.branch_tip(assignment.branch_name)
                except GitOperationError as error:
                    raise _git_error(error) from error
                if head != assignment.result_commit or branch_tip != assignment.result_commit:
                    raise WorktreeManagerError(
                        "RESULT_COMMIT_MISMATCH",
                        "completed worktree no longer matches its recorded result commit",
                    )
                if not self._is_ancestor(assignment.baseline_commit, head):
                    raise WorktreeManagerError(
                        "NON_DESCENDANT_RESULT",
                        "recorded result is no longer descended from baseline",
                    )
            try:
                self._git.remove_worktree(path)
            except GitOperationError as error:
                raise _git_error(error) from error
            if _find_worktree(self._list_worktrees(), path) is not None or path.exists():
                raise WorktreeManagerError(
                    "REMOVE_VERIFICATION_FAILED", "worktree remains after Git removal"
                )
        candidate_assignment = replace(assignment, status=WorktreeStatus.CLEANED)
        try:
            self._persist(
                registry,
                _registry_with(registry, candidate_assignment),
                "CLEANUP",
            )
        except WorktreeManagerError as error:
            raise WorktreeManagerError(
                "REGISTRY_WRITE_FAILED_AFTER_GIT",
                "worktree removal succeeded but CLEANED registry write failed",
                reasons=(error.code,),
            ) from error
        return candidate_assignment

    def inspect_all(self, *, current_generation: int) -> WorktreeReconciliation:
        registry = self._load_registry()
        worktrees = self._list_worktrees()
        inspections = tuple(
            self._inspect_assignment(item, current_generation=current_generation)
            for item in registry.assignments
        )
        anomalies: set[str] = set()
        by_id = {item.assignment_id: item for item in registry.assignments}
        for assignment, inspection in zip(registry.assignments, inspections, strict=True):
            expected_physical = assignment.status in {
                WorktreeStatus.ACTIVE,
                WorktreeStatus.COMPLETED,
                WorktreeStatus.FAILED,
            }
            if expected_physical and (
                not inspection.physical_exists or not inspection.branch_matches
            ):
                anomalies.add(f"REGISTRY_GIT_MISMATCH:{assignment.assignment_id}")
            if not expected_physical and _find_worktree(
                worktrees, Path(assignment.worktree_path)
            ) is not None:
                anomalies.add(f"UNEXPECTED_PHYSICAL_WORKTREE:{assignment.assignment_id}")
            if not expected_physical and Path(assignment.worktree_path).exists():
                anomalies.add(f"UNEXPECTED_WORKTREE_PATH:{assignment.assignment_id}")
            if assignment.status is WorktreeStatus.PLANNED and self._branch_exists(
                assignment.branch_name
            ):
                anomalies.add(f"BRANCH_COLLISION:{assignment.assignment_id}")
        live_records = {
            (_path_key(Path(item.worktree_path)), _branch_key(item.branch_name))
            for item in by_id.values()
            if item.status is not WorktreeStatus.CLEANED
        }
        for worktree in worktrees:
            if worktree.branch_name is None or not worktree.branch_name.startswith(
                "agentic/"
            ):
                continue
            key = (_path_key(worktree.path), _branch_key(worktree.branch_name))
            if key not in live_records:
                anomalies.add(f"ORPHAN_AGENTIC_WORKTREE:{worktree.path}")
        return WorktreeReconciliation(
            inspections=inspections,
            anomalies=tuple(sorted(anomalies)),
        )

    def _inspect_assignment(
        self,
        assignment: WorktreeAssignment,
        *,
        current_generation: int,
    ) -> WorktreeInspection:
        _validate_generation_value(current_generation)
        self._verify_repository()
        path = Path(assignment.worktree_path)
        path_exists = path.exists()
        worktrees = self._list_worktrees()
        worktree = _find_worktree(worktrees, path)
        reasons: list[str] = []
        if assignment.status is not WorktreeStatus.ACTIVE:
            reasons.append(f"STATUS_{assignment.status.value}")
        if assignment.workflow_generation != current_generation:
            reasons.append("STALE_GENERATION")
        if not path_exists:
            reasons.append("PHYSICAL_PATH_MISSING")
        if worktree is None:
            reasons.append("WORKTREE_NOT_REGISTERED")
            if any(
                _branch_key(item.branch_name) == _branch_key(assignment.branch_name)
                for item in worktrees
                if item.branch_name is not None
            ):
                reasons.append("PATH_MISMATCH")
            return WorktreeInspection(
                assignment_id=assignment.assignment_id,
                registry_status=assignment.status,
                physical_exists=path_exists,
                branch_matches=False,
                head_commit=None,
                clean=None,
                resumable=False,
                reasons=tuple(reasons),
            )
        if not path_exists:
            reasons.append("REGISTERED_WORKTREE_PATH_MISSING")
            return WorktreeInspection(
                assignment_id=assignment.assignment_id,
                registry_status=assignment.status,
                physical_exists=False,
                branch_matches=worktree.branch_name == assignment.branch_name,
                head_commit=worktree.head_commit,
                clean=None,
                resumable=False,
                reasons=tuple(reasons),
            )
        branch_matches = worktree.branch_name == assignment.branch_name
        if not branch_matches:
            reasons.append("BRANCH_MISMATCH")
        head = self._current_head(path)
        clean = self._is_clean(path)
        if not self._is_ancestor(assignment.baseline_commit, head):
            reasons.append("BASELINE_NOT_ANCESTOR")
        try:
            branch_tip = self._git.branch_tip(assignment.branch_name)
        except GitOperationError:
            reasons.append("BRANCH_REF_MISSING")
        else:
            if branch_tip != head:
                reasons.append("BRANCH_TIP_MISMATCH")
        return WorktreeInspection(
            assignment_id=assignment.assignment_id,
            registry_status=assignment.status,
            physical_exists=path_exists,
            branch_matches=branch_matches,
            head_commit=head,
            clean=clean,
            resumable=not reasons,
            reasons=tuple(reasons),
        )

    def _verify_structural_operation(self, assignment: WorktreeAssignment) -> None:
        self._verify_repository()
        if self._resolve_commit(assignment.baseline_commit) != assignment.baseline_commit:
            raise WorktreeManagerError(
                "BASELINE_MISMATCH", "assignment baseline no longer resolves exactly"
            )
        self._require_clean_primary()

    def _verify_new_worktree(self, assignment: WorktreeAssignment) -> None:
        path = Path(assignment.worktree_path)
        worktree = _find_worktree(self._list_worktrees(), path)
        if worktree is None or not path.exists():
            raise WorktreeManagerError("WORKTREE_MISSING", "created worktree is absent")
        if worktree.branch_name != assignment.branch_name:
            raise WorktreeManagerError("BRANCH_MISMATCH", "created branch is incorrect")
        if self._current_branch(path) != assignment.branch_name:
            raise WorktreeManagerError("BRANCH_MISMATCH", "checked-out branch is incorrect")
        if self._current_head(path) != assignment.baseline_commit:
            raise WorktreeManagerError("HEAD_MISMATCH", "created HEAD differs from baseline")
        if not self._is_clean(path):
            raise WorktreeManagerError("DIRTY_WORKTREE", "new worktree is not clean")

    def _cleanup_partial_creation(self, assignment: WorktreeAssignment) -> str:
        path = Path(assignment.worktree_path)
        try:
            worktree = _find_worktree(self._list_worktrees(), path)
            if (
                worktree is not None
                and worktree.branch_name == assignment.branch_name
                and self._is_clean(path)
            ):
                self._git.remove_worktree(path)
                return "safe partial worktree cleanup completed; branch retained"
        except (GitOperationError, WorktreeManagerError):
            pass
        return "partial resources may remain and require inspection"

    def _persist(
        self,
        before: WorktreeRegistry,
        candidate: WorktreeRegistry,
        operation: str,
    ) -> None:
        authorization = _issue_registry_write(
            store=self._store,
            before=before,
            candidate=candidate,
            operation=operation,
        )
        try:
            self._store._save_authorized(
                candidate,
                authorization=authorization,
                operation=operation,
            )
        except PersistenceError as error:
            raise _registry_error(error) from error

    def _load_registry(self) -> WorktreeRegistry:
        try:
            registry = self._store.load()
        except PersistenceError as error:
            raise _registry_error(error) from error
        for assignment in registry.assignments:
            path = Path(assignment.worktree_path)
            if (
                _path_key(path.parent) != _path_key(self._worktree_root)
                or path.name != assignment.assignment_id
                or len(str(path)) > 240
            ):
                raise WorktreeManagerError(
                    "WORKTREE_ROOT_MISMATCH",
                    "registry assignment path is outside configured worktree root",
                    reasons=(assignment.assignment_id,),
                )
        return registry

    def _verify_repository(self) -> None:
        try:
            self._git.verify_repository()
        except GitOperationError as error:
            raise _git_error(error) from error

    def _require_clean_primary(self) -> None:
        try:
            clean = self._git.is_clean(
                self._repository_root,
                exclude_registry=True,
            )
        except GitOperationError as error:
            raise _git_error(error) from error
        if not clean:
            raise WorktreeManagerError(
                "DIRTY_PRIMARY", "primary worktree has unexpected changes"
            )

    def _resolve_commit(self, commit: str) -> str:
        try:
            return self._git.resolve_commit(commit)
        except GitOperationError as error:
            raise _git_error(error) from error

    def _list_worktrees(self) -> tuple[GitWorktree, ...]:
        try:
            return self._git.list_worktrees()
        except GitOperationError as error:
            raise _git_error(error) from error

    def _branch_exists(self, branch: str) -> bool:
        try:
            return self._git.branch_exists(branch)
        except GitOperationError as error:
            raise _git_error(error) from error

    def _current_branch(self, path: Path) -> str:
        try:
            return self._git.current_branch(path)
        except GitOperationError as error:
            raise _git_error(error) from error

    def _current_head(self, path: Path) -> str:
        try:
            return self._git.current_head(path)
        except GitOperationError as error:
            raise _git_error(error) from error

    def _is_clean(self, path: Path) -> bool:
        try:
            return self._git.is_clean(path)
        except GitOperationError as error:
            raise _git_error(error) from error

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        try:
            return self._git.is_ancestor(ancestor, descendant)
        except GitOperationError as error:
            raise _git_error(error) from error


def _registry_with(
    registry: WorktreeRegistry,
    assignment: WorktreeAssignment,
) -> WorktreeRegistry:
    assignments = {
        item.assignment_id: item for item in registry.assignments
    }
    assignments[assignment.assignment_id] = assignment
    return WorktreeRegistry(
        schema_version=registry.schema_version,
        assignments=tuple(assignments[key] for key in sorted(assignments)),
    )


def _assignment_or_none(
    registry: WorktreeRegistry, assignment_id: str
) -> WorktreeAssignment | None:
    return next(
        (item for item in registry.assignments if item.assignment_id == assignment_id),
        None,
    )


def _require_assignment(
    registry: WorktreeRegistry, assignment_id: object
) -> WorktreeAssignment:
    if not isinstance(assignment_id, str) or not assignment_id.strip():
        raise WorktreeManagerError("INVALID_INPUT", "assignment_id is required")
    assignment = _assignment_or_none(registry, assignment_id)
    if assignment is None:
        raise WorktreeManagerError("ASSIGNMENT_NOT_FOUND", "assignment is absent")
    return assignment


def _validate_generation_value(value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise WorktreeManagerError(
            "INVALID_INPUT", "current_generation must be a non-negative integer"
        )


def _require_generation(assignment: WorktreeAssignment, current: object) -> None:
    _validate_generation_value(current)
    if assignment.workflow_generation != current:
        raise WorktreeManagerError(
            "STALE_GENERATION", "assignment generation differs from current mission"
        )


def _find_worktree(
    worktrees: tuple[GitWorktree, ...], path: Path
) -> GitWorktree | None:
    key = _path_key(path)
    return next((item for item in worktrees if _path_key(item.path) == key), None)


def _contains(parent: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False))).casefold()


def _branch_key(branch: str) -> str:
    return branch.casefold()


def _git_error(error: GitOperationError) -> WorktreeManagerError:
    return WorktreeManagerError(
        error.code,
        error.message,
        reasons=(f"GIT_EXIT:{error.exit_code}",) if error.exit_code is not None else (),
    )


def _registry_error(error: PersistenceError) -> WorktreeManagerError:
    return WorktreeManagerError(
        error.code,
        error.message,
        reasons=(f"REGISTRY:{error.code}",),
    )
