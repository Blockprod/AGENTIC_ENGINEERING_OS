"""Controlled mission creation and replacement for product composition."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from agentic_engineering_os._authoritative_write import _issue_authoritative_write
from agentic_engineering_os.domain import (
    MaintenanceOperation,
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    ProjectState,
    UserStoryStatus,
)

from .mission_admission import MissionAdmission, MissionAdmissionStatus, MissionRequest
from .orchestration_record import request_fingerprint


class MissionLifecycleError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class MissionLifecycleStore(Protocol):
    def initialize(self, state: MissionState) -> MissionState: ...
    def load(self) -> MissionState: ...
    def save(
        self,
        state: MissionState,
        *,
        authorization: object | None = None,
        operation: str | None = None,
    ) -> object: ...


class MissionProjectReader(Protocol):
    def load(self) -> ProjectState: ...


class MissionMaintenanceEnforcer(Protocol):
    def enforce(self, admission: object, operation: MaintenanceOperation) -> None: ...


@dataclass(frozen=True, slots=True)
class MissionLifecycleStart:
    mission: MissionState
    replaced_terminal_mission: bool


@dataclass(frozen=True, slots=True)
class MissionLifecyclePreparation:
    """Exact non-mutating intent consumed by one guarded mission-state commit."""

    mission: MissionState
    previous_mission: MissionState | None


class MissionLifecycleService:
    """Own only MissionState start/replacement; never mutate ProjectState."""

    def __init__(
        self,
        *,
        mission_store: MissionLifecycleStore,
        project_store: MissionProjectReader,
        maintenance: MissionMaintenanceEnforcer,
    ) -> None:
        self._mission_store = mission_store
        self._project_store = project_store
        self._maintenance = maintenance

    def start(
        self,
        request: MissionRequest,
        admission: MissionAdmission,
        *,
        updated_at: datetime,
    ) -> MissionLifecycleStart:
        prepared = self.prepare(request, admission, updated_at=updated_at)
        return self.commit(prepared)

    def prepare(
        self,
        request: MissionRequest,
        admission: MissionAdmission,
        *,
        updated_at: datetime,
    ) -> MissionLifecyclePreparation:
        """Validate a complete start without writing either mission store."""

        _validate_start_inputs(request, admission, updated_at)
        assert admission.maintenance_admission is not None
        self._maintenance.enforce(
            admission.maintenance_admission, MaintenanceOperation.START_MISSION
        )
        project = self._project_store.load()
        if not isinstance(project, ProjectState) or project.project_id != admission.project_id:
            raise MissionLifecycleError(
                "PROJECT_STATE_BINDING_MISMATCH",
                "ProjectState no longer matches the admitted project",
            )
        subject = _next_story_id(project)
        mission = MissionState(
            schema_version="1.0",
            mission_id=_mission_id(admission),
            workflow_generation=0,
            status=MissionStatus.ACTIVE,
            role=MissionRole.ORCHESTRATOR,
            objective=request.objective,
            subject=subject,
            operating_step=OperatingStep.UNDERSTAND_CONTRACT,
            next_action="Route the admitted objective to Architect.",
            observed_commit=admission.repository_head or "",
            updated_at=updated_at,
            blockers=[],
        )
        try:
            current = self._mission_store.load()
        except Exception as error:
            if getattr(error, "code", None) != "MISSION_ABSENT":
                raise MissionLifecycleError(
                    "MISSION_STATE_UNAVAILABLE", str(getattr(error, "code", error))
                ) from error
            return MissionLifecyclePreparation(mission, None)

        if current.status not in {MissionStatus.COMPLETED, MissionStatus.CANCELLED}:
            raise MissionLifecycleError(
                "MISSION_NOT_TERMINAL", "an existing mission cannot be replaced"
            )
        nonterminal = tuple(
            story.id
            for story in project.user_stories
            if story.status not in {UserStoryStatus.CERTIFIED, UserStoryStatus.CANCELLED}
        )
        if nonterminal:
            raise MissionLifecycleError(
                "PROJECT_WORK_NOT_TERMINAL",
                "terminal mission replacement requires all existing User Stories terminal",
            )
        return MissionLifecyclePreparation(mission, current)

    def commit(
        self, prepared: MissionLifecyclePreparation
    ) -> MissionLifecycleStart:
        """Persist one previously prepared intent after rechecking its predecessor."""

        if not isinstance(prepared, MissionLifecyclePreparation):
            raise MissionLifecycleError(
                "INVALID_START_PREPARATION", "canonical preparation is required"
            )
        mission = prepared.mission
        previous = prepared.previous_mission
        if previous is None:
            try:
                self._mission_store.load()
            except Exception as error:
                if getattr(error, "code", None) != "MISSION_ABSENT":
                    raise MissionLifecycleError(
                        "MISSION_STATE_UNAVAILABLE", str(getattr(error, "code", error))
                    ) from error
            else:
                raise MissionLifecycleError(
                    "MISSION_START_CHANGED", "mission state appeared after preparation"
                )
            self._mission_store.initialize(mission)
            persisted = self._mission_store.load()
            if persisted != mission:
                raise MissionLifecycleError(
                    "MISSION_START_NOT_DURABLE", "initialized mission differs after reread"
                )
            return MissionLifecycleStart(mission, False)

        try:
            current = self._mission_store.load()
        except Exception as error:
            raise MissionLifecycleError(
                "MISSION_STATE_UNAVAILABLE", str(getattr(error, "code", error))
            ) from error
        if current != previous:
            raise MissionLifecycleError(
                "MISSION_START_CHANGED", "terminal predecessor changed after preparation"
            )
        authorization = _issue_authoritative_write(
            store_kind="MISSION_STATE",
            store=self._mission_store,
            before_state=previous,
            candidate_state=mission,
            operation="START_MISSION_REPLACE_TERMINAL",
        )
        self._mission_store.save(
            mission,
            authorization=authorization,
            operation="START_MISSION_REPLACE_TERMINAL",
        )
        persisted = self._mission_store.load()
        if persisted != mission:
            raise MissionLifecycleError(
                "MISSION_START_NOT_DURABLE", "replacement mission differs after reread"
            )
        return MissionLifecycleStart(mission, True)


def _validate_start_inputs(
    request: MissionRequest, admission: MissionAdmission, updated_at: datetime
) -> None:
    if not isinstance(request, MissionRequest) or not isinstance(admission, MissionAdmission):
        raise MissionLifecycleError("INVALID_START_INPUT", "canonical request and admission are required")
    if admission.status is not MissionAdmissionStatus.ADMITTED:
        raise MissionLifecycleError("ADMISSION_REQUIRED", "mission start requires ADMITTED")
    if admission.request_fingerprint != request_fingerprint(request):
        raise MissionLifecycleError("ADMISSION_REQUEST_MISMATCH", "admission belongs to a different request")
    if admission.repository_head is None or admission.project_id is None:
        raise MissionLifecycleError("ADMISSION_BINDING_INCOMPLETE", "admission lacks repository bindings")
    if not isinstance(updated_at, datetime) or updated_at.tzinfo is None or updated_at.utcoffset() != timedelta(0):
        raise MissionLifecycleError("INVALID_UPDATED_AT", "updated_at must be aware UTC")


def _mission_id(admission: MissionAdmission) -> str:
    payload = f"{admission.request_fingerprint}:{admission.repository_head}".encode()
    return f"mission-{hashlib.sha256(payload).hexdigest()[:16]}"


def _next_story_id(project: ProjectState) -> str:
    values = []
    for story in project.user_stories:
        if story.id.startswith("US-") and story.id[3:].isdigit():
            values.append(int(story.id[3:]))
    candidate = max(values, default=0) + 1
    if candidate > 9999:
        raise MissionLifecycleError("USER_STORY_ID_EXHAUSTED", "no V1 User Story ID remains")
    return f"US-{candidate:04d}"
