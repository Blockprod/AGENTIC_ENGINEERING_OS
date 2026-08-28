"""Read-only, fail-closed admission gate for parallel Implementer results."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from itertools import combinations
from typing import TYPE_CHECKING, Protocol

from agentic_engineering_os.domain import (
    ConflictClassification,
    MissionState,
    ParallelExecutionPlan,
    ProjectState,
    UserStory,
    WorktreeAssignment,
    WorktreeRegistry,
    WorktreeStatus,
    to_dict,
)
if TYPE_CHECKING:
    from agentic_engineering_os.infrastructure.git_adapter import (
        GitDiffEntry,
        GitMergePreflight,
        GitPrimaryState,
    )
    from agentic_engineering_os.infrastructure.worktree_manager import WorktreeInspection

from .contract_validator import ContractValidator
from .execution_conflict_analyzer import ExecutionConflictAnalyzer
from .implementer import (
    ImplementerInputError,
    ImplementerResultValidator,
    ImplementerVerdict,
    _normalize_path,
    _scope_matches,
)
from .parallel_implementer_coordinator import (
    ParallelCoordinationInput,
    ParallelGroupResult,
    ParallelGroupStatus,
    ParallelImplementerCoordinator,
    ParallelMemberResult,
)


class IntegrationGateClassification(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class IntegrationFindingCode(str, Enum):
    INCOMPLETE_MEMBER = "INCOMPLETE_MEMBER"
    BASELINE_MISMATCH = "BASELINE_MISMATCH"
    GENERATION_MISMATCH = "GENERATION_MISMATCH"
    ASSIGNMENT_MISMATCH = "ASSIGNMENT_MISMATCH"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    DECLARED_DIFF_MISMATCH = "DECLARED_DIFF_MISMATCH"
    CROSS_BRANCH_PATH_COLLISION = "CROSS_BRANCH_PATH_COLLISION"
    CONFLICT_ANALYSIS_MISMATCH = "CONFLICT_ANALYSIS_MISMATCH"
    GIT_MERGE_CONFLICT = "GIT_MERGE_CONFLICT"
    GIT_STATE_UNKNOWN = "GIT_STATE_UNKNOWN"


@dataclass(frozen=True, slots=True)
class IntegrationFinding:
    code: IntegrationFindingCode
    summary: str
    members: tuple[str, ...]
    paths: tuple[str, ...]
    blocking: bool
    classification: IntegrationGateClassification


@dataclass(frozen=True, slots=True)
class IntegrationMemberCommit:
    user_story_id: str
    assignment_id: str
    result_commit: str
    changed_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IntegrationGateResult:
    mission_id: str
    workflow_generation: int
    wave_index: int
    group_index: int
    baseline_commit: str
    member_commits: tuple[IntegrationMemberCommit, ...]
    result: IntegrationGateClassification
    findings: tuple[IntegrationFinding, ...]
    integration_order: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IntegrationGateContext:
    coordination_input: ParallelCoordinationInput
    parallel_plan: ParallelExecutionPlan
    group_result: ParallelGroupResult
    current_mission_state: MissionState


class IntegrationGateError(RuntimeError):
    """The call context is too malformed to produce a meaningful gate result."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class IntegrationWorktreeManagerPort(Protocol):
    @property
    def registry_store(self) -> object: ...

    def inspect_primary(self) -> GitPrimaryState: ...
    def inspect(self, assignment_id: str, *, current_generation: int) -> WorktreeInspection: ...
    def inspect_all(self, *, current_generation: int) -> object: ...
    def diff_name_status(
        self, baseline_commit: str, result_commit: str
    ) -> tuple[GitDiffEntry, ...]: ...
    def merge_preflight(
        self, baseline_commit: str, left_commit: str, right_commit: str
    ) -> GitMergePreflight: ...


@dataclass(frozen=True, slots=True)
class _ObservedFinding:
    finding: IntegrationFinding


