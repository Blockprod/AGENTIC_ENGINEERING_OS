"""Pure evaluation and bounded observation for operational resource budgets."""

from __future__ import annotations

import os
import stat
from datetime import datetime
from pathlib import Path

from agentic_engineering_os.domain import (
    MAX_RESOURCE_VALUE,
    RESOURCE_BUDGET_MAX_OBSERVATION_AGE,
    RESOURCE_BUDGET_SCHEMA_VERSION,
    GovernedOperation,
    GovernancePolicyClass,
    ResourceBudget,
    ResourceBudgetApplicability,
    ResourceBudgetDecision,
    ResourceBudgetDecisionSet,
    ResourceBudgetDomain,
    ResourceBudgetDomainDecision,
    ResourceBudgetEvaluationContext,
    ResourceBudgetReason,
    ResourceBudgetScope,
    ResourceBudgetUnit,
    ResourceUsageObservation,
    ResourceUsageSource,
    ResourceUsageStatus,
    allowed_usage_sources,
    expected_unit,
    resource_budget_decision_fingerprint,
)


class ResourceBudgetEvaluationError(ValueError):
    """The caller did not provide a valid typed resource budget context."""


class ResourceBudgetEvaluator:
    """Evaluate admissions without reserving resources or mutating workflows."""

    def evaluate(
        self, context: ResourceBudgetEvaluationContext
    ) -> ResourceBudgetDecisionSet:
        if not isinstance(context, ResourceBudgetEvaluationContext):
            raise ResourceBudgetEvaluationError(
                "context must be ResourceBudgetEvaluationContext"
            )
        domains = sorted(
            {
                item.domain
                for item in context.budgets
                if item.applicability is ResourceBudgetApplicability.APPLICABLE
            }
            | {item.domain for item in context.usage},
            key=lambda item: item.value,
        )
        if not domains:
            raise ResourceBudgetEvaluationError("no applicable budget domain was provided")
        decisions = tuple(self._evaluate_domain(context, domain) for domain in domains)
        fingerprint = resource_budget_decision_fingerprint(
            context.scope, context.operation, context.evaluated_at, decisions
        )
        return ResourceBudgetDecisionSet(
            RESOURCE_BUDGET_SCHEMA_VERSION,
            context.scope,
            context.operation,
            context.evaluated_at,
            decisions,
            fingerprint,
        )

    def _evaluate_domain(
        self,
        context: ResourceBudgetEvaluationContext,
        domain: ResourceBudgetDomain,
    ) -> ResourceBudgetDomainDecision:
        budgets = tuple(
            sorted(
                (
                    item
                    for item in context.budgets
                    if item.domain is domain
                    and item.applicability is ResourceBudgetApplicability.APPLICABLE
                ),
                key=lambda item: (item.limit, item.policy_class.value, item.budget_id),
            )
        )
        observation = next((item for item in context.usage if item.domain is domain), None)
        budget_ids = tuple(sorted({f"{item.budget_id}:{item.version}" for item in budgets})) or ("missing-budget",)
        source_ids = (
            (observation.source_identity,)
            if observation is not None
            else ("missing-usage",)
        )
        reasons: set[ResourceBudgetReason] = set()

        non_preference = tuple(
            item
            for item in budgets
            if item.policy_class is not GovernancePolicyClass.OPERATOR_PREFERENCE
        )
        if not non_preference:
            reasons.add(ResourceBudgetReason.MISSING_NON_PREFERENCE_CEILING)
        scope_mismatch = any(item.scope != context.scope for item in budgets)
        if scope_mismatch:
            reasons.add(ResourceBudgetReason.BUDGET_SCOPE_MISMATCH)

        effective_limit = min((item.limit for item in budgets), default=None)
        hard_limits = tuple(
            item.limit
            for item in budgets
            if item.policy_class is GovernancePolicyClass.HARD_SAFETY_POLICY
        )
        if hard_limits:
            hard_ceiling = min(hard_limits)
            if any(
                item.limit > hard_ceiling
                for item in budgets
                if item.policy_class is GovernancePolicyClass.OPERATOR_PREFERENCE
            ):
                reasons.add(ResourceBudgetReason.PREFERENCE_ABOVE_CEILING_IGNORED)
            if any(item.limit > hard_ceiling for item in non_preference):
                reasons.add(ResourceBudgetReason.HIGHER_LIMIT_IGNORED)

        usage_problem = self._usage_problem(context, domain, observation)
        if usage_problem is not None:
            reasons.add(usage_problem)
        if observation is None:
            reasons.add(ResourceBudgetReason.MISSING_USAGE)

        if reasons & {
            ResourceBudgetReason.MISSING_NON_PREFERENCE_CEILING,
            ResourceBudgetReason.BUDGET_SCOPE_MISMATCH,
            ResourceBudgetReason.USAGE_UNKNOWN,
            ResourceBudgetReason.USAGE_UNAVAILABLE,
            ResourceBudgetReason.USAGE_STALE,
            ResourceBudgetReason.USAGE_SCOPE_MISMATCH,
            ResourceBudgetReason.USAGE_SOURCE_INCOMPATIBLE,
            ResourceBudgetReason.USAGE_UNIT_MISMATCH,
            ResourceBudgetReason.DUPLICATE_ACTIVE_IDENTITY,
            ResourceBudgetReason.ACTIVE_IDENTITY_COUNT_MISMATCH,
            ResourceBudgetReason.CROSS_REPOSITORY_RESOURCE,
            ResourceBudgetReason.INVALID_REQUEST,
            ResourceBudgetReason.ARITHMETIC_OVERFLOW,
            ResourceBudgetReason.MISSING_USAGE,
        }:
            return _unknown_decision(domain, effective_limit, observation, budget_ids, source_ids, reasons)

        assert observation is not None
        assert observation.current_value is not None
        assert effective_limit is not None
        future = observation.current_value + observation.requested_value
        if future > MAX_RESOURCE_VALUE:
            reasons.add(ResourceBudgetReason.ARITHMETIC_OVERFLOW)
            return _unknown_decision(domain, effective_limit, observation, budget_ids, source_ids, reasons)
        decision, reason = _classify(future, effective_limit)
        reasons.add(reason)
        return ResourceBudgetDomainDecision(
            domain,
            expected_unit(domain),
            decision,
            effective_limit,
            observation.current_value,
            observation.requested_value,
            future,
            budget_ids,
            source_ids,
            tuple(sorted(reasons, key=lambda item: item.value)),
        )

    @staticmethod
    def _usage_problem(
        context: ResourceBudgetEvaluationContext,
        domain: ResourceBudgetDomain,
        observation: ResourceUsageObservation | None,
    ) -> ResourceBudgetReason | None:
        if observation is None:
            return None
        if observation.status is ResourceUsageStatus.UNKNOWN:
            return ResourceBudgetReason.USAGE_UNKNOWN
        if observation.status is ResourceUsageStatus.UNAVAILABLE:
            return ResourceBudgetReason.USAGE_UNAVAILABLE
        if observation.scope != context.scope:
            return ResourceBudgetReason.USAGE_SCOPE_MISMATCH
        age = context.evaluated_at - observation.observed_at
        if age.total_seconds() < 0 or age > RESOURCE_BUDGET_MAX_OBSERVATION_AGE:
            return ResourceBudgetReason.USAGE_STALE
        if observation.source not in allowed_usage_sources(domain):
            return ResourceBudgetReason.USAGE_SOURCE_INCOMPATIBLE
        if observation.unit is not expected_unit(domain):
            return ResourceBudgetReason.USAGE_UNIT_MISMATCH
        if domain in {
            ResourceBudgetDomain.CODEX_CONCURRENCY,
            ResourceBudgetDomain.WORKTREE_CONCURRENCY,
        }:
            if observation.requested_value <= 0:
                return ResourceBudgetReason.INVALID_REQUEST
            if len(set(observation.active_identities)) != len(observation.active_identities):
                return ResourceBudgetReason.DUPLICATE_ACTIVE_IDENTITY
            if len(observation.active_identities) != observation.current_value:
                return ResourceBudgetReason.ACTIVE_IDENTITY_COUNT_MISMATCH
            expected_root = _path_key(context.scope.repository_root)
            if any(_path_key(root) != expected_root for root in observation.repository_roots):
                return ResourceBudgetReason.CROSS_REPOSITORY_RESOURCE
            if len(observation.repository_roots) != observation.current_value:
                return ResourceBudgetReason.ACTIVE_IDENTITY_COUNT_MISMATCH
        elif domain is ResourceBudgetDomain.EXECUTION_TIME and observation.requested_value <= 0:
            return ResourceBudgetReason.INVALID_REQUEST
        elif domain is ResourceBudgetDomain.REMEDIATION_GENERATIONS and observation.requested_value != 1:
            return ResourceBudgetReason.INVALID_REQUEST
        return None


