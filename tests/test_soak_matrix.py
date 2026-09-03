"""Protected repeated stability exercises for the supported Windows V1 path."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_engineering_os.domain import MissionStatus
from agentic_engineering_os.infrastructure import OperationalEventStore

from test_operational_event_store import _event
from test_parallel_mission_workflow import (
    NOW,
    certify_member,
    git,
    implement_group,
    implement_group_from_prepared,
    make_harness,
    make_story,
)


pytestmark = pytest.mark.soak


def _finish_current_group(harness, workflow):
    plan = workflow.plan_current()
    _, implementations, group = implement_group(harness, workflow, plan, 0)
    attempt = workflow.integrate_group(plan, group, updated_at=NOW)
    for story_id in plan.execution_plan.groups[0].user_story_ids:
        certify_member(
            harness,
            harness.restarted(),
            attempt,
            story_id,
            implementations[story_id],
        )
    return attempt


def _finalize(harness) -> None:
    workflow = harness.restarted()
    head = git(harness.root, "rev-parse", "HEAD").casefold()
    result = workflow.finalize(current_commit=head, updated_at=NOW)
    assert result.status is MissionStatus.COMPLETED
    assert harness.mission_store.load().status is MissionStatus.COMPLETED


@pytest.mark.parametrize("cycle", range(10))
def test_ten_sequential_single_story_missions_complete(
    tmp_path: Path, cycle: int
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    _finish_current_group(harness, harness.workflow)
    _finalize(harness)


@pytest.mark.parametrize("cycle", range(5))
def test_five_parallel_groups_are_deterministic_and_complete(
    tmp_path: Path, cycle: int
) -> None:
    harness = make_harness(
        tmp_path, (make_story("US-0001"), make_story("US-0002"))
    )
    attempt = _finish_current_group(harness, harness.workflow)
    assert attempt.gate_result.integration_order == ("US-0001", "US-0002")
    _finalize(harness)


@pytest.mark.parametrize("cycle", range(5))
def test_five_implementer_failures_recover_in_new_generations(
    tmp_path: Path, cycle: int
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    plan = harness.workflow.plan_current()
    prepared = harness.workflow.prepare_group(plan, 0)
    failed = harness.workflow.fail_member(prepared, prepared.assignment_ids[0])
    remediation = harness.restarted().remediate_failed_group(
        plan,
        failed,
        affected_user_story_ids=("US-0001",),
        updated_at=NOW,
    )
    assert remediation.new_generation == 1
    _finish_current_group(harness, harness.restarted())
    _finalize(harness)


@pytest.mark.parametrize(
    "boundary",
    ("plan", "preparation", "implementation", "gate", "merge"),
)
def test_five_distinct_restart_boundaries_preserve_exactly_once_progress(
    tmp_path: Path, boundary: str
) -> None:
    harness = make_harness(tmp_path, (make_story("US-0001"),))
    plan = harness.workflow.plan_current()
    workflow = harness.restarted() if boundary == "plan" else harness.workflow
    prepared = workflow.prepare_group(plan, 0)
    if boundary == "preparation":
        workflow = harness.restarted()
    _, implementations, group = implement_group_from_prepared(
        harness, workflow, prepared
    )
    if boundary == "implementation":
        workflow = harness.restarted()
    gate_attempt = workflow.evaluate_group(plan, group)
    if boundary == "gate":
        workflow = harness.restarted()
    merged = workflow.merge_gated_group(gate_attempt, updated_at=NOW)
    if boundary == "merge":
        workflow = harness.restarted()
    certify_member(
        harness,
        workflow,
        merged,
        "US-0001",
        implementations["US-0001"],
    )
    after_certification = harness.restarted()
    head = git(harness.root, "rev-parse", "HEAD").casefold()
    assert (
        after_certification.finalize(current_commit=head, updated_at=NOW).status
        is MissionStatus.COMPLETED
    )


def test_event_store_rotates_through_at_least_three_segments(tmp_path: Path) -> None:
    store = OperationalEventStore(
        tmp_path, max_segment_bytes=1_024, max_segments=4
    )
    for index in range(3):
        store.append(
            _event(
                index + 1,
            )
        )
    segments = tuple(store.event_directory.glob("segment-*.jsonl"))
    assert len(segments) >= 3
    assert len(store.read()) == 3
