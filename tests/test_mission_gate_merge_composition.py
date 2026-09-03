from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from agentic_engineering_os.application import (
    ImplementerInput,
    ImplementerResult,
    ImplementerVerdict,
    IntegrationGateClassification,
    MissionIntegrationCoordinator,
    MissionIntegrationStatus,
    MissionRequest,
    ORCHESTRATION_RECORD_VERSION,
    OrchestrationRecord,
    ParallelCodexGroupExecution,
    ParallelCodexGroupStatus,
    ParallelCodexMemberExecution,
    ParallelCodexRuntimeResult,
    ParallelIntegrationReference,
    RoleExecutionReference,
    TransitionContext,
    VerificationOutcome,
    VerificationResult,
    request_fingerprint,
)
from agentic_engineering_os.application.mission_integration import (
    _plan_reference_fingerprint,
)
from agentic_engineering_os.domain import MissionRole, UserStoryStatus
from agentic_engineering_os.infrastructure import OrchestrationRecordStore
from tests._validated_execution_ledger import write_validated_implementer_execution
from tests.test_parallel_mission_workflow import COMMAND, NOW, git, make_harness, make_story


class FakeImplementerRuntime:
    def __init__(self, workflow) -> None:
        self.workflow = workflow
        self.calls = 0

    def execute_parallel_implementers(
        self, plan, prepared_group, *, request_id_prefix
    ) -> ParallelCodexRuntimeResult:
        self.calls += 1
        members = []
        executions = []
        for context in prepared_group.contexts:
            story = next(
                item
                for item in plan.coordination_input.project_state.user_stories
                if item.id == context.user_story_id
            )
            story = replace(story, status=UserStoryStatus.IN_PROGRESS)
            relative = f"src/{story.id.casefold().replace('us-', '')}.py"
            target = Path(context.worktree_path) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("VALUE = 1\n", encoding="utf-8")
            result = ImplementerResult(
                plan.mission_id,
                plan.workflow_generation,
                story.id,
                story.id,
                plan.baseline_commit,
                "Fake Implementer completed the exact change.",
                (relative,),
                (),
                (COMMAND,),
                (VerificationResult(COMMAND, True, VerificationOutcome.PASS, 0, "pass"),),
                (),
                (),
                (),
                MissionRole.TESTER,
                ImplementerVerdict.READY_FOR_TEST,
            )
            execution_id = write_validated_implementer_execution(
                context, result, observed_at=NOW
            )
            member = self.workflow.submit_member(
                prepared_group,
                context.assignment_id,
                result,
                execution_id=execution_id,
                implementer_input=ImplementerInput.from_handoff(context.handoff, story),
            )
            members.append(member)
            executions.append(
                ParallelCodexMemberExecution(
                    story.id,
                    context.assignment_id,
                    f"{request_id_prefix}-{story.id}",
                    execution_id,
                    None,
                    result,
                    True,
                    (),
                )
            )
        group = self.workflow.complete_group(prepared_group, tuple(members))
        execution = ParallelCodexGroupExecution(
            prepared_group.group_index,
            ParallelCodexGroupStatus.READY_FOR_P3_HANDOFF,
            1,
            tuple(executions),
        )
        return ParallelCodexRuntimeResult(execution, group)


def setup(tmp_path):
    harness = make_harness(tmp_path, (make_story("US-0001", allowed=("src/0001.py",)),))
    harness.control.transition_user_story(
        "US-0001",
        UserStoryStatus.READY,
        context=TransitionContext(preconditions_proven=True),
    )
    request = MissionRequest(
        "Execute a parallel mission end to end.", str(harness.root), (), ()
    )
    orchestration = OrchestrationRecordStore(harness.root)
    orchestration.initialize(
        OrchestrationRecord(
            ORCHESTRATION_RECORD_VERSION,
            "P3.11",
            request,
            request_fingerprint(request),
            harness.baseline,
            0,
            "a" * 64,
            (
                RoleExecutionReference(
                    MissionRole.ARCHITECT,
                    "US-0001",
                    0,
                    "architect-request",
                    "architect-execution",
                    "b" * 64,
                ),
            ),
            ("US-0001",),
        )
    )
    runtime = FakeImplementerRuntime(harness.workflow)
    coordinator = MissionIntegrationCoordinator(
        workflow=harness.workflow,
        runtime=runtime,
        mission_store=harness.mission_store,
        project_store=harness.project_store,
        record_store=orchestration,
    )
    return harness, orchestration, runtime, coordinator


def test_golden_path_and_completed_restart_are_exactly_once(tmp_path: Path) -> None:
    harness, records, runtime, coordinator = setup(tmp_path)

    first = coordinator.resume("P3.11", updated_at=NOW)
    integrated = git(harness.root, "rev-parse", "HEAD").casefold()
    commit_count = git(harness.root, "rev-list", "--count", f"{harness.baseline}..HEAD")
    restarted_runtime = FakeImplementerRuntime(harness.restarted())
    restarted = MissionIntegrationCoordinator(
        workflow=restarted_runtime.workflow,
        runtime=restarted_runtime,
        mission_store=harness.mission_store,
        project_store=harness.project_store,
        record_store=OrchestrationRecordStore(harness.root),
    )
    second = restarted.resume("P3.11", updated_at=NOW)

    assert first.status is second.status is MissionIntegrationStatus.READY_FOR_TESTER
    assert first.integrated_commit == second.integrated_commit == integrated
    assert first.integrated_contexts == second.integrated_contexts
    assert first.integrated_contexts[0].integrated_commit == integrated
    assert first.implementer_results[0].observed_commit == harness.baseline
    assert runtime.calls == 1
    assert restarted_runtime.calls == 0
    assert git(harness.root, "rev-list", "--count", f"{harness.baseline}..HEAD") == commit_count
    assert harness.project_store.load().user_stories[0].status is UserStoryStatus.TESTING
    assert records.load().parallel_integration.integrated_commit == integrated