class IntegrationGate:
    """Evaluate integration eligibility without changing Git or control state."""

    def __init__(
        self,
        *,
        worktree_manager: IntegrationWorktreeManagerPort,
        conflict_analyzer: ExecutionConflictAnalyzer | None = None,
        contract_validator: ContractValidator | None = None,
        result_validator: ImplementerResultValidator | None = None,
    ) -> None:
        self._manager = worktree_manager
        self._conflict_analyzer = conflict_analyzer or ExecutionConflictAnalyzer()
        self._contract_validator = contract_validator or ContractValidator()
        self._result_validator = result_validator or ImplementerResultValidator()

    def evaluate(self, context: IntegrationGateContext) -> IntegrationGateResult:
        if not isinstance(context, IntegrationGateContext):
            raise IntegrationGateError("INVALID_CONTEXT", "IntegrationGateContext is required")
        coordination = context.coordination_input
        plan = context.parallel_plan
        group_result = context.group_result
        if not isinstance(coordination, ParallelCoordinationInput):
            raise IntegrationGateError("INVALID_CONTEXT", "coordination_input is required")
        if not isinstance(plan, ParallelExecutionPlan):
            raise IntegrationGateError("INVALID_CONTEXT", "ParallelExecutionPlan is required")
        if not isinstance(group_result, ParallelGroupResult):
            raise IntegrationGateError("INVALID_CONTEXT", "ParallelGroupResult is required")
        if not isinstance(context.current_mission_state, MissionState):
            raise IntegrationGateError("INVALID_CONTEXT", "current MissionState is required")
        try:
            mission_validations = tuple(
                self._contract_validator.validate("mission-state", to_dict(mission))
                for mission in (
                    coordination.mission_state,
                    context.current_mission_state,
                )
            )
            plan_validation = self._contract_validator.validate(
                "parallel-execution-plan", to_dict(plan)
            )
        except Exception as error:
            raise IntegrationGateError(
                "INVALID_CONTEXT",
                f"context validation could not complete: {type(error).__name__}",
            ) from error
        for mission_validation in mission_validations:
            if not mission_validation.is_valid:
                raise IntegrationGateError(
                    "INVALID_CONTEXT", "MissionState violates its contract"
                )
        if (
            not isinstance(group_result.group_index, int)
            or isinstance(group_result.group_index, bool)
            or not isinstance(group_result.member_results, tuple)
            or not isinstance(group_result.assignment_ids, tuple)
            or not isinstance(group_result.result_commits, tuple)
        ):
            raise IntegrationGateError(
                "INVALID_CONTEXT", "ParallelGroupResult is malformed"
            )
        if not plan_validation.is_valid:
            raise IntegrationGateError("INVALID_CONTEXT", "parallel plan violates its contract")
        if group_result.group_index < 0 or group_result.group_index >= len(plan.groups):
            raise IntegrationGateError("INVALID_CONTEXT", "group index is absent from plan")

        target_group = plan.groups[group_result.group_index]
        order = target_group.user_story_ids
        observed: list[_ObservedFinding] = []
        member_commits: list[IntegrationMemberCommit] = []

        primary_before = self._observe_primary(observed)
        if primary_before is not None:
            if primary_before.head_commit != plan.baseline_commit:
                _add(
                    observed,
                    IntegrationFindingCode.BASELINE_MISMATCH,
                    "primary HEAD differs from the planned baseline",
                    classification=IntegrationGateClassification.FAIL,
                )
            if not primary_before.clean:
                _add(
                    observed,
                    IntegrationFindingCode.GIT_STATE_UNKNOWN,
                    "primary worktree is not clean",
                    classification=IntegrationGateClassification.UNKNOWN,
                )

        canonical_plan = self._revalidate_plan(coordination, observed)
        if canonical_plan is not None and plan != canonical_plan:
            _add(
                observed,
                IntegrationFindingCode.CONFLICT_ANALYSIS_MISMATCH,
                "parallel plan is not canonical for the current conflict analysis",
                members=order,
                classification=IntegrationGateClassification.FAIL,
            )

        self._validate_mission(context, observed)
        if group_result.status is not ParallelGroupStatus.COMPLETED:
            _add(
                observed,
                IntegrationFindingCode.INCOMPLETE_MEMBER,
                "parallel group is not COMPLETED",
                members=order,
                classification=IntegrationGateClassification.FAIL,
            )

        member_by_story = _unique_members(group_result.member_results, observed)
        if tuple(member.user_story_id for member in group_result.member_results) != order:
            _add(
                observed,
                IntegrationFindingCode.INCOMPLETE_MEMBER,
                "member results do not exactly follow the canonical group membership",
                members=order,
                classification=IntegrationGateClassification.FAIL,
            )
        if group_result.assignment_ids != tuple(
            member_by_story[item].assignment_id for item in order if item in member_by_story
        ) or group_result.result_commits != tuple(
            member_by_story[item].result_commit for item in order if item in member_by_story
        ):
            _add(
                observed,
                IntegrationFindingCode.ASSIGNMENT_MISMATCH,
                "group assignment or result commit summary is inconsistent",
                members=order,
                classification=IntegrationGateClassification.FAIL,
            )

        stories = _stories_by_id(coordination.project_state, observed)
        registry = self._load_registry(observed)
        assignments = (
            {item.assignment_id: item for item in registry.assignments}
            if registry is not None
            else {}
        )
        self._reconcile_registry(coordination.workflow_generation, observed)

        paths_by_member: dict[str, tuple[str, ...]] = {}
        valid_commits: dict[str, str] = {}
        for story_id in order:
            member = member_by_story.get(story_id)
            story = stories.get(story_id)
            if member is None or story is None:
                _add(
                    observed,
                    IntegrationFindingCode.INCOMPLETE_MEMBER,
                    "required member or User Story is absent",
                    members=(story_id,),
                    classification=IntegrationGateClassification.UNKNOWN,
                )
                continue
            assignment = assignments.get(member.assignment_id)
            if assignment is None:
                _add(
                    observed,
                    IntegrationFindingCode.INCOMPLETE_MEMBER,
                    "assignment is absent from the authoritative registry",
                    members=(story_id,),
                    classification=IntegrationGateClassification.UNKNOWN,
                )
                continue
            self._validate_member_contract(
                coordination,
                story,
                member,
                assignment,
                observed,
            )
            physical_ok = self._validate_physical_assignment(
                assignment, coordination.workflow_generation, observed
            )
            changed = self._observe_changed_files(assignment, story_id, observed)
            if changed is not None:
                paths_by_member[story_id] = changed
                self._validate_declared_and_scope(story, member, changed, observed)
            if physical_ok and assignment.result_commit is not None:
                valid_commits[story_id] = assignment.result_commit
            member_commits.append(
                IntegrationMemberCommit(
                    user_story_id=story_id,
                    assignment_id=assignment.assignment_id,
                    result_commit=assignment.result_commit or member.result_commit,
                    changed_files=changed or (),
                )
            )

        self._detect_path_collisions(order, paths_by_member, observed)
        self._revalidate_safe_pairs(coordination, order, observed)
        self._merge_preflight(plan.baseline_commit, order, valid_commits, observed)

        primary_after = self._observe_primary(observed)
        if primary_before is not None and primary_after is not None and primary_after != primary_before:
            _add(
                observed,
                IntegrationFindingCode.GIT_STATE_UNKNOWN,
                "primary repository changed during IntegrationGate evaluation",
                classification=IntegrationGateClassification.UNKNOWN,
            )

        findings = tuple(
            item.finding
            for item in sorted(
                observed,
                key=lambda item: (
                    item.finding.code.value,
                    item.finding.members,
                    item.finding.paths,
                    item.finding.summary,
                ),
            )
        )
        result_classification = _classification(findings)
        result = IntegrationGateResult(
            mission_id=coordination.mission_id,
            workflow_generation=coordination.workflow_generation,
            wave_index=plan.wave_index,
            group_index=group_result.group_index,
            baseline_commit=plan.baseline_commit,
            member_commits=tuple(member_commits),
            result=result_classification,
            findings=findings,
            integration_order=order,
        )
        validation = self._contract_validator.validate(
            "integration-gate-result", to_dict(result)
        )
        if not validation.is_valid:
            raise IntegrationGateError("INVALID_RESULT", "gate result violates its schema")
        return result

    def _observe_primary(
        self, observed: list[_ObservedFinding]
    ) -> GitPrimaryState | None:
        try:
            return self._manager.inspect_primary()
        except Exception as error:
            _add_unknown(observed, "primary Git state is unavailable", error)
            return None

    def _revalidate_plan(
        self,
        coordination: ParallelCoordinationInput,
        observed: list[_ObservedFinding],
    ) -> ParallelExecutionPlan | None:
        try:
            canonical_conflicts = self._conflict_analyzer.analyze(
                coordination.wave_plan, coordination.project_state
            )
        except Exception as error:
            _add_unknown(observed, "canonical conflict analysis is unavailable", error)
            return None
        if coordination.conflict_analysis != canonical_conflicts:
            _add(
                observed,
                IntegrationFindingCode.CONFLICT_ANALYSIS_MISMATCH,
                "supplied ConflictAnalysis is not canonical",
                classification=IntegrationGateClassification.FAIL,
            )
        canonical_input = ParallelCoordinationInput(
            mission_id=coordination.mission_id,
            workflow_generation=coordination.workflow_generation,
            wave_index=coordination.wave_index,
            wave_plan=coordination.wave_plan,
            conflict_analysis=canonical_conflicts,
            project_state=coordination.project_state,
            mission_state=coordination.mission_state,
            baseline_commit=coordination.baseline_commit,
        )
        try:
            return ParallelImplementerCoordinator(
                worktree_manager=self._manager,
                conflict_analyzer=self._conflict_analyzer,
                contract_validator=self._contract_validator,
            ).plan(canonical_input)
        except Exception as error:
            _add_unknown(observed, "canonical parallel plan is unavailable", error)
            return None

    @staticmethod
    def _validate_mission(
        context: IntegrationGateContext,
        observed: list[_ObservedFinding],
    ) -> None:
        current = context.current_mission_state
        coordination = context.coordination_input
        if current.mission_id != coordination.mission_id:
            _add(
                observed,
                IntegrationFindingCode.ASSIGNMENT_MISMATCH,
                "current mission identity differs from group mission",
                classification=IntegrationGateClassification.FAIL,
            )
        if current.workflow_generation != coordination.workflow_generation:
            _add(
                observed,
                IntegrationFindingCode.GENERATION_MISMATCH,
                "current mission generation differs from group generation",
                classification=IntegrationGateClassification.FAIL,
            )
        if current.observed_commit.casefold() != coordination.baseline_commit:
            _add(
                observed,
                IntegrationFindingCode.BASELINE_MISMATCH,
                "current mission baseline differs from group baseline",
                classification=IntegrationGateClassification.FAIL,
            )

    def _load_registry(
        self, observed: list[_ObservedFinding]
    ) -> WorktreeRegistry | None:
        try:
            registry = self._manager.registry_store.load()  # type: ignore[attr-defined]
        except Exception as error:
            _add_unknown(observed, "worktree registry is unavailable", error)
            return None
        if not isinstance(registry, WorktreeRegistry):
            _add_unknown(observed, "worktree registry has an invalid type")
            return None
        return registry

    def _reconcile_registry(
        self, generation: int, observed: list[_ObservedFinding]
    ) -> None:
        try:
            reconciliation = self._manager.inspect_all(current_generation=generation)
        except Exception as error:
            _add_unknown(observed, "registry/Git reconciliation is unavailable", error)
            return
        anomalies = getattr(reconciliation, "anomalies", None)
        if not isinstance(anomalies, tuple):
            _add_unknown(observed, "registry/Git reconciliation is malformed")
        elif anomalies:
            _add(
                observed,
                IntegrationFindingCode.GIT_STATE_UNKNOWN,
                "registry/Git reconciliation reports anomalies",
                paths=tuple(str(item) for item in anomalies),
                classification=IntegrationGateClassification.UNKNOWN,
            )

    def _validate_member_contract(
        self,
        coordination: ParallelCoordinationInput,
        story: UserStory,
        member: ParallelMemberResult,
        assignment: WorktreeAssignment,
        observed: list[_ObservedFinding],
    ) -> None:
        story_id = story.id
        if (
            assignment.status is not WorktreeStatus.COMPLETED
            or assignment.result_commit is None
            or member.result_commit != assignment.result_commit
            or member.assignment_id != assignment.assignment_id
            or assignment.user_story_id != story_id
            or member.user_story_id != story_id
        ):
            _add(
                observed,
                IntegrationFindingCode.INCOMPLETE_MEMBER,
                "member is not backed by one exact COMPLETED assignment",
                members=(story_id,),
                classification=IntegrationGateClassification.FAIL,
            )
        if assignment.mission_id != coordination.mission_id:
            _add(
                observed,
                IntegrationFindingCode.ASSIGNMENT_MISMATCH,
                "assignment mission differs from integration mission",
                members=(story_id,),
                classification=IntegrationGateClassification.FAIL,
            )
        if assignment.workflow_generation != coordination.workflow_generation:
            _add(
                observed,
                IntegrationFindingCode.GENERATION_MISMATCH,
                "assignment generation differs from integration generation",
                members=(story_id,),
                classification=IntegrationGateClassification.FAIL,
            )
        if assignment.baseline_commit != coordination.baseline_commit:
            _add(
                observed,
                IntegrationFindingCode.BASELINE_MISMATCH,
                "assignment baseline differs from integration baseline",
                members=(story_id,),
                classification=IntegrationGateClassification.FAIL,
            )
        validation = self._result_validator.validate(
            member.implementer_result,
            implementer_input=member.implementer_input,
        )
        if (
            not validation.is_valid
            or member.implementer_result.verdict is not ImplementerVerdict.READY_FOR_TEST
            or member.implementer_input.mission_id != coordination.mission_id
            or member.implementer_input.workflow_generation != coordination.workflow_generation
            or member.implementer_input.user_story.id != story_id
            or member.implementer_input.observed_commit.casefold() != coordination.baseline_commit
            or member.implementer_input.user_story.scope != story.scope
        ):
            _add(
                observed,
                IntegrationFindingCode.INCOMPLETE_MEMBER,
                "Implementer result/input is invalid or not attributable to the User Story",
                members=(story_id,),
                classification=IntegrationGateClassification.FAIL,
            )

    def _validate_physical_assignment(
        self,
        assignment: WorktreeAssignment,
        generation: int,
        observed: list[_ObservedFinding],
    ) -> bool:
        try:
            inspection = self._manager.inspect(
                assignment.assignment_id, current_generation=generation
            )
        except Exception as error:
            _add_unknown(
                observed,
                "completed assignment cannot be inspected",
                error,
                members=(assignment.user_story_id,),
            )
            return False
        expected_reasons = ("STATUS_COMPLETED",)
        if (
            inspection.registry_status is not WorktreeStatus.COMPLETED
            or not inspection.physical_exists
            or not inspection.branch_matches
            or inspection.clean is not True
            or inspection.head_commit != assignment.result_commit
            or inspection.reasons != expected_reasons
        ):
            _add(
                observed,
                IntegrationFindingCode.ASSIGNMENT_MISMATCH,
                "completed assignment differs from branch/worktree reality",
                members=(assignment.user_story_id,),
                classification=IntegrationGateClassification.FAIL,
            )
            return False
        return True

    def _observe_changed_files(
        self,
        assignment: WorktreeAssignment,
        story_id: str,
        observed: list[_ObservedFinding],
    ) -> tuple[str, ...] | None:
        if assignment.result_commit is None:
            return None
        try:
            entries = self._manager.diff_name_status(
                assignment.baseline_commit, assignment.result_commit
            )
            paths = tuple(
                sorted(
                    {
                        _normalize_path(path, allow_directory=False)
                        for entry in entries
                        for path in entry.paths
                    }
                )
            )
        except Exception as error:
            _add_unknown(
                observed,
                "Git changed-file observation is unavailable",
                error,
                members=(story_id,),
            )
            return None
        return paths

    @staticmethod
    def _validate_declared_and_scope(
        story: UserStory,
        member: ParallelMemberResult,
        changed: tuple[str, ...],
        observed: list[_ObservedFinding],
    ) -> None:
        try:
            declared = tuple(
                sorted(
                    _normalize_path(path, allow_directory=False)
                    for path in member.implementer_result.files_changed
                )
            )
        except ImplementerInputError as error:
            _add_unknown(
                observed,
                "declared changed files cannot be normalized",
                error,
                members=(story.id,),
            )
            return
        if declared != changed:
            _add(
                observed,
                IntegrationFindingCode.DECLARED_DIFF_MISMATCH,
                "ImplementerResult files_changed differs from Git reality",
                members=(story.id,),
                paths=tuple(sorted(set(declared) ^ set(changed))),
                classification=IntegrationGateClassification.FAIL,
            )
        try:
            violating = tuple(
                path
                for path in changed
                if any(
                    _scope_matches(path, denied)
                    for denied in story.scope.forbidden_paths
                )
                or not any(
                    _scope_matches(path, allowed)
                    for allowed in story.scope.allowed_paths
                )
            )
        except ImplementerInputError as error:
            _add_unknown(
                observed,
                "authoritative User Story scope cannot be normalized",
                error,
                members=(story.id,),
            )
            return
        if violating:
            _add(
                observed,
                IntegrationFindingCode.SCOPE_VIOLATION,
                "Git changed files exceed the authoritative User Story scope",
                members=(story.id,),
                paths=violating,
                classification=IntegrationGateClassification.FAIL,
            )

    @staticmethod
    def _detect_path_collisions(
        order: tuple[str, ...],
        paths_by_member: dict[str, tuple[str, ...]],
        observed: list[_ObservedFinding],
    ) -> None:
        for left, right in combinations(order, 2):
            collision = tuple(
                sorted(set(paths_by_member.get(left, ())) & set(paths_by_member.get(right, ())))
            )
            if collision:
                _add(
                    observed,
                    IntegrationFindingCode.CROSS_BRANCH_PATH_COLLISION,
                    "parallel branches modify the same repository path",
                    members=(left, right),
                    paths=collision,
                    classification=IntegrationGateClassification.FAIL,
                )

    def _revalidate_safe_pairs(
        self,
        coordination: ParallelCoordinationInput,
        order: tuple[str, ...],
        observed: list[_ObservedFinding],
    ) -> None:
        try:
            canonical = self._conflict_analyzer.analyze(
                coordination.wave_plan, coordination.project_state
            )
        except Exception:
            return
        pair_map = {
            frozenset((item.left_user_story_id, item.right_user_story_id)): item.classification
            for item in canonical.pairs
        }
        for left, right in combinations(order, 2):
            if pair_map.get(frozenset((left, right))) is not ConflictClassification.SAFE:
                _add(
                    observed,
                    IntegrationFindingCode.CONFLICT_ANALYSIS_MISMATCH,
                    "group contains a pair that is not canonically SAFE",
                    members=(left, right),
                    classification=IntegrationGateClassification.FAIL,
                )

    def _merge_preflight(
        self,
        baseline: str,
        order: tuple[str, ...],
        commits: dict[str, str],
        observed: list[_ObservedFinding],
    ) -> None:
        for left, right in combinations(order, 2):
            if left not in commits or right not in commits:
                continue
            try:
                result = self._manager.merge_preflight(
                    baseline, commits[left], commits[right]
                )
            except Exception as error:
                _add_unknown(
                    observed,
                    "Git merge-tree preflight is unavailable",
                    error,
                    members=(left, right),
                )
                continue
            if not result.mergeable:
                _add(
                    observed,
                    IntegrationFindingCode.GIT_MERGE_CONFLICT,
                    "Git merge-tree reports a merge conflict",
                    members=(left, right),
                    classification=IntegrationGateClassification.FAIL,
                )


