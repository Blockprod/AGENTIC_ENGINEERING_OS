"""Deterministic routing for the Phase 2 operating workflow."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Protocol

from agentic_engineering_os.domain import (
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    ProjectState,
)


_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
_HUMAN_REQUIRED_MARKER = "HUMAN_REQUIRED"

_ROUTING_POLICY: tuple[tuple[OperatingStep, MissionRole], ...] = (
    (OperatingStep.RECONSTRUCT, MissionRole.ORCHESTRATOR),
    (OperatingStep.PREFLIGHT, MissionRole.ORCHESTRATOR),
    (OperatingStep.UNDERSTAND_CONTRACT, MissionRole.ARCHITECT),
    (OperatingStep.PROVE_READINESS, MissionRole.IMPLEMENTER),
    (OperatingStep.ACT, MissionRole.IMPLEMENTER),
    (OperatingStep.VERIFY, MissionRole.TESTER),
    (OperatingStep.RECORD_EVIDENCE, MissionRole.TESTER),
    (OperatingStep.CONTROLLED_TRANSITION, MissionRole.CERTIFIER),
    (OperatingStep.REPORT, MissionRole.REVIEWER),
)

_STEP_INSTRUCTIONS: dict[OperatingStep, str] = {
    OperatingStep.RECONSTRUCT: "Reconstruct the actual repository state.",
    OperatingStep.PREFLIGHT: "Verify repository and mission preconditions.",
    OperatingStep.UNDERSTAND_CONTRACT: "Specify the applicable contract.",
    OperatingStep.PROVE_READINESS: "Prove readiness before any implementation.",
    OperatingStep.ACT: "Implement only the authorized mission scope.",
    OperatingStep.VERIFY: "Run the required deterministic verification.",
    OperatingStep.RECORD_EVIDENCE: "Record only observed, reproducible evidence.",
    OperatingStep.CONTROLLED_TRANSITION: (
        "Verify final proof and request transition through the Control Plane."
    ),
    OperatingStep.REPORT: "Review quality and report factual findings.",
}


class MissionStateStorePort(Protocol):
    def load(self) -> MissionState: ...

    def save(self, state: MissionState) -> Path: ...


class ProjectStateReaderPort(Protocol):
    def load(self) -> ProjectState: ...


@dataclass(frozen=True, slots=True)
class RoleHandoff:
    """Context transfer without any Control Plane authority."""

    from_role: MissionRole
    to_role: MissionRole
    mission_id: str
    subject: str
    objective: str
    observed_commit: str
    operating_step: OperatingStep
    blockers: tuple[str, ...]
    instructions: str


@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    """One deterministic routing outcome; this is not a generic Result type."""

    success: bool
    current_role: MissionRole | None
    next_role: MissionRole | None
    handoff: RoleHandoff | None
    blockers: tuple[str, ...]
    reason: str
    updated_mission_state: MissionState | None


class OrchestratorConfigurationError(RuntimeError):
    """The explicit repository context is unusable."""


class Orchestrator:
    """Route one mission step without executing roles or Control Plane actions."""

    def __init__(
        self,
        *,
        repository_root: Path | str,
        mission_store: MissionStateStorePort,
        project_state_store: ProjectStateReaderPort,
    ) -> None:
        root = Path(repository_root)
        try:
            resolved_root = root.resolve(strict=True)
        except OSError as error:
            raise OrchestratorConfigurationError(
                f"repository root cannot be resolved: {root}"
            ) from error
        if not resolved_root.is_dir():
            raise OrchestratorConfigurationError(
                f"repository root is not a directory: {resolved_root}"
            )
        self._repository_root = resolved_root
        self._mission_store = mission_store
        self._project_state_store = project_state_store

    @property
    def repository_root(self) -> Path:
        return self._repository_root

    def orchestrate(
        self,
        *,
        current_commit: str,
        updated_at: datetime,
    ) -> OrchestrationResult:
        """Load authoritative inputs, route once, then persist a candidate mission."""

        input_error = _input_error(current_commit, updated_at)
        if input_error is not None:
            return _failure(reason=input_error)

        try:
            mission = self._mission_store.load()
        except Exception as error:
            return _failure(reason=_unavailable_reason("MISSION_STATE", error))
        mission_error = _mission_error(mission)
        if mission_error is not None:
            return _failure(reason=mission_error)

        try:
            project_state = self._project_state_store.load()
        except Exception as error:
            return _failure(
                reason=_unavailable_reason("PROJECT_STATE", error),
                mission=mission,
            )
        if not isinstance(project_state, ProjectState):
            return _failure(reason="INVALID_PROJECT_STATE", mission=mission)

        subject_context, subject_error, project_requires_human = (
            _project_subject_context(project_state, mission.subject)
        )
        if subject_error is not None:
            return _failure(reason=subject_error, mission=mission)

        blockers = tuple(mission.blockers)
        if project_requires_human or any(
            _requires_human(blocker) for blocker in blockers
        ):
            return _failure(reason="HUMAN_REQUIRED", mission=mission)
        if mission.status is MissionStatus.BLOCKED:
            return _failure(reason="MISSION_BLOCKED", mission=mission)
        if mission.status is not MissionStatus.ACTIVE:
            return _failure(
                reason=f"MISSION_{mission.status.value}", mission=mission
            )
        target_step = mission.operating_step
        route_reason = "ROUTED"
        if current_commit.casefold() != mission.observed_commit.casefold():
            target_step = OperatingStep.RECONSTRUCT
            route_reason = "RECONSTRUCT_REQUIRED"

        next_role, routing_error = _route(target_step)
        if routing_error is not None or next_role is None:
            return _failure(
                reason=routing_error or "ROUTING_UNAVAILABLE", mission=mission
            )

        instructions = (
            f"{_STEP_INSTRUCTIONS[target_step]} Subject: {mission.subject}. "
            f"{subject_context} This handoff grants no Control Plane authority."
        )
        handoff = RoleHandoff(
            from_role=mission.role,
            to_role=next_role,
            mission_id=mission.mission_id,
            subject=mission.subject,
            objective=mission.objective,
            observed_commit=current_commit,
            operating_step=target_step,
            blockers=blockers,
            instructions=instructions,
        )
        candidate = replace(
            mission,
            role=next_role,
            operating_step=target_step,
            next_action=instructions,
            observed_commit=current_commit,
            updated_at=updated_at,
            blockers=list(blockers),
        )
        try:
            self._mission_store.save(candidate)
        except Exception as error:
            return _failure(
                reason=_unavailable_reason("MISSION_PERSISTENCE", error),
                mission=mission,
            )

        return OrchestrationResult(
            success=True,
            current_role=mission.role,
            next_role=next_role,
            handoff=handoff,
            blockers=blockers,
            reason=route_reason,
            updated_mission_state=candidate,
        )


def _input_error(current_commit: object, updated_at: object) -> str | None:
    if not isinstance(current_commit, str) or not _COMMIT_PATTERN.fullmatch(
        current_commit
    ):
        return "INVALID_CURRENT_COMMIT"
    if (
        not isinstance(updated_at, datetime)
        or updated_at.tzinfo is None
        or updated_at.utcoffset() is None
    ):
        return "INVALID_UPDATED_AT"
    return None


def _mission_error(mission: object) -> str | None:
    if not isinstance(mission, MissionState):
        return "INVALID_MISSION_STATE"
    if mission.schema_version != "1.0":
        return "INVALID_MISSION_SCHEMA_VERSION"
    if not isinstance(mission.mission_id, str) or not mission.mission_id.strip():
        return "EMPTY_MISSION_ID"
    if not isinstance(mission.status, MissionStatus):
        return "UNKNOWN_MISSION_STATUS"
    if not isinstance(mission.role, MissionRole):
        return "UNKNOWN_MISSION_ROLE"
    if not isinstance(mission.operating_step, OperatingStep):
        return "UNKNOWN_OPERATING_STEP"
    if not all(
        isinstance(value, str) and value.strip()
        for value in (mission.objective, mission.subject, mission.next_action)
    ):
        return "INVALID_MISSION_TEXT"
    if not isinstance(mission.observed_commit, str) or not _COMMIT_PATTERN.fullmatch(
        mission.observed_commit
    ):
        return "INVALID_OBSERVED_COMMIT"
    if (
        not isinstance(mission.updated_at, datetime)
        or mission.updated_at.tzinfo is None
        or mission.updated_at.utcoffset() is None
    ):
        return "INVALID_MISSION_UPDATED_AT"
    if not isinstance(mission.blockers, list) or not all(
        isinstance(blocker, str) and blocker.strip() for blocker in mission.blockers
    ):
        return "INVALID_BLOCKERS"
    if mission.status is MissionStatus.BLOCKED and not mission.blockers:
        return "BLOCKED_WITHOUT_REASON"
    return None


def _route(step: OperatingStep) -> tuple[MissionRole | None, str | None]:
    matches = [role for candidate, role in _ROUTING_POLICY if candidate is step]
    if not matches:
        return None, "ROUTING_UNAVAILABLE"
    if len(matches) != 1:
        return None, "AMBIGUOUS_ROUTING"
    return matches[0], None


def _project_subject_context(
    state: ProjectState, subject: str
) -> tuple[str, str | None, bool]:
    matches = [story for story in state.user_stories if story.id == subject]
    if len(matches) > 1:
        return "", "AMBIGUOUS_PROJECT_SUBJECT", False
    if not matches:
        return "ProjectState provides no matching User Story status.", None, False
    story = matches[0]
    return (
        f"ProjectState reports User Story {subject} as {story.status.value}.",
        None,
        story.human_approval.required and not story.human_approval.approved,
    )


def _requires_human(blocker: str) -> bool:
    marker = blocker.strip().split(":", maxsplit=1)[0]
    return marker.casefold() == _HUMAN_REQUIRED_MARKER.casefold()


def _unavailable_reason(prefix: str, error: Exception) -> str:
    code = getattr(error, "code", type(error).__name__)
    return f"{prefix}_UNAVAILABLE:{code}"


def _failure(
    *,
    reason: str,
    mission: MissionState | None = None,
) -> OrchestrationResult:
    blockers = tuple(mission.blockers) if mission is not None else ()
    return OrchestrationResult(
        success=False,
        current_role=mission.role if mission is not None else None,
        next_role=None,
        handoff=None,
        blockers=blockers,
        reason=reason,
        updated_mission_state=mission,
    )
