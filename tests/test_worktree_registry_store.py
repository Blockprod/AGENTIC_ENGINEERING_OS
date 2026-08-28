import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

import agentic_engineering_os
from agentic_engineering_os import infrastructure
from agentic_engineering_os._worktree_registry_write import _issue_registry_write
from agentic_engineering_os.domain import (
    WorktreeAssignment,
    WorktreeRegistry,
    WorktreeStatus,
)
from agentic_engineering_os.infrastructure import PersistenceError, WorktreeRegistryStore
from agentic_engineering_os.infrastructure._worktree_identity import (
    derive_assignment_id,
    derive_branch_name,
)
import agentic_engineering_os.infrastructure.worktree_registry_store as store_module


COMMIT = "1" * 40


def assignment(tmp_path: Path, *, story_id: str = "US-0001") -> WorktreeAssignment:
    identifier = derive_assignment_id("mission-1", story_id, 0, COMMIT)
    return WorktreeAssignment(
        assignment_id=identifier,
        mission_id="mission-1",
        user_story_id=story_id,
        workflow_generation=0,
        baseline_commit=COMMIT,
        branch_name=derive_branch_name(story_id, 0, identifier),
        worktree_path=str((tmp_path / "worktrees" / identifier).resolve()),
        status=WorktreeStatus.PLANNED,
        result_commit=None,
    )


def authorized_save(
    store: WorktreeRegistryStore,
    before: WorktreeRegistry,
    candidate: WorktreeRegistry,
    operation: str,
) -> None:
    authorization = _issue_registry_write(
        store=store,
        before=before,
        candidate=candidate,
        operation=operation,
    )
    store._save_authorized(
        candidate,
        authorization=authorization,
        operation=operation,
    )


def test_registry_initialization_is_explicit_and_non_overwriting(tmp_path: Path) -> None:
    store = WorktreeRegistryStore(tmp_path)
    created = store.initialize()

    assert created == WorktreeRegistry(schema_version="1.0", assignments=())
    assert store.load() == created
    with pytest.raises(PersistenceError, match="REGISTRY_ALREADY_EXISTS"):
        store.initialize()


def test_load_absent_never_falls_back_to_empty(tmp_path: Path) -> None:
    with pytest.raises(PersistenceError, match="REGISTRY_ABSENT"):
        WorktreeRegistryStore(tmp_path).load()


@pytest.mark.parametrize(
    "content",
    [
        "{not-json",
        '{"schema_version":"1.0","schema_version":"1.0","assignments":[]}',
    ],
)
def test_invalid_or_duplicate_json_is_refused_without_fallback(
    tmp_path: Path, content: str
) -> None:
    store = WorktreeRegistryStore(tmp_path)
    store.initialize()
    store.registry_path.write_text(content, encoding="utf-8")

    with pytest.raises(PersistenceError, match="INVALID_JSON"):
        store.load()


def test_registry_snapshots_are_immutable_and_have_no_public_save(tmp_path: Path) -> None:
    store = WorktreeRegistryStore(tmp_path)
    original = store.initialize()
    item = assignment(tmp_path)
    candidate = WorktreeRegistry(schema_version="1.0", assignments=(item,))
    authorized_save(store, original, candidate, "PLAN")
    loaded = store.load()

    with pytest.raises(FrozenInstanceError):
        loaded.assignments = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        loaded.assignments[0].status = WorktreeStatus.COMPLETED  # type: ignore[misc]
    assert not hasattr(store, "save")
    assert not hasattr(agentic_engineering_os, "_issue_registry_write")
    assert not hasattr(infrastructure, "_issue_registry_write")


def test_arbitrary_valid_candidate_cannot_be_persisted_without_capability(
    tmp_path: Path,
) -> None:
    store = WorktreeRegistryStore(tmp_path)
    original = store.initialize()
    item = assignment(tmp_path)
    planned = WorktreeRegistry(schema_version="1.0", assignments=(item,))
    authorized_save(store, original, planned, "PLAN")
    forged_item = replace(
        item,
        status=WorktreeStatus.COMPLETED,
        result_commit="2" * 40,
    )
    forged = WorktreeRegistry(schema_version="1.0", assignments=(forged_item,))

    with pytest.raises(PersistenceError, match="WRITE_NOT_AUTHORIZED"):
        store._save_authorized(
            forged,
            authorization=None,
            operation="COMPLETE",
        )

    assert store.load() == planned