class BoundedStorageUsageObserver:
    """Measure a repository-contained tree without following reparse points."""

    def __init__(self, *, max_entries: int = 10_000) -> None:
        if not isinstance(max_entries, int) or isinstance(max_entries, bool) or max_entries <= 0:
            raise ValueError("max_entries must be a positive integer")
        self._max_entries = max_entries

    def observe(
        self,
        *,
        scope: ResourceBudgetScope,
        domain: ResourceBudgetDomain,
        path: str | Path,
        observed_at: datetime,
        requested_bytes: int,
        source_identity: str,
    ) -> ResourceUsageObservation:
        if domain not in {
            ResourceBudgetDomain.RUNTIME_STORAGE,
            ResourceBudgetDomain.OBSERVABILITY_STORAGE,
        }:
            raise ResourceBudgetEvaluationError("storage observer requires a storage domain")
        candidate = Path(path)
        status = ResourceUsageStatus.COMPLETE
        current: int | None = 0
        try:
            lexical_root = Path(scope.repository_root).absolute()
            lexical_target = candidate.absolute()
            if (
                not candidate.is_absolute()
                or ".." in candidate.parts
                or not lexical_target.is_relative_to(lexical_root)
            ):
                raise OSError("storage path is not a traversal-free repository path")
            _reject_reparse_chain(lexical_root, lexical_target)
            root = lexical_root.resolve(strict=True)
            target = lexical_target.resolve(strict=True)
            if not target.is_relative_to(root):
                raise OSError("storage path escapes repository root")
            total = 0
            entries = 0
            pending = [target]
            while pending:
                current_path = pending.pop()
                info = current_path.lstat()
                if _is_reparse(info):
                    raise OSError("storage path contains a symlink or junction")
                entries += 1
                if entries > self._max_entries:
                    raise OSError("storage scan exceeds entry bound")
                if stat.S_ISDIR(info.st_mode):
                    pending.extend(current_path.iterdir())
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                    if total > MAX_RESOURCE_VALUE:
                        raise OverflowError("storage usage exceeds numeric bound")
            current = total
        except (OSError, OverflowError, RuntimeError):
            status = ResourceUsageStatus.UNKNOWN
            current = None
        return ResourceUsageObservation(
            domain,
            ResourceBudgetUnit.BYTES,
            status,
            ResourceUsageSource.FILESYSTEM,
            source_identity,
            scope,
            observed_at,
            current,
            requested_bytes,
        )


