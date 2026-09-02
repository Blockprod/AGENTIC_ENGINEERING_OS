from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from agentic_engineering_os.application import (
    CodexEndToEndRuntime,
    CodexEndToEndRuntimeError,
    ControlPlaneSubmission,
    ParallelCodexGroupStatus,
    ParallelCodexGroupExecution,
    ParallelCodexMemberExecution,
    ParallelCodexRuntimeResult,
    ParallelDossierCodexRuntimeResult,
    ParallelMissionPlan,
    ParallelStoryDossier,
    ParallelStoryStage,
    CodexApprovalPolicy,
    CodexExecutionBinding,
    CodexSandboxMode,
    CompiledPrompt,
    PreparedImplementerContext,
    PreparedParallelGroup,
    RoleHandoff,
    SequentialCodexRuntimeResult,
    SingleRoleArtifacts,
    SingleRoleExecutionOutcome,
)
from agentic_engineering_os.application.execution_state import CodexExecutionStatus
from agentic_engineering_os.domain import (
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    ProjectState,
    UserStoryStatus,
)
from agentic_engineering_os.infrastructure import CodexRuntimeAdapter, CodexRuntimeConfiguration
from test_codex_result_intake import (
    architect_result,
    certifier_result,
    handoff,
    implementer_result,
    make_tester_result,
    reviewer_result,
    story,
)


COMMIT = "a" * 40
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class SingleExecutor:
    def __init__(self, result, *, validated: bool = True, role: MissionRole | None = None):
        self.result = result
        self.validated = validated
        self.role = role
        self.calls = 0

    def execute(self, role_handoff, **kwargs):
        self.calls += 1
        return SingleRoleExecutionOutcome(
            kwargs["request_id"],
            f"exec-{self.calls}",
            self.role or role_handoff.to_role,
            CodexExecutionStatus.VALIDATED if self.validated else CodexExecutionStatus.FAILED,
            self.validated,
            self.result if self.validated else None,
            False,
            False,
            () if self.validated else ("TRANSPORT_FAILED",),
        )


class SequentialWorkflow:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("accept_") or name == "submit_control_plane":
            def call(*args, **kwargs):
                self.calls.append((name, args, kwargs))
                return SimpleNamespace(status="AUTHORITATIVE")
            return call
        raise AttributeError(name)


def runtime(result, *, validated=True, role=None):
    execution = SingleExecutor(result, validated=validated, role=role)
    workflow = SequentialWorkflow()
    return CodexEndToEndRuntime(
        single_executor=execution,  # type: ignore[arg-type]
        sequential_workflow=workflow,  # type: ignore[arg-type]
    ), execution, workflow


@pytest.mark.parametrize(
    ("role", "result", "artifacts", "method"),
    (
        (MissionRole.ARCHITECT, architect_result(COMMIT), SingleRoleArtifacts(), "accept_architect"),
        (MissionRole.IMPLEMENTER, implementer_result(COMMIT), SingleRoleArtifacts(), "accept_implementer"),
        (
            MissionRole.TESTER,
            make_tester_result(COMMIT),
            SingleRoleArtifacts(implementer_result=implementer_result(COMMIT)),
            "accept_tester",
        ),
        (
            MissionRole.REVIEWER,
            reviewer_result(COMMIT),
            SingleRoleArtifacts(
                implementer_result=implementer_result(COMMIT),
                tester_result=make_tester_result(COMMIT),
            ),
            "accept_reviewer",
        ),
    ),
)
def test_validated_role_result_is_handed_to_existing_sequential_workflow(
    role, result, artifacts, method
):
    bridge, _, workflow = runtime(result)

    outcome = bridge.execute_sequential_role(
        handoff(role, COMMIT),
        request_id=f"p4-10/{role.value}",
        artifacts=artifacts,
        updated_at=NOW,
    )

    assert isinstance(outcome, SequentialCodexRuntimeResult)
    assert outcome.handed_off is True
    assert workflow.calls[0][0] == method


def test_failed_or_recovery_execution_never_advances_workflow():
    bridge, _, workflow = runtime(implementer_result(COMMIT), validated=False)

    outcome = bridge.execute_sequential_role(
        handoff(MissionRole.IMPLEMENTER, COMMIT),
        request_id="p4-10/interrupted",
        updated_at=NOW,
    )

    assert outcome.handed_off is False
    assert workflow.calls == []


