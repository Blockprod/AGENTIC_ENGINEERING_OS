from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    ConfigurationAuthorityClass,
    CONFIGURATION_PRECEDENCE,
    ConfigurationDirective,
    ConfigurationKey,
    ConfigurationLayer,
    ConfigurationRejectionReason,
    ConfigurationResolutionContext,
    ConfigurationResolutionError,
    ConfigurationResolver,
    EffectiveConfiguration,
    HardSafetyPolicy,
    MachineFactBinding,
    VerificationTier,
    project_configuration_fingerprint,
)
from agentic_engineering_os.domain import (
    CodexSandboxConstraint,
    GovernancePolicyClass,
    ResourceBudget,
    ResourceBudgetApplicability,
    ResourceBudgetDomain,
    ResourceBudgetRationale,
    ResourceBudgetScope,
    ResourceBudgetUnit,
)
from agentic_engineering_os.infrastructure import ProjectConfigurationValidator


NOW = datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)
SHA = "a" * 64


def configuration(*, project_id: str = "demo", concurrency: int = 3, sandbox: str = "workspace-write"):
    return ProjectConfigurationValidator().validate({
        "config_version": "1.0",
        "project_id": project_id,
        "repository_root_policy": "CONFIG_PARENT_GIT_ROOT",
        "toolchains": [{"identity": "python", "version_constraint": ">=3.11"}],
        "verification_commands": [{
            "command_id": "tests", "kind": "TEST", "executable": "python",
            "args": ["-m", "pytest", "tests"], "cwd": ".",
            "cwd_policy": "REPOSITORY_RELATIVE", "required": True,
        }],
        "path_policy": {
            "allowed_paths": ["src", "tests"],
            "protected_paths": ["pyproject.toml"], "forbidden_paths": [],
        },
        "context_sources": ["AGENTS.md"],
        "codex_constraints": {
            "maximum_sandbox": sandbox, "approval_policy": "never",
            "require_clean_git": True,
            "maximum_parallel_executions": concurrency,
        },
        "mission_state_git_policy": "TRACKED",
    })


def directive(key: ConfigurationKey, value: object, source: str = "policy") -> ConfigurationDirective:
    return ConfigurationDirective(key, value, source, SHA)


def layer(authority: ConfigurationAuthorityClass, *items: ConfigurationDirective) -> ConfigurationLayer:
    return ConfigurationLayer(authority, tuple(sorted(items, key=lambda item: item.key.value)))


def context(
    root: Path, *, config=None, expected: str | None = None,
    operational: ConfigurationLayer | None = None,
    preferences: ConfigurationLayer | None = None,
    machine: MachineFactBinding | None = None,
    generation: int = 2,
) -> ConfigurationResolutionContext:
    config = config or configuration()
    return ConfigurationResolutionContext(
        project_id=config.project_id, repository_root=str(root.resolve()),
        repository_head="b" * 40, project_configuration=config,
        project_configuration_repository_root=str(root.resolve()),
        expected_configuration_fingerprint=expected or project_configuration_fingerprint(config),
        hard_safety=HardSafetyPolicy(
            4, 900, 5, CodexSandboxConstraint.WORKSPACE_WRITE
        ),
        operational_policy=operational, operator_preferences=preferences,
        machine_facts=machine, evaluated_at=NOW,
        resource_budgets=(),
        mission_id="mission-7-6", workflow_generation=generation,
    )


def test_taxonomy_is_closed_and_precedence_is_explicit() -> None:
    assert tuple(item.value for item in ConfigurationAuthorityClass) == (
        "SYSTEM_INVARIANT", "PROJECT_CONFIGURATION", "HARD_SAFETY_POLICY",
        "OPERATIONAL_POLICY", "OPERATOR_PREFERENCE", "MACHINE_FACT",
    )
    assert CONFIGURATION_PRECEDENCE == (
        ConfigurationAuthorityClass.SYSTEM_INVARIANT,
        ConfigurationAuthorityClass.HARD_SAFETY_POLICY,
        ConfigurationAuthorityClass.PROJECT_CONFIGURATION,
        ConfigurationAuthorityClass.OPERATIONAL_POLICY,
        ConfigurationAuthorityClass.OPERATOR_PREFERENCE,
    )


def test_project_and_operator_more_restrictive_limits_win(tmp_path: Path) -> None:
    preferences = layer(
        ConfigurationAuthorityClass.OPERATOR_PREFERENCE,
        directive(ConfigurationKey.MAX_CONCURRENCY, 2, "operator"),
        directive(ConfigurationKey.MAX_TIMEOUT_SECONDS, 60, "operator"),
        directive(ConfigurationKey.MINIMUM_VERIFICATION_TIER, VerificationTier.STRICT, "operator"),
    )
    result = ConfigurationResolver().resolve(context(tmp_path, preferences=preferences))
    assert result.value(ConfigurationKey.MAX_CONCURRENCY) == 2
    assert result.value(ConfigurationKey.MAX_TIMEOUT_SECONDS) == 60
    assert result.value(ConfigurationKey.MINIMUM_VERIFICATION_TIER) is VerificationTier.STRICT
    assert result.project_configuration.path_policy.allowed_paths == ("src", "tests")


