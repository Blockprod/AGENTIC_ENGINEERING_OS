"""Deterministic coordination of the certified Phase 1 services."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

from agentic_engineering_os._authoritative_write import _issue_authoritative_write
from agentic_engineering_os.domain import (
    Certification,
    CertificationResult,
    Evidence,
    Gate,
    ProjectState,
    UserStory,
    UserStoryStatus,
)

from .certification_service import (
    AcceptanceResult,
    CertificationContext,
    CertificationService,
)
from .certification_integrity import certified_dossier_issues
from .evidence_recorder import EvidenceObservation, EvidenceRecorder
from .gate_evaluator import (
    GateContract,
    GateEvaluation,
    GateEvaluationContext,
    GateEvaluator,
)
from .human_approval_service import HumanApprovalResult, HumanApprovalService
from .state_transition_service import (
    StateTransitionService,
    TransitionContext,
    TransitionResult,
    _issue_certified_transition_authorization,
)


class ProjectStateStorePort(Protocol):
    """The single persistence boundary required by the application layer."""

    def load(self) -> ProjectState: ...

    def save(
        self,
        state: ProjectState,
        *,
        authorization: object | None = None,
        operation: str | None = None,
    ) -> Path: ...


EvidenceRecorderFactory = Callable[[list[Evidence]], EvidenceRecorder]


class ControlLoopError(RuntimeError):
    """An integrated operation was refused before persistence succeeded."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class ControlLoop:
    """Coordinate specialized services over one authoritative ProjectState."""

    def __init__(
        self,
        *,
        state_store: ProjectStateStorePort,
        evidence_recorder_factory: EvidenceRecorderFactory,
        gate_evaluator: GateEvaluator,
        certification_service: CertificationService,
        transition_service: StateTransitionService,
        human_approval_service: HumanApprovalService | None = None,
    ) -> None:
        self._state_store = state_store
        self._evidence_recorder_factory = evidence_recorder_factory
        self._gate_evaluator = gate_evaluator
        self._certification_service = certification_service
        self._transition_service = transition_service
        self._human_approval_service = human_approval_service or HumanApprovalService()

    def apply_human_approval(
        self,
        user_story_id: str,
        evidence_id: str,
        *,
        expected_commit: str,
    ) -> HumanApprovalResult:
        """Apply one already-persisted Human Evidence to a copied User Story."""
        current_state = self.load_state()
        candidate = _candidate_state(current_state)
        index, current = _require_unique_story(candidate, user_story_id)
        matches = [
            item for item in candidate.evidence if item.evidence_id == evidence_id
        ]
        if len(matches) != 1:
            raise ControlLoopError(
                "HUMAN_EVIDENCE_NOT_FOUND",
                "persisted Human Evidence is missing or ambiguous",
            )
        story = _candidate_story(current)
        candidate.user_stories[index] = story
        result = self._human_approval_service.apply(
            story,
            matches[0],
            expected_commit=expected_commit,
        )
        self._save_candidate(
            current_state,
            candidate,
            operation="APPLY_HUMAN_APPROVAL",
        )
        return result

    def load_state(self) -> ProjectState:
        """Load the authoritative state without manufacturing a fallback."""

        return self._state_store.load()

    def record_evidence(
        self,
        observation: EvidenceObservation,
        *,
        evidence_id: str | None = None,
        timestamp: datetime | None = None,
    ) -> Evidence:
        """Record validated Evidence in a candidate state, then persist it."""

        current_state = self.load_state()
        candidate = _candidate_state(current_state)
        recorder = self._evidence_recorder_factory(candidate.evidence)
        evidence = recorder.record(
            observation,
            evidence_id=evidence_id,
            timestamp=timestamp,
        )
        if not isinstance(evidence, Evidence):
            raise ControlLoopError(
                "INVALID_SERVICE_RESULT", "EvidenceRecorder returned no Evidence"
            )
        matches = [
            item
            for item in candidate.evidence
            if item.evidence_id == evidence.evidence_id
        ]
        if len(matches) != 1 or matches[0] != evidence:
            raise ControlLoopError(
                "INVALID_SERVICE_RESULT",
                "EvidenceRecorder did not append exactly its validated Evidence",
            )
        self._save_candidate(current_state, candidate, operation="RECORD_EVIDENCE")
        return evidence

    def evaluate_gate(
        self,
        contract: GateContract,
        *,
        context: GateEvaluationContext | None = None,
        evaluated_at: datetime | None = None,
    ) -> GateEvaluation:
        """Evaluate a Gate from persisted Evidence and persist the valid result."""

        current_state = self.load_state()
        candidate = _candidate_state(current_state)
        evaluation = self._gate_evaluator.evaluate(
            contract,
            candidate.evidence,
            context=context,
            evaluated_at=evaluated_at,
        )
        if not isinstance(evaluation, GateEvaluation) or not isinstance(
            evaluation.gate, Gate
        ):
            raise ControlLoopError(
                "INVALID_SERVICE_RESULT", "GateEvaluator returned no valid Gate"
            )
        if evaluation.gate.gate_id != contract.gate_id:
            raise ControlLoopError(
                "INVALID_SERVICE_RESULT", "GateEvaluator changed the targeted Gate id"
            )
        _require_new_id(candidate.gates, "gate_id", evaluation.gate.gate_id, "Gate")
        candidate.gates.append(evaluation.gate)
        self._save_candidate(current_state, candidate, operation="EVALUATE_GATE")
        return evaluation

    def certify_user_story(
        self,
        user_story_id: str,
        commit: str,
        acceptance_results: Iterable[AcceptanceResult],
        *,
        certifier: str,
        context: CertificationContext | None = None,
        certification_id: str | None = None,
        certified_at: datetime | None = None,
    ) -> Certification:
        """Produce and persist a verdict without changing UserStory.status."""

        current_state = self.load_state()
        candidate = _candidate_state(current_state)
        _, story = _require_unique_story(candidate, user_story_id)
        certification = self._certification_service.certify(
            story,
            commit,
            acceptance_results,
            candidate.gates,
            candidate.evidence,
            certifier=certifier,
            context=context,
            certification_id=certification_id,
            certified_at=certified_at,
        )
        if not isinstance(certification, Certification):
            raise ControlLoopError(
                "INVALID_SERVICE_RESULT",
                "CertificationService returned no Certification",
            )
        if certification.subject != story.id:
            raise ControlLoopError(
                "INVALID_SERVICE_RESULT",
                "CertificationService changed the targeted User Story",
            )
        _require_new_id(
            candidate.certifications,
            "certification_id",
            certification.certification_id,
            "Certification",
        )
        candidate.certifications.append(certification)
        self._save_candidate(
            current_state,
            candidate,
            operation="PERSIST_CERTIFICATION",
        )
        return certification

    def add_user_story(self, user_story: UserStory) -> UserStory:
        """Persist one validated initial User Story through Project authority."""

        if not isinstance(user_story, UserStory):
            raise ControlLoopError(
                "INVALID_USER_STORY", "add_user_story requires a UserStory"
            )
        if user_story.status is not UserStoryStatus.PROPOSED:
            raise ControlLoopError(
                "INVALID_INITIAL_USER_STORY_STATUS",
                "new User Stories must enter ProjectState as PROPOSED",
            )
        current_state = self.load_state()
        candidate = _candidate_state(current_state)
        _require_new_id(candidate.user_stories, "id", user_story.id, "User Story")
        candidate.user_stories.append(_candidate_story(user_story))
        self._save_candidate(current_state, candidate, operation="ADD_USER_STORY")
        return user_story

    def transition_user_story(
        self,
        user_story_id: str,
        target: UserStoryStatus | str,
        *,
        context: TransitionContext | None = None,
    ) -> TransitionResult:
        """Apply one service-authorized transition to a copied User Story."""

        current_state = self.load_state()
        candidate = _candidate_state(current_state)
        story_index, current_story = _require_unique_story(candidate, user_story_id)
        candidate_story = _candidate_story(current_story)
        candidate.user_stories[story_index] = candidate_story
        resolved_context, authorization = _resolve_transition_context(
            candidate,
            candidate_story,
            target,
            context,
        )
        if authorization is None:
            result = self._transition_service.apply(
                candidate_story,
                target,
                context=resolved_context,
            )
        else:
            if resolved_context is None:
                raise ControlLoopError(
                    "INVALID_AUTHORIZATION",
                    "trusted transition authorization requires resolved context",
                )
            result = self._transition_service._apply_authorized(
                candidate_story,
                target,
                context=resolved_context,
                authorization=authorization,
            )
        if not isinstance(result, TransitionResult):
            raise ControlLoopError(
                "INVALID_SERVICE_RESULT",
                "StateTransitionService returned no TransitionResult",
            )
        if not result.allowed:
            codes = ", ".join(refusal.code for refusal in result.refusals)
            raise ControlLoopError(
                "TRANSITION_REFUSED", f"transition was refused: {codes}"
            )
        if candidate_story.status.value != result.target:
            raise ControlLoopError(
                "INVALID_SERVICE_RESULT",
                "StateTransitionService did not apply its allowed transition",
            )
        self._save_candidate(
            current_state,
            candidate,
            operation="TRANSITION_USER_STORY",
        )
        return result

    def _save_candidate(
        self,
        current_state: ProjectState,
        candidate: ProjectState,
        *,
        operation: str,
    ) -> None:
        authorization = _issue_authoritative_write(
            store_kind="PROJECT_STATE",
            store=self._state_store,
            before_state=current_state,
            candidate_state=candidate,
            operation=operation,
        )
        self._state_store.save(
            candidate,
            authorization=authorization,
            operation=operation,
        )