def _unknown_decision(
    domain: ResourceBudgetDomain,
    effective_limit: int | None,
    observation: ResourceUsageObservation | None,
    budget_ids: tuple[str, ...],
    source_ids: tuple[str, ...],
    reasons: set[ResourceBudgetReason],
) -> ResourceBudgetDomainDecision:
    return ResourceBudgetDomainDecision(
        domain,
        expected_unit(domain),
        ResourceBudgetDecision.UNKNOWN,
        effective_limit,
        observation.current_value if observation is not None else None,
        observation.requested_value if observation is not None else None,
        None,
        budget_ids,
        source_ids,
        tuple(sorted(reasons, key=lambda item: item.value)),
    )


def _classify(future: int, limit: int) -> tuple[ResourceBudgetDecision, ResourceBudgetReason]:
    if future > limit:
        return ResourceBudgetDecision.LIMIT_EXCEEDED, ResourceBudgetReason.USAGE_EXCEEDS_LIMIT
    if future == limit:
        return ResourceBudgetDecision.LIMIT_REACHED, ResourceBudgetReason.USAGE_REACHES_LIMIT
    if limit > 0 and future * 5 >= limit * 4:
        return ResourceBudgetDecision.NEAR_LIMIT, ResourceBudgetReason.USAGE_NEAR_LIMIT
    return ResourceBudgetDecision.WITHIN_BUDGET, ResourceBudgetReason.USAGE_WITHIN_BUDGET


def _reject_reparse_chain(root: Path, target: Path) -> None:
    current = target
    while True:
        if _is_reparse(current.lstat()):
            raise OSError("storage boundary contains a symlink or junction")
        if current == root:
            return
        parent = current.parent
        if parent == current:
            raise OSError("storage path is not contained by repository root")
        current = parent


def _is_reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse)


def _path_key(value: str) -> str:
    return os.path.normcase(str(Path(value).resolve(strict=False))).casefold()