def test_restart_after_preparation_reuses_assignment(tmp_path: Path) -> None:
    harness, records, runtime, coordinator = setup(tmp_path)
    plan = harness.workflow.plan_current()
    prepared = harness.workflow.prepare_group(plan, 0)
    before = prepared.assignment_ids

    result = coordinator.resume("P3.11", updated_at=NOW)

    assert result.status is MissionIntegrationStatus.READY_FOR_TESTER
    assert runtime.calls == 1
    assert records.load().parallel_integration.assignment_ids == before
    assert len(harness.manager.registry_store.load().assignments) == 1


def test_restart_after_gate_pass_recovers_without_implementer_replay(tmp_path: Path) -> None:
    harness, records, runtime, coordinator = setup(tmp_path)
    plan = harness.workflow.plan_current()
    prepared = harness.workflow.prepare_group(plan, 0)
    executed = runtime.execute_parallel_implementers(
        plan, prepared, request_id_prefix="precrash"
    )
    gate_attempt = harness.workflow.evaluate_group(plan, executed.group_result)
    from agentic_engineering_os.application.integration_gate import integration_gate_fingerprint

    reference = ParallelIntegrationReference(
        _plan_reference_fingerprint(plan, prepared),
        prepared.wave_index,
        prepared.group_index,
        prepared.assignment_ids,
        integration_gate_fingerprint(gate_attempt.gate_result),
    )
    current = records.load()
    records.replace(
        current.with_parallel_integration(reference),
        expected_fingerprint=current.fingerprint,
    )
    runtime.calls = 0

    result = coordinator.resume("P3.11", updated_at=NOW)

    assert result.status is MissionIntegrationStatus.READY_FOR_TESTER
    assert runtime.calls == 0


def test_restart_after_merge_before_recording_recognizes_exact_merge(tmp_path: Path) -> None:
    harness, records, runtime, coordinator = setup(tmp_path)
    plan = harness.workflow.plan_current()
    prepared = harness.workflow.prepare_group(plan, 0)
    executed = runtime.execute_parallel_implementers(
        plan, prepared, request_id_prefix="lost-merge-return"
    )
    gate_attempt = harness.workflow.evaluate_group(plan, executed.group_result)
    from agentic_engineering_os.application.integration_gate import integration_gate_fingerprint

    reference = ParallelIntegrationReference(
        _plan_reference_fingerprint(plan, prepared),
        prepared.wave_index,
        prepared.group_index,
        prepared.assignment_ids,
        integration_gate_fingerprint(gate_attempt.gate_result),
    )
    current = records.load()
    records.replace(
        current.with_parallel_integration(reference),
        expected_fingerprint=current.fingerprint,
    )
    merged = harness.workflow.merge_gated_group(gate_attempt, updated_at=NOW)
    integration_commit = merged.merge_result.integration_commit
    count = git(harness.root, "rev-list", "--count", f"{harness.baseline}..HEAD")
    harness.control.transition_user_story(
        "US-0001",
        UserStoryStatus.IMPLEMENTED,
        context=TransitionContext(preconditions_proven=True),
    )
    runtime.calls = 0

    result = coordinator.resume("P3.11", updated_at=NOW)

    assert result.status is MissionIntegrationStatus.READY_FOR_TESTER
    assert result.integrated_commit == integration_commit
    assert runtime.calls == 0
    assert git(harness.root, "rev-list", "--count", f"{harness.baseline}..HEAD") == count
    assert records.load().parallel_integration.integrated_commit == integration_commit
    assert harness.project_store.load().user_stories[0].status is UserStoryStatus.TESTING


def test_merge_without_gate_pass_is_impossible(tmp_path: Path) -> None:
    harness, _, runtime, _ = setup(tmp_path)
    plan = harness.workflow.plan_current()
    prepared = harness.workflow.prepare_group(plan, 0)
    executed = runtime.execute_parallel_implementers(
        plan, prepared, request_id_prefix="blocked"
    )
    attempt = harness.workflow.evaluate_group(plan, executed.group_result)
    forged = replace(
        attempt,
        gate_result=replace(
            attempt.gate_result, result=IntegrationGateClassification.FAIL
        ),
    )

    try:
        harness.workflow.merge_gated_group(forged, updated_at=NOW)
    except Exception as error:
        assert getattr(error, "code", None) == "GATE_NOT_PASS"
    else:
        raise AssertionError("merge without Gate PASS was accepted")
    assert git(harness.root, "rev-parse", "HEAD").casefold() == harness.baseline
