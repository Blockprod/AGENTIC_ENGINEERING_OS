"""Deterministic coordination of the certified Phase 1 services."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

from agentic_engineering_os.domain import (
    Certification,
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
from .evidence_recorder import EvidenceObservation, EvidenceRecorder
from .gate_evaluator import (
    GateContract,
    GateEvaluation,
    GateEvaluationContext,
    GateEvaluator,
)
from .state_transition_service import (
    StateTransitionService,
    TransitionContext,
    TransitionResult,
)


class ProjectStateStorePort(Protocol):
    """The single persistence boundary required by the application layer."""

    def load(self) -> ProjectState: ...

    def save(self, state: ProjectState) -> Path: ...


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
    ) -> None:
        self._state_store = state_store
        self._evidence_recorder_factory = evidence_recorder_factory
        self._gate_evaluator = gate_evaluator
        self._certification_service = certification_service
        self._transition_service = transition_service

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

        candidate = _candidate_state(self.load_state())
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
        self._state_store.save(candidate)
        return evidence

    def evaluate_gate(
        self,
        contract: GateContract,
        *,
        context: GateEvaluationContext | None = None,
        evaluated_at: datetime | None = None,
    ) -> GateEvaluation:
        """Evaluate a Gate from persisted Evidence and persist the valid result."""

        candidate = _candidate_state(self.load_state())
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
        self._state_store.save(candidate)
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

        candidate = _candidate_state(self.load_state())
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
        self._state_store.save(candidate)
        return certification

    def transition_user_story(
        self,
        user_story_id: str,
        target: UserStoryStatus | str,
        *,
        context: TransitionContext | None = None,
    ) -> TransitionResult:
        """Apply one service-authorized transition to a copied User Story."""

        candidate = _candidate_state(self.load_state())
        story_index, current_story = _require_unique_story(candidate, user_story_id)
        candidate_story = _candidate_story(current_story)
        candidate.user_stories[story_index] = candidate_story
        result = self._transition_service.apply(
            candidate_story,
            target,
            context=context,
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
        self._state_store.save(candidate)
        return result


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
    )


def _candidate_story(story: UserStory) -> UserStory:
    return replace(
        story,
        human_approval=replace(story.human_approval),
        metadata=replace(story.metadata),
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
