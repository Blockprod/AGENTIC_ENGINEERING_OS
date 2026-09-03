from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_engineering_os.application.execution_budget_boundary import (
    ExecutionBudgetBoundary,
    ExecutionBudgetError,
)
from agentic_engineering_os.application.execution_state import CodexExecutionLedger
from agentic_engineering_os.domain import (
    CodexApprovalConstraint,
    CodexProjectConstraints,
    CodexSandboxConstraint,
    MissionRole,
    MissionState,
    MissionStateGitPolicy,
    MissionStatus,
    OperatingStep,
    ProjectConfiguration,
    ProjectPathPolicy,
    RepositoryRootPolicy,
)


HEAD = "a" * 40


class Store:
    def __init__(self, value: object) -> None:
        self.value = value

    def load(self) -> object:
        return self.value


def _boundary(root: Path, *, limit: int = 1) -> ExecutionBudgetBoundary:
    mission = MissionState(
        "1.0",
        "mission-budget",
        1,
        MissionStatus.ACTIVE,
        MissionRole.ORCHESTRATOR,
        "Execute safely.",
        "mission-budget",
        OperatingStep.ACT,
        "Continue.",
        HEAD,
        datetime.now(timezone.utc),
        [],
    )
    configuration = ProjectConfiguration(
        "1.0",
        "project-budget",
        RepositoryRootPolicy.CONFIG_PARENT_GIT_ROOT,
        (),
        (),
        ProjectPathPolicy(("src",), (), ()),
        (),
        CodexProjectConstraints(
            CodexSandboxConstraint.WORKSPACE_WRITE,
            CodexApprovalConstraint.NEVER,
            True,
            limit,
        ),
        MissionStateGitPolicy.IGNORED,
    )
    return ExecutionBudgetBoundary(
        repository_root=root,
        configuration=configuration,
        mission_store=Store(mission),
        execution_store=Store(CodexExecutionLedger("1.1", ())),
    )


def test_allocation_at_limit_is_authorized(tmp_path: Path) -> None:
    _boundary(tmp_path).authorize(requested=1, repository_head=HEAD)


def test_allocation_above_limit_is_refused_before_launch(tmp_path: Path) -> None:
    with pytest.raises(ExecutionBudgetError) as captured:
        _boundary(tmp_path).authorize(requested=2, repository_head=HEAD)
    assert captured.value.code == "RESOURCE_BUDGET_LIMIT_EXCEEDED"


def test_commit_scope_mismatch_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ExecutionBudgetError) as captured:
        _boundary(tmp_path).authorize(requested=1, repository_head="b" * 40)
    assert captured.value.code == "RESOURCE_BUDGET_SCOPE_MISMATCH"
