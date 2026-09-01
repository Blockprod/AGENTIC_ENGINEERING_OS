from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import agentic_engineering_os.application.resource_budget as resource_budget_module
import pytest

from agentic_engineering_os.application import (
    BoundedStorageUsageObserver,
    ResourceBudgetEvaluationError,
    ResourceBudgetEvaluator,
)
from agentic_engineering_os.domain import (
    MAX_RESOURCE_VALUE,
    GovernedOperation,
    GovernancePolicyClass,
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    ResourceBudget,
    ResourceBudgetApplicability,
    ResourceBudgetDecision,
    ResourceBudgetDomain,
    ResourceBudgetEvaluationContext,
    ResourceBudgetRationale,
    ResourceBudgetReason,
    ResourceBudgetScope,
    ResourceBudgetUnit,
    ResourceUsageObservation,
    ResourceUsageSource,
    ResourceUsageStatus,
)


NOW = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
HEAD = "a" * 40
PROJECT = "project-one"
MISSION = "mission-one"
GENERATION = 3


def _scope(root: Path, *, project: str = PROJECT, generation: int = GENERATION):
    return ResourceBudgetScope(project, HEAD, str(root), MISSION, generation)


def _unit(domain: ResourceBudgetDomain) -> ResourceBudgetUnit:
    return {
        ResourceBudgetDomain.CODEX_CONCURRENCY: ResourceBudgetUnit.EXECUTIONS,
        ResourceBudgetDomain.WORKTREE_CONCURRENCY: ResourceBudgetUnit.WORKTREES,
        ResourceBudgetDomain.EXECUTION_TIME: ResourceBudgetUnit.SECONDS,
        ResourceBudgetDomain.REMEDIATION_GENERATIONS: ResourceBudgetUnit.GENERATIONS,
        ResourceBudgetDomain.RUNTIME_STORAGE: ResourceBudgetUnit.BYTES,
        ResourceBudgetDomain.OBSERVABILITY_STORAGE: ResourceBudgetUnit.BYTES,
    }[domain]


def _source(domain: ResourceBudgetDomain) -> ResourceUsageSource:
    return {
        ResourceBudgetDomain.CODEX_CONCURRENCY: ResourceUsageSource.EXECUTION_STATE_STORE,
        ResourceBudgetDomain.WORKTREE_CONCURRENCY: ResourceUsageSource.WORKTREE_REGISTRY,
        ResourceBudgetDomain.EXECUTION_TIME: ResourceUsageSource.CODEX_RUNTIME,
        ResourceBudgetDomain.REMEDIATION_GENERATIONS: ResourceUsageSource.MISSION_STATE,
        ResourceBudgetDomain.RUNTIME_STORAGE: ResourceUsageSource.FILESYSTEM,
        ResourceBudgetDomain.OBSERVABILITY_STORAGE: ResourceUsageSource.EVENT_STORE_RETENTION,
    }[domain]


def _budget(
    root: Path,
    domain: ResourceBudgetDomain,
    limit: int,
    *,
    budget_id: str = "hard-budget",
    policy_class: GovernancePolicyClass = GovernancePolicyClass.HARD_SAFETY_POLICY,
    scope: ResourceBudgetScope | None = None,
) -> ResourceBudget:
    return ResourceBudget(
        budget_id,
        "1.0",
        domain,
        scope or _scope(root),
        limit,
        _unit(domain),
        policy_class,
        f"source-{budget_id}",
        ResourceBudgetApplicability.APPLICABLE,
        ResourceBudgetRationale.SAFETY_CEILING,
    )


