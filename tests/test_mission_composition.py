from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_engineering_os.application import (
    CodexExecutionStatus,
    MissionRunner,
)
from agentic_engineering_os.application.mission_composition import (
    _ContinuationAdmissionBoundary,
    build_production_mission_runner,
)
from agentic_engineering_os.application.mission_runner import MissionContinuationError
from agentic_engineering_os.domain import CodexSandboxConstraint, MaintenanceState
from test_existing_repository_adoption import adopt, configuration, existing_repository


def test_production_composition_reconstructs_all_services_without_state_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    root = existing_repository(tmp_path)
    desired = configuration()
    desired = replace(
        desired,
        codex_constraints=replace(
            desired.codex_constraints,
            maximum_sandbox=CodexSandboxConstraint.WORKSPACE_WRITE,
        ),
    )
    adopt(root, desired)
    before = {
        item.name: item.read_bytes()
        for item in (root / ".agentic-engineering-os").iterdir()
        if item.is_file()
    }
    monkeypatch.setattr(
        "agentic_engineering_os.application.mission_composition.shutil.which",
        lambda name: sys.executable,
    )
    monkeypatch.setattr(
        "agentic_engineering_os.application.mission_composition.tempfile.gettempdir",
        lambda: str(tmp_path / "external"),
    )
    (tmp_path / "external").mkdir()

    runner = build_production_mission_runner(root)

    assert isinstance(runner, MissionRunner)
    after = {
        item.name: item.read_bytes()
        for item in (root / ".agentic-engineering-os").iterdir()
        if item.is_file()
    }
    assert after == before


class _Store:
    def __init__(self, value) -> None:
        self.value = value

    def load(self):
        return self.value


class _Worktrees:
    def __init__(self, head: str, assignment) -> None:
        self._head = head
        self.registry_store = _Store(SimpleNamespace(assignments=(assignment,)))

    def inspect_primary(self):
        return SimpleNamespace(clean=True, head_commit=self._head)

    def inspect_all(self, *, current_generation: int):
        assert current_generation == 0
        return SimpleNamespace(anomalies=())


def test_continuation_refuses_uncertain_execution_in_current_worktree(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path.resolve()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    head = "a" * 40
    mission = SimpleNamespace(
        mission_id="mission-1", workflow_generation=0, observed_commit=head
    )
    assignment = SimpleNamespace(
        workflow_generation=0, worktree_path=str(worktree)
    )
    uncertain = SimpleNamespace(
        execution_id="execution-1",
        mission_id="mission-1",
        workflow_generation=0,
        status=CodexExecutionStatus.INTERRUPTED,
    )
    ledgers = {
        str(root): SimpleNamespace(records=()),
        str(worktree.resolve()): SimpleNamespace(records=(uncertain,)),
    }
    monkeypatch.setattr(
        "agentic_engineering_os.application.mission_composition.ExecutionStateStore",
        lambda path: _Store(ledgers[str(Path(path).resolve())]),
    )
    boundary = _ContinuationAdmissionBoundary(
        root=root,
        project_id="project",
        maintenance_store=_Store(
            SimpleNamespace(
                state=MaintenanceState.NORMAL,
                scope=SimpleNamespace(
                    project_id="project", repository_root=str(root).casefold()
                ),
            )
        ),
        mission_store=_Store(mission),
        project_store=_Store(SimpleNamespace(project_id="project")),
        record_store=_Store(
            SimpleNamespace(mission_id="mission-1", workflow_generation=0)
        ),
        execution_store=_Store(ledgers[str(root)]),
        worktrees=_Worktrees(head, assignment),
        event_store=SimpleNamespace(read=lambda: ()),
    )

    with pytest.raises(MissionContinuationError, match="uncertain execution"):
        boundary.authorize()
