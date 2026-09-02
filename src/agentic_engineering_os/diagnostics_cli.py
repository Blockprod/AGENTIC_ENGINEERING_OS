"""Read-only operator diagnostics for the existing command-line interface."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agentic_engineering_os.application import (
    CodexExecutionStatus,
    GovernancePolicyEvaluator,
    HealthEvaluationEngine,
    IncidentManager,
    MetricsEngine,
    ResourceBudgetEvaluator,
)
from agentic_engineering_os.domain import (
    GovernedOperation,
    GovernanceCondition,
    GovernanceDecision,
    GovernanceEvaluationContext,
    GovernancePolicy,
    GovernancePolicyClass,
    GovernancePolicyDomain,
    GovernancePolicyScope,
    GovernanceRationale,
    GovernanceScope,
    HealthCondition,
    HealthEvaluationContext,
    HealthObservation,
    HealthScope,
    HealthSource,
    HealthState,
    IncidentEvaluationContext,
    IncidentScope,
    IncidentState,
    MetricsHealthInput,
    MetricsScope,
    MetricsSnapshotStatus,
    MissionRole,
    MissionStatus,
    ResourceBudget,
    ResourceBudgetApplicability,
    ResourceBudgetDecision,
    ResourceBudgetDomain,
    ResourceBudgetEvaluationContext,
    ResourceBudgetRationale,
    ResourceBudgetScope,
    ResourceBudgetUnit,
    ResourceUsageObservation,
    ResourceUsageSource,
    ResourceUsageStatus,
    WorktreeStatus,
)
from agentic_engineering_os.infrastructure import (
    ExecutionStateStore,
    GitAdapter,
    IncidentEventJournal,
    MissionStateStore,
    OperationalEventStore,
    OperationalEventStoreError,
    PersistenceError,
    ProjectConfigurationLoader,
    ProjectStateStore,
    WorktreeRegistryStore,
)
from agentic_engineering_os.infrastructure._negative_outcome_store import (
    _NegativeOutcomeStore,
)


DIAGNOSTIC_COMMANDS = frozenset({"health", "metrics", "incidents", "diagnose"})
MAX_DIAGNOSTIC_INCIDENTS = 256
MAX_DIAGNOSTIC_EVENTS = 1_024


class OperatorDiagnosticError(RuntimeError):
    """A diagnostic request cannot be evaluated safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class DiagnosticCommandResult:
    status: str
    result: object
    exit_code: int


@dataclass(frozen=True, slots=True)
class _DiagnosticEvaluation:
    repository_root: Path
    project_id: str
    repository_head: str
    evaluated_at: datetime
    metrics: object
    metrics_input: MetricsHealthInput
    health: object
    governance: object | None
    budgets: object | None
    incidents: object | None
    event_store_status: str
    event_count: int | None
    retention_exhausted: bool | None


def add_diagnostic_subparsers(subparsers: argparse._SubParsersAction) -> None:
    """Register the bounded P6.9 surface on the existing parser."""

    health = subparsers.add_parser(
        "health", help="evaluate operational health (read-only)",
        description="Evaluate Health facts read-only; HEALTHY is not Certification.",
    )
    _base_arguments(health)
    _mission_arguments(health)

    metrics = subparsers.add_parser(
        "metrics", help="compute runtime metrics (read-only)",
        description="Compute bounded runtime metrics without granting authority.",
    )
    _base_arguments(metrics)
    _metric_scope_arguments(metrics)

    incidents = subparsers.add_parser(
        "incidents", help="inspect operational incidents (read-only)",
        description="Inspect incidents read-only; observation does not perform recovery.",
    )
    _base_arguments(incidents)
    _mission_arguments(incidents)
    incidents.add_argument("--incident", help="inspect one exact incident ID")

    diagnose = subparsers.add_parser(
        "diagnose", help="aggregate operator diagnostics (read-only)",
        description=(
            "Aggregate Health, metrics, governance constraints, budgets and incidents. "
            "Governance ALLOW is not Control Plane authorization."
        ),
    )
    _base_arguments(diagnose)
    _mission_arguments(diagnose)


