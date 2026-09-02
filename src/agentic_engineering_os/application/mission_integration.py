"""Restart-safe composition from durable planning through Gate-owned merge."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Protocol

from agentic_engineering_os.domain import MissionRole, MissionState, ProjectState, UserStoryStatus, WorktreeStatus
from agentic_engineering_os.infrastructure.execution_state_store import ExecutionStateStore

from .codex_e2e_runtime import ParallelCodexRuntimeResult
from .execution_state import CodexExecutionRecord, CodexExecutionStatus, result_json_fingerprint
from .implementer import ImplementerInput, ImplementerResult, ImplementerResultValidator, ImplementerVerdict
from .integration_gate import IntegrationGateClassification, integration_gate_fingerprint
from .merge_coordinator import MergeStatus
from .orchestration_record import OrchestrationRecord, ParallelIntegrationReference, RoleExecutionReference
from .parallel_implementer_coordinator import ParallelMemberResult, PreparedParallelGroup
from .parallel_mission_workflow import ParallelMissionPlan, ParallelMissionWorkflow
from .result_intake import PersistedRoleResultError, reconstruct_persisted_implementer_result


class MissionIntegrationStatus(str, Enum):
    READY_FOR_TESTER = "READY_FOR_TESTER"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class MissionIntegrationResult:
    status: MissionIntegrationStatus
    mission_id: str
    workflow_generation: int
    user_story_ids: tuple[str, ...]
    integrated_commit: str | None
    blockers: tuple[str, ...]
    next_role: MissionRole | None
    implementer_results: tuple[ImplementerResult, ...] = ()


class MissionIntegrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class _Reader(Protocol):
    def load(self) -> object: ...


class _RecordStore(Protocol):
    def load(self) -> OrchestrationRecord: ...
    def replace(self, record: OrchestrationRecord, *, expected_fingerprint: str) -> object: ...


class _ParallelRuntime(Protocol):
    def execute_parallel_implementers(
        self,
        plan: ParallelMissionPlan,
        prepared_group: PreparedParallelGroup,
        *,
        request_id_prefix: str,
    ) -> ParallelCodexRuntimeResult: ...


class MissionIntegrationCoordinator:
    """Compose existing authorities and persist references only."""

    def __init__(
        self,
        *,
        workflow: ParallelMissionWorkflow,
        runtime: _ParallelRuntime,
        mission_store: _Reader,
        project_store: _Reader,
        record_store: _RecordStore,
    ) -> None:
        self._workflow = workflow
        self._runtime = runtime
        self._missions = mission_store
        self._projects = project_store
        self._records = record_store
        self._validator = ImplementerResultValidator()

    def resume(self, mission_id: str, *, updated_at: datetime) -> MissionIntegrationResult:
        record = self._record(mission_id)
        mission = self._mission(mission_id, record)
        progress = record.parallel_integration
        assignment_ids = (
            progress.assignment_ids
            if progress is not None
            else self._workflow.claimed_assignment_ids(
                mission_id=record.mission_id,
                workflow_generation=record.workflow_generation,
                baseline_commit=record.baseline_commit,
                user_story_ids=record.user_story_ids,
            )
        )
        if not assignment_ids:
            if mission.observed_commit.casefold() != record.baseline_commit:
                raise MissionIntegrationError(
                    "RECOVERY_REQUIRED", "primary advanced without assignment references"
                )
            plan = self._workflow.plan_current()
            if not plan.execution_plan.groups:
                return self._blocked(record, "NO_READY_IMPLEMENTATION_GROUP")
            prepared = self._workflow.prepare_group(plan, 0)
            assignment_ids = prepared.assignment_ids
        else:
            plan, prepared = self._workflow.reconstruct_group(assignment_ids)
        plan_reference = _plan_reference_fingerprint(plan, prepared)
        if progress is not None and (
            progress.plan_fingerprint != plan_reference
            or progress.wave_index != prepared.wave_index
            or progress.group_index != prepared.group_index
            or progress.assignment_ids != prepared.assignment_ids
        ):
            raise MissionIntegrationError(
                "STALE_ORCHESTRATION_RECORD", "parallel references differ from P3 authority"
            )
        if progress is None:
            progress = ParallelIntegrationReference(
                plan_reference,
                prepared.wave_index,
                prepared.group_index,
                prepared.assignment_ids,
            )
            record = self._replace(record, record.with_parallel_integration(progress))

        members = self._reconstruct_members(record, prepared)
        if members is None:
            outcome = self._runtime.execute_parallel_implementers(
                plan,
                prepared,
                request_id_prefix=f"{record.mission_id}-implementer-g{record.workflow_generation}",
            )
            if not isinstance(outcome, ParallelCodexRuntimeResult) or outcome.group_result is None:
                blockers = tuple(
                    blocker
                    for member in getattr(getattr(outcome, "execution", None), "members", ())
                    for blocker in member.blockers
                ) or ("IMPLEMENTER_GROUP_INCOMPLETE",)
                return MissionIntegrationResult(
                    MissionIntegrationStatus.BLOCKED,
                    record.mission_id,
                    record.workflow_generation,
                    prepared.user_story_ids,
                    None,
                    blockers,
                    None,
                )
            members = outcome.group_result.member_results
        group = self._workflow.complete_group(prepared, members)
        record = self._persist_implementer_references(record, prepared)
        progress = record.parallel_integration
        assert progress is not None

        if progress.gate_fingerprint is None:
            attempt = self._workflow.evaluate_group(plan, group)
            if attempt.gate_result.result is not IntegrationGateClassification.PASS:
                return self._blocked(
                    record, f"INTEGRATION_GATE_{attempt.gate_result.result.value}"
                )
            progress = replace(
                progress,
                gate_fingerprint=integration_gate_fingerprint(attempt.gate_result),
            )
            record = self._replace(record, record.with_parallel_integration(progress))
            attempt = self._workflow.merge_gated_group(attempt, updated_at=updated_at)
        else:
            attempt = self._workflow.resume_gated_group(
                plan,
                group,
                gate_fingerprint=progress.gate_fingerprint,
                updated_at=updated_at,
            )
        if (
            attempt.merge_result is None
            or attempt.merge_result.result is not MergeStatus.MERGED
            or attempt.merge_result.integration_commit is None
        ):
            return self._blocked(record, "MERGE_NOT_COMPLETED")
        integrated = attempt.merge_result.integration_commit
        for member in members:
            self._workflow.accept_integrated_implementer(
                attempt, member.user_story_id, member.implementer_result
            )
        progress = replace(progress, integrated_commit=integrated)
        if record.parallel_integration != progress:
            record = self._replace(record, record.with_parallel_integration(progress))
        project = self._project()
        if any(
            _story(project, identifier).status is not UserStoryStatus.TESTING
            for identifier in prepared.user_story_ids
        ):
            raise MissionIntegrationError(
                "POST_MERGE_STATE_INVALID", "integrated stories are not ready for Tester"
            )
        return MissionIntegrationResult(
            MissionIntegrationStatus.READY_FOR_TESTER,
            record.mission_id,
            record.workflow_generation,
            prepared.user_story_ids,
            integrated,
            (),
            MissionRole.TESTER,
            tuple(member.implementer_result for member in members),
        )

    def _reconstruct_members(
        self, orchestration: OrchestrationRecord, prepared: PreparedParallelGroup
    ) -> tuple[ParallelMemberResult, ...] | None:
        project = self._project()
        results: list[ParallelMemberResult] = []
        found = 0
        for context in prepared.contexts:
            record = _validated_execution(context)
            if record is None:
                continue
            found += 1
            _require_execution_reference(orchestration, context, record)
            result = _implementer_result(record)
            observation = record.observation
            assert observation is not None and observation.git_after is not None
            if observation.git_after.changed_paths != result.files_changed:
                raise MissionIntegrationError(
                    "IMPLEMENTER_REFERENCE_INVALID",
                    "ledger physical diff differs from the Implementer result",
                )
            story = _story(project, context.user_story_id)
            if story.status in {
                UserStoryStatus.IMPLEMENTED,
                UserStoryStatus.TESTING,
            }:
                story = replace(story, status=UserStoryStatus.IN_PROGRESS)
            implementer_input = ImplementerInput.from_handoff(context.handoff, story)
            if not self._validator.validate(result, implementer_input=implementer_input).is_valid:
                raise MissionIntegrationError(
                    "IMPLEMENTER_REFERENCE_INVALID", "ledger result no longer validates"
                )
            assignment = self._workflow.assignment(context.assignment_id)
            if assignment.status is WorktreeStatus.ACTIVE:
                results.append(
                    self._workflow.submit_member(
                        prepared,
                        context.assignment_id,
                        result,
                        execution_id=record.execution_id,
                        implementer_input=implementer_input,
                    )
                )
            elif assignment.status is WorktreeStatus.COMPLETED and assignment.result_commit:
                results.append(
                    ParallelMemberResult(
                        context.assignment_id,
                        context.user_story_id,
                        assignment.result_commit,
                        implementer_input,
                        result,
                    )
                )
            else:
                raise MissionIntegrationError(
                    "ASSIGNMENT_MISMATCH", "ledger result has no resumable assignment"
                )
        if found == 0:
            return None
        if found != len(prepared.contexts):
            raise MissionIntegrationError(
                "RECOVERY_REQUIRED", "partial Implementer group cannot be replayed blindly"
            )
        return tuple(results)

    def _persist_implementer_references(
        self, record: OrchestrationRecord, prepared: PreparedParallelGroup
    ) -> OrchestrationRecord:
        candidate = record
        for context in prepared.contexts:
            execution = _validated_execution(context)
            if execution is None or execution.validated_result_fingerprint is None:
                raise MissionIntegrationError(
                    "IMPLEMENTER_REFERENCE_MISSING", "validated execution is absent"
                )
            reference = RoleExecutionReference(
                MissionRole.IMPLEMENTER,
                context.user_story_id,
                context.workflow_generation,
                execution.request_id,
                execution.execution_id,
                execution.validated_result_fingerprint,
            )
            existing = tuple(
                item
                for item in record.execution_references
                if item.role is MissionRole.IMPLEMENTER
                and item.subject == context.user_story_id
                and item.workflow_generation == context.workflow_generation
            )
            if existing and existing != (reference,):
                raise MissionIntegrationError(
                    "IMPLEMENTER_REFERENCE_INVALID",
                    "durable Implementer reference differs from execution authority",
                )
            candidate = candidate.with_reference(reference)
        return record if candidate == record else self._replace(record, candidate)

    def _record(self, mission_id: str) -> OrchestrationRecord:
        value = self._records.load()
        if not isinstance(value, OrchestrationRecord) or value.mission_id != mission_id:
            raise MissionIntegrationError(
                "MISSION_BINDING_MISMATCH", "durable mission reference is not current"
            )
        if not value.user_story_ids or value.plan_fingerprint is None:
            raise MissionIntegrationError(
                "PLANNING_REFERENCE_MISSING", "M2 planning is incomplete"
            )
        return value

    def _mission(self, mission_id: str, record: OrchestrationRecord) -> MissionState:
        value = self._missions.load()
        if (
            not isinstance(value, MissionState)
            or value.mission_id != mission_id
            or value.workflow_generation != record.workflow_generation
            or value.objective != record.request.objective
        ):
            raise MissionIntegrationError("MISSION_BINDING_MISMATCH", "MissionState is stale")
        return value

    def _project(self) -> ProjectState:
        value = self._projects.load()
        if not isinstance(value, ProjectState):
            raise MissionIntegrationError("PROJECT_STATE_INVALID", "ProjectState is unavailable")
        return value

    def _replace(
        self, before: OrchestrationRecord, candidate: OrchestrationRecord
    ) -> OrchestrationRecord:
        self._records.replace(candidate, expected_fingerprint=before.fingerprint)
        return candidate

    @staticmethod
    def _blocked(record: OrchestrationRecord, blocker: str) -> MissionIntegrationResult:
        return MissionIntegrationResult(
            MissionIntegrationStatus.BLOCKED,
            record.mission_id,
            record.workflow_generation,
            record.user_story_ids,
            None,
            (blocker,),
            None,
        )


def _validated_execution(context) -> CodexExecutionRecord | None:
    try:
        ledger = ExecutionStateStore(Path(context.worktree_path)).load()
    except Exception as error:
        if getattr(error, "code", None) == "LEDGER_ABSENT":
            return None
        raise MissionIntegrationError(
            "EXECUTION_LEDGER_UNAVAILABLE", str(getattr(error, "code", error))
        ) from error
    matches = tuple(
        item
        for item in ledger.records
        if item.status is CodexExecutionStatus.VALIDATED
        and item.role is MissionRole.IMPLEMENTER
        and item.mission_id == context.handoff.mission_id
        and item.workflow_generation == context.workflow_generation
        and item.subject == context.user_story_id
        and item.expected_result_contract == "implementer-result@1.0"
        and item.expected_commit == context.baseline_commit
        and item.worktree_path is not None
        and _path_key(item.worktree_path) == _path_key(context.worktree_path)
        and _path_key(item.cwd) == _path_key(context.worktree_path)
        and item.observation is not None
        and item.observation.git_before is not None
        and item.observation.git_before.error is None
        and item.observation.git_before.head_commit == context.baseline_commit
        and item.observation.git_before.clean is True
        and item.observation.git_after is not None
        and item.observation.git_after.error is None
        and item.observation.git_after.head_commit == context.baseline_commit
        and item.observation.git_after.clean is False
        and item.validated_result_json is not None
        and item.validated_result_fingerprint is not None
        and result_json_fingerprint(item.validated_result_json)
        == item.validated_result_fingerprint
    )
    if len(matches) > 1:
        raise MissionIntegrationError(
            "IMPLEMENTER_EXECUTION_AMBIGUOUS", "multiple validated executions match assignment"
        )
    return matches[0] if matches else None


def _implementer_result(record: CodexExecutionRecord) -> ImplementerResult:
    assert record.validated_result_json is not None
    try:
        result = reconstruct_persisted_implementer_result(record.validated_result_json)
    except PersistedRoleResultError as error:
        raise MissionIntegrationError("IMPLEMENTER_REFERENCE_INVALID", str(error)) from error
    if result.verdict is not ImplementerVerdict.READY_FOR_TEST:
        raise MissionIntegrationError(
            "IMPLEMENTER_REFERENCE_INVALID", "persisted result is not READY_FOR_TEST"
        )
    return result


def _require_execution_reference(
    orchestration: OrchestrationRecord,
    context,
    execution: CodexExecutionRecord,
) -> None:
    references = tuple(
        item
        for item in orchestration.execution_references
        if item.role is MissionRole.IMPLEMENTER
        and item.subject == context.user_story_id
        and item.workflow_generation == context.workflow_generation
    )
    if not references:
        return
    expected = RoleExecutionReference(
        MissionRole.IMPLEMENTER,
        context.user_story_id,
        context.workflow_generation,
        execution.request_id,
        execution.execution_id,
        execution.validated_result_fingerprint or "",
    )
    if references != (expected,):
        raise MissionIntegrationError(
            "IMPLEMENTER_REFERENCE_INVALID",
            "durable Implementer reference differs from execution authority",
        )


def _plan_reference_fingerprint(
    plan: ParallelMissionPlan, prepared: PreparedParallelGroup
) -> str:
    payload = {
        "mission_id": plan.mission_id,
        "workflow_generation": plan.workflow_generation,
        "baseline_commit": plan.baseline_commit,
        "wave_index": prepared.wave_index,
        "group_index": prepared.group_index,
        "user_story_ids": list(prepared.user_story_ids),
        "assignment_ids": list(prepared.assignment_ids),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _story(project: ProjectState, identifier: str):
    matches = tuple(item for item in project.user_stories if item.id == identifier)
    if len(matches) != 1:
        raise MissionIntegrationError(
            "STORY_BINDING_MISMATCH", "User Story is absent or ambiguous"
        )
    return matches[0]


def _path_key(value: str) -> str:
    return os.path.normcase(str(Path(value).resolve(strict=False))).casefold()