def test_capability_is_bound_to_store_before_candidate_and_operation(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = WorktreeRegistryStore(first_root)
    second = WorktreeRegistryStore(second_root)
    before = first.initialize()
    second.initialize()
    item = assignment(tmp_path)
    candidate = WorktreeRegistry(schema_version="1.0", assignments=(item,))
    authorization = _issue_registry_write(
        store=first,
        before=before,
        candidate=candidate,
        operation="PLAN",
    )

    with pytest.raises(PersistenceError, match="WRITE_NOT_AUTHORIZED"):
        second._save_authorized(
            candidate,
            authorization=authorization,
            operation="PLAN",
        )


def test_illegal_lifecycle_transition_is_refused_even_with_exact_capability(
    tmp_path: Path,
) -> None:
    store = WorktreeRegistryStore(tmp_path)
    original = store.initialize()
    item = assignment(tmp_path)
    planned = WorktreeRegistry(schema_version="1.0", assignments=(item,))
    authorized_save(store, original, planned, "PLAN")
    cleaned = WorktreeRegistry(
        schema_version="1.0",
        assignments=(replace(item, status=WorktreeStatus.CLEANED),),
    )

    with pytest.raises(PersistenceError, match="INVALID_REGISTRY_TRANSITION"):
        authorized_save(store, planned, cleaned, "CLEANUP")

    assert store.load() == planned


def test_duplicate_non_cleaned_branch_and_path_are_refused_on_load(
    tmp_path: Path,
) -> None:
    store = WorktreeRegistryStore(tmp_path)
    store.initialize()
    first = assignment(tmp_path, story_id="US-0001")
    second = assignment(tmp_path, story_id="US-0002")
    raw_assignments = [
        _as_json(first),
        {
            **_as_json(second),
            "branch_name": first.branch_name,
            "worktree_path": first.worktree_path,
        },
    ]
    raw = {
        "schema_version": "1.0",
        "assignments": sorted(raw_assignments, key=lambda item: item["assignment_id"]),
    }
    store.registry_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(PersistenceError, match="DUPLICATE_ACTIVE_RESOURCE"):
        store.load()


def test_noncanonical_assignment_order_is_refused(tmp_path: Path) -> None:
    store = WorktreeRegistryStore(tmp_path)
    store.initialize()
    first = assignment(tmp_path, story_id="US-0001")
    second = assignment(tmp_path, story_id="US-0002")
    ordered = sorted((first, second), key=lambda item: item.assignment_id, reverse=True)
    store.registry_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "assignments": [_as_json(item) for item in ordered],
            }
        ),
        encoding="utf-8",
    )

    if ordered[0].assignment_id > ordered[1].assignment_id:
        with pytest.raises(PersistenceError, match="NON_CANONICAL_REGISTRY"):
            store.load()


def test_atomic_write_failure_preserves_old_registry_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorktreeRegistryStore(tmp_path)
    original = store.initialize()
    item = assignment(tmp_path)
    candidate = WorktreeRegistry(schema_version="1.0", assignments=(item,))
    authorization = _issue_registry_write(
        store=store,
        before=original,
        candidate=candidate,
        operation="PLAN",
    )
    original_bytes = store.registry_path.read_bytes()

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(store_module.os, "replace", fail_replace)
    with pytest.raises(PersistenceError, match="WRITE_FAILED"):
        store._save_authorized(
            candidate,
            authorization=authorization,
            operation="PLAN",
        )

    assert store.registry_path.read_bytes() == original_bytes
    assert not list(store.registry_path.parent.glob(".worktrees.*.tmp"))


def _as_json(item: WorktreeAssignment) -> dict[str, object]:
    return {
        "assignment_id": item.assignment_id,
        "mission_id": item.mission_id,
        "user_story_id": item.user_story_id,
        "workflow_generation": item.workflow_generation,
        "baseline_commit": item.baseline_commit,
        "branch_name": item.branch_name,
        "worktree_path": item.worktree_path,
        "status": item.status.value,
        "result_commit": item.result_commit,
    }