def _candidate_state(current: ProjectState) -> ProjectState:
    if not isinstance(current, ProjectState):
        raise ControlLoopError(
            "INVALID_STATE", "ProjectStateStore returned no ProjectState"
        )
    return ProjectState(
        schema_version=current.schema_version,
        user_stories=list(current.user_stories),
        evidence=list(current.evidence),
        gates=list(current.gates),
        certifications=list(current.certifications),
        audit_events=list(current.audit_events),
        project_id=current.project_id,
    )


def _candidate_story(story: UserStory) -> UserStory:
    return replace(
        story,
        human_approval=replace(story.human_approval),
        metadata=replace(story.metadata),
    )


def _resolve_transition_context(
    state: ProjectState,
    story: UserStory,
    target: UserStoryStatus | str,
    context: TransitionContext | None,
) -> tuple[TransitionContext | None, object | None]:
    try:
        target_state = UserStoryStatus(target)
    except (TypeError, ValueError):
        return context, None
    if not (
        story.status is UserStoryStatus.CERTIFICATION
        and target_state is UserStoryStatus.CERTIFIED
    ):
        return context, None

    if (
        context is None
        or not isinstance(context.target_commit, str)
        or not context.target_commit.strip()
    ):
        raise ControlLoopError(
            "CERTIFICATION_COMMIT_REQUIRED",
            "promotion to CERTIFIED requires an explicit target commit",
        )

    matches = [
        certification
        for certification in state.certifications
        if isinstance(certification, Certification)
        and certification.subject == story.id
        and certification.commit == context.target_commit
    ]
    if not matches:
        raise ControlLoopError(
            "CERTIFICATION_NOT_FOUND",
            "no applicable Certification exists for the User Story and commit",
        )
    if len(matches) != 1:
        raise ControlLoopError(
            "AMBIGUOUS_CERTIFICATION",
            "multiple Certifications apply to the User Story and commit",
        )
    if matches[0].result is not CertificationResult.CERTIFIED:
        raise ControlLoopError(
            "CERTIFICATION_NOT_CERTIFIED",
            "the applicable Certification does not authorize promotion",
        )
    certification = matches[0]
    integrity_issues = certified_dossier_issues(
        story,
        certification,
        state.gates,
        state.evidence,
    )
    if integrity_issues:
        details = "; ".join(issue.code for issue in integrity_issues)
        raise ControlLoopError(
            "CERTIFICATION_INTEGRITY_INVALID",
            f"applicable Certification dossier is invalid: {details}",
        )
    return (
        replace(context, preconditions_proven=True),
        _issue_certified_transition_authorization(
            subject=story.id,
            target_commit=context.target_commit,
            certification_id=certification.certification_id,
        ),
    )


def _require_unique_story(
    state: ProjectState, user_story_id: str
) -> tuple[int, UserStory]:
    matches = [
        (index, story)
        for index, story in enumerate(state.user_stories)
        if story.id == user_story_id
    ]
    if not matches:
        raise ControlLoopError(
            "USER_STORY_NOT_FOUND", f"User Story is absent: {user_story_id}"
        )
    if len(matches) != 1:
        raise ControlLoopError(
            "AMBIGUOUS_USER_STORY", f"User Story id is duplicated: {user_story_id}"
        )
    return matches[0]


def _require_new_id(
    items: Iterable[object], field: str, identifier: str, label: str
) -> None:
    matches = [item for item in items if getattr(item, field, None) == identifier]
    if matches:
        raise ControlLoopError(
            f"DUPLICATE_{label.upper()}_ID".replace(" ", "_"),
            f"{label} id already exists: {identifier}",
        )