def _usage(
    root: Path,
    domain: ResourceBudgetDomain,
    current: int | None,
    requested: int,
    *,
    status: ResourceUsageStatus = ResourceUsageStatus.COMPLETE,
    observed_at: datetime = NOW,
    scope: ResourceBudgetScope | None = None,
    identities: tuple[str, ...] | None = None,
    roots: tuple[str, ...] | None = None,
    source: ResourceUsageSource | None = None,
) -> ResourceUsageObservation:
    if domain in {
        ResourceBudgetDomain.CODEX_CONCURRENCY,
        ResourceBudgetDomain.WORKTREE_CONCURRENCY,
    }:
        count = current or 0
        identities = identities if identities is not None else tuple(
            f"active-{index}" for index in range(count)
        )
        roots = roots if roots is not None else (str(root),) * count
    return ResourceUsageObservation(
        domain,
        _unit(domain),
        status,
        source or _source(domain),
        f"usage-{domain.value.lower()}",
        scope or _scope(root),
        observed_at,
        current,
        requested,
        identities or (),
        roots or (),
    )


def _evaluate(
    root: Path,
    domain: ResourceBudgetDomain,
    limit: int,
    current: int | None,
    requested: int,
    **usage_options,
):
    context = ResourceBudgetEvaluationContext(
        _scope(root),
        GovernedOperation.EXECUTION,
        NOW,
        (_budget(root, domain, limit),),
        (_usage(root, domain, current, requested, **usage_options),),
    )
    return ResourceBudgetEvaluator().evaluate(context).decisions[0]


def test_codex_concurrency_under_limit(tmp_path: Path) -> None:
    result = _evaluate(tmp_path, ResourceBudgetDomain.CODEX_CONCURRENCY, 5, 1, 1)
    assert result.decision is ResourceBudgetDecision.WITHIN_BUDGET
    assert result.effective_future_value == 2


def test_concurrency_exactly_at_limit_is_admitted_as_reached(tmp_path: Path) -> None:
    result = _evaluate(tmp_path, ResourceBudgetDomain.CODEX_CONCURRENCY, 3, 2, 1)
    assert result.decision is ResourceBudgetDecision.LIMIT_REACHED
    assert result.effective_future_value == result.effective_limit


def test_concurrency_near_limit_uses_explicit_effective_limit_base(tmp_path: Path) -> None:
    result = _evaluate(tmp_path, ResourceBudgetDomain.CODEX_CONCURRENCY, 10, 7, 1)
    assert result.decision is ResourceBudgetDecision.NEAR_LIMIT


def test_hard_limit_exceeded_without_optimistic_oversubscription(tmp_path: Path) -> None:
    result = _evaluate(tmp_path, ResourceBudgetDomain.WORKTREE_CONCURRENCY, 2, 2, 1)
    assert result.decision is ResourceBudgetDecision.LIMIT_EXCEEDED


def test_unknown_usage_never_produces_within_budget(tmp_path: Path) -> None:
    result = _evaluate(
        tmp_path,
        ResourceBudgetDomain.CODEX_CONCURRENCY,
        5,
        None,
        1,
        status=ResourceUsageStatus.UNKNOWN,
    )
    assert result.decision is ResourceBudgetDecision.UNKNOWN
    assert ResourceBudgetReason.USAGE_UNKNOWN in result.reasons


def test_time_budget_uses_explicit_requested_timeout(tmp_path: Path) -> None:
    result = _evaluate(tmp_path, ResourceBudgetDomain.EXECUTION_TIME, 600, 0, 300)
    assert result.decision is ResourceBudgetDecision.WITHIN_BUDGET
    assert result.unit is ResourceBudgetUnit.SECONDS


def test_elapsed_plus_requested_time_is_exact(tmp_path: Path) -> None:
    result = _evaluate(tmp_path, ResourceBudgetDomain.EXECUTION_TIME, 600, 400, 200)
    assert result.decision is ResourceBudgetDecision.LIMIT_REACHED


def test_zero_time_request_is_unknown_instead_of_inventing_time(tmp_path: Path) -> None:
    result = _evaluate(tmp_path, ResourceBudgetDomain.EXECUTION_TIME, 600, 10, 0)
    assert result.decision is ResourceBudgetDecision.UNKNOWN
    assert ResourceBudgetReason.INVALID_REQUEST in result.reasons