def test_operator_cannot_raise_concurrency_or_timeout_ceiling(tmp_path: Path) -> None:
    preferences = layer(
        ConfigurationAuthorityClass.OPERATOR_PREFERENCE,
        directive(ConfigurationKey.MAX_CONCURRENCY, 8, "operator"),
        directive(ConfigurationKey.MAX_TIMEOUT_SECONDS, 1200, "operator"),
    )
    result = ConfigurationResolver().resolve(context(tmp_path, preferences=preferences))
    assert result.value(ConfigurationKey.MAX_CONCURRENCY) == 3
    assert result.value(ConfigurationKey.MAX_TIMEOUT_SECONDS) == 900
    assert {item.key for item in result.rejected_overrides} == {
        ConfigurationKey.MAX_CONCURRENCY, ConfigurationKey.MAX_TIMEOUT_SECONDS,
    }
    assert all(item.reason is ConfigurationRejectionReason.EXCEEDS_SAFETY_CEILING for item in result.rejected_overrides)


def test_project_and_policy_can_only_strengthen_sandbox(tmp_path: Path) -> None:
    config = configuration(sandbox="read-only")
    operational = layer(
        ConfigurationAuthorityClass.OPERATIONAL_POLICY,
        directive(ConfigurationKey.SANDBOX_MAXIMUM, CodexSandboxConstraint.WORKSPACE_WRITE),
    )
    result = ConfigurationResolver().resolve(context(tmp_path, config=config, operational=operational))
    assert result.value(ConfigurationKey.SANDBOX_MAXIMUM) is CodexSandboxConstraint.READ_ONLY
    assert result.rejected_overrides[0].reason is ConfigurationRejectionReason.WEAKENS_HIGHER_AUTHORITY


@pytest.mark.parametrize(
    ("key", "attempt"),
    (
        (ConfigurationKey.REQUIRE_REVIEWER, False),
        (ConfigurationKey.REQUIRE_HUMAN_AUTHORITY, False),
        (ConfigurationKey.REQUIRE_CERTIFICATION_INTEGRITY, False),
        (ConfigurationKey.REQUIRE_STRICT_HEALTH, False),
        (ConfigurationKey.REQUIRE_EVIDENCE, False),
        (ConfigurationKey.REQUIRE_OBSERVABILITY, False),
        (ConfigurationKey.ALLOW_ARBITRARY_EXECUTABLE, True),
    ),
)
def test_no_layer_can_disable_a_certified_invariant(tmp_path: Path, key, attempt) -> None:
    operational = layer(
        ConfigurationAuthorityClass.OPERATIONAL_POLICY,
        directive(key, attempt),
    )
    result = ConfigurationResolver().resolve(context(tmp_path, operational=operational))
    assert result.rejected_overrides[0].reason is ConfigurationRejectionReason.SYSTEM_INVARIANT
    expected = False if key is ConfigurationKey.ALLOW_ARBITRARY_EXECUTABLE else True
    assert result.value(key) is expected
    effective = next(item for item in result.values if item.key is key)
    assert effective.provenance is ConfigurationAuthorityClass.SYSTEM_INVARIANT
    assert effective.source_id == "system-invariant"


def test_operational_policy_can_require_operator_earlier(tmp_path: Path) -> None:
    operational = layer(
        ConfigurationAuthorityClass.OPERATIONAL_POLICY,
        directive(ConfigurationKey.REQUIRE_OPERATOR_EARLY, True),
    )
    result = ConfigurationResolver().resolve(context(tmp_path, operational=operational))
    assert result.value(ConfigurationKey.REQUIRE_OPERATOR_EARLY) is True


def test_p6_resource_budget_restricts_effective_concurrency(tmp_path: Path) -> None:
    current = context(tmp_path)
    budget = ResourceBudget(
        "operations.codex", "1.0", ResourceBudgetDomain.CODEX_CONCURRENCY,
        ResourceBudgetScope(
            "demo", "b" * 40, str(tmp_path.resolve()), "mission-7-6", 2
        ),
        2, ResourceBudgetUnit.EXECUTIONS,
        GovernancePolicyClass.OPERATIONAL_POLICY, "operations",
        ResourceBudgetApplicability.APPLICABLE,
        ResourceBudgetRationale.OPERATOR_CONSERVATION,
    )
    result = ConfigurationResolver().resolve(
        replace(current, resource_budgets=(budget,))
    )
    assert result.value(ConfigurationKey.MAX_CONCURRENCY) == 2
    value = next(
        item for item in result.values
        if item.key is ConfigurationKey.MAX_CONCURRENCY
    )
    assert value.provenance is ConfigurationAuthorityClass.OPERATIONAL_POLICY


