"""Fail-closed resource admission immediately before Codex allocation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from agentic_engineering_os.domain import (
    GovernedOperation,
    GovernancePolicyClass,
    MissionState,
    ProjectConfiguration,
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
)

from .execution_state import CodexExecutionLedger, CodexExecutionStatus
from .resource_budget import ResourceBudgetEvaluator


class _Reader(Protocol):
    def load(self) -> object: ...


class ExecutionBudgetError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class ExecutionBudgetBoundary:
    """Re-observe the ledger and reject unsafe Codex concurrency allocations."""

    def __init__(
        self,
        *,
        repository_root: str | Path,
        configuration: ProjectConfiguration,
        mission_store: _Reader,
        execution_store: _Reader,
    ) -> None:
        self._root = Path(repository_root).resolve(strict=True)
        self._configuration = configuration
        self._missions = mission_store
        self._executions = execution_store
        self._evaluator = ResourceBudgetEvaluator()

    def authorize(self, *, requested: int, repository_head: str) -> None:
        if not isinstance(requested, int) or isinstance(requested, bool) or requested <= 0:
            raise ExecutionBudgetError(
                "RESOURCE_BUDGET_REQUEST_INVALID", "requested Codex slots must be positive"
            )
        mission = self._missions.load()
        ledger = self._executions.load()
        if not isinstance(mission, MissionState) or not isinstance(ledger, CodexExecutionLedger):
            raise ExecutionBudgetError(
                "RESOURCE_BUDGET_USAGE_UNKNOWN", "mission or execution ledger is unavailable"
            )
        if mission.observed_commit != repository_head:
            raise ExecutionBudgetError(
                "RESOURCE_BUDGET_SCOPE_MISMATCH", "mission and launch commits differ"
            )
        active = tuple(
            sorted(
                item.execution_id
                for item in ledger.records
                if item.status is CodexExecutionStatus.RUNNING
            )
        )
        active_roots = tuple(
            str(self._root)
            for _ in active
        )
        now = datetime.now(timezone.utc)
        scope = ResourceBudgetScope(
            self._configuration.project_id,
            repository_head,
            str(self._root),
            mission.mission_id,
            mission.workflow_generation,
        )
        limit = self._configuration.codex_constraints.maximum_parallel_executions
        budget = ResourceBudget(
            "project-codex-concurrency",
            "1.0",
            ResourceBudgetDomain.CODEX_CONCURRENCY,
            scope,
            limit,
            ResourceBudgetUnit.EXECUTIONS,
            GovernancePolicyClass.HARD_SAFETY_POLICY,
            "project-configuration",
            ResourceBudgetApplicability.APPLICABLE,
            ResourceBudgetRationale.PROJECT_CAPACITY,
        )
        usage = ResourceUsageObservation(
            ResourceBudgetDomain.CODEX_CONCURRENCY,
            ResourceBudgetUnit.EXECUTIONS,
            ResourceUsageStatus.COMPLETE,
            ResourceUsageSource.EXECUTION_STATE_STORE,
            "execution-ledger",
            scope,
            now,
            len(active),
            requested,
            active,
            active_roots,
        )
        decision = self._evaluator.evaluate(
            ResourceBudgetEvaluationContext(
                scope, GovernedOperation.EXECUTION, now, (budget,), (usage,)
            )
        ).decisions[0]
        if decision.decision not in {
            ResourceBudgetDecision.WITHIN_BUDGET,
            ResourceBudgetDecision.NEAR_LIMIT,
            ResourceBudgetDecision.LIMIT_REACHED,
        }:
            reasons = ",".join(item.value for item in decision.reasons)
            raise ExecutionBudgetError(
                f"RESOURCE_BUDGET_{decision.decision.value}",
                f"Codex allocation refused ({reasons})",
            )