def test_next_remediation_generation_is_checked_without_mutation(tmp_path: Path) -> None:
    result = _evaluate(
        tmp_path, ResourceBudgetDomain.REMEDIATION_GENERATIONS, 4, GENERATION, 1
    )
    assert result.decision is ResourceBudgetDecision.LIMIT_REACHED


def test_remediation_request_other_than_n_plus_one_fails_closed(tmp_path: Path) -> None:
    result = _evaluate(
        tmp_path, ResourceBudgetDomain.REMEDIATION_GENERATIONS, 5, GENERATION, 2
    )
    assert result.decision is ResourceBudgetDecision.UNKNOWN


def test_storage_budget_uses_factual_bytes(tmp_path: Path) -> None:
    runtime = tmp_path / ".agentic-engineering-os"
    runtime.mkdir()
    (runtime / "state.bin").write_bytes(b"12345")
    observation = BoundedStorageUsageObserver().observe(
        scope=_scope(tmp_path),
        domain=ResourceBudgetDomain.RUNTIME_STORAGE,
        path=runtime,
        observed_at=NOW,
        requested_bytes=3,
        source_identity="runtime-directory",
    )
    context = ResourceBudgetEvaluationContext(
        _scope(tmp_path),
        GovernedOperation.EXECUTION,
        NOW,
        (_budget(tmp_path, ResourceBudgetDomain.RUNTIME_STORAGE, 8),),
        (observation,),
    )
    result = ResourceBudgetEvaluator().evaluate(context).decisions[0]
    assert observation.current_value == 5
    assert result.decision is ResourceBudgetDecision.LIMIT_REACHED


def test_observability_storage_budget(tmp_path: Path) -> None:
    events = tmp_path / ".agentic-engineering-os" / "operational-events"
    events.mkdir(parents=True)
    (events / "segment-000001.jsonl").write_bytes(b"event\n")
    observation = BoundedStorageUsageObserver().observe(
        scope=_scope(tmp_path),
        domain=ResourceBudgetDomain.OBSERVABILITY_STORAGE,
        path=events,
        observed_at=NOW,
        requested_bytes=1,
        source_identity="event-segments",
    )
    result = ResourceBudgetEvaluator().evaluate(
        ResourceBudgetEvaluationContext(
            _scope(tmp_path),
            GovernedOperation.EXECUTION,
            NOW,
            (_budget(tmp_path, ResourceBudgetDomain.OBSERVABILITY_STORAGE, 8),),
            (observation,),
        )
    ).decisions[0]
    assert result.decision is ResourceBudgetDecision.NEAR_LIMIT


def test_most_restrictive_hard_limit_wins_independent_of_order(tmp_path: Path) -> None:
    first = _budget(tmp_path, ResourceBudgetDomain.CODEX_CONCURRENCY, 8, budget_id="machine")
    second = _budget(tmp_path, ResourceBudgetDomain.CODEX_CONCURRENCY, 3, budget_id="product")
    usage = _usage(tmp_path, ResourceBudgetDomain.CODEX_CONCURRENCY, 1, 1)
    make = lambda budgets: ResourceBudgetEvaluationContext(
        _scope(tmp_path), GovernedOperation.EXECUTION, NOW, budgets, (usage,)
    )
    one = ResourceBudgetEvaluator().evaluate(make((first, second)))
    two = ResourceBudgetEvaluator().evaluate(make((second, first)))
    assert one == two
    assert one.decisions[0].effective_limit == 3


def test_lower_operator_preference_can_only_tighten(tmp_path: Path) -> None:
    hard = _budget(tmp_path, ResourceBudgetDomain.CODEX_CONCURRENCY, 5)
    preference = _budget(
        tmp_path,
        ResourceBudgetDomain.CODEX_CONCURRENCY,
        2,
        budget_id="operator-lower",
        policy_class=GovernancePolicyClass.OPERATOR_PREFERENCE,
    )
    usage = _usage(tmp_path, ResourceBudgetDomain.CODEX_CONCURRENCY, 1, 1)
    result = ResourceBudgetEvaluator().evaluate(
        ResourceBudgetEvaluationContext(
            _scope(tmp_path), GovernedOperation.EXECUTION, NOW, (preference, hard), (usage,)
        )
    ).decisions[0]
    assert result.effective_limit == 2
    assert result.decision is ResourceBudgetDecision.LIMIT_REACHED