def test_foreign_project_repository_and_stale_config_fail_closed(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    base = context(tmp_path)
    with pytest.raises(ConfigurationResolutionError, match="FOREIGN_CONFIGURATION"):
        ConfigurationResolver().resolve(replace(base, project_id="foreign"))
    with pytest.raises(ConfigurationResolutionError, match="FOREIGN_CONFIGURATION"):
        ConfigurationResolver().resolve(replace(base, project_configuration_repository_root=str(other)))
    with pytest.raises(ConfigurationResolutionError, match="STALE_CONFIGURATION"):
        ConfigurationResolver().resolve(replace(base, expected_configuration_fingerprint="0" * 64))


def test_machine_facts_are_read_only_restrictors_and_must_be_fresh(tmp_path: Path) -> None:
    machine = MachineFactBinding(
        "demo", str(tmp_path.resolve()), NOW, "c" * 64
    )
    result = ConfigurationResolver().resolve(context(tmp_path, machine=machine))
    assert result.platform_fingerprint == "c" * 64
    assert all(item.provenance is not ConfigurationAuthorityClass.MACHINE_FACT for item in result.values)
    stale = replace(machine, observed_at=NOW - timedelta(minutes=6))
    with pytest.raises(ConfigurationResolutionError, match="STALE_MACHINE_FACT"):
        ConfigurationResolver().resolve(context(tmp_path, machine=stale))
    with pytest.raises(ValueError):
        ConfigurationLayer(ConfigurationAuthorityClass.MACHINE_FACT, ())


def test_foreign_machine_fact_fails_closed(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    machine = MachineFactBinding("demo", str(other), NOW, "c" * 64)
    with pytest.raises(ConfigurationResolutionError, match="FOREIGN_MACHINE_FACT"):
        ConfigurationResolver().resolve(context(tmp_path, machine=machine))


def test_generation_and_configuration_drift_invalidate_effective_result(tmp_path: Path) -> None:
    resolver = ConfigurationResolver()
    original_context = context(tmp_path, generation=2)
    effective = resolver.resolve(original_context)
    assert resolver.verify_current(effective, original_context).fingerprint == effective.fingerprint
    with pytest.raises(ConfigurationResolutionError, match="STALE_EFFECTIVE_CONFIGURATION"):
        resolver.verify_current(effective, context(tmp_path, generation=3))
    changed = configuration(concurrency=2)
    with pytest.raises(ConfigurationResolutionError, match="STALE_EFFECTIVE_CONFIGURATION"):
        resolver.verify_current(effective, context(tmp_path, config=changed))


def test_forged_effective_configuration_cannot_be_reused(tmp_path: Path) -> None:
    resolver = ConfigurationResolver()
    current = context(tmp_path)
    effective = resolver.resolve(current)
    forged = replace(effective, fingerprint="0" * 64)
    assert not forged.authentically_resolved
    with pytest.raises(ConfigurationResolutionError, match="FORGED_EFFECTIVE_CONFIGURATION"):
        resolver.verify_current(forged, current)


def test_conflicting_same_level_values_are_rejected() -> None:
    with pytest.raises(ValueError, match="conflicting same-level"):
        ConfigurationLayer(
            ConfigurationAuthorityClass.OPERATOR_PREFERENCE,
            (
                directive(ConfigurationKey.MAX_CONCURRENCY, 2, "a"),
                directive(ConfigurationKey.MAX_CONCURRENCY, 3, "b"),
            ),
        )


def test_secret_shell_and_unbounded_inputs_are_rejected() -> None:
    with pytest.raises(ValueError):
        directive(ConfigurationKey.MAX_TIMEOUT_SECONDS, "cmd /c whoami")
    with pytest.raises(ValueError):
        directive(ConfigurationKey.MAX_TIMEOUT_SECONDS, 10, "token:secret")
    with pytest.raises(ValueError):
        HardSafetyPolicy(9, 900, 5, CodexSandboxConstraint.WORKSPACE_WRITE)


def test_resolution_is_deterministic_ordered_and_non_mutating(tmp_path: Path) -> None:
    config = configuration()
    operational = layer(
        ConfigurationAuthorityClass.OPERATIONAL_POLICY,
        directive(ConfigurationKey.MAX_TIMEOUT_SECONDS, 300),
        directive(ConfigurationKey.MAX_CONCURRENCY, 2),
    )
    current = context(tmp_path, config=config, operational=operational)
    before = repr((config, operational))
    first = ConfigurationResolver().resolve(current)
    second = ConfigurationResolver().resolve(current)
    assert first.fingerprint == second.fingerprint
    assert tuple(item.key for item in first.values) == tuple(ConfigurationKey)
    assert repr((config, operational)) == before
    for forbidden in ("save", "transition", "certify", "record_evidence"):
        assert not hasattr(ConfigurationResolver(), forbidden)


def test_preferences_cannot_be_misclassified_as_project_or_hard_policy() -> None:
    values = (directive(ConfigurationKey.MAX_CONCURRENCY, 2),)
    for authority in (
        ConfigurationAuthorityClass.SYSTEM_INVARIANT,
        ConfigurationAuthorityClass.PROJECT_CONFIGURATION,
        ConfigurationAuthorityClass.HARD_SAFETY_POLICY,
        ConfigurationAuthorityClass.MACHINE_FACT,
    ):
        with pytest.raises(ValueError):
            ConfigurationLayer(authority, values)
