"""Backup-first application of exact, closed repository upgrade plans."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path, PurePosixPath

from agentic_engineering_os.application._identity import (
    is_attributable_human_identity,
)
from agentic_engineering_os.domain import (
    HumanUpgradeConfirmation,
    UpgradeFinding,
    UpgradeOperationResult,
    UpgradeOperationStatus,
    UpgradePlan,
    UpgradePlanStatus,
    UpgradeResult,
    UpgradeResultStatus,
)

from .git_adapter import GitAdapter, GitOperationError, GitReadOnlyState
from .migration_registry import MigrationRegistryError, RepositoryMigrationRegistry
from .repository_reconnaissance import (
    RepositoryReconnaissance,
    RepositoryReconnaissanceError,
)


class RepositoryUpgradeService:
    """Apply only a freshly reconstructed UpgradePlan through registered edges."""

    def __init__(self) -> None:
        from agentic_engineering_os.application.upgrade_planner import UpgradePlanner

        self._planner = UpgradePlanner()
        self._registry = RepositoryMigrationRegistry()
        self._reconnaissance = RepositoryReconnaissance()

    def apply(
        self,
        plan: UpgradePlan,
        *,
        confirmations: tuple[HumanUpgradeConfirmation, ...] = (),
    ) -> UpgradeResult:
        if not isinstance(plan, UpgradePlan):
            raise TypeError("RepositoryUpgradeService.apply requires UpgradePlan")
        try:
            reconstructed = self._planner.plan(plan.repository_root)
        except Exception as error:
            return _refused(plan, "TRUST_RECONSTRUCTION_FAILED", _error_code(error))
        if reconstructed != plan:
            return _refused(plan, "STALE_OR_FOREIGN_PLAN", "plan no longer matches repository")
        if plan.status is UpgradePlanStatus.ALREADY_CURRENT:
            return UpgradeResult(
                plan.repository_root,
                UpgradeResultStatus.ALREADY_CURRENT,
                plan.plan_fingerprint,
                (),
                (),
                _inspect_optional(self._reconnaissance, plan.repository_root),
            )
        if plan.blockers or plan.status is UpgradePlanStatus.BLOCKED:
            return _refused(plan, "PLAN_BLOCKED", "upgrade plan contains blockers")
        confirmation_error = _confirmation_error(plan, confirmations)
        if confirmation_error is not None:
            return _refused(plan, confirmation_error, "Human confirmation is not valid")

        root = Path(plan.repository_root)
        try:
            baseline_git = GitAdapter(root).observe_read_only()
        except GitOperationError as error:
            return _refused(plan, f"GIT_PREFLIGHT_FAILED:{error.code}", error.message)
        expected_changed: set[str] = set()
        operations: list[UpgradeOperationResult] = []
        mutated = False
        for step in plan.steps:
            backup_created = False
            try:
                _revalidate_git(root, baseline_git, expected_changed)
                source = _read_exact_source(root, step.target_path)
                if _sha256(source) != step.source_fingerprint:
                    raise _UpgradeFailure(
                        "SOURCE_FINGERPRINT_MISMATCH", "source bytes changed after planning"
                    )
                candidate = self._registry.prepare_candidate(
                    step.artifact,
                    step.source_version,
                    step.target_version,
                    source,
                )
                if (
                    _sha256(candidate.content) != step.target_fingerprint
                    or candidate.authority_fingerprint_before
                    != step.authority_fingerprint_before
                    or candidate.authority_fingerprint_after
                    != step.authority_fingerprint_after
                ):
                    raise _UpgradeFailure(
                        "CANDIDATE_FINGERPRINT_MISMATCH",
                        "registered transformation differs from plan",
                    )
                _create_backup(root, step.backup_path, source)
                backup_created = True
                mutated = True
                expected_changed.add(step.backup_path)
                _revalidate_git(root, baseline_git, expected_changed)
                if _read_exact_source(root, step.target_path) != source:
                    raise _UpgradeFailure(
                        "SOURCE_CHANGED_BEFORE_REPLACE",
                        "source changed after backup creation",
                    )
                _replace_candidate(root, step.target_path, candidate.content)
                definition = self._registry.definition(
                    step.artifact, step.source_version, step.target_version
                )
                if definition is None:
                    raise _UpgradeFailure(
                        "MIGRATION_EDGE_DISAPPEARED",
                        "registered migration edge is unavailable",
                    )
                if definition.versioned_in_git:
                    expected_changed.add(step.target_path)
                written = _read_exact_source(root, step.target_path)
                if written != candidate.content:
                    raise _UpgradeFailure(
                        "POST_WRITE_MISMATCH", "migrated bytes differ from candidate"
                    )
                self._registry.validate_current(
                    step.artifact, step.target_version, written
                )
                _revalidate_git(root, baseline_git, expected_changed)
                operations.append(
                    UpgradeOperationResult(
                        step.step_id,
                        step.artifact,
                        step.target_path,
                        step.backup_path,
                        UpgradeOperationStatus.MIGRATED,
                        "backup preserved and migrated artifact validated",
                    )
                )
            except (
                _UpgradeFailure,
                MigrationRegistryError,
                GitOperationError,
                OSError,
            ) as error:
                if backup_created:
                    mutated = True
                operations.append(
                    UpgradeOperationResult(
                        step.step_id,
                        step.artifact,
                        step.target_path,
                        step.backup_path,
                        UpgradeOperationStatus.FAILED,
                        _error_code(error),
                    )
                )
                attempted = len(operations)
                operations.extend(
                    UpgradeOperationResult(
                        item.step_id,
                        item.artifact,
                        item.target_path,
                        item.backup_path,
                        UpgradeOperationStatus.NOT_ATTEMPTED,
                        "stopped after prior failure",
                    )
                    for item in plan.steps[attempted:]
                )
                return UpgradeResult(
                    plan.repository_root,
                    UpgradeResultStatus.PARTIAL_FAILURE
                    if mutated
                    else UpgradeResultStatus.FAILED,
                    plan.plan_fingerprint,
                    tuple(operations),
                    (
                        UpgradeFinding(
                            f"MIGRATION_FAILED:{_error_code(error)}",
                            step.target_path,
                            "upgrade stopped without rollback; backups are preserved",
                        ),
                    ),
                    _inspect_optional(self._reconnaissance, root),
                )

        final_profile = _inspect_optional(self._reconnaissance, root)
        final_plan = self._planner.plan(root)
        if final_plan.status is not UpgradePlanStatus.ALREADY_CURRENT:
            return UpgradeResult(
                plan.repository_root,
                UpgradeResultStatus.PARTIAL_FAILURE,
                plan.plan_fingerprint,
                tuple(operations),
                (
                    UpgradeFinding(
                        "FINAL_VALIDATION_FAILED",
                        ".",
                        "fresh planning did not prove every artifact current",
                    ),
                ),
                final_profile,
            )
        return UpgradeResult(
            plan.repository_root,
            UpgradeResultStatus.MIGRATED,
            plan.plan_fingerprint,
            tuple(operations),
            (),
            final_profile,
        )


class _UpgradeFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _confirmation_error(
    plan: UpgradePlan, confirmations: tuple[HumanUpgradeConfirmation, ...]
) -> str | None:
    if not isinstance(confirmations, tuple) or any(
        not isinstance(item, HumanUpgradeConfirmation) for item in confirmations
    ):
        return "INVALID_CONFIRMATION_COLLECTION"
    required = {
        item.step_id: item
        for item in plan.steps
        if item.human_confirmation_required
    }
    supplied: dict[str, HumanUpgradeConfirmation] = {}
    for confirmation in confirmations:
        if confirmation.step_id in supplied:
            return "DUPLICATE_HUMAN_CONFIRMATION"
        supplied[confirmation.step_id] = confirmation
        step = required.get(confirmation.step_id)
        if step is None:
            return "UNEXPECTED_HUMAN_CONFIRMATION"
        if not is_attributable_human_identity(confirmation.confirmed_by):
            return "INVALID_HUMAN_IDENTITY"
        if (
            confirmation.plan_fingerprint != plan.plan_fingerprint
            or confirmation.artifact is not step.artifact
            or confirmation.source_fingerprint != step.source_fingerprint
            or confirmation.target_version != step.target_version
        ):
            return "HUMAN_CONFIRMATION_BINDING_MISMATCH"
    if set(supplied) != set(required):
        return "MISSING_HUMAN_CONFIRMATION"
    return None


def _revalidate_git(
    root: Path, baseline: GitReadOnlyState, expected_changed: set[str]
) -> None:
    adapter = GitAdapter(root)
    current = adapter.observe_read_only()
    if (
        current.top_level != baseline.top_level
        or current.head_commit != baseline.head_commit
        or current.branch_name != baseline.branch_name
        or current.detached != baseline.detached
        or current.worktrees != baseline.worktrees
    ):
        raise _UpgradeFailure("GIT_IDENTITY_CHANGED", "repository identity changed")
    changed = set(adapter.worktree_changed_paths(root))
    if changed != expected_changed:
        raise _UpgradeFailure(
            "UNEXPECTED_DIRTY_STATE",
            "Git changes differ from migration-owned paths",
        )


def _read_exact_source(root: Path, relative: str) -> bytes:
    target = _safe_target(root, relative)
    if target.is_symlink() or not target.is_file():
        raise _UpgradeFailure("UNSAFE_ARTIFACT", "migration source is not a regular file")
    maximum = 256_000 if relative == "AGENTS.md" else 16_000_000
    if target.stat().st_size > maximum:
        raise _UpgradeFailure("SOURCE_TOO_LARGE", "migration source exceeds policy")
    return target.read_bytes()


def _create_backup(root: Path, relative: str, source: bytes) -> None:
    target = _safe_target(root, relative)
    if target.exists() or target.is_symlink():
        raise _UpgradeFailure("BACKUP_COLLISION", "backup target already exists")
    temporary = _write_temporary(target.parent, source, ".migration-backup.")
    try:
        try:
            os.link(temporary, target)
        except FileExistsError as error:
            raise _UpgradeFailure("BACKUP_COLLISION", "backup appeared concurrently") from error
        if target.read_bytes() != source:
            raise _UpgradeFailure("BACKUP_VERIFICATION_FAILED", "backup bytes differ")
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_candidate(root: Path, relative: str, content: bytes) -> None:
    target = _safe_target(root, relative)
    temporary = _write_temporary(target.parent, content, ".migration-candidate.")
    try:
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_temporary(directory: Path, content: bytes, prefix: str) -> Path:
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=".tmp")
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _safe_target(root: Path, relative: str) -> Path:
    candidate = PurePosixPath(relative)
    if (
        not relative
        or str(candidate) != relative
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise _UpgradeFailure("UNSAFE_PATH", "migration path is not canonical")
    target = root.joinpath(*candidate.parts)
    cursor = root
    for part in candidate.parts[:-1]:
        cursor /= part
        if cursor.is_symlink():
            raise _UpgradeFailure("UNSAFE_PATH", "migration parent is a symlink")
    if target.parent.resolve(strict=True) != target.parent:
        raise _UpgradeFailure("UNSAFE_PATH", "migration parent is not canonical")
    return target


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _refused(plan: UpgradePlan, code: str, detail: str) -> UpgradeResult:
    return UpgradeResult(
        plan.repository_root,
        UpgradeResultStatus.REFUSED,
        plan.plan_fingerprint,
        (),
        (UpgradeFinding(code, ".", detail),),
        None,
    )


def _inspect_optional(
    reconnaissance: RepositoryReconnaissance, root: Path | str
):
    try:
        return reconnaissance.inspect(root)
    except (RepositoryReconnaissanceError, OSError):
        return None


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _error_code(error: object) -> str:
    return str(getattr(error, "code", type(error).__name__))