def test_operator_preference_above_hard_ceiling_is_ignored(tmp_path: Path) -> None:
    hard = _budget(tmp_path, ResourceBudgetDomain.CODEX_CONCURRENCY, 2)
    preference = _budget(
        tmp_path,
        ResourceBudgetDomain.CODEX_CONCURRENCY,
        100,
        budget_id="operator-higher",
        policy_class=GovernancePolicyClass.OPERATOR_PREFERENCE,
    )
    usage = _usage(tmp_path, ResourceBudgetDomain.CODEX_CONCURRENCY, 2, 1)
    result = ResourceBudgetEvaluator().evaluate(
        ResourceBudgetEvaluationContext(
            _scope(tmp_path), GovernedOperation.EXECUTION, NOW, (hard, preference), (usage,)
        )
    ).decisions[0]
    assert result.effective_limit == 2
    assert result.decision is ResourceBudgetDecision.LIMIT_EXCEEDED
    assert ResourceBudgetReason.PREFERENCE_ABOVE_CEILING_IGNORED in result.reasons


def test_preference_without_non_preference_ceiling_is_unknown(tmp_path: Path) -> None:
    preference = _budget(
        tmp_path,
        ResourceBudgetDomain.CODEX_CONCURRENCY,
        5,
        policy_class=GovernancePolicyClass.OPERATOR_PREFERENCE,
    )
    result = ResourceBudgetEvaluator().evaluate(
        ResourceBudgetEvaluationContext(
            _scope(tmp_path),
            GovernedOperation.EXECUTION,
            NOW,
            (preference,),
            (_usage(tmp_path, ResourceBudgetDomain.CODEX_CONCURRENCY, 1, 1),),
        )
    ).decisions[0]
    assert result.decision is ResourceBudgetDecision.UNKNOWN


@pytest.mark.parametrize("limit", [-1, MAX_RESOURCE_VALUE + 1, True, 1.5, float("nan"), float("inf")])
def test_negative_overflow_non_integer_and_non_finite_limits_are_refused(
    tmp_path: Path, limit: object
) -> None:
    with pytest.raises(ValueError, match="limit"):
        _budget(tmp_path, ResourceBudgetDomain.CODEX_CONCURRENCY, limit)  # type: ignore[arg-type]


def test_incoherent_unit_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unit"):
        replace(
            _budget(tmp_path, ResourceBudgetDomain.CODEX_CONCURRENCY, 2),
            unit=ResourceBudgetUnit.BYTES,
        )


def test_dynamic_expression_is_not_a_budget_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="limit"):
        _budget(tmp_path, ResourceBudgetDomain.EXECUTION_TIME, "2 * cpu")  # type: ignore[arg-type]


def test_secret_like_source_identity_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="source_identity"):
        replace(
            _budget(tmp_path, ResourceBudgetDomain.EXECUTION_TIME, 10),
            source_identity="token:synthetic",
        )


def test_stale_usage_fails_closed(tmp_path: Path) -> None:
    result = _evaluate(
        tmp_path,
        ResourceBudgetDomain.CODEX_CONCURRENCY,
        5,
        1,
        1,
        observed_at=NOW - timedelta(minutes=6),
    )
    assert result.decision is ResourceBudgetDecision.UNKNOWN
    assert ResourceBudgetReason.USAGE_STALE in result.reasons


