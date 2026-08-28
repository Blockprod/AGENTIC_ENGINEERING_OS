"""Deterministic, read-only readiness diagnostics for validated DAGs."""

from __future__ import annotations

from datetime import datetime

from agentic_engineering_os.domain import (
    DAGSnapshot,
    HumanApproval,
    NodeReadiness,
    ProjectState,
    ReadinessClassification,
    ReadinessSnapshot,
    UserStory,
    UserStoryStatus,
    to_dict,
)

from ._identity import is_attributable_human_identity
from .contract_validator import ContractValidator
from .dag_validator import DAGValidationError, DAGValidator


class ReadinessEvaluationError(RuntimeError):
    """Readiness cannot be determined reliably from the supplied state."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        subjects: tuple[str, ...] = (),
    ) -> None:
        self.code = code
        self.message = message
        self.subjects = subjects
        super().__init__(f"{code}: {message}")


_CANDIDATE_STATES = frozenset(
    {UserStoryStatus.PLANNED, UserStoryStatus.READY}
)
_BLOCKED_STATES = frozenset(
    {
        UserStoryStatus.PROPOSED,
        UserStoryStatus.BLOCKED,
        UserStoryStatus.REJECTED,
        UserStoryStatus.REMEDIATION_REQUIRED,
    }
)
_TERMINAL_STATES = frozenset(
    {UserStoryStatus.CERTIFIED, UserStoryStatus.CANCELLED}
)


class ReadinessEngine:
    """Classify every DAG node without mutating or transitioning project state."""

    def __init__(
        self,
        *,
        dag_validator: DAGValidator | None = None,
        contract_validator: ContractValidator | None = None,
    ) -> None:
        self._dag_validator = dag_validator or DAGValidator()
        self._contract_validator = contract_validator or ContractValidator()

    def evaluate(
        self,
        dag_snapshot: DAGSnapshot,
        project_state: ProjectState,
    ) -> ReadinessSnapshot:
        """Return canonical readiness diagnoses for a matching DAG and state."""

        self._assert_consistent(dag_snapshot, project_state)
        stories = {story.id: story for story in project_state.user_stories}
        statuses = {story.id: story.status for story in project_state.user_stories}
        results = tuple(
            self._classify(stories[node.user_story_id], statuses)
            for node in dag_snapshot.nodes
        )
        snapshot = ReadinessSnapshot(nodes=results)
        try:
            validation = self._contract_validator.validate(
                "readiness-snapshot", to_dict(snapshot)
            )
        except Exception as error:
            raise ReadinessEvaluationError(
                "AMBIGUOUS_READINESS",
                "readiness validation could not complete: "
                f"{type(error).__name__}: {error}",
            ) from error
        if not validation.is_valid:
            raise ReadinessEvaluationError(
                "AMBIGUOUS_READINESS",
                "readiness result violates its structural contract",
            )
        return snapshot

    def _assert_consistent(
        self,
        dag_snapshot: DAGSnapshot,
        project_state: ProjectState,
    ) -> None:
        if not isinstance(dag_snapshot, DAGSnapshot):
            raise ReadinessEvaluationError(
                "INVALID_DAG", "an explicit DAGSnapshot is required"
            )
        if not isinstance(project_state, ProjectState):
            raise ReadinessEvaluationError(
                "DAG_STATE_MISMATCH", "an explicit ProjectState is required"
            )
        try:
            structural = self._contract_validator.validate(
                "dag-snapshot", to_dict(dag_snapshot)
            )
        except Exception as error:
            raise ReadinessEvaluationError(
                "INVALID_DAG",
                f"DAG validation could not complete: {type(error).__name__}: {error}",
            ) from error
        if not structural.is_valid:
            raise ReadinessEvaluationError(
                "INVALID_DAG", "DAGSnapshot violates its structural contract"
            )
        try:
            expected = self._dag_validator.build(project_state)
        except DAGValidationError as error:
            raise ReadinessEvaluationError(
                "INVALID_DAG",
                f"ProjectState cannot produce a valid DAG: {error.code}",
                subjects=error.subjects,
            ) from error
        except Exception as error:
            raise ReadinessEvaluationError(
                "INVALID_DAG",
                "canonical DAG reconstruction could not complete: "
                f"{type(error).__name__}: {error}",
            ) from error

        expected_ids = {node.user_story_id for node in expected.nodes}
        actual_ids = {node.user_story_id for node in dag_snapshot.nodes}
        absent_from_state = tuple(sorted(actual_ids - expected_ids))
        if absent_from_state:
            raise ReadinessEvaluationError(
                "MISSING_USER_STORY",
                "DAG nodes are absent from ProjectState: "
                f"{', '.join(absent_from_state)}",
                subjects=absent_from_state,
            )
        if dag_snapshot != expected:
            divergent = tuple(sorted(actual_ids ^ expected_ids))
            raise ReadinessEvaluationError(
                "DAG_STATE_MISMATCH",
                "DAGSnapshot is not the canonical projection of ProjectState",
                subjects=divergent,
            )

    def _classify(
        self,
        story: UserStory,
        statuses: dict[str, UserStoryStatus],
    ) -> NodeReadiness:
        satisfied = tuple(
            dependency
            for dependency in sorted(story.depends_on)
            if statuses[dependency] is UserStoryStatus.CERTIFIED
        )
        unsatisfied = tuple(
            dependency
            for dependency in sorted(story.depends_on)
            if statuses[dependency] is not UserStoryStatus.CERTIFIED
        )

        if story.status in _TERMINAL_STATES:
            classification = ReadinessClassification.TERMINAL
            reason = f"TERMINAL_STATUS:{story.status.value}"
        elif story.status in _BLOCKED_STATES:
            classification = ReadinessClassification.BLOCKED
            reason = f"OWN_STATUS_BLOCKS_READINESS:{story.status.value}"
        elif not _approval_is_applied(story.human_approval):
            classification = ReadinessClassification.BLOCKED
            reason = "REQUIRED_HUMAN_APPROVAL_NOT_APPLIED"
        elif story.status not in _CANDIDATE_STATES:
            classification = ReadinessClassification.INELIGIBLE
            reason = f"STATUS_NOT_READINESS_CANDIDATE:{story.status.value}"
        elif unsatisfied:
            classification = ReadinessClassification.WAITING_DEPENDENCIES
            reason = "DEPENDENCIES_NOT_CERTIFIED"
        else:
            classification = ReadinessClassification.READY
            reason = "LOGICALLY_ELIGIBLE"

        return NodeReadiness(
            user_story_id=story.id,
            classification=classification,
            satisfied_dependencies=satisfied,
            unsatisfied_dependencies=unsatisfied,
            reason=reason,
        )


def _approval_is_applied(approval: HumanApproval) -> bool:
    if not approval.required:
        return True
    return (
        approval.approved is True
        and is_attributable_human_identity(approval.approved_by)
        and isinstance(approval.approved_at, datetime)
        and isinstance(approval.evidence_ref, str)
        and bool(approval.evidence_ref.strip())
    )