def execute_diagnostic_command(repository: Path, arguments: argparse.Namespace) -> DiagnosticCommandResult:
    """Evaluate one diagnostic command without writing repository state."""

    evaluation = _evaluate_repository(repository, arguments)
    command = arguments.command
    if command == "health":
        healthy = evaluation.health.global_state is HealthState.HEALTHY
        return DiagnosticCommandResult(
            evaluation.health.global_state.value,
            evaluation.health,
            0 if healthy else 2,
        )
    if command == "metrics":
        complete = evaluation.metrics.status is MetricsSnapshotStatus.COMPLETE
        return DiagnosticCommandResult(
            evaluation.metrics.status.value,
            evaluation.metrics,
            0 if complete else 2,
        )
    if command == "incidents":
        assert evaluation.incidents is not None
        records = evaluation.incidents.records
        requested = arguments.incident
        if requested is not None:
            matches = tuple(item for item in records if item.incident_id == requested)
            if len(matches) != 1:
                raise OperatorDiagnosticError(
                    "INCIDENT_NOT_FOUND", "the exact incident is absent from the current scope"
                )
            payload: object = matches[0]
            attention = matches[0].state is not IncidentState.RESOLVED
        else:
            payload = {
                "count": len(records),
                "records": records,
            }
            attention = any(item.state is not IncidentState.RESOLVED for item in records)
        return DiagnosticCommandResult(
            "ATTENTION_REQUIRED" if attention else "OK",
            payload,
            2 if attention else 0,
        )
    if command == "diagnose":
        assert evaluation.governance is not None
        assert evaluation.budgets is not None
        assert evaluation.incidents is not None
        governance_label = (
            "NO_ADDITIONAL_GOVERNANCE_BLOCK"
            if evaluation.governance.decision is GovernanceDecision.ALLOW
            else "GOVERNANCE_CONSTRAINT_OBSERVED"
        )
        budget_attention = any(
            item.decision
            not in {ResourceBudgetDecision.WITHIN_BUDGET, ResourceBudgetDecision.NEAR_LIMIT}
            for item in evaluation.budgets.decisions
        )
        active_incidents = tuple(
            item
            for item in evaluation.incidents.records
            if item.state is not IncidentState.RESOLVED
        )
        attention = (
            evaluation.health.global_state is not HealthState.HEALTHY
            or evaluation.metrics.status is not MetricsSnapshotStatus.COMPLETE
            or evaluation.governance.decision is not GovernanceDecision.ALLOW
            or budget_attention
            or bool(active_incidents)
        )
        payload = {
            "authority_notice": "DIAGNOSTIC_ONLY_NOT_AUTHORIZATION",
            "scope": {
                "project_id": evaluation.project_id,
                "repository_head": evaluation.repository_head,
                "repository_root": str(evaluation.repository_root),
                "mission_id": evaluation.health.scope.mission_id,
                "workflow_generation": evaluation.health.scope.workflow_generation,
            },
            "health": evaluation.health,
            "metrics": evaluation.metrics,
            "governance": {
                "meaning": governance_label,
                "decision_set": evaluation.governance,
            },
            "budgets": {
                "meaning": "ONE_EXECUTION_DIAGNOSTIC_PROBE_NOT_AUTHORIZATION",
                "decision_set": evaluation.budgets,
            },
            "incidents": {
                "active_count": len(active_incidents),
                "records": evaluation.incidents.records,
            },
            "store_diagnostics": {
                "operational_event_store": evaluation.event_store_status,
                "event_count": evaluation.event_count,
                "retention_exhausted": evaluation.retention_exhausted,
            },
        }
        return DiagnosticCommandResult(
            "ATTENTION_REQUIRED" if attention else "OK",
            payload,
            2 if attention else 0,
        )
    raise OperatorDiagnosticError("UNKNOWN_COMMAND", "diagnostic command is unsupported")


def _base_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", default=".", help="target repository root")
    parser.add_argument("--project-id", help="expected configured project identity")
    parser.add_argument("--json", action="store_true", help="emit deterministic compact JSON")


def _mission_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mission", help="exact mission identity")
    parser.add_argument("--generation", type=int, help="exact workflow generation")


def _metric_scope_arguments(parser: argparse.ArgumentParser) -> None:
    _mission_arguments(parser)
    parser.add_argument("--story", help="exact User Story identity")
    parser.add_argument("--role", choices=tuple(item.value for item in MissionRole))
    parser.add_argument("--execution", help="exact execution identity")