def test_forged_cross_role_result_is_refused_before_workflow():
    bridge, _, workflow = runtime(reviewer_result(COMMIT))

    with pytest.raises(CodexEndToEndRuntimeError, match="ROLE_RESULT_TYPE_MISMATCH"):
        bridge.execute_sequential_role(
            handoff(MissionRole.TESTER, COMMIT),
            request_id="p4-10/forged",
            artifacts=SingleRoleArtifacts(implementer_result=implementer_result(COMMIT)),
            updated_at=NOW,
        )

    assert workflow.calls == []


def test_swapped_execution_role_is_refused_before_workflow():
    bridge, _, workflow = runtime(
        implementer_result(COMMIT), role=MissionRole.REVIEWER
    )

    with pytest.raises(CodexEndToEndRuntimeError, match="ROLE_RESULT_BINDING_MISMATCH"):
        bridge.execute_sequential_role(
            handoff(MissionRole.IMPLEMENTER, COMMIT),
            request_id="p4-10/swapped-role",
            updated_at=NOW,
        )

    assert workflow.calls == []


def test_codex_certifier_cannot_invent_control_plane_submission():
    artifacts = SingleRoleArtifacts(
        architect_result=architect_result(COMMIT),
        implementer_result=implementer_result(COMMIT),
        tester_result=make_tester_result(COMMIT),
        reviewer_result=reviewer_result(COMMIT),
    )
    bridge, _, workflow = runtime(certifier_result(COMMIT))

    with pytest.raises(CodexEndToEndRuntimeError, match="CONTROL_PLANE_INPUT_MISSING"):
        bridge.execute_sequential_role(
            handoff(MissionRole.CERTIFIER, COMMIT),
            request_id="p4-10/self-certification",
            artifacts=artifacts,
            updated_at=NOW,
        )

    assert workflow.calls == []


def test_five_role_chain_uses_runtime_transport_then_existing_workflow_only():
    architect = architect_result(COMMIT)
    implementer = implementer_result(COMMIT)
    tester = make_tester_result(COMMIT)
    reviewer = reviewer_result(COMMIT)
    role_cases = (
        (MissionRole.ARCHITECT, architect, SingleRoleArtifacts(), None),
        (MissionRole.IMPLEMENTER, implementer, SingleRoleArtifacts(), None),
        (
            MissionRole.TESTER,
            tester,
            SingleRoleArtifacts(implementer_result=implementer),
            None,
        ),
        (
            MissionRole.REVIEWER,
            reviewer,
            SingleRoleArtifacts(implementer_result=implementer, tester_result=tester),
            None,
        ),
        (
            MissionRole.CERTIFIER,
            certifier_result(COMMIT),
            SingleRoleArtifacts(
                architect_result=architect,
                implementer_result=implementer,
                tester_result=tester,
                reviewer_result=reviewer,
            ),
            ControlPlaneSubmission((), object(), "Human/Certifier", COMMIT),  # type: ignore[arg-type]
        ),
    )
    workflow = SequentialWorkflow()

    for role, role_result, artifacts, control in role_cases:
        bridge = CodexEndToEndRuntime(
            single_executor=SingleExecutor(role_result),  # type: ignore[arg-type]
            sequential_workflow=workflow,  # type: ignore[arg-type]
        )
        outcome = bridge.execute_sequential_role(
            handoff(role, COMMIT),
            request_id=f"p4-10/chain/{role.value}",
            artifacts=artifacts,
            updated_at=NOW,
            control_plane=control,
        )
        assert outcome.handed_off is True

    assert [item[0] for item in workflow.calls] == [
        "accept_architect",
        "accept_implementer",
        "accept_tester",
        "accept_reviewer",
        "submit_control_plane",
    ]


class ParallelExecutor:
    def __init__(self, execution):
        self.execution = execution

    def execute_group(self, *args, **kwargs):
        return self.execution


class ParallelWorkflow:
    def __init__(self):
        self.submitted = []

    def submit_member(
        self, prepared, assignment_id, result, *, execution_id, implementer_input
    ):
        marker = SimpleNamespace(
            assignment_id=assignment_id,
            execution_id=execution_id,
            result=result,
        )
        self.submitted.append(marker)
        return marker

    def complete_group(self, prepared, members):
        return SimpleNamespace(status="P3_COMPLETED", member_results=members)


