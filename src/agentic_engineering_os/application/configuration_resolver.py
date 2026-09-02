"""Pure, fail-closed resolution of configuration and policy layers."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

from agentic_engineering_os.domain import (
    CodexApprovalConstraint,
    CodexSandboxConstraint,
    GovernancePolicyClass,
    ProjectConfiguration,
    ResourceBudget,
    ResourceBudgetApplicability,
    ResourceBudgetDomain,
    to_dict,
)

from .codex_capabilities import (
    CodexCapability,
    CodexCapabilityAssessment,
    CodexCapabilityStatus,
)


CONFIGURATION_RESOLUTION_VERSION = "1.0"
MACHINE_FACT_MAX_AGE = timedelta(minutes=5)
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SENSITIVE = re.compile(
    r"(?:^|[._:/-])(?:api[_-]?key|password|secret|token|credential)(?:[._:/-]|$)",
    re.IGNORECASE,
)


class ConfigurationAuthorityClass(str, Enum):
    SYSTEM_INVARIANT = "SYSTEM_INVARIANT"
    PROJECT_CONFIGURATION = "PROJECT_CONFIGURATION"
    HARD_SAFETY_POLICY = "HARD_SAFETY_POLICY"
    OPERATIONAL_POLICY = "OPERATIONAL_POLICY"
    OPERATOR_PREFERENCE = "OPERATOR_PREFERENCE"
    MACHINE_FACT = "MACHINE_FACT"


class ConfigurationKey(str, Enum):
    MAX_CONCURRENCY = "MAX_CONCURRENCY"
    MAX_TIMEOUT_SECONDS = "MAX_TIMEOUT_SECONDS"
    MAX_REMEDIATION_GENERATIONS = "MAX_REMEDIATION_GENERATIONS"
    SANDBOX_MAXIMUM = "SANDBOX_MAXIMUM"
    MINIMUM_VERIFICATION_TIER = "MINIMUM_VERIFICATION_TIER"
    REQUIRE_CLEAN_GIT = "REQUIRE_CLEAN_GIT"
    REQUIRE_OBSERVABILITY = "REQUIRE_OBSERVABILITY"
    REQUIRE_STRICT_HEALTH = "REQUIRE_STRICT_HEALTH"
    REQUIRE_REVIEWER = "REQUIRE_REVIEWER"
    REQUIRE_HUMAN_AUTHORITY = "REQUIRE_HUMAN_AUTHORITY"
    REQUIRE_EVIDENCE = "REQUIRE_EVIDENCE"
    REQUIRE_CERTIFICATION_INTEGRITY = "REQUIRE_CERTIFICATION_INTEGRITY"
    REQUIRE_OPERATOR_EARLY = "REQUIRE_OPERATOR_EARLY"
    ALLOW_ARBITRARY_EXECUTABLE = "ALLOW_ARBITRARY_EXECUTABLE"


class VerificationTier(str, Enum):
    BASIC = "BASIC"
    STANDARD = "STANDARD"
    STRICT = "STRICT"


class ConfigurationRejectionReason(str, Enum):
    SYSTEM_INVARIANT = "SYSTEM_INVARIANT"
    WEAKENS_HIGHER_AUTHORITY = "WEAKENS_HIGHER_AUTHORITY"
    EXCEEDS_SAFETY_CEILING = "EXCEEDS_SAFETY_CEILING"


class ConfigurationDiagnostic(str, Enum):
    PROJECT_CONFIGURATION_BOUND = "PROJECT_CONFIGURATION_BOUND"
    OVERRIDE_APPLIED = "OVERRIDE_APPLIED"
    OVERRIDE_REJECTED = "OVERRIDE_REJECTED"
    MACHINE_FACT_RESTRICTED = "MACHINE_FACT_RESTRICTED"
    CONFIGURATION_RESOLVED = "CONFIGURATION_RESOLVED"


_SYSTEM_INVARIANT_KEYS = frozenset(
    {
        ConfigurationKey.REQUIRE_CLEAN_GIT,
        ConfigurationKey.REQUIRE_OBSERVABILITY,
        ConfigurationKey.REQUIRE_STRICT_HEALTH,
        ConfigurationKey.REQUIRE_REVIEWER,
        ConfigurationKey.REQUIRE_HUMAN_AUTHORITY,
        ConfigurationKey.REQUIRE_EVIDENCE,
        ConfigurationKey.REQUIRE_CERTIFICATION_INTEGRITY,
        ConfigurationKey.ALLOW_ARBITRARY_EXECUTABLE,
    }
)

CONFIGURATION_PRECEDENCE = (
    ConfigurationAuthorityClass.SYSTEM_INVARIANT,
    ConfigurationAuthorityClass.HARD_SAFETY_POLICY,
    ConfigurationAuthorityClass.PROJECT_CONFIGURATION,
    ConfigurationAuthorityClass.OPERATIONAL_POLICY,
    ConfigurationAuthorityClass.OPERATOR_PREFERENCE,
)


@dataclass(frozen=True, slots=True)
class HardSafetyPolicy:
    max_concurrency: int
    max_timeout_seconds: int
    max_remediation_generations: int
    maximum_sandbox: CodexSandboxConstraint
    minimum_verification_tier: VerificationTier = VerificationTier.STANDARD
    require_clean_git: bool = True
    require_observability: bool = True
    require_strict_health: bool = True
    require_reviewer: bool = True
    require_human_authority: bool = True
    require_evidence: bool = True
    require_certification_integrity: bool = True
    allow_arbitrary_executable: bool = False

    def __post_init__(self) -> None:
        for name in (
            "max_concurrency", "max_timeout_seconds", "max_remediation_generations"
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_concurrency > 8:
            raise ValueError("hard concurrency cannot exceed the P4 product ceiling of 8")
        if not isinstance(self.maximum_sandbox, CodexSandboxConstraint):
            raise ValueError("maximum_sandbox must use the closed sandbox contract")
        if not isinstance(self.minimum_verification_tier, VerificationTier):
            raise ValueError("minimum_verification_tier is invalid")
        mandatory = (
            self.require_clean_git, self.require_observability,
            self.require_strict_health, self.require_reviewer,
            self.require_human_authority, self.require_evidence,
            self.require_certification_integrity,
        )
        if any(value is not True for value in mandatory):
            raise ValueError("certified system invariants cannot be configurable")
        if self.allow_arbitrary_executable is not False:
            raise ValueError("arbitrary executable trust is prohibited")


@dataclass(frozen=True, slots=True)
class ConfigurationDirective:
    key: ConfigurationKey
    value: object
    source_id: str
    source_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, ConfigurationKey):
            raise ValueError("directive key must use the closed catalogue")
        if not _safe_id(self.source_id) or not _SHA256.fullmatch(self.source_fingerprint):
            raise ValueError("directive source is unsafe or unbound")
        _validate_value(self.key, self.value)


@dataclass(frozen=True, slots=True)
class ConfigurationLayer:
    authority: ConfigurationAuthorityClass
    directives: tuple[ConfigurationDirective, ...]

    def __post_init__(self) -> None:
        if self.authority not in {
            ConfigurationAuthorityClass.OPERATIONAL_POLICY,
            ConfigurationAuthorityClass.OPERATOR_PREFERENCE,
        }:
            raise ValueError("only operational policies and preferences use directive layers")
        if not isinstance(self.directives, tuple):
            raise ValueError("directives must be immutable")
        if any(not isinstance(item, ConfigurationDirective) for item in self.directives):
            raise ValueError("directive layer contains an invalid value")
        expected = tuple(sorted(self.directives, key=lambda item: item.key.value))
        if self.directives != expected:
            raise ValueError("directives must be canonically ordered")
        keys = tuple(item.key for item in self.directives)
        if len(keys) != len(set(keys)):
            raise ValueError("conflicting same-level directives are forbidden")


@dataclass(frozen=True, slots=True)
class MachineFactBinding:
    project_id: str
    repository_root: str
    observed_at: datetime
    platform_fingerprint: str
    codex_capabilities: CodexCapabilityAssessment | None = None
    maximum_parallel_executions: int | None = None

    def __post_init__(self) -> None:
        _validate_binding_identity(self.project_id, self.repository_root)
        _validate_utc(self.observed_at, "observed_at")
        if not _SHA256.fullmatch(self.platform_fingerprint):
            raise ValueError("platform fingerprint must be SHA-256")
        if self.maximum_parallel_executions is not None:
            value = self.maximum_parallel_executions
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError("machine parallel limit must be positive")
            assessment = self.codex_capabilities
            if (
                assessment is None
                or not assessment.authentically_discovered
                or assessment.status(CodexCapability.INDEPENDENT_PROCESS_PARALLELISM)
                is not CodexCapabilityStatus.SUPPORTED
                or assessment.tested_parallelism is None
                or value > assessment.tested_parallelism
            ):
                raise ValueError("machine parallel limit requires matching Codex evidence")


@dataclass(frozen=True, slots=True)
class ConfigurationResolutionContext:
    project_id: str
    repository_root: str
    repository_head: str
    project_configuration: ProjectConfiguration
    project_configuration_repository_root: str
    expected_configuration_fingerprint: str
    hard_safety: HardSafetyPolicy
    operational_policy: ConfigurationLayer | None
    operator_preferences: ConfigurationLayer | None
    machine_facts: MachineFactBinding | None
    resource_budgets: tuple[ResourceBudget, ...]
    evaluated_at: datetime
    mission_id: str | None = None
    workflow_generation: int | None = None

    def __post_init__(self) -> None:
        _validate_binding_identity(self.project_id, self.repository_root)
        _validate_binding_identity(self.project_id, self.project_configuration_repository_root)
        if not _SHA40.fullmatch(self.repository_head):
            raise ValueError("repository_head must be lowercase SHA-1")
        if not isinstance(self.project_configuration, ProjectConfiguration):
            raise ValueError("canonical ProjectConfiguration is required")
        if not _SHA256.fullmatch(self.expected_configuration_fingerprint):
            raise ValueError("expected configuration fingerprint must be SHA-256")
        if not isinstance(self.hard_safety, HardSafetyPolicy):
            raise ValueError("hard safety policy is required")
        if self.operational_policy is not None and (
            not isinstance(self.operational_policy, ConfigurationLayer)
            or self.operational_policy.authority
            is not ConfigurationAuthorityClass.OPERATIONAL_POLICY
        ):
            raise ValueError("operational policy layer is invalid")
        if self.operator_preferences is not None and (
            not isinstance(self.operator_preferences, ConfigurationLayer)
            or self.operator_preferences.authority
            is not ConfigurationAuthorityClass.OPERATOR_PREFERENCE
        ):
            raise ValueError("operator preference layer is invalid")
        if self.machine_facts is not None and not isinstance(
            self.machine_facts, MachineFactBinding
        ):
            raise ValueError("machine facts binding is invalid")
        if not isinstance(self.resource_budgets, tuple) or any(
            not isinstance(item, ResourceBudget) for item in self.resource_budgets
        ):
            raise ValueError("resource budgets must use the immutable P6.7 contract")
        _validate_utc(self.evaluated_at, "evaluated_at")
        if (self.mission_id is None) != (self.workflow_generation is None):
            raise ValueError("mission and generation bindings must be provided together")
        if self.mission_id is not None and (
            not _safe_id(self.mission_id)
            or not isinstance(self.workflow_generation, int)
            or isinstance(self.workflow_generation, bool)
            or self.workflow_generation < 0
        ):
            raise ValueError("mission/generation binding is invalid")


@dataclass(frozen=True, slots=True)
class RejectedConfigurationOverride:
    key: ConfigurationKey
    authority: ConfigurationAuthorityClass
    requested_value: object
    source_id: str
    reason: ConfigurationRejectionReason


@dataclass(frozen=True, slots=True)
class EffectiveConfigurationValue:
    key: ConfigurationKey
    value: object
    provenance: ConfigurationAuthorityClass
    source_id: str
    applied_ceiling: object


_ATTESTATION_KEY = secrets.token_bytes(32)


@dataclass(frozen=True, slots=True)
class EffectiveConfiguration:
    resolution_version: str
    project_id: str
    repository_root: str
    repository_head: str
    configuration_fingerprint: str
    project_configuration: ProjectConfiguration
    mission_id: str | None
    workflow_generation: int | None
    platform_fingerprint: str | None
    codex_identity_fingerprint: str | None
    values: tuple[EffectiveConfigurationValue, ...]
    rejected_overrides: tuple[RejectedConfigurationOverride, ...]
    diagnostics: tuple[ConfigurationDiagnostic, ...]
    fingerprint: str
    _attestation: str = field(default="", repr=False, compare=False)

    @property
    def authentically_resolved(self) -> bool:
        return bool(self._attestation) and hmac.compare_digest(
            self._attestation, _sign_effective(self)
        )

    def value(self, key: ConfigurationKey) -> object:
        return next(item.value for item in self.values if item.key is key)


class ConfigurationResolutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class ConfigurationResolver:
    """Combine only closed compatible layers without mutation or authority."""

    def resolve(self, context: ConfigurationResolutionContext) -> EffectiveConfiguration:
        if not isinstance(context, ConfigurationResolutionContext):
            raise ConfigurationResolutionError("INVALID_CONTEXT", "canonical context is required")
        configuration = context.project_configuration
        actual_fingerprint = project_configuration_fingerprint(configuration)
        if actual_fingerprint != context.expected_configuration_fingerprint:
            raise ConfigurationResolutionError("STALE_CONFIGURATION", "configuration fingerprint changed")
        if configuration.project_id != context.project_id:
            raise ConfigurationResolutionError("FOREIGN_CONFIGURATION", "configuration belongs to another project")
        constraints = configuration.codex_constraints
        if (
            constraints.approval_policy is not CodexApprovalConstraint.NEVER
            or constraints.require_clean_git is not True
            or not isinstance(constraints.maximum_sandbox, CodexSandboxConstraint)
        ):
            raise ConfigurationResolutionError(
                "UNSAFE_PROJECT_CONFIGURATION",
                "project configuration cannot weaken approval, Git, or sandbox invariants",
            )
        if _path_key(context.repository_root) != _path_key(
            context.project_configuration_repository_root
        ):
            raise ConfigurationResolutionError("FOREIGN_CONFIGURATION", "configuration belongs to another repository")
        machine = context.machine_facts
        if machine is not None:
            if (
                machine.project_id != context.project_id
                or _path_key(machine.repository_root) != _path_key(context.repository_root)
            ):
                raise ConfigurationResolutionError("FOREIGN_MACHINE_FACT", "machine facts are cross-project")
            age = context.evaluated_at - machine.observed_at
            if age < timedelta(0) or age > MACHINE_FACT_MAX_AGE:
                raise ConfigurationResolutionError("STALE_MACHINE_FACT", "machine facts require fresh observation")

        values, ceilings = _hard_values(context.hard_safety)
        provenance = {
            key: (
                ConfigurationAuthorityClass.SYSTEM_INVARIANT
                if key in _SYSTEM_INVARIANT_KEYS
                else ConfigurationAuthorityClass.HARD_SAFETY_POLICY
            )
            for key in values
        }
        sources = {
            key: (
                "system-invariant"
                if key in _SYSTEM_INVARIANT_KEYS
                else "hard-safety-policy"
            )
            for key in values
        }
        rejected: list[RejectedConfigurationOverride] = []
        diagnostics = [ConfigurationDiagnostic.PROJECT_CONFIGURATION_BOUND]

        budget_layers = _budget_directives(context)
        for authority, directives in budget_layers:
            if authority is ConfigurationAuthorityClass.HARD_SAFETY_POLICY:
                _apply_layer(
                    authority, directives, values, ceilings, provenance, sources, rejected,
                )
        project_directives = _project_directives(configuration, actual_fingerprint)
        _apply_layer(
            ConfigurationAuthorityClass.PROJECT_CONFIGURATION,
            project_directives, values, ceilings, provenance, sources, rejected,
        )
        for target_authority, layer in (
            (ConfigurationAuthorityClass.OPERATIONAL_POLICY, context.operational_policy),
            (ConfigurationAuthorityClass.OPERATOR_PREFERENCE, context.operator_preferences),
        ):
            for authority, directives in budget_layers:
                if authority is target_authority:
                    _apply_layer(
                        authority, directives, values, ceilings, provenance, sources, rejected,
                    )
            if layer is not None:
                _apply_layer(
                    layer.authority, layer.directives, values, ceilings,
                    provenance, sources, rejected,
                )
        if machine is not None and machine.maximum_parallel_executions is not None:
            current = values[ConfigurationKey.MAX_CONCURRENCY]
            if machine.maximum_parallel_executions < current:
                values[ConfigurationKey.MAX_CONCURRENCY] = machine.maximum_parallel_executions
                provenance[ConfigurationKey.MAX_CONCURRENCY] = ConfigurationAuthorityClass.MACHINE_FACT
                sources[ConfigurationKey.MAX_CONCURRENCY] = "codex-capability-assessment"
                diagnostics.append(ConfigurationDiagnostic.MACHINE_FACT_RESTRICTED)
        diagnostics.extend(
            ConfigurationDiagnostic.OVERRIDE_REJECTED for _ in rejected
        )
        if any(
            provenance[key]
            in {
                ConfigurationAuthorityClass.PROJECT_CONFIGURATION,
                ConfigurationAuthorityClass.OPERATIONAL_POLICY,
                ConfigurationAuthorityClass.OPERATOR_PREFERENCE,
            }
            for key in values
        ):
            diagnostics.append(ConfigurationDiagnostic.OVERRIDE_APPLIED)
        diagnostics.append(ConfigurationDiagnostic.CONFIGURATION_RESOLVED)
        effective_values = tuple(
            EffectiveConfigurationValue(
                key, values[key], provenance[key], sources[key], ceilings[key]
            )
            for key in ConfigurationKey
        )
        platform_fingerprint = machine.platform_fingerprint if machine else None
        codex_fingerprint = (
            _codex_fingerprint(machine.codex_capabilities)
            if machine and machine.codex_capabilities is not None
            else None
        )
        payload = _effective_payload(
            context, actual_fingerprint, effective_values, tuple(rejected),
            tuple(diagnostics), platform_fingerprint, codex_fingerprint,
        )
        fingerprint = _sha256_json(payload)
        result = EffectiveConfiguration(
            CONFIGURATION_RESOLUTION_VERSION, context.project_id,
            str(Path(context.repository_root).resolve(strict=True)),
            context.repository_head, actual_fingerprint, configuration, context.mission_id,
            context.workflow_generation, platform_fingerprint, codex_fingerprint,
            effective_values, tuple(rejected), tuple(diagnostics), fingerprint,
        )
        object.__setattr__(result, "_attestation", _sign_effective(result))
        return result

    def verify_current(
        self,
        effective: EffectiveConfiguration,
        context: ConfigurationResolutionContext,
    ) -> EffectiveConfiguration:
        """Re-resolve and reject forged, stale, foreign, or generation-drifted output."""

        if not isinstance(effective, EffectiveConfiguration) or not effective.authentically_resolved:
            raise ConfigurationResolutionError(
                "FORGED_EFFECTIVE_CONFIGURATION", "effective configuration is not resolver-authentic"
            )
        current = self.resolve(context)
        if effective.fingerprint != current.fingerprint:
            raise ConfigurationResolutionError(
                "STALE_EFFECTIVE_CONFIGURATION", "configuration or binding drift requires re-evaluation"
            )
        return current


def project_configuration_fingerprint(configuration: ProjectConfiguration) -> str:
    if not isinstance(configuration, ProjectConfiguration):
        raise TypeError("canonical ProjectConfiguration is required")
    return _sha256_json(to_dict(configuration))


def _hard_values(policy: HardSafetyPolicy) -> tuple[dict[ConfigurationKey, object], dict[ConfigurationKey, object]]:
    values: dict[ConfigurationKey, object] = {
        ConfigurationKey.MAX_CONCURRENCY: policy.max_concurrency,
        ConfigurationKey.MAX_TIMEOUT_SECONDS: policy.max_timeout_seconds,
        ConfigurationKey.MAX_REMEDIATION_GENERATIONS: policy.max_remediation_generations,
        ConfigurationKey.SANDBOX_MAXIMUM: policy.maximum_sandbox,
        ConfigurationKey.MINIMUM_VERIFICATION_TIER: policy.minimum_verification_tier,
        ConfigurationKey.REQUIRE_CLEAN_GIT: True,
        ConfigurationKey.REQUIRE_OBSERVABILITY: True,
        ConfigurationKey.REQUIRE_STRICT_HEALTH: True,
        ConfigurationKey.REQUIRE_REVIEWER: True,
        ConfigurationKey.REQUIRE_HUMAN_AUTHORITY: True,
        ConfigurationKey.REQUIRE_EVIDENCE: True,
        ConfigurationKey.REQUIRE_CERTIFICATION_INTEGRITY: True,
        ConfigurationKey.REQUIRE_OPERATOR_EARLY: False,
        ConfigurationKey.ALLOW_ARBITRARY_EXECUTABLE: False,
    }
    return values, dict(values)


def _project_directives(configuration: ProjectConfiguration, fingerprint: str) -> tuple[ConfigurationDirective, ...]:
    source = "project-configuration"
    values = {
        ConfigurationKey.MAX_CONCURRENCY: configuration.codex_constraints.maximum_parallel_executions,
        ConfigurationKey.SANDBOX_MAXIMUM: configuration.codex_constraints.maximum_sandbox,
        ConfigurationKey.REQUIRE_CLEAN_GIT: configuration.codex_constraints.require_clean_git,
    }
    return tuple(
        ConfigurationDirective(key, values[key], source, fingerprint)
        for key in sorted(values, key=lambda item: item.value)
    )


def _budget_directives(context: ConfigurationResolutionContext):
    mapped: list[tuple[ConfigurationAuthorityClass, ConfigurationDirective]] = []
    key_by_domain = {
        ResourceBudgetDomain.CODEX_CONCURRENCY: ConfigurationKey.MAX_CONCURRENCY,
        ResourceBudgetDomain.EXECUTION_TIME: ConfigurationKey.MAX_TIMEOUT_SECONDS,
        ResourceBudgetDomain.REMEDIATION_GENERATIONS: ConfigurationKey.MAX_REMEDIATION_GENERATIONS,
    }
    authority_by_class = {
        GovernancePolicyClass.HARD_SAFETY_POLICY: ConfigurationAuthorityClass.HARD_SAFETY_POLICY,
        GovernancePolicyClass.OPERATIONAL_POLICY: ConfigurationAuthorityClass.OPERATIONAL_POLICY,
        GovernancePolicyClass.OPERATOR_PREFERENCE: ConfigurationAuthorityClass.OPERATOR_PREFERENCE,
    }
    for budget in context.resource_budgets:
        scope = budget.scope
        if (
            scope.project_id != context.project_id
            or scope.repository_head != context.repository_head
            or _path_key(scope.repository_root) != _path_key(context.repository_root)
            or scope.mission_id != context.mission_id
            or scope.workflow_generation != context.workflow_generation
        ):
            raise ConfigurationResolutionError(
                "FOREIGN_RESOURCE_BUDGET", "resource budget scope differs from resolution scope"
            )
        if budget.applicability is not ResourceBudgetApplicability.APPLICABLE:
            continue
        key = key_by_domain.get(budget.domain)
        if key is None:
            continue
        directive = ConfigurationDirective(
            key, budget.limit, budget.budget_id,
            _sha256_json({"budget": repr(budget)}),
        )
        mapped.append((authority_by_class[budget.policy_class], directive))
    rank = {item: index for index, item in enumerate(CONFIGURATION_PRECEDENCE)}
    mapped.sort(key=lambda item: (rank[item[0]], item[1].key.value, item[1].source_id))
    return tuple((authority, (directive,)) for authority, directive in mapped)


def _apply_layer(authority, directives, values, ceilings, provenance, sources, rejected) -> None:
    for directive in directives:
        key = directive.key
        requested = directive.value
        current = values[key]
        if key in _SYSTEM_INVARIANT_KEYS and requested != current:
            rejected.append(RejectedConfigurationOverride(
                key, authority, requested, directive.source_id,
                ConfigurationRejectionReason.SYSTEM_INVARIANT,
            ))
            continue
        if _is_more_restrictive_or_equal(key, requested, current):
            if requested != current:
                values[key] = requested
                provenance[key] = authority
                sources[key] = directive.source_id
            continue
        rejected.append(RejectedConfigurationOverride(
            key, authority, requested, directive.source_id,
            ConfigurationRejectionReason.EXCEEDS_SAFETY_CEILING
            if key in {
                ConfigurationKey.MAX_CONCURRENCY,
                ConfigurationKey.MAX_TIMEOUT_SECONDS,
                ConfigurationKey.MAX_REMEDIATION_GENERATIONS,
            }
            else ConfigurationRejectionReason.WEAKENS_HIGHER_AUTHORITY,
        ))


def _is_more_restrictive_or_equal(key: ConfigurationKey, requested: object, current: object) -> bool:
    if key in {
        ConfigurationKey.MAX_CONCURRENCY,
        ConfigurationKey.MAX_TIMEOUT_SECONDS,
        ConfigurationKey.MAX_REMEDIATION_GENERATIONS,
    }:
        return int(requested) <= int(current)
    if key is ConfigurationKey.SANDBOX_MAXIMUM:
        rank = {CodexSandboxConstraint.READ_ONLY: 0, CodexSandboxConstraint.WORKSPACE_WRITE: 1}
        return rank[requested] <= rank[current]
    if key is ConfigurationKey.MINIMUM_VERIFICATION_TIER:
        rank = {VerificationTier.BASIC: 0, VerificationTier.STANDARD: 1, VerificationTier.STRICT: 2}
        return rank[requested] >= rank[current]
    if key is ConfigurationKey.ALLOW_ARBITRARY_EXECUTABLE:
        return requested is False or current is True
    return requested is True or current is False


def _validate_value(key: ConfigurationKey, value: object) -> None:
    if key in {
        ConfigurationKey.MAX_CONCURRENCY,
        ConfigurationKey.MAX_TIMEOUT_SECONDS,
        ConfigurationKey.MAX_REMEDIATION_GENERATIONS,
    }:
        minimum = 1 if key is ConfigurationKey.MAX_TIMEOUT_SECONDS else 0
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ValueError(f"{key.value} requires a positive integer")
        return
    if key is ConfigurationKey.SANDBOX_MAXIMUM:
        if not isinstance(value, CodexSandboxConstraint):
            raise ValueError("sandbox directive uses a closed enum")
        return
    if key is ConfigurationKey.MINIMUM_VERIFICATION_TIER:
        if not isinstance(value, VerificationTier):
            raise ValueError("verification tier uses a closed enum")
        return
    if not isinstance(value, bool):
        raise ValueError(f"{key.value} requires an explicit boolean")


def _effective_payload(context, config_fingerprint, values, rejected, diagnostics, platform, codex) -> dict[str, object]:
    return {
        "version": CONFIGURATION_RESOLUTION_VERSION,
        "project_id": context.project_id,
        "repository_root": str(Path(context.repository_root).resolve(strict=True)),
        "repository_head": context.repository_head,
        "configuration_fingerprint": config_fingerprint,
        "mission_id": context.mission_id,
        "workflow_generation": context.workflow_generation,
        "platform_fingerprint": platform,
        "codex_identity_fingerprint": codex,
        "values": [
            {"key": item.key.value, "value": _json_value(item.value),
             "provenance": item.provenance.value, "source": item.source_id,
             "ceiling": _json_value(item.applied_ceiling)} for item in values
        ],
        "rejected": [
            {"key": item.key.value, "authority": item.authority.value,
             "value": _json_value(item.requested_value), "source": item.source_id,
             "reason": item.reason.value} for item in rejected
        ],
        "diagnostics": [item.value for item in diagnostics],
    }


def _sign_effective(value: EffectiveConfiguration) -> str:
    payload = {
        "resolution_version": value.resolution_version,
        "project_id": value.project_id,
        "repository_root": value.repository_root,
        "repository_head": value.repository_head,
        "configuration_fingerprint": value.configuration_fingerprint,
        "project_configuration": to_dict(value.project_configuration),
        "mission_id": value.mission_id,
        "workflow_generation": value.workflow_generation,
        "platform_fingerprint": value.platform_fingerprint,
        "codex_identity_fingerprint": value.codex_identity_fingerprint,
        "values": [repr(item) for item in value.values],
        "rejected": [repr(item) for item in value.rejected_overrides],
        "diagnostics": [item.value for item in value.diagnostics],
        "fingerprint": value.fingerprint,
    }
    return hmac.new(_ATTESTATION_KEY, json.dumps(payload, sort_keys=True).encode(), hashlib.sha256).hexdigest()


def _codex_fingerprint(value: CodexCapabilityAssessment) -> str:
    if not value.authentically_discovered:
        raise ConfigurationResolutionError("FORGED_MACHINE_FACT", "Codex assessment is not authentic")
    return _sha256_json({"path": value.executable_path, "sha256": value.executable_sha256, "version": value.executable_version})


def _sha256_json(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_value).encode("utf-8")).hexdigest()


def _json_value(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


def _safe_id(value: object) -> bool:
    return bool(isinstance(value, str) and _ID.fullmatch(value) and not _SENSITIVE.search(value))


def _validate_binding_identity(project_id: str, repository_root: str) -> None:
    if not _safe_project_id(project_id):
        raise ValueError("project identity is unsafe")
    path = Path(repository_root)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ValueError("repository root cannot be resolved") from error
    if not path.is_absolute() or not resolved.is_dir():
        raise ValueError("repository root must be an existing absolute directory")


def _path_key(value: str) -> str:
    return os.path.normcase(str(Path(value).resolve(strict=True))).casefold()


def _validate_utc(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be an aware UTC datetime")


def _safe_project_id(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and value == value.strip()
        and 1 <= len(value) <= 128
        and unicodedata.normalize("NFC", value) == value
        and not any(character.isspace() or character in "/\\:" for character in value)
        and not _SENSITIVE.search(value)
    )