def _evaluate_repository(repository: Path, arguments: argparse.Namespace) -> _DiagnosticEvaluation:
    now = datetime.now(timezone.utc)
    try:
        git = GitAdapter(repository).observe_read_only()
        configuration = ProjectConfigurationLoader(repository).load()
    except Exception as error:
        raise OperatorDiagnosticError(
            "REPOSITORY_CONTEXT_UNAVAILABLE", "Git or project configuration is unavailable"
        ) from error
    if git.top_level != repository:
        raise OperatorDiagnosticError("REPOSITORY_SCOPE_MISMATCH", "Git root differs from target")
    requested_project = getattr(arguments, "project_id", None)
    if requested_project is not None and requested_project != configuration.project_id:
        raise OperatorDiagnosticError(
            "PROJECT_SCOPE_MISMATCH", "requested project identity differs from configuration"
        )

    mission, mission_active = _mission_scope(repository, arguments)
    mission_id = mission.mission_id if mission_active else None
    generation = mission.workflow_generation if mission_active else None
    role = _role(getattr(arguments, "role", None))
    metrics_scope = MetricsScope(
        configuration.project_id,
        mission_id,
        generation,
        user_story_id=getattr(arguments, "story", None),
        role=role,
        execution_id=getattr(arguments, "execution", None),
    )
    event_store = OperationalEventStore(repository)
    metrics = MetricsEngine().compute_from_store(event_store, metrics_scope)
    metrics_input = MetricsHealthInput(metrics, now, git.head_commit)

    events, event_status, retention = _event_store_facts(event_store)
    observations, parallel = _health_observations(
        repository,
        configuration.project_id,
        git,
        now,
        mission if mission_active else None,
        event_status,
    )
    health_context = HealthEvaluationContext(
        HealthScope(configuration.project_id, git.head_commit, mission_id, generation),
        now,
        mission_active,
        parallel,
        observations,
        MetricsHealthInput(
            MetricsEngine().compute_from_store(
                event_store, MetricsScope(configuration.project_id, mission_id, generation)
            ),
            now,
            git.head_commit,
        ),
    )
    health = HealthEvaluationEngine().evaluate(health_context)
    governance = None
    budgets = None
    incidents = None
    if arguments.command in {"incidents", "diagnose"}:
        governance = _governance(
            configuration.project_id,
            git.head_commit,
            now,
            health,
            health_context.metrics,
        )
        budgets = _budgets(
            repository, configuration, git.head_commit, now, mission_id, generation
        )
        incidents = _incidents(
            event_store,
            configuration.project_id,
            git.head_commit,
            now,
            mission_id,
            generation,
            health,
            governance,
            budgets,
            health_context.metrics,
            events,
            event_status == "AVAILABLE",
        )
    return _DiagnosticEvaluation(
        repository,
        configuration.project_id,
        git.head_commit,
        now,
        metrics,
        metrics_input,
        health,
        governance,
        budgets,
        incidents,
        event_status,
        len(events) if event_status == "AVAILABLE" else None,
        retention,
    )


def _mission_scope(repository: Path, arguments: argparse.Namespace):
    requested_id = getattr(arguments, "mission", None)
    requested_generation = getattr(arguments, "generation", None)
    if (requested_id is None) != (requested_generation is None):
        raise OperatorDiagnosticError(
            "INCOMPLETE_MISSION_SCOPE", "mission and generation must be supplied together"
        )
    store = MissionStateStore(repository)
    try:
        mission = store.load()
    except PersistenceError as error:
        if error.code == "MISSION_ABSENT" and requested_id is None:
            return None, False
        raise OperatorDiagnosticError(
            "MISSION_CONTEXT_UNAVAILABLE", "mission state cannot be inspected safely"
        ) from error
    if requested_id is not None:
        if (mission.mission_id, mission.workflow_generation) != (
            requested_id,
            requested_generation,
        ):
            raise OperatorDiagnosticError(
                "MISSION_SCOPE_MISMATCH", "requested mission scope differs from persisted state"
            )
        return mission, True
    return mission, mission.status in {MissionStatus.ACTIVE, MissionStatus.BLOCKED}


def _event_store_facts(store: OperationalEventStore):
    try:
        events = store.read()
        retention = store.retention_exhausted()
        return events, "DEGRADED" if retention else "AVAILABLE", retention
    except OperationalEventStoreError:
        return (), "UNAVAILABLE", None


