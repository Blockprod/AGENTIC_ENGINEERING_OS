"""Deterministic test taxonomy used by local runs and CI gates."""

from __future__ import annotations

from pathlib import Path

import pytest


_REAL_GIT_MODULES = frozenset(
    {
        "test_agents_integration.py",
        "test_codex_execution_recovery.py",
        "test_codex_result_intake.py",
        "test_codex_runtime_adapter.py",
        "test_existing_repository_adoption.py",
        "test_initialization_planner.py",
        "test_installation_cli.py",
        "test_installation_upgrade_compatibility_matrix.py",
        "test_integration_gate.py",
        "test_merge_coordinator.py",
        "test_mission_composition.py",
        "test_mission_gate_merge_composition.py",
        "test_mission_lifecycle_planning.py",
        "test_mission_state_git_policy_adoption.py",
        "test_multi_repository_deployment.py",
        "test_negative_merge_outcome_authority.py",
        "test_operator_acceptance.py",
        "test_operator_diagnostics_cli.py",
        "test_parallel_codex_implementers.py",
        "test_parallel_implementer_coordinator.py",
        "test_parallel_mission_workflow.py",
        "test_parallel_remediation_recovery.py",
        "test_platform_environment.py",
        "test_production_governance_failure_injection.py",
        "test_repository_archetype.py",
        "test_repository_initializer.py",
        "test_repository_reconnaissance.py",
        "test_repository_upgrade.py",
        "test_runtime_state_bootstrap.py",
        "test_remediation_transactions.py",
        "test_single_role_codex.py",
        "test_verification_command_runner.py",
        "test_verification_coordinator.py",
        "test_worktree_manager.py",
    }
)

_INTEGRATION_MODULES = frozenset(
    {
        "test_mission_cli.py",
        "test_mission_runner.py",
        "test_mission_operational_events.py",
        "test_mission_certification_persistence.py",
        "test_operational_event_store.py",
    }
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Give every unclassified test exactly one baseline cost category."""

    for item in items:
        existing = {marker.name for marker in item.iter_markers()}
        if existing & {"real_codex", "clean_room", "soak"}:
            continue
        filename = Path(str(item.fspath)).name
        category = (
            "real_git"
            if filename in _REAL_GIT_MODULES
            else "integration"
            if filename in _INTEGRATION_MODULES
            else "unit"
        )
        item.add_marker(getattr(pytest.mark, category))