@pytest.mark.parametrize(
    "scope",
    [
        lambda root: _scope(root, project="project-two"),
        lambda root: _scope(root, generation=GENERATION + 1),
    ],
)
def test_wrong_project_or_generation_usage_fails_closed(tmp_path: Path, scope) -> None:
    result = _evaluate(
        tmp_path,
        ResourceBudgetDomain.CODEX_CONCURRENCY,
        5,
        1,
        1,
        scope=scope(tmp_path),
    )
    assert result.decision is ResourceBudgetDecision.UNKNOWN
    assert ResourceBudgetReason.USAGE_SCOPE_MISMATCH in result.reasons


def test_wrong_project_budget_fails_closed(tmp_path: Path) -> None:
    foreign = _budget(
        tmp_path,
        ResourceBudgetDomain.CODEX_CONCURRENCY,
        5,
        scope=_scope(tmp_path, project="project-two"),
    )
    result = ResourceBudgetEvaluator().evaluate(
        ResourceBudgetEvaluationContext(
            _scope(tmp_path),
            GovernedOperation.EXECUTION,
            NOW,
            (foreign,),
            (_usage(tmp_path, ResourceBudgetDomain.CODEX_CONCURRENCY, 1, 1),),
        )
    ).decisions[0]
    assert result.decision is ResourceBudgetDecision.UNKNOWN


def test_duplicate_active_executions_fail_closed(tmp_path: Path) -> None:
    result = _evaluate(
        tmp_path,
        ResourceBudgetDomain.CODEX_CONCURRENCY,
        5,
        2,
        1,
        identities=("execution-one", "execution-one"),
    )
    assert result.decision is ResourceBudgetDecision.UNKNOWN
    assert ResourceBudgetReason.DUPLICATE_ACTIVE_IDENTITY in result.reasons


def test_cross_repository_worktrees_fail_closed(tmp_path: Path) -> None:
    foreign = tmp_path.parent / "foreign-repository"
    result = _evaluate(
        tmp_path,
        ResourceBudgetDomain.WORKTREE_CONCURRENCY,
        4,
        1,
        1,
        roots=(str(foreign),),
    )
    assert result.decision is ResourceBudgetDecision.UNKNOWN
    assert ResourceBudgetReason.CROSS_REPOSITORY_RESOURCE in result.reasons


def test_zero_concurrency_request_fails_closed(tmp_path: Path) -> None:
    result = _evaluate(tmp_path, ResourceBudgetDomain.CODEX_CONCURRENCY, 4, 1, 0)
    assert result.decision is ResourceBudgetDecision.UNKNOWN


def test_unavailable_storage_usage_is_unknown(tmp_path: Path) -> None:
    observation = BoundedStorageUsageObserver().observe(
        scope=_scope(tmp_path),
        domain=ResourceBudgetDomain.RUNTIME_STORAGE,
        path=tmp_path / "missing",
        observed_at=NOW,
        requested_bytes=0,
        source_identity="runtime-directory",
    )
    result = ResourceBudgetEvaluator().evaluate(
        ResourceBudgetEvaluationContext(
            _scope(tmp_path),
            GovernedOperation.EXECUTION,
            NOW,
            (_budget(tmp_path, ResourceBudgetDomain.RUNTIME_STORAGE, 100),),
            (observation,),
        )
    ).decisions[0]
    assert result.decision is ResourceBudgetDecision.UNKNOWN


def test_storage_path_outside_repository_is_unknown(tmp_path: Path) -> None:
    observation = BoundedStorageUsageObserver().observe(
        scope=_scope(tmp_path),
        domain=ResourceBudgetDomain.RUNTIME_STORAGE,
        path=tmp_path.parent,
        observed_at=NOW,
        requested_bytes=0,
        source_identity="outside-directory",
    )
    assert observation.status is ResourceUsageStatus.UNKNOWN


def test_symlink_or_junction_storage_path_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(resource_budget_module, "_is_reparse", lambda _info: True)
    observation = BoundedStorageUsageObserver().observe(
        scope=_scope(tmp_path),
        domain=ResourceBudgetDomain.RUNTIME_STORAGE,
        path=target,
        observed_at=NOW,
        requested_bytes=0,
        source_identity="linked-directory",
    )
    assert observation.status is ResourceUsageStatus.UNKNOWN