def _health_observations(repository, project_id, git, now, mission, event_status):
    mission_id = mission.mission_id if mission is not None else None
    generation = mission.workflow_generation if mission is not None else None
    bound = {"mission_id": mission_id, "workflow_generation": generation}
    observations: list[HealthObservation] = []

    try:
        state = ProjectStateStore(repository).load()
        project_condition = (
            HealthCondition.AVAILABLE
            if state.project_id == project_id
            else HealthCondition.UNKNOWN
        )
    except PersistenceError:
        project_condition = HealthCondition.UNAVAILABLE
    observations.append(_observation(HealthSource.PROJECT_STATE_STORE, project_condition, project_id, now, git.head_commit))

    event_condition = {
        "AVAILABLE": HealthCondition.AVAILABLE,
        "DEGRADED": HealthCondition.SATURATED,
        "UNAVAILABLE": HealthCondition.CORRUPTED,
    }[event_status]
    observations.append(_observation(HealthSource.OPERATIONAL_EVENT_STORE, event_condition, project_id, now, git.head_commit))
    persistence_condition = (
        HealthCondition.AVAILABLE
        if project_condition is HealthCondition.AVAILABLE and event_status != "UNAVAILABLE"
        else HealthCondition.UNKNOWN
        if event_status == "UNAVAILABLE" or project_condition is HealthCondition.UNKNOWN
        else HealthCondition.FAILED
    )
    observations.append(
        _observation(
            HealthSource.PERSISTENCE_DIAGNOSTIC,
            persistence_condition,
            project_id,
            now,
            git.head_commit,
        )
    )
    observations.append(_observation(HealthSource.PROJECT_CONFIGURATION, HealthCondition.VALID, project_id, now, git.head_commit))
    runtime_available = Path(sys.executable).is_file()
    observations.append(
        _observation(
            HealthSource.CODEX_RUNTIME,
            HealthCondition.AVAILABLE if runtime_available else HealthCondition.UNKNOWN,
            project_id,
            now,
            git.head_commit,
            **bound,
        )
    )
    parallel = False
    if mission is not None:
        observations.append(
            _observation(
                HealthSource.MISSION_STATE_STORE,
                HealthCondition.AVAILABLE,
                project_id,
                now,
                mission.observed_commit,
                **bound,
            )
        )
        try:
            ledger = ExecutionStateStore(repository).load()
            pending = any(
                item.status in {CodexExecutionStatus.PLANNED, CodexExecutionStatus.RUNNING}
                for item in ledger.records
                if (item.mission_id, item.workflow_generation) == (mission_id, generation)
            )
            ledger_condition = HealthCondition.RECOVERY_PENDING if pending else HealthCondition.CLEAR
        except PersistenceError:
            ledger_condition = HealthCondition.UNAVAILABLE
        observations.append(_observation(HealthSource.EXECUTION_LEDGER, ledger_condition, project_id, now, git.head_commit, **bound))
        try:
            remediation_pending = _NegativeOutcomeStore(repository)._pending(mission_id) is not None
            remediation_condition = HealthCondition.PENDING if remediation_pending else HealthCondition.CLEAR
        except PersistenceError:
            remediation_condition = HealthCondition.UNKNOWN
        observations.append(_observation(HealthSource.REMEDIATION_STORE, remediation_condition, project_id, now, git.head_commit, **bound))
        try:
            registry = WorktreeRegistryStore(repository).load()
            active = tuple(
                item
                for item in registry.assignments
                if item.mission_id == mission_id
                and item.workflow_generation == generation
                and item.status is WorktreeStatus.ACTIVE
            )
            parallel = bool(active)
            if parallel:
                actual_paths = {str(item.path).casefold() for item in git.worktrees}
                reconciled = all(item.worktree_path.casefold() in actual_paths for item in active)
                observations.append(
                    _observation(
                        HealthSource.GIT_RECONCILIATION,
                        HealthCondition.RECONCILED if reconciled else HealthCondition.DRIFT,
                        project_id,
                        now,
                        git.head_commit,
                        **bound,
                    )
                )
        except PersistenceError:
            parallel = False
    return tuple(observations), parallel


