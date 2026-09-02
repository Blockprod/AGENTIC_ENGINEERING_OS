"""Transactional, fail-closed integration of a P3.9-approved parallel group."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from agentic_engineering_os.domain import WorktreeAssignment, WorktreeStatus, to_dict
from agentic_engineering_os.infrastructure.git_adapter import GitOperationError
from agentic_engineering_os.infrastructure._negative_outcome_store import (
    _NegativeOutcomeStore,
)
from agentic_engineering_os.infrastructure.project_state_store import PersistenceError
from agentic_engineering_os.infrastructure.worktree_manager import (
    WorktreeManager,
    WorktreeManagerError,
)

from .contract_validator import ContractValidator
from .integration_gate import (
    IntegrationGate,
    IntegrationGateClassification,
    IntegrationGateContext,
    IntegrationGateError,
    IntegrationGateResult,
)
from .parallel_implementer_coordinator import ParallelGroupStatus


class MergeStatus(str, Enum):
    MERGED = "MERGED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class MergeFindingCode(str, Enum):
    GATE_NOT_PASS = "GATE_NOT_PASS"
    STALE_INTEGRATION_GATE = "STALE_INTEGRATION_GATE"
    PRIMARY_DRIFT = "PRIMARY_DRIFT"
    PRIMARY_DIRTY = "PRIMARY_DIRTY"
    MEMBER_MISMATCH = "MEMBER_MISMATCH"
    MEMBER_DRIFT = "MEMBER_DRIFT"
    TEMP_RESOURCE_DIVERGENCE = "TEMP_RESOURCE_DIVERGENCE"
    GIT_MERGE_CONFLICT = "GIT_MERGE_CONFLICT"
    GIT_OPERATION_FAILED = "GIT_OPERATION_FAILED"
    PROMOTION_FAILED = "PROMOTION_FAILED"
    FINAL_STATE_INVALID = "FINAL_STATE_INVALID"
    ALREADY_MERGED = "ALREADY_MERGED"


@dataclass(frozen=True, slots=True)
class MergeFinding:
    code: MergeFindingCode
    summary: str
    members: tuple[str, ...] = ()
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class MergeResult:
    mission_id: str
    workflow_generation: int
    wave_index: int
    group_index: int
    baseline_commit: str
    integration_order: tuple[str, ...]
    member_commits: tuple[str, ...]
    integration_commit: str | None
    primary_before: str
    primary_after: str
    result: MergeStatus
    findings: tuple[MergeFinding, ...]


@dataclass(frozen=True, slots=True)
class MergeContext:
    gate_context: IntegrationGateContext
    gate_result: IntegrationGateResult


class MergeCoordinationError(RuntimeError):
    """The input is too malformed to evaluate as a merge request."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class MergeCoordinator:
    """Stage exact member commits, then promote one proven integration result."""

    def __init__(
        self,
        *,
        worktree_manager: WorktreeManager,
        integration_gate: IntegrationGate | None = None,
        contract_validator: ContractValidator | None = None,
    ) -> None:
        if not isinstance(worktree_manager, WorktreeManager):
            raise MergeCoordinationError(
                "INVALID_CONFIGURATION", "WorktreeManager is required"
            )
        self._manager = worktree_manager
        self._git = worktree_manager._integration_git_adapter
        self._gate = integration_gate or IntegrationGate(
            worktree_manager=worktree_manager
        )
        self._validator = contract_validator or ContractValidator()
        self._outcomes = _NegativeOutcomeStore(worktree_manager.repository_root)

    def merge(self, context: MergeContext) -> MergeResult:
        if not isinstance(context, MergeContext):
            raise MergeCoordinationError("INVALID_CONTEXT", "MergeContext is required")
        try:
            pending = self._outcomes._pending()
        except PersistenceError as error:
            raise MergeCoordinationError(
                "TRANSACTION_AUTHORITY_UNAVAILABLE",
                "pending remediation authority cannot be inspected",
            ) from error
        if pending is not None:
            raise MergeCoordinationError(
                "RECOVERY_PENDING",
                "merge is blocked until the pending remediation transaction is resolved",
            )
        gate = context.gate_result
        gate_context = context.gate_context
        if not isinstance(gate, IntegrationGateResult) or not isinstance(
            gate_context, IntegrationGateContext
        ):
            raise MergeCoordinationError(
                "INVALID_CONTEXT", "gate context and result are required"
            )
        self._validate_static_binding(context)

        try:
            primary = self._manager.inspect_primary()
        except WorktreeManagerError as error:
            raise MergeCoordinationError(
                "GIT_STATE_UNKNOWN", "primary repository cannot be inspected"
            ) from error
        primary_before = primary.head_commit
        member_commits = tuple(item.result_commit for item in gate.member_commits)

        if gate.result is not IntegrationGateClassification.PASS:
            return self._result(
                gate,
                member_commits,
                primary_before,
                primary_before,
                MergeStatus.BLOCKED,
                MergeFinding(
                    MergeFindingCode.GATE_NOT_PASS,
                    "IntegrationGate result is not PASS",
                    gate.integration_order,
                ),
            )

        identity = _integration_identity(gate)
        branch_name = (
            f"agentic/integration/g{gate.workflow_generation}/"
            f"w{gate.wave_index}-g{gate.group_index}-{identity[:12]}"
        )
        integration_path = self._manager.worktree_root / f"integration-{identity[:24]}"
        try:
            assignments = self._assignments(gate)
        except MergeCoordinationError:
            return self._blocked(
                gate,
                member_commits,
                primary_before,
                MergeFindingCode.MEMBER_MISMATCH,
                "authoritative assignments do not exactly match Gate members",
            )

        idempotent = self._already_merged(
            gate,
            assignments,
            branch_name,
            integration_path,
            primary,
        )
        if idempotent is not None:
            return idempotent

        if primary.head_commit != gate.baseline_commit:
            return self._blocked(
                gate,
                member_commits,
                primary_before,
                MergeFindingCode.PRIMARY_DRIFT,
                "primary HEAD differs from the gated baseline",
            )
        if not primary.clean:
            return self._blocked(
                gate,
                member_commits,
                primary_before,
                MergeFindingCode.PRIMARY_DIRTY,
                "primary worktree is not clean",
            )

        try:
            fresh_gate = self._gate.evaluate(gate_context)
        except (IntegrationGateError, WorktreeManagerError, GitOperationError) as error:
            return self._blocked(
                gate,
                member_commits,
                primary_before,
                MergeFindingCode.STALE_INTEGRATION_GATE,
                f"IntegrationGate freshness cannot be proven: {type(error).__name__}",
            )
        if fresh_gate != gate or fresh_gate.result is not IntegrationGateClassification.PASS:
            return self._blocked(
                gate,
                member_commits,
                primary_before,
                MergeFindingCode.STALE_INTEGRATION_GATE,
                "current IntegrationGate evaluation differs from the supplied PASS",
            )

        registry_before = self._manager.registry_store.load()
        try:
            self._prepare_integration_resource(
                gate, branch_name, integration_path
            )
        except (GitOperationError, OSError) as error:
            return self._blocked(
                gate,
                member_commits,
                primary_before,
                MergeFindingCode.TEMP_RESOURCE_DIVERGENCE,
                f"temporary integration resource is unavailable: {type(error).__name__}",
            )

        expected_head = gate.baseline_commit
        for story_id, commit in zip(gate.integration_order, member_commits, strict=True):
            drift = self._pre_merge_drift(
                gate,
                assignments[story_id],
                commit,
                integration_path,
                expected_head,
                registry_before,
            )
            if drift is not None:
                return self._blocked(
                    gate,
                    member_commits,
                    primary_before,
                    drift[0],
                    drift[1],
                )
            try:
                attempt = self._git.merge_no_ff(
                    integration_path,
                    commit,
                    message=f"merge: integrate {story_id}",
                )
            except GitOperationError as error:
                return self._failed_git_operation(
                    gate, member_commits, primary_before, integration_path, error
                )
            if not attempt.merged:
                return self._abort_conflict(
                    gate,
                    member_commits,
                    primary_before,
                    integration_path,
                    expected_head,
                    story_id,
                )
            expected_head = attempt.head_commit

        integration_commit = expected_head
        if not self._is_exact_integration_sequence(
            gate.baseline_commit, member_commits, integration_commit
        ):
            return self._blocked(
                gate,
                member_commits,
                primary_before,
                MergeFindingCode.FINAL_STATE_INVALID,
                "staged integration does not preserve the exact certified merge order",
            )
        final_drift = self._pre_promotion_drift(
            gate, assignments, integration_path, integration_commit, registry_before
        )
        if final_drift is not None:
            return self._blocked(
                gate,
                member_commits,
                primary_before,
                final_drift[0],
                final_drift[1],
            )

        try:
            promoted = self._git.fast_forward(
                self._manager.repository_root,
                gate.baseline_commit,
                integration_commit,
            )
        except GitOperationError as error:
            observed = self._manager.inspect_primary()
            return self._result(
                gate,
                member_commits,
                primary_before,
                observed.head_commit,
                MergeStatus.BLOCKED,
                MergeFinding(
                    MergeFindingCode.PROMOTION_FAILED,
                    f"primary fast-forward failed: {error.code}",
                    gate.integration_order,
                ),
            )

        final = self._manager.inspect_primary()
        registry_after = self._manager.registry_store.load()
        branches_unchanged = all(
            self._git.branch_tip(assignments[story].branch_name)
            == assignments[story].result_commit
            for story in gate.integration_order
        )
        if (
            promoted != integration_commit
            or final.head_commit != integration_commit
            or not final.clean
            or registry_after != registry_before
            or not branches_unchanged
        ):
            return self._result(
                gate,
                member_commits,
                primary_before,
                final.head_commit,
                MergeStatus.FAILED,
                MergeFinding(
                    MergeFindingCode.FINAL_STATE_INVALID,
                    "post-promotion invariants are not satisfied",
                    gate.integration_order,
                ),
                integration_commit=integration_commit,
            )
        return self._result(
            gate,
            member_commits,
            primary_before,
            final.head_commit,
            MergeStatus.MERGED,
            integration_commit=integration_commit,
        )

    def recover_merged(
        self,
        gate_context: IntegrationGateContext,
        *,
        expected_gate_fingerprint: str,
    ) -> tuple[IntegrationGateResult, MergeResult]:
        """Recognize one exact post-merge result without synthesizing Gate authority."""

        try:
            primary = self._manager.inspect_primary()
        except WorktreeManagerError as error:
            raise MergeCoordinationError(
                "GIT_STATE_UNKNOWN", "primary repository cannot be inspected"
            ) from error
        if not primary.clean:
            raise MergeCoordinationError(
                "RECOVERY_REQUIRED", "post-merge primary is not clean"
            )
        try:
            gate = self._gate._reconstruct_after_merge(
                gate_context,
                integrated_commit=primary.head_commit,
                expected_fingerprint=expected_gate_fingerprint,
            )
        except IntegrationGateError as error:
            raise MergeCoordinationError(error.code, error.message) from error
        if gate.result is not IntegrationGateClassification.PASS:
            raise MergeCoordinationError(
                "STALE_INTEGRATION_GATE", "historical Gate is no longer PASS"
            )
        result = self.merge(MergeContext(gate_context=gate_context, gate_result=gate))
        if (
            result.result is not MergeStatus.MERGED
            or result.integration_commit != primary.head_commit
            or not any(
                finding.code is MergeFindingCode.ALREADY_MERGED
                for finding in result.findings
            )
        ):
            raise MergeCoordinationError(
                "RECOVERY_REQUIRED", "current primary is not the exact gated merge"
            )
        return gate, result

    def _validate_negative_outcome(
        self, context: MergeContext, result: MergeResult
    ) -> None:
        """Prove that an exact negative DTO was emitted by a real merge attempt."""

        if not isinstance(context, MergeContext) or not isinstance(result, MergeResult):
            raise MergeCoordinationError(
                "UNTRUSTED_MERGE_OUTCOME", "merge context and result are required"
            )
        self._validate_static_binding(context)
        gate = context.gate_result
        expected_members = tuple(item.result_commit for item in gate.member_commits)
        if (
            gate.result is not IntegrationGateClassification.PASS
            or result.result not in {MergeStatus.FAILED, MergeStatus.BLOCKED}
            or result.mission_id != gate.mission_id
            or result.workflow_generation != gate.workflow_generation
            or result.wave_index != gate.wave_index
            or result.group_index != gate.group_index
            or result.baseline_commit != gate.baseline_commit
            or result.integration_order != gate.integration_order
            or result.member_commits != expected_members
        ):
            raise MergeCoordinationError(
                "UNTRUSTED_MERGE_OUTCOME",
                "negative merge outcome is not exactly bound to its Gate context",
            )
        validation = self._validator.validate("merge-result", to_dict(result))
        if not validation.is_valid:
            raise MergeCoordinationError(
                "UNTRUSTED_MERGE_OUTCOME", "negative merge outcome is structurally invalid"
            )
        self._assignments(gate)
        if result.result is MergeStatus.FAILED:
            try:
                observed_gate = self._gate.evaluate(context.gate_context)
            except (IntegrationGateError, WorktreeManagerError, GitOperationError) as error:
                raise MergeCoordinationError(
                    "MERGE_OUTCOME_AUTHORITY_UNAVAILABLE",
                    "current Gate and Git reality cannot be revalidated",
                ) from error
            if observed_gate != gate:
                raise MergeCoordinationError(
                    "STALE_MERGE_OUTCOME",
                    "current Gate and assignments differ from the failed merge attempt",
                )
        try:
            current = self._manager.inspect_primary()
            if current.head_commit != result.primary_after:
                raise MergeCoordinationError(
                    "STALE_MERGE_OUTCOME",
                    "primary HEAD differs from the recorded negative merge outcome",
                )
            if not self._outcomes._contains_unconsumed(to_dict(result)):
                raise MergeCoordinationError(
                    "UNTRUSTED_MERGE_OUTCOME",
                    "negative merge outcome was not emitted or was already consumed",
                )
        except PersistenceError as error:
            raise MergeCoordinationError(
                "MERGE_OUTCOME_AUTHORITY_UNAVAILABLE",
                "negative merge outcome authority cannot be read",
            ) from error

    def _consume_negative_outcome(
        self, context: MergeContext, result: MergeResult
    ) -> None:
        self._validate_negative_outcome(context, result)
        try:
            self._outcomes._consume(to_dict(result))
        except PersistenceError as error:
            raise MergeCoordinationError(
                "UNTRUSTED_MERGE_OUTCOME",
                "negative merge outcome cannot be consumed exactly once",
            ) from error

    @staticmethod
    def _validate_static_binding(context: MergeContext) -> None:
        gate = context.gate_result
        gate_context = context.gate_context
        plan = gate_context.parallel_plan
        group = gate_context.group_result
        coordination = gate_context.coordination_input
        if (
            not isinstance(group.group_index, int)
            or isinstance(group.group_index, bool)
            or group.group_index < 0
            or group.group_index >= len(plan.groups)
        ):
            raise MergeCoordinationError(
                "INVALID_CONTEXT", "merge group index is invalid"
            )
        if (
            gate.mission_id != coordination.mission_id
            or gate.workflow_generation != coordination.workflow_generation
            or gate.wave_index != plan.wave_index
            or gate.group_index != group.group_index
            or gate.baseline_commit != plan.baseline_commit
            or group.status is not ParallelGroupStatus.COMPLETED
            or gate.integration_order != plan.groups[group.group_index].user_story_ids
            or tuple(item.user_story_id for item in gate.member_commits)
            != gate.integration_order
            or tuple(item.assignment_id for item in gate.member_commits)
            != group.assignment_ids
            or tuple(item.result_commit for item in gate.member_commits)
            != group.result_commits
        ):
            raise MergeCoordinationError(
                "INVALID_CONTEXT", "merge context is not exactly bound to its group"
            )

    def _assignments(
        self, gate: IntegrationGateResult
    ) -> dict[str, WorktreeAssignment]:
        registry = self._manager.registry_store.load()
        by_id = {item.assignment_id: item for item in registry.assignments}
        assignments: dict[str, WorktreeAssignment] = {}
        for member in gate.member_commits:
            assignment = by_id.get(member.assignment_id)
            if (
                assignment is None
                or assignment.status is not WorktreeStatus.COMPLETED
                or assignment.mission_id != gate.mission_id
                or assignment.workflow_generation != gate.workflow_generation
                or assignment.baseline_commit != gate.baseline_commit
                or assignment.user_story_id != member.user_story_id
                or assignment.result_commit != member.result_commit
            ):
                raise MergeCoordinationError(
                    "INVALID_CONTEXT", "authoritative assignment does not match Gate member"
                )
            assignments[member.user_story_id] = assignment
        return assignments

    def _already_merged(
        self,
        gate: IntegrationGateResult,
        assignments: dict[str, WorktreeAssignment],
        branch_name: str,
        path: Path,
        primary: object,
    ) -> MergeResult | None:
        branch = next(
            (
                item
                for item in self._git.list_worktrees()
                if item.branch_name is not None
                and item.branch_name.casefold() == branch_name.casefold()
            ),
            None,
        )
        if branch is None or _path_key(branch.path) != _path_key(path):
            return None
        tip = self._git.branch_tip(branch_name)
        if (
            primary.head_commit != tip
            or primary.head_commit == gate.baseline_commit
            or not primary.clean
            or not self._git.is_clean(path)
        ):
            return None
        commits = tuple(item.result_commit for item in gate.member_commits)
        if not self._is_exact_integration_sequence(
            gate.baseline_commit, commits, tip
        ):
            return None
        if not all(
            self._git.branch_tip(assignments[story].branch_name)
            == assignments[story].result_commit
            for story in gate.integration_order
        ):
            return None
        return self._result(
            gate,
            commits,
            primary.head_commit,
            primary.head_commit,
            MergeStatus.MERGED,
            MergeFinding(
                MergeFindingCode.ALREADY_MERGED,
                "exact integration result is already promoted",
                gate.integration_order,
                blocking=False,
            ),
            integration_commit=tip,
        )

    def _is_exact_integration_sequence(
        self,
        baseline: str,
        member_commits: tuple[str, ...],
        integration_commit: str,
    ) -> bool:
        current = integration_commit
        try:
            for member in reversed(member_commits):
                parents = self._git.commit_parents(current)
                if len(parents) != 2 or parents[1] != member:
                    return False
                current = parents[0]
        except GitOperationError:
            return False
        return current == baseline

    def _prepare_integration_resource(
        self, gate: IntegrationGateResult, branch_name: str, path: Path
    ) -> None:
        self._git.validate_branch_name(branch_name)
        branch_exists = self._git.branch_exists(branch_name)
        worktrees = self._git.list_worktrees()
        at_path = next((item for item in worktrees if _path_key(item.path) == _path_key(path)), None)
        by_branch = next(
            (
                item
                for item in worktrees
                if item.branch_name is not None
                and item.branch_name.casefold() == branch_name.casefold()
            ),
            None,
        )
        if not branch_exists and at_path is None and by_branch is None and not path.exists():
            self._git.add_worktree(path, branch_name, gate.baseline_commit)
            return
        if (
            not branch_exists
            or at_path is None
            or by_branch is None
            or at_path != by_branch
            or at_path.branch_name is None
            or at_path.branch_name.casefold() != branch_name.casefold()
            or self._git.branch_tip(branch_name) != gate.baseline_commit
            or at_path.head_commit != gate.baseline_commit
            or not self._git.is_clean(path)
        ):
            raise GitOperationError(
                "INTEGRATION_RESOURCE_DIVERGED",
                "existing integration resource is not a pristine resumable baseline",
            )

    def _pre_merge_drift(
        self,
        gate: IntegrationGateResult,
        assignment: WorktreeAssignment,
        commit: str,
        path: Path,
        expected_head: str,
        registry_before: object,
    ) -> tuple[MergeFindingCode, str] | None:
        primary = self._manager.inspect_primary()
        if primary.head_commit != gate.baseline_commit:
            return MergeFindingCode.PRIMARY_DRIFT, "primary HEAD changed during staging"
        if not primary.clean:
            return MergeFindingCode.PRIMARY_DIRTY, "primary became dirty during staging"
        if (
            self._git.current_head(path) != expected_head
            or not self._git.is_clean(path)
            or self._git.merge_in_progress(path)
        ):
            return (
                MergeFindingCode.TEMP_RESOURCE_DIVERGENCE,
                "integration worktree drifted before member merge",
            )
        if (
            assignment.result_commit != commit
            or self._git.branch_tip(assignment.branch_name) != commit
            or not self._git.is_ancestor(gate.baseline_commit, commit)
        ):
            return MergeFindingCode.MEMBER_DRIFT, "member branch tip or ancestry drifted"
        if self._manager.registry_store.load() != registry_before:
            return MergeFindingCode.MEMBER_DRIFT, "worktree registry changed during staging"
        return None

    def _pre_promotion_drift(
        self,
        gate: IntegrationGateResult,
        assignments: dict[str, WorktreeAssignment],
        path: Path,
        integration_commit: str,
        registry_before: object,
    ) -> tuple[MergeFindingCode, str] | None:
        primary = self._manager.inspect_primary()
        if primary.head_commit != gate.baseline_commit:
            return MergeFindingCode.PRIMARY_DRIFT, "primary HEAD changed before promotion"
        if not primary.clean:
            return MergeFindingCode.PRIMARY_DIRTY, "primary became dirty before promotion"
        if self._git.current_head(path) != integration_commit or not self._git.is_clean(path):
            return (
                MergeFindingCode.TEMP_RESOURCE_DIVERGENCE,
                "staged integration is not clean and exact",
            )
        for story in gate.integration_order:
            assignment = assignments[story]
            if self._git.branch_tip(assignment.branch_name) != assignment.result_commit:
                return MergeFindingCode.MEMBER_DRIFT, "member branch changed before promotion"
        if self._manager.registry_store.load() != registry_before:
            return MergeFindingCode.MEMBER_DRIFT, "worktree registry changed before promotion"
        return None

    def _abort_conflict(
        self,
        gate: IntegrationGateResult,
        member_commits: tuple[str, ...],
        primary_before: str,
        path: Path,
        expected_head: str,
        story_id: str,
    ) -> MergeResult:
        try:
            if self._git.merge_in_progress(path):
                self._git.abort_merge(path)
            restored = (
                self._git.current_head(path) == expected_head
                and self._git.is_clean(path)
            )
        except GitOperationError:
            restored = False
        summary = "Git reported an integration conflict; temporary merge was aborted"
        if not restored:
            summary = "Git conflict cleanup could not restore the expected temporary state"
        return self._result(
            gate,
            member_commits,
            primary_before,
            self._manager.inspect_primary().head_commit,
            MergeStatus.FAILED,
            MergeFinding(
                MergeFindingCode.GIT_MERGE_CONFLICT,
                summary,
                (story_id,),
            ),
        )

    def _failed_git_operation(
        self,
        gate: IntegrationGateResult,
        member_commits: tuple[str, ...],
        primary_before: str,
        path: Path,
        error: GitOperationError,
    ) -> MergeResult:
        if self._git.merge_in_progress(path):
            try:
                self._git.abort_merge(path)
            except GitOperationError:
                pass
        return self._result(
            gate,
            member_commits,
            primary_before,
            self._manager.inspect_primary().head_commit,
            MergeStatus.FAILED,
            MergeFinding(
                MergeFindingCode.GIT_OPERATION_FAILED,
                f"Git integration operation failed: {error.code}",
                gate.integration_order,
            ),
        )

    def _blocked(
        self,
        gate: IntegrationGateResult,
        member_commits: tuple[str, ...],
        primary_before: str,
        code: MergeFindingCode,
        summary: str,
    ) -> MergeResult:
        return self._result(
            gate,
            member_commits,
            primary_before,
            self._manager.inspect_primary().head_commit,
            MergeStatus.BLOCKED,
            MergeFinding(code, summary, gate.integration_order),
        )

    def _result(
        self,
        gate: IntegrationGateResult,
        member_commits: tuple[str, ...],
        primary_before: str,
        primary_after: str,
        status: MergeStatus,
        *findings: MergeFinding,
        integration_commit: str | None = None,
    ) -> MergeResult:
        result = MergeResult(
            mission_id=gate.mission_id,
            workflow_generation=gate.workflow_generation,
            wave_index=gate.wave_index,
            group_index=gate.group_index,
            baseline_commit=gate.baseline_commit,
            integration_order=gate.integration_order,
            member_commits=member_commits,
            integration_commit=integration_commit,
            primary_before=primary_before,
            primary_after=primary_after,
            result=status,
            findings=findings,
        )
        validation = self._validator.validate("merge-result", to_dict(result))
        if not validation.is_valid:
            raise MergeCoordinationError(
                "INVALID_RESULT", "MergeResult violates its schema"
            )
        if status in {MergeStatus.FAILED, MergeStatus.BLOCKED}:
            try:
                self._outcomes._record(to_dict(result))
            except PersistenceError as error:
                raise MergeCoordinationError(
                    "OUTCOME_PERSISTENCE_FAILED",
                    "negative merge outcome could not be recorded authoritatively",
                ) from error
        return result


def _integration_identity(gate: IntegrationGateResult) -> str:
    parts = (
        gate.mission_id,
        str(gate.workflow_generation),
        str(gate.wave_index),
        str(gate.group_index),
        gate.baseline_commit,
    )
    encoded = "".join(f"{len(part.encode('utf-8'))}:{part}" for part in parts)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False))).casefold()