def test_bounded_storage_scan_fails_closed(tmp_path: Path) -> None:
    directory = tmp_path / "runtime"
    directory.mkdir()
    (directory / "one").write_bytes(b"1")
    observation = BoundedStorageUsageObserver(max_entries=1).observe(
        scope=_scope(tmp_path),
        domain=ResourceBudgetDomain.RUNTIME_STORAGE,
        path=directory,
        observed_at=NOW,
        requested_bytes=0,
        source_identity="bounded-directory",
    )
    assert observation.status is ResourceUsageStatus.UNKNOWN


def test_forged_within_budget_result_is_rejected(tmp_path: Path) -> None:
    exceeded = _evaluate(tmp_path, ResourceBudgetDomain.EXECUTION_TIME, 10, 8, 3)
    with pytest.raises(ValueError, match="contradicts"):
        replace(exceeded, decision=ResourceBudgetDecision.WITHIN_BUDGET)


def test_decision_set_is_immutable_and_non_authoritative(tmp_path: Path) -> None:
    context = ResourceBudgetEvaluationContext(
        _scope(tmp_path),
        GovernedOperation.EXECUTION,
        NOW,
        (_budget(tmp_path, ResourceBudgetDomain.EXECUTION_TIME, 20),),
        (_usage(tmp_path, ResourceBudgetDomain.EXECUTION_TIME, 0, 10),),
    )
    result = ResourceBudgetEvaluator().evaluate(context)
    with pytest.raises(FrozenInstanceError):
        result.operation = GovernedOperation.MERGE  # type: ignore[misc]
    for forbidden in (
        "reserve", "schedule", "save", "mutate", "to_gate", "to_evidence",
        "to_certification", "approve", "merge", "authorize",
    ):
        assert not hasattr(result, forbidden)


def test_budget_evaluation_cannot_mutate_workflow_generation(tmp_path: Path) -> None:
    mission = MissionState(
        "1.0",
        MISSION,
        GENERATION,
        MissionStatus.ACTIVE,
        MissionRole.IMPLEMENTER,
        "objective",
        "subject",
        OperatingStep.ACT,
        "next",
        HEAD,
        NOW,
    )
    before = mission.workflow_generation
    _evaluate(tmp_path, ResourceBudgetDomain.REMEDIATION_GENERATIONS, 5, GENERATION, 1)
    assert mission.workflow_generation == before


def test_multiple_domains_are_canonically_ordered_and_deterministic(tmp_path: Path) -> None:
    budgets = (
        _budget(tmp_path, ResourceBudgetDomain.EXECUTION_TIME, 100, budget_id="time"),
        _budget(tmp_path, ResourceBudgetDomain.CODEX_CONCURRENCY, 4, budget_id="codex"),
    )
    usages = (
        _usage(tmp_path, ResourceBudgetDomain.EXECUTION_TIME, 0, 20),
        _usage(tmp_path, ResourceBudgetDomain.CODEX_CONCURRENCY, 1, 1),
    )
    first = ResourceBudgetEvaluator().evaluate(
        ResourceBudgetEvaluationContext(_scope(tmp_path), GovernedOperation.EXECUTION, NOW, budgets, usages)
    )
    second = ResourceBudgetEvaluator().evaluate(
        ResourceBudgetEvaluationContext(_scope(tmp_path), GovernedOperation.EXECUTION, NOW, budgets[::-1], usages[::-1])
    )
    assert first == second
    assert tuple(item.domain.value for item in first.decisions) == tuple(
        sorted(item.domain.value for item in first.decisions)
    )


def test_wrong_evaluator_input_fails_closed() -> None:
    with pytest.raises(ResourceBudgetEvaluationError):
        ResourceBudgetEvaluator().evaluate(ResourceBudgetDecision.WITHIN_BUDGET)  # type: ignore[arg-type]