def parallel_case(*, successful=True, swapped=False):
    role_handoff = handoff(MissionRole.IMPLEMENTER, COMMIT)
    context = PreparedImplementerContext(
        "assignment-1", "US-0001", "C:/tmp/wt", "agentic/us-1", COMMIT, 6, role_handoff
    )
    prepared = PreparedParallelGroup(
        0, 0, ("US-0001",), ("assignment-1",), ("C:/tmp/wt",),
        ("agentic/us-1",), COMMIT, 6, (context,),
    )
    coordination = SimpleNamespace(
        project_state=ProjectState("1.0", [story(UserStoryStatus.IN_PROGRESS)], [], [], [])
    )
    plan = ParallelMissionPlan("P4.6", 6, COMMIT, None, None, None, None, coordination, None)  # type: ignore[arg-type]
    result = replace(implementer_result(COMMIT), workflow_generation=6)
    member = ParallelCodexMemberExecution(
        "US-swapped" if swapped else "US-0001",
        "assignment-1",
        "p4-10/assignment-1",
        "execution-1",
        None,
        result if successful else None,
        successful,
        () if successful else ("RECOVERY_REQUIRED",),
    )
    execution = ParallelCodexGroupExecution(
        0,
        (ParallelCodexGroupStatus.READY_FOR_P3_HANDOFF if successful else ParallelCodexGroupStatus.INCOMPLETE),
        1,
        (member,),
    )
    workflow = ParallelWorkflow()
    bridge = CodexEndToEndRuntime(
        single_executor=SingleExecutor(result),  # type: ignore[arg-type]
        sequential_workflow=SequentialWorkflow(),  # type: ignore[arg-type]
        parallel_executor=ParallelExecutor(execution),  # type: ignore[arg-type]
        parallel_workflow=workflow,  # type: ignore[arg-type]
    )
    return bridge, plan, prepared, workflow


def test_safe_parallel_results_are_returned_to_existing_p3_workflow():
    bridge, plan, prepared, workflow = parallel_case()

    outcome = bridge.execute_parallel_implementers(
        plan, prepared, request_id_prefix="p4-10/parallel"
    )

    assert isinstance(outcome, ParallelCodexRuntimeResult)
    assert outcome.handed_off is True
    assert len(workflow.submitted) == 1


def test_incomplete_parallel_execution_does_not_advance_p3():
    bridge, plan, prepared, workflow = parallel_case(successful=False)

    outcome = bridge.execute_parallel_implementers(
        plan, prepared, request_id_prefix="p4-10/restart"
    )

    assert outcome.handed_off is False
    assert workflow.submitted == []


def test_swapped_parallel_result_is_refused_before_p3_submission():
    bridge, plan, prepared, workflow = parallel_case(swapped=True)

    with pytest.raises(CodexEndToEndRuntimeError, match="PARALLEL_RESULT_BINDING_MISMATCH"):
        bridge.execute_parallel_implementers(
            plan, prepared, request_id_prefix="p4-10/swapped"
        )

    assert workflow.submitted == []


def test_duplicate_parallel_member_set_is_refused_before_p3_submission():
    bridge, plan, prepared, workflow = parallel_case()
    original = bridge._parallel.execution  # type: ignore[attr-defined]
    bridge._parallel.execution = replace(  # type: ignore[attr-defined]
        original, members=(original.members[0], original.members[0])
    )

    with pytest.raises(CodexEndToEndRuntimeError, match="PARALLEL_RESULT_SET_MISMATCH"):
        bridge.execute_parallel_implementers(
            plan, prepared, request_id_prefix="p4-10/incomplete-set"
        )

    assert workflow.submitted == []


class ValueStore:
    def __init__(self, value):
        self.value = value

    def load(self):
        return self.value


class DossierWorkflow:
    def __init__(self, role_handoff):
        self.role_handoff = role_handoff
        self.accepted = []

    def runtime_handoff(self, dossier, role):
        assert role is self.role_handoff.to_role
        return self.role_handoff

    def accept_tester(self, dossier, result):
        self.accepted.append((dossier, result))
        return replace(dossier, stage=ParallelStoryStage.REVIEW, tester_result=result)


class DossierFactory:
    def __init__(self, execution):
        self.execution = execution
        self.projected = None

    def create(self, role_handoff, mission_store):
        self.projected = mission_store.load()
        executor = object.__new__(__import__(
            "agentic_engineering_os.application.single_role_codex",
            fromlist=["SingleRoleCodexExecutor"],
        ).SingleRoleCodexExecutor)
        executor.execute = lambda *args, **kwargs: self.execution
        return executor