def _unique_members(
    members: tuple[ParallelMemberResult, ...],
    observed: list[_ObservedFinding],
) -> dict[str, ParallelMemberResult]:
    result: dict[str, ParallelMemberResult] = {}
    for member in members:
        if not isinstance(member, ParallelMemberResult) or member.user_story_id in result:
            identifier = getattr(member, "user_story_id", "UNKNOWN")
            _add(
                observed,
                IntegrationFindingCode.INCOMPLETE_MEMBER,
                "member results are duplicated or malformed",
                members=(str(identifier),),
                classification=IntegrationGateClassification.FAIL,
            )
            continue
        result[member.user_story_id] = member
    return result


def _stories_by_id(
    state: ProjectState,
    observed: list[_ObservedFinding],
) -> dict[str, UserStory]:
    if not isinstance(state, ProjectState):
        _add_unknown(observed, "ProjectState is unavailable")
        return {}
    result: dict[str, UserStory] = {}
    for story in state.user_stories:
        if not isinstance(story, UserStory) or story.id in result:
            _add_unknown(observed, "ProjectState User Stories are malformed or duplicated")
            continue
        result[story.id] = story
    return result


def _add(
    observed: list[_ObservedFinding],
    code: IntegrationFindingCode,
    summary: str,
    *,
    members: tuple[str, ...] = (),
    paths: tuple[str, ...] = (),
    classification: IntegrationGateClassification,
) -> None:
    observed.append(
        _ObservedFinding(
            IntegrationFinding(
                code=code,
                summary=summary,
                members=tuple(sorted(members)),
                paths=tuple(sorted(paths)),
                blocking=True,
                classification=classification,
            )
        )
    )


def _add_unknown(
    observed: list[_ObservedFinding],
    summary: str,
    error: Exception | None = None,
    *,
    members: tuple[str, ...] = (),
) -> None:
    detail = f" ({getattr(error, 'code', type(error).__name__)})" if error is not None else ""
    _add(
        observed,
        IntegrationFindingCode.GIT_STATE_UNKNOWN,
        f"{summary}{detail}",
        members=members,
        classification=IntegrationGateClassification.UNKNOWN,
    )


def _classification(findings: tuple[IntegrationFinding, ...]) -> IntegrationGateClassification:
    if any(item.classification is IntegrationGateClassification.FAIL for item in findings):
        return IntegrationGateClassification.FAIL
    if findings:
        return IntegrationGateClassification.UNKNOWN
    return IntegrationGateClassification.PASS