def _observation(source, condition, project_id, now, head, *, mission_id=None, workflow_generation=None):
    return HealthObservation(
        source,
        condition,
        project_id,
        now,
        f"operator-diagnostics:{source.value.lower()}",
        head,
        mission_id,
        workflow_generation,
    )


def _governance(project_id, head, now, health, metrics_input):
    operation = GovernedOperation.VERIFICATION
    scope = GovernanceScope(project_id, head, health.scope.mission_id, health.scope.workflow_generation)
    policy = GovernancePolicy(
        "operator-diagnostics-health-floor",
        "1.0",
        GovernancePolicyClass.HARD_SAFETY_POLICY,
        GovernancePolicyDomain.HEALTH_GATING,
        True,
        GovernancePolicyScope(project_id, tuple(GovernedOperation), health.scope.mission_id, health.scope.workflow_generation),
        GovernanceCondition.HEALTH_BLOCKED,
        GovernanceDecision.BLOCK,
        GovernanceRationale.PROTECT_SAFETY_INVARIANTS,
    )
    return GovernancePolicyEvaluator().evaluate(
        GovernanceEvaluationContext(scope, operation, now, health, metrics_input, (policy,))
    )


def _budgets(repository, configuration, head, now, mission_id, generation):
    scope = ResourceBudgetScope(configuration.project_id, head, str(repository), mission_id, generation)
    budget = ResourceBudget(
        "project-codex-concurrency",
        "1.0",
        ResourceBudgetDomain.CODEX_CONCURRENCY,
        scope,
        configuration.codex_constraints.maximum_parallel_executions,
        ResourceBudgetUnit.EXECUTIONS,
        GovernancePolicyClass.HARD_SAFETY_POLICY,
        "project-configuration",
        ResourceBudgetApplicability.APPLICABLE,
        ResourceBudgetRationale.PROJECT_CAPACITY,
    )
    try:
        ledger = ExecutionStateStore(repository).load()
        active = tuple(
            sorted(
                item.execution_id
                for item in ledger.records
                if item.status in {CodexExecutionStatus.PLANNED, CodexExecutionStatus.RUNNING}
                and (mission_id is None or (item.mission_id, item.workflow_generation) == (mission_id, generation))
            )
        )
        usage_status = ResourceUsageStatus.COMPLETE
        current = len(active)
        roots = tuple(str(repository) for _ in active)
    except PersistenceError:
        usage_status = ResourceUsageStatus.UNAVAILABLE
        current = None
        active = ()
        roots = ()
    usage = ResourceUsageObservation(
        ResourceBudgetDomain.CODEX_CONCURRENCY,
        ResourceBudgetUnit.EXECUTIONS,
        usage_status,
        ResourceUsageSource.EXECUTION_STATE_STORE,
        "execution-state-store",
        scope,
        now,
        current,
        1,
        active,
        roots,
    )
    return ResourceBudgetEvaluator().evaluate(
        ResourceBudgetEvaluationContext(scope, GovernedOperation.EXECUTION, now, (budget,), (usage,))
    )


def _incidents(store, project_id, head, now, mission_id, generation, health, governance, budgets, metrics_input, events, events_complete):
    scope = IncidentScope(project_id, head, mission_id, generation)
    try:
        prior = IncidentEventJournal(store).latest(scope)
    except Exception as error:
        raise OperatorDiagnosticError(
            "INCIDENT_SOURCE_UNAVAILABLE", "incident history cannot be inspected safely"
        ) from error
    if len(prior) > MAX_DIAGNOSTIC_INCIDENTS:
        raise OperatorDiagnosticError("INCIDENT_LIMIT_EXCEEDED", "incident output exceeds policy")
    selected = tuple(
        item
        for item in events
        if item.project_id == project_id
        and item.source_component != "incident-manager"
        and item.correlation.mission_id == mission_id
        and item.correlation.workflow_generation == generation
    )
    if len(selected) > MAX_DIAGNOSTIC_EVENTS:
        selected = selected[:MAX_DIAGNOSTIC_EVENTS]
        events_complete = False
    return IncidentManager().evaluate(
        IncidentEvaluationContext(
            scope,
            now,
            health,
            governance,
            budgets,
            metrics_input,
            selected,
            events_complete,
            (),
            True,
            prior,
        )
    )


def _role(value: str | None) -> MissionRole | None:
    return MissionRole(value) if value is not None else None