def test_post_merge_tester_uses_p3_handoff_and_returns_result_to_p3():
    role_handoff = handoff(MissionRole.TESTER, COMMIT)
    mission = MissionState(
        "1.0", role_handoff.mission_id, role_handoff.workflow_generation,
        MissionStatus.ACTIVE, MissionRole.ORCHESTRATOR, "Objective", role_handoff.subject,
        OperatingStep.ACT, "Continue P3", COMMIT, NOW, [],
    )
    dossier = ParallelStoryDossier(
        role_handoff.mission_id, role_handoff.workflow_generation, role_handoff.subject,
        COMMIT, ParallelStoryStage.TESTING, implementer_result(COMMIT),
    )
    tester = make_tester_result(COMMIT)
    execution = SingleRoleExecutionOutcome(
        "p4-10/post-merge", "execution-post-merge", MissionRole.TESTER,
        CodexExecutionStatus.VALIDATED, True, tester, False, False, (),
    )
    workflow = DossierWorkflow(role_handoff)
    factory = DossierFactory(execution)
    bridge = CodexEndToEndRuntime(
        single_executor=SingleExecutor(tester),  # type: ignore[arg-type]
        sequential_workflow=SequentialWorkflow(),  # type: ignore[arg-type]
        parallel_workflow=workflow,  # type: ignore[arg-type]
        parallel_mission_store=ValueStore(mission),
        dossier_executor_factory=factory,
    )

    outcome = bridge.execute_parallel_dossier_role(
        dossier,
        MissionRole.TESTER,
        request_id="p4-10/post-merge",
        artifacts=SingleRoleArtifacts(implementer_result=dossier.implementer_result),
        updated_at=NOW,
    )

    assert isinstance(outcome, ParallelDossierCodexRuntimeResult)
    assert outcome.handed_off is True
    assert outcome.dossier.stage is ParallelStoryStage.REVIEW
    assert factory.projected.role is MissionRole.TESTER
    assert workflow.accepted == [(dossier, tester)]


@pytest.mark.skipif(
    os.environ.get("AGENTIC_OS_RUN_CODEX_CANARY") != "1",
    reason="real Codex canary requires explicit opt-in",
)
def test_real_codex_structured_read_only_canary_uses_only_temporary_repo(
    tmp_path: Path,
):
    executable_text = shutil.which("codex")
    assert executable_text is not None
    executable = Path(executable_text).resolve()
    version = subprocess.run(
        [str(executable), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout.strip()
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "P4.10 Canary"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "p4.10@example.invalid"], check=True)
    (root / "README.md").write_text("isolated P4.10 canary\n", encoding="utf-8")
    schema = root / "canary.schema.json"
    schema.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["status"],
                "properties": {"status": {"type": "string", "const": "P4_10_CANARY_OK"}},
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "test: P4.10 canary"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip().casefold()
    prompt_text = (
        'Return exactly the JSON object {"status":"P4_10_CANARY_OK"}. '
        "Do not use tools and do not modify files."
    )
    prompt = CompiledPrompt(
        "p4-10-real-canary", "b" * 64, "P4.10", 10, MissionRole.ARCHITECT,
        "canary", str(root.resolve()), None, commit, "p4-10-canary@1.0",
        prompt_text, len(prompt_text), 10, 0,
    )
    binding = CodexExecutionBinding(
        prompt.request_id, prompt.context_fingerprint, prompt.mission_id,
        prompt.workflow_generation, prompt.role, prompt.subject, str(root.resolve()),
        commit, CodexSandboxMode.READ_ONLY, CodexApprovalPolicy.NEVER, 180,
        output_schema_path=str(schema.resolve()),
    )
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    observation = CodexRuntimeAdapter(
        CodexRuntimeConfiguration(
            executable=str(executable),
            expected_executable_path=str(executable),
            expected_executable_version=version,
            expected_executable_sha256=digest,
        )
    ).execute(prompt, binding)

    assert observation.exit_code == 0
    assert observation.final_output is not None
    assert json.loads(observation.final_output) == {"status": "P4_10_CANARY_OK"}
    assert observation.tool_failure_observed is False
    assert observation.git_after is not None and observation.git_after.clean is True
    assert observation.git_after.head_commit == commit
