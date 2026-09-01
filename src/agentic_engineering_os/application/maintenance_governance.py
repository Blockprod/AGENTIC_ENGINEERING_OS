"""Admission-only governance for maintenance, freeze and recovery routing."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Protocol
from unicodedata import category, normalize

from agentic_engineering_os._maintenance_write import _issue_maintenance_write
from agentic_engineering_os.domain.governance import GovernedOperation, GovernanceDecision
from agentic_engineering_os.domain.health import HealthState
from agentic_engineering_os.domain.incidents import IncidentSeverity, IncidentState
from agentic_engineering_os.domain.maintenance import (
    MAINTENANCE_MAX_SOURCE_AGE,
    MaintenanceAdmission,
    MaintenanceAdmissionDecision,
    MaintenanceAdmissionReason,
    MaintenanceEvaluationContext,
    MaintenanceInitializationRequest,
    MaintenanceOperation,
    MaintenanceRecord,
    MaintenanceState,
    MaintenanceTransitionReason,
    MaintenanceTransitionRequest,
    MaintenanceTransitionResult,
    RecoveryDispatchRequest,
    RecoveryObservationStatus,
    RecoveryRoute,
)
from agentic_engineering_os.domain.resource_budgets import ResourceBudgetDecision


class MaintenanceGovernanceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class MaintenanceStateStorePort(Protocol):
    @property
    def repository_root(self) -> Path: ...
    def load(self) -> MaintenanceRecord: ...
    def _initialize_authorized(self, record: MaintenanceRecord, *, authorization: object) -> Path: ...
    def _replace_authorized(self, record: MaintenanceRecord, *, authorization: object) -> Path: ...


_NEW_WORK = frozenset({
    MaintenanceOperation.START_MISSION,
    MaintenanceOperation.START_ROLE_EXECUTION,
    MaintenanceOperation.START_PARALLEL_GROUP,
    MaintenanceOperation.CREATE_WORKTREE,
    MaintenanceOperation.MERGE,
    MaintenanceOperation.APPLY_ADOPTION_MIGRATION,
})
_RECOVERY = frozenset({MaintenanceOperation.START_REMEDIATION, MaintenanceOperation.RESUME_RECOVERY})
_ALLOWED_TRANSITIONS = {
    MaintenanceState.NORMAL: frozenset({MaintenanceState.DRAINING, MaintenanceState.MAINTENANCE, MaintenanceState.RECOVERY_REQUIRED, MaintenanceState.FROZEN}),
    MaintenanceState.DRAINING: frozenset({MaintenanceState.NORMAL, MaintenanceState.MAINTENANCE, MaintenanceState.RECOVERY_REQUIRED, MaintenanceState.FROZEN}),
    MaintenanceState.MAINTENANCE: frozenset({MaintenanceState.NORMAL, MaintenanceState.RECOVERY_REQUIRED, MaintenanceState.FROZEN}),
    MaintenanceState.RECOVERY_REQUIRED: frozenset({MaintenanceState.NORMAL, MaintenanceState.MAINTENANCE, MaintenanceState.FROZEN}),
    MaintenanceState.FROZEN: frozenset({MaintenanceState.MAINTENANCE, MaintenanceState.RECOVERY_REQUIRED}),
}
_TARGET_REASONS = {
    MaintenanceState.NORMAL: frozenset({MaintenanceTransitionReason.OPERATOR_RETURN_TO_NORMAL}),
    MaintenanceState.DRAINING: frozenset({MaintenanceTransitionReason.OPERATOR_DRAIN}),
    MaintenanceState.MAINTENANCE: frozenset({MaintenanceTransitionReason.OPERATOR_MAINTENANCE}),
    MaintenanceState.RECOVERY_REQUIRED: frozenset({MaintenanceTransitionReason.RECOVERY_COORDINATION}),
    MaintenanceState.FROZEN: frozenset({MaintenanceTransitionReason.INCIDENT_ESCALATION}),
}
_ROUTE_BOUNDARIES = {
    RecoveryRoute.P2_SEQUENTIAL_REMEDIATION: "SequentialMissionWorkflow remediation boundary",
    RecoveryRoute.P3_PARALLEL_RECOVERY: "ParallelMissionWorkflow.resume_recovery",
    RecoveryRoute.P4_EXECUTION_RECOVERY: "RestartSafeCodexExecutionService.inspect_restart",
    RecoveryRoute.P5_ADOPTION_MIGRATION_RECOVERY: "ExistingRepositoryAdoption / UpgradePlanner recovery boundary",
}


class MaintenanceGovernanceService:
    """Control admission without performing or authorizing business mutations."""

    def __init__(self, store: MaintenanceStateStorePort) -> None:
        self._store = store

    def initialize(self, request: MaintenanceInitializationRequest) -> MaintenanceRecord:
        self._require_root(request.scope.repository_root)
        record = MaintenanceRecord.create(
            scope=request.scope, state=MaintenanceState.NORMAL, revision=1,
            updated_at=request.requested_at, actor_identity=request.operator_identity,
            repository_head=request.repository_head, mission_id=request.mission_id,
            workflow_generation=request.workflow_generation,
            transition_reason=MaintenanceTransitionReason.OPERATOR_RETURN_TO_NORMAL,
            recovery_route=None, previous_fingerprint=None,
        )
        authority = _issue_maintenance_write(store=self._store, before=None, after=record, operation="INITIALIZE")
        self._store._initialize_authorized(record, authorization=authority)
        return record

    def evaluate(self, context: MaintenanceEvaluationContext, operation: MaintenanceOperation) -> MaintenanceAdmission:
        if not isinstance(operation, MaintenanceOperation):
            raise MaintenanceGovernanceError("UNKNOWN_OPERATION", "operation must belong to the closed catalog")
        record = self._load_bound(context)
        if operation is MaintenanceOperation.READ_DIAGNOSTICS:
            return self._admission(operation, record, context, MaintenanceAdmissionDecision.ADMITTED, {MaintenanceAdmissionReason.READ_ONLY_DIAGNOSTIC})
        problems = self._source_problems(context, operation)
        fatal = problems - {MaintenanceAdmissionReason.GOVERNANCE_REQUIRES_HUMAN}
        if fatal:
            return self._admission(operation, record, context, MaintenanceAdmissionDecision.REFUSED, fatal)
        governance_human = MaintenanceAdmissionReason.GOVERNANCE_REQUIRES_HUMAN in problems
        if record.state is MaintenanceState.FROZEN:
            if operation in _RECOVERY:
                return self._admission(operation, record, context, MaintenanceAdmissionDecision.HUMAN_REQUIRED, {MaintenanceAdmissionReason.RECOVERY_OPERATION_REQUIRES_HUMAN})
            return self._admission(operation, record, context, MaintenanceAdmissionDecision.REFUSED, {MaintenanceAdmissionReason.FROZEN_NEW_WORK_REFUSED})
        if record.state is MaintenanceState.RECOVERY_REQUIRED:
            if operation in _RECOVERY:
                return self._admission(operation, record, context, MaintenanceAdmissionDecision.HUMAN_REQUIRED, {MaintenanceAdmissionReason.RECOVERY_OPERATION_REQUIRES_HUMAN})
            return self._admission(operation, record, context, MaintenanceAdmissionDecision.REFUSED, {MaintenanceAdmissionReason.MAINTENANCE_NEW_WORK_REFUSED})
        if record.state is MaintenanceState.MAINTENANCE:
            if operation in _RECOVERY:
                return self._admission(operation, record, context, MaintenanceAdmissionDecision.HUMAN_REQUIRED, {MaintenanceAdmissionReason.RECOVERY_OPERATION_REQUIRES_HUMAN})
            return self._admission(operation, record, context, MaintenanceAdmissionDecision.REFUSED, {MaintenanceAdmissionReason.MAINTENANCE_NEW_WORK_REFUSED})
        if record.state is MaintenanceState.DRAINING:
            if operation is MaintenanceOperation.COMPLETE_IN_FLIGHT:
                if governance_human:
                    return self._admission(operation, record, context, MaintenanceAdmissionDecision.HUMAN_REQUIRED, {MaintenanceAdmissionReason.GOVERNANCE_REQUIRES_HUMAN})
                return self._admission(operation, record, context, MaintenanceAdmissionDecision.ADMITTED, {MaintenanceAdmissionReason.DRAINING_SAFE_COMPLETION})
            if operation in _RECOVERY:
                return self._admission(operation, record, context, MaintenanceAdmissionDecision.HUMAN_REQUIRED, {MaintenanceAdmissionReason.RECOVERY_OPERATION_REQUIRES_HUMAN})
            return self._admission(operation, record, context, MaintenanceAdmissionDecision.REFUSED, {MaintenanceAdmissionReason.DRAINING_NEW_WORK_REFUSED})
        if governance_human:
            return self._admission(operation, record, context, MaintenanceAdmissionDecision.HUMAN_REQUIRED, {MaintenanceAdmissionReason.GOVERNANCE_REQUIRES_HUMAN})
        return self._admission(operation, record, context, MaintenanceAdmissionDecision.ADMITTED, {MaintenanceAdmissionReason.NORMAL_OPERATION})

    def enforce(self, admission: MaintenanceAdmission, operation: MaintenanceOperation) -> None:
        """Minimal integration hook: reject forged, stale, or non-admitted decisions."""
        current = self._store.load()
        if (
            not isinstance(admission, MaintenanceAdmission)
            or admission.operation is not operation
            or admission.decision is not MaintenanceAdmissionDecision.ADMITTED
            or admission.maintenance_fingerprint != current.fingerprint
        ):
            raise MaintenanceGovernanceError("ADMISSION_REQUIRED", "exact current ADMITTED decision is required")

    def request_transition(self, context: MaintenanceEvaluationContext, request: MaintenanceTransitionRequest) -> MaintenanceTransitionResult:
        record = self._load_bound(context)
        self._require_root(request.scope.repository_root)
        if request.scope != context.scope or request.repository_head != context.repository_head or request.mission_id != context.mission_id or request.workflow_generation != context.workflow_generation:
            raise MaintenanceGovernanceError("FOREIGN_TRANSITION", "transition request does not match evaluation scope")
        if request.expected_revision != record.revision or request.expected_fingerprint != record.fingerprint:
            raise MaintenanceGovernanceError("STALE_TRANSITION", "transition does not extend exact current state")
        if _identity_key(request.operator_identity) != _identity_key(record.actor_identity):
            raise MaintenanceGovernanceError(
                "OPERATOR_MISMATCH",
                "transition operator differs from the durable current operator",
            )
        if request.requested_at != context.evaluated_at or request.requested_at <= record.updated_at:
            raise MaintenanceGovernanceError("STALE_TRANSITION", "transition timestamp is stale or not current")
        if request.target_state not in _ALLOWED_TRANSITIONS[record.state]:
            raise MaintenanceGovernanceError("TRANSITION_REFUSED", "state transition is not in the closed graph")
        if request.reason not in _TARGET_REASONS[request.target_state]:
            raise MaintenanceGovernanceError("TRANSITION_REFUSED", "transition reason does not match target state")
        problems = self._source_problems(context, MaintenanceOperation.START_REMEDIATION, expected_operation=GovernedOperation.MAINTENANCE)
        scope_freshness_problems = problems & {MaintenanceAdmissionReason.SOURCE_SCOPE_MISMATCH, MaintenanceAdmissionReason.SOURCE_STALE}
        if scope_freshness_problems:
            raise MaintenanceGovernanceError("SOURCE_INVALID", ",".join(item.value for item in sorted(scope_freshness_problems, key=lambda item: item.value)))
        if request.target_state is MaintenanceState.NORMAL:
            normal_problems = (problems - {MaintenanceAdmissionReason.GOVERNANCE_REQUIRES_HUMAN}) | self._normal_exit_problems(context)
            if normal_problems:
                raise MaintenanceGovernanceError("NORMAL_EXIT_REFUSED", ",".join(item.value for item in sorted(normal_problems, key=lambda item: item.value)))
            if record.state in {MaintenanceState.FROZEN, MaintenanceState.RECOVERY_REQUIRED}:
                recovery = context.recovery
                if recovery is None or recovery.status is not RecoveryObservationStatus.SUCCEEDED or recovery.route is not record.recovery_route:
                    raise MaintenanceGovernanceError("RECOVERY_NOT_PROVEN", "current successful routed recovery observation is required")
        candidate = MaintenanceRecord.create(
            scope=request.scope, state=request.target_state, revision=record.revision + 1,
            updated_at=request.requested_at, actor_identity=request.operator_identity,
            repository_head=request.repository_head, mission_id=request.mission_id,
            workflow_generation=request.workflow_generation, transition_reason=request.reason,
            recovery_route=request.recovery_route, previous_fingerprint=record.fingerprint,
        )
        authority = _issue_maintenance_write(store=self._store, before=record, after=candidate, operation="TRANSITION")
        self._store._replace_authorized(candidate, authorization=authority)
        dispatch = None
        if request.target_state is MaintenanceState.RECOVERY_REQUIRED:
            assert request.recovery_route is not None
            dispatch = RecoveryDispatchRequest(
                request.recovery_route, _ROUTE_BOUNDARIES[request.recovery_route], candidate.fingerprint,
                request.scope.project_id, request.repository_head, request.mission_id,
                request.workflow_generation, request.requested_at, request.operator_identity,
            )
        return MaintenanceTransitionResult(record.state, candidate, dispatch)

    def _load_bound(self, context: MaintenanceEvaluationContext) -> MaintenanceRecord:
        self._require_root(context.scope.repository_root)
        record = self._store.load()
        if record.scope != context.scope or record.repository_head != context.repository_head or record.mission_id != context.mission_id or record.workflow_generation != context.workflow_generation:
            raise MaintenanceGovernanceError("STATE_SCOPE_MISMATCH", "persisted maintenance state is foreign or stale")
        return record

    def _require_root(self, value: str) -> None:
        if os.path.normcase(str(Path(value).resolve(strict=False))) != os.path.normcase(str(self._store.repository_root)):
            raise MaintenanceGovernanceError("REPOSITORY_MISMATCH", "maintenance scope is not bound to this store")

    def _source_problems(self, context: MaintenanceEvaluationContext, operation: MaintenanceOperation, *, expected_operation: GovernedOperation | None = None) -> set[MaintenanceAdmissionReason]:
        expected = expected_operation or _governed_operation(operation)
        result: set[MaintenanceAdmissionReason] = set()
        exact = (context.scope.project_id, context.repository_head, context.mission_id, context.workflow_generation)
        health_scope = context.health.scope
        governance_scope = context.governance.scope
        budget_scope = context.resource_budgets.scope
        if (
            (health_scope.project_id, health_scope.repository_head, health_scope.mission_id, health_scope.workflow_generation) != exact
            or (governance_scope.project_id, governance_scope.repository_head, governance_scope.mission_id, governance_scope.workflow_generation) != exact
            or (budget_scope.project_id, budget_scope.repository_head, budget_scope.mission_id, budget_scope.workflow_generation) != exact
            or os.path.normcase(str(Path(budget_scope.repository_root).resolve(strict=False))) != os.path.normcase(str(Path(context.scope.repository_root).resolve(strict=False)))
            or context.governance.operation is not expected
            or context.resource_budgets.operation is not expected
            or any((item.scope.project_id, item.scope.repository_head, item.scope.mission_id, item.scope.workflow_generation) != exact for item in context.incidents)
        ):
            result.add(MaintenanceAdmissionReason.SOURCE_SCOPE_MISMATCH)
        timestamps = (context.health.evaluated_at, context.governance.evaluated_at, context.resource_budgets.evaluated_at)
        if any(stamp > context.evaluated_at or context.evaluated_at - stamp > MAINTENANCE_MAX_SOURCE_AGE for stamp in timestamps):
            result.add(MaintenanceAdmissionReason.SOURCE_STALE)
        if context.recovery is not None:
            recovery = context.recovery
            if (
                recovery.scope != context.scope
                or recovery.repository_head != context.repository_head
                or recovery.mission_id != context.mission_id
                or recovery.workflow_generation != context.workflow_generation
            ):
                result.add(MaintenanceAdmissionReason.SOURCE_SCOPE_MISMATCH)
            if recovery.observed_at > context.evaluated_at or context.evaluated_at - recovery.observed_at > MAINTENANCE_MAX_SOURCE_AGE:
                result.add(MaintenanceAdmissionReason.SOURCE_STALE)
        if context.health.global_state in {HealthState.BLOCKED, HealthState.UNKNOWN}:
            result.add(MaintenanceAdmissionReason.HEALTH_BLOCKED_OR_UNKNOWN)
        if context.governance.decision is GovernanceDecision.BLOCK:
            result.add(MaintenanceAdmissionReason.GOVERNANCE_BLOCK)
        elif context.governance.decision is GovernanceDecision.REQUIRE_OPERATOR:
            result.add(MaintenanceAdmissionReason.GOVERNANCE_REQUIRES_HUMAN)
        if any(item.decision in {ResourceBudgetDecision.LIMIT_REACHED, ResourceBudgetDecision.LIMIT_EXCEEDED, ResourceBudgetDecision.UNKNOWN} for item in context.resource_budgets.decisions):
            result.add(MaintenanceAdmissionReason.RESOURCE_BUDGET_REFUSAL)
        if any(item.severity is IncidentSeverity.CRITICAL and item.state is not IncidentState.RESOLVED for item in context.incidents):
            result.add(MaintenanceAdmissionReason.CRITICAL_INCIDENT_ACTIVE)
        return result

    @staticmethod
    def _normal_exit_problems(context: MaintenanceEvaluationContext) -> set[MaintenanceAdmissionReason]:
        return set() if context.health.global_state is HealthState.HEALTHY else {MaintenanceAdmissionReason.HEALTH_BLOCKED_OR_UNKNOWN}

    @staticmethod
    def _admission(operation: MaintenanceOperation, record: MaintenanceRecord, context: MaintenanceEvaluationContext, decision: MaintenanceAdmissionDecision, reasons: set[MaintenanceAdmissionReason]) -> MaintenanceAdmission:
        return MaintenanceAdmission(operation, record.state, decision, tuple(sorted(reasons, key=lambda item: item.value)), record.fingerprint, context.evaluated_at)


def _governed_operation(operation: MaintenanceOperation) -> GovernedOperation:
    if operation is MaintenanceOperation.MERGE:
        return GovernedOperation.MERGE
    if operation in _RECOVERY:
        return GovernedOperation.RECOVERY
    if operation is MaintenanceOperation.APPLY_ADOPTION_MIGRATION:
        return GovernedOperation.DEPLOYMENT
    if operation in {MaintenanceOperation.READ_DIAGNOSTICS, MaintenanceOperation.COMPLETE_IN_FLIGHT}:
        return GovernedOperation.VERIFICATION
    return GovernedOperation.EXECUTION


def _identity_key(value: str) -> str:
    normalized = normalize("NFKC", value).strip()
    return "".join(character for character in normalized if category(character) != "Cf").casefold()
