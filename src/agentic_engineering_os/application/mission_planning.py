"""Restart-safe composition from admitted request through durable Architect planning."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, cast

from agentic_engineering_os.domain import MissionRole, MissionState, OperatingStep, ProjectState, to_dict

from .architect import ArchitectResult
from .execution_state import (
    CodexExecutionLedger,
    CodexExecutionRecord,
    CodexExecutionStatus,
    result_json_fingerprint,
)
from .mission_admission import MissionAdmission, MissionRequest
from .mission_lifecycle import MissionLifecycleService
from .orchestration_record import (
    ORCHESTRATION_RECORD_VERSION,
    OrchestrationRecord,
    RoleExecutionReference,
    request_fingerprint,
)
from .orchestrator import RoleHandoff
from .result_intake import (
    PersistedRoleResultError,
    reconstruct_persisted_architect_result,
)
from .sequential_mission_workflow import SequentialMissionResult
from .single_role_codex import SingleRoleExecutionOutcome


class MissionPlanningStatus(str, Enum):
    PLANNED = "PLANNED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class MissionPlanningResult:
    status: MissionPlanningStatus
    mission_id: str
    subject: str
    blockers: tuple[str, ...]
    execution_reference: RoleExecutionReference | None
    architect_result: ArchitectResult | None = None


class MissionPlanningError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class PlanningWorkflowPort(Protocol):
    def route(self, *, current_commit: str, updated_at: datetime) -> SequentialMissionResult: ...
    def accept_architect(self, handoff: RoleHandoff, candidate: ArchitectResult, *, updated_at: datetime) -> SequentialMissionResult: ...


class ArchitectExecutorPort(Protocol):
    def execute(self, handoff: RoleHandoff, *, request_id: str) -> SingleRoleExecutionOutcome: ...


class OrchestrationRecordStorePort(Protocol):
    def load(self) -> OrchestrationRecord: ...
    def initialize(self, record: OrchestrationRecord) -> object: ...
    def replace(self, record: OrchestrationRecord, *, expected_fingerprint: str) -> object: ...


class _Reader(Protocol):
    def load(self) -> object: ...


class MissionPlanningCoordinator:
    """Sequence existing authorities; retain only exact ledger references."""

    def __init__(
        self,
        *,
        lifecycle: MissionLifecycleService,
        workflow: PlanningWorkflowPort,
        architect_executor: ArchitectExecutorPort,
        mission_store: _Reader,
        project_store: _Reader,
        execution_store: _Reader,
        record_store: OrchestrationRecordStorePort,
    ) -> None:
        self._lifecycle = lifecycle
        self._workflow = workflow
        self._architect = architect_executor
        self._missions = mission_store
        self._projects = project_store
        self._executions = execution_store
        self._records = record_store

    def start(
        self,
        request: MissionRequest,
        admission: MissionAdmission,
        *,
        updated_at: datetime,
    ) -> MissionPlanningResult:
        started = self._lifecycle.start(request, admission, updated_at=updated_at)
        mission = started.mission
        record = OrchestrationRecord(
            ORCHESTRATION_RECORD_VERSION,
            mission.mission_id,
            request,
            request_fingerprint(request),
            mission.observed_commit,
            mission.workflow_generation,
        )
        try:
            current = self._records.load()
        except Exception as error:
            if getattr(error, "code", None) != "ORCHESTRATION_RECORD_ABSENT":
                raise MissionPlanningError("ORCHESTRATION_RECORD_UNAVAILABLE", str(getattr(error, "code", error))) from error
            self._records.initialize(record)
        else:
            self._records.replace(record, expected_fingerprint=current.fingerprint)
        return self._continue(record, updated_at=updated_at)

    def resume(self, mission_id: str, *, updated_at: datetime) -> MissionPlanningResult:
        record = self._records.load()
        mission = self._mission()
        if record.mission_id != mission_id or mission.mission_id != mission_id:
            raise MissionPlanningError("MISSION_BINDING_MISMATCH", "resume identity is not current")
        self._validate_record_binding(record, mission)
        return self._continue(record, updated_at=updated_at)

    def _continue(
        self, record: OrchestrationRecord, *, updated_at: datetime
    ) -> MissionPlanningResult:
        mission = self._mission()
        existing = _unique_architect_reference(record, mission)
        if existing is not None:
            architect = self._reconstruct_architect(record, existing, mission)
            return MissionPlanningResult(
                MissionPlanningStatus.PLANNED,
                mission.mission_id,
                mission.subject,
                (),
                existing,
                architect,
            )

        if mission.operating_step is not OperatingStep.UNDERSTAND_CONTRACT:
            if mission.operating_step is OperatingStep.ACT:
                reference, architect = self._recover_reference(record, mission)
                return MissionPlanningResult(
                    MissionPlanningStatus.PLANNED,
                    mission.mission_id,
                    mission.subject,
                    (),
                    reference,
                    architect,
                )
            raise MissionPlanningError("PLANNING_STAGE_DIVERGENCE", "mission is outside Architect planning")

        routed = self._workflow.route(current_commit=mission.observed_commit, updated_at=updated_at)
        if routed.handoff is None or routed.handoff.to_role is not MissionRole.ARCHITECT:
            return MissionPlanningResult(MissionPlanningStatus.BLOCKED, mission.mission_id, mission.subject, routed.blockers or ("ARCHITECT_HANDOFF_UNAVAILABLE",), None)
        request_id = _architect_request_id(mission)
        outcome = self._architect.execute(routed.handoff, request_id=request_id)
        if not outcome.validated or not isinstance(outcome.validated_result, ArchitectResult):
            return MissionPlanningResult(MissionPlanningStatus.BLOCKED, mission.mission_id, mission.subject, outcome.blockers or ("ARCHITECT_RESULT_UNAVAILABLE",), None)
        self._workflow.accept_architect(routed.handoff, outcome.validated_result, updated_at=updated_at)
        current = self._mission()
        reference = self._exact_ledger_reference(outcome.execution_id, request_id, mission)
        plan = _plan_fingerprint(self._project(), mission.subject)
        updated = record.with_reference(
            reference,
            plan_fingerprint=plan,
            user_story_ids=(mission.subject,),
        )
        self._records.replace(updated, expected_fingerprint=record.fingerprint)
        architect = self._reconstruct_architect(updated, reference, mission)
        if architect != outcome.validated_result:
            raise MissionPlanningError(
                "ARCHITECT_RESULT_MISMATCH",
                "runtime result differs from its authoritative ledger reconstruction",
            )
        return MissionPlanningResult(
            MissionPlanningStatus.PLANNED,
            current.mission_id,
            current.subject,
            (),
            reference,
            architect,
        )

    def _recover_reference(
        self, record: OrchestrationRecord, mission: MissionState
    ) -> tuple[RoleExecutionReference, ArchitectResult]:
        _require_persisted_story(self._project(), mission.subject)
        request_id = _architect_request_id(mission)
        ledger = self._ledger()
        matches = tuple(
            item
            for item in ledger.records
            if item.mission_id == mission.mission_id
            and item.workflow_generation == mission.workflow_generation
            and item.role is MissionRole.ARCHITECT
            and item.subject == mission.subject
            and item.request_id == request_id
            and item.status is CodexExecutionStatus.VALIDATED
            and item.validated_result_fingerprint is not None
        )
        if len(matches) != 1:
            raise MissionPlanningError("ARCHITECT_EXECUTION_AMBIGUOUS", "exactly one validated Architect execution is required")
        item = matches[0]
        reference = RoleExecutionReference(MissionRole.ARCHITECT, mission.subject, mission.workflow_generation, request_id, item.execution_id, cast(str, item.validated_result_fingerprint))
        updated = record.with_reference(
            reference,
            plan_fingerprint=_plan_fingerprint(self._project(), mission.subject),
            user_story_ids=(mission.subject,),
        )
        self._records.replace(updated, expected_fingerprint=record.fingerprint)
        return reference, self._reconstruct_architect(updated, reference, mission)

    def _exact_ledger_reference(self, execution_id: str, request_id: str, mission: MissionState) -> RoleExecutionReference:
        matches = tuple(item for item in self._ledger().records if item.execution_id == execution_id)
        if len(matches) != 1:
            raise MissionPlanningError("ARCHITECT_EXECUTION_UNRESOLVED", "execution ID does not resolve exactly once")
        item = matches[0]
        if (
            item.status is not CodexExecutionStatus.VALIDATED
            or item.role is not MissionRole.ARCHITECT
            or item.mission_id != mission.mission_id
            or item.subject != mission.subject
            or item.workflow_generation != mission.workflow_generation
            or item.request_id != request_id
            or item.validated_result_fingerprint is None
        ):
            raise MissionPlanningError("ARCHITECT_EXECUTION_BINDING_MISMATCH", "ledger record differs from planning result")
        return RoleExecutionReference(MissionRole.ARCHITECT, mission.subject, mission.workflow_generation, request_id, execution_id, item.validated_result_fingerprint)

    def _exact_ledger_record(
        self, execution_id: str, request_id: str, mission: MissionState
    ) -> CodexExecutionRecord:
        matches = tuple(item for item in self._ledger().records if item.execution_id == execution_id)
        if len(matches) != 1:
            raise MissionPlanningError("ARCHITECT_EXECUTION_UNRESOLVED", "execution ID does not resolve exactly once")
        item = matches[0]
        if (
            item.status is not CodexExecutionStatus.VALIDATED
            or item.role is not MissionRole.ARCHITECT
            or item.mission_id != mission.mission_id
            or item.subject != mission.subject
            or item.workflow_generation != mission.workflow_generation
            or item.request_id != request_id
            or item.validated_result_fingerprint is None
            or item.validated_result_json is None
        ):
            raise MissionPlanningError("ARCHITECT_EXECUTION_BINDING_MISMATCH", "ledger record differs from planning result")
        try:
            fingerprint = result_json_fingerprint(item.validated_result_json)
        except Exception as error:
            raise MissionPlanningError(
                "ARCHITECT_RESULT_INVALID", "ledger result is not canonical"
            ) from error
        if fingerprint != item.validated_result_fingerprint:
            raise MissionPlanningError(
                "ARCHITECT_RESULT_INVALID", "ledger result fingerprint is invalid"
            )
        return item

    def _validate_reference(self, reference: RoleExecutionReference, mission: MissionState) -> CodexExecutionRecord:
        actual = self._exact_ledger_reference(reference.execution_id, reference.request_id, mission)
        if actual != reference:
            raise MissionPlanningError("ARCHITECT_REFERENCE_MISMATCH", "stored reference differs from execution ledger")
        return self._exact_ledger_record(
            reference.execution_id, reference.request_id, mission
        )

    def _reconstruct_architect(
        self,
        record: OrchestrationRecord,
        reference: RoleExecutionReference,
        mission: MissionState,
    ) -> ArchitectResult:
        execution = self._validate_reference(reference, mission)
        assert execution.validated_result_json is not None
        try:
            architect = reconstruct_persisted_architect_result(
                execution.validated_result_json
            )
        except PersistedRoleResultError as error:
            raise MissionPlanningError(
                "ARCHITECT_RESULT_INVALID", str(error)
            ) from error
        if (
            architect.mission_id != mission.mission_id
            or architect.workflow_generation != mission.workflow_generation
            or architect.subject != mission.subject
            or architect.observed_commit.casefold() != mission.observed_commit.casefold()
        ):
            raise MissionPlanningError(
                "ARCHITECT_RESULT_BINDING_MISMATCH",
                "reconstructed ArchitectResult differs from authoritative mission",
            )
        project = self._project()
        persisted = _require_persisted_story(project, mission.subject)
        matching = tuple(item for item in architect.user_stories if item.id == mission.subject)
        plan = _plan_fingerprint(project, mission.subject)
        if (
            len(matching) != 1
            or _story_plan_fingerprint(matching[0])
            != _story_plan_fingerprint(persisted)
            or record.plan_fingerprint != plan
            or record.user_story_ids != (mission.subject,)
        ):
            raise MissionPlanningError(
                "PLANNING_REFERENCE_MISMATCH",
                "orchestration references differ from authoritative planning",
            )
        return architect

    def _validate_record_binding(
        self, record: OrchestrationRecord, mission: MissionState
    ) -> None:
        if (
            record.baseline_commit != mission.observed_commit
            or record.workflow_generation != mission.workflow_generation
            or record.request.objective != mission.objective
            or record.request_fingerprint != request_fingerprint(record.request)
        ):
            raise MissionPlanningError(
                "STALE_ORCHESTRATION_RECORD",
                "record differs from authoritative mission or request",
            )

    def _mission(self) -> MissionState:
        value = self._missions.load()
        if not isinstance(value, MissionState):
            raise MissionPlanningError("MISSION_STATE_INVALID", "MissionState is unavailable")
        return value

    def _project(self) -> ProjectState:
        value = self._projects.load()
        if not isinstance(value, ProjectState):
            raise MissionPlanningError("PROJECT_STATE_INVALID", "ProjectState is unavailable")
        return value

    def _ledger(self) -> CodexExecutionLedger:
        value = self._executions.load()
        if not isinstance(value, CodexExecutionLedger):
            raise MissionPlanningError("EXECUTION_LEDGER_INVALID", "execution ledger is unavailable")
        return value


def _architect_request_id(mission: MissionState) -> str:
    return f"{mission.mission_id}-architect-g{mission.workflow_generation}"


def _unique_architect_reference(record: OrchestrationRecord, mission: MissionState) -> RoleExecutionReference | None:
    matches = tuple(item for item in record.execution_references if item.role is MissionRole.ARCHITECT and item.subject == mission.subject and item.workflow_generation == mission.workflow_generation)
    if len(matches) > 1:
        raise MissionPlanningError("ARCHITECT_REFERENCE_AMBIGUOUS", "multiple Architect references exist")
    return matches[0] if matches else None


def _require_persisted_story(project: ProjectState, subject: str):
    matches = [item for item in project.user_stories if item.id == subject]
    if len(matches) != 1:
        raise MissionPlanningError("PLANNED_STORY_UNRESOLVED", "Architect story is not uniquely persisted")
    return matches[0]


def _plan_fingerprint(project: ProjectState, subject: str) -> str:
    matches = [item for item in project.user_stories if item.id == subject]
    if len(matches) != 1:
        raise MissionPlanningError("PLANNED_STORY_UNRESOLVED", "Architect story is not uniquely persisted")
    return _story_plan_fingerprint(matches[0])


def _story_plan_fingerprint(story) -> str:
    data = to_dict(story)
    data.pop("status", None)
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("updated_at", None)
    approval = data.get("human_approval")
    if isinstance(approval, dict):
        approval.pop("approved", None)
        approval.pop("approved_by", None)
        approval.pop("approved_at", None)
        approval.pop("evidence_ref", None)
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
