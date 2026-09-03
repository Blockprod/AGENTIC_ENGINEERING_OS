from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from agentic_engineering_os.application import (
    MissionAdmission,
    MissionAdmissionStatus,
    MissionCertificationResult,
    MissionCertificationStatus,
    MissionIntegrationResult,
    MissionIntegrationError,
    MissionIntegrationStatus,
    IntegrationGateClassification,
    MissionPhase,
    MissionPlanningResult,
    MissionPlanningStatus,
    MissionRequest,
    MissionRoleLaunchResult,
    MissionRunStatus,
    MissionRunner,
    ORCHESTRATION_RECORD_VERSION,
    OrchestrationRecord,
    ParallelIntegrationReference,
    ParallelMissionResult,
    ParallelStoryDossier,
    ParallelStoryStage,
    RoleExecutionReference,
    request_fingerprint,
)
from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    Evidence,
    EvidenceType,
    HumanApproval,
    MaintenanceAdmission,
    MaintenanceAdmissionDecision,
    MaintenanceAdmissionReason,
    MaintenanceOperation,
    MaintenanceState,
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    ProjectState,
    RiskLevel,
    ProjectConfiguration,
    RepositoryRootPolicy,
    ProjectPathPolicy,
    CodexProjectConstraints,
    CodexSandboxConstraint,
    CodexApprovalConstraint,
    MissionStateGitPolicy,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
)


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
HEAD = "a" * 40


class Store:
    def __init__(self, value):
        self.value = value

    def load(self):
        return self.value


class RecordStore(Store):
    def replace(self, record, *, expected_fingerprint):
        assert self.value.fingerprint == expected_fingerprint
        self.value = record


def story(identifier: str, dependency: str | None = None) -> UserStory:
    return UserStory(
        "1.0",
        identifier,
        f"Story {identifier}",
        "Exercise bounded composition.",
        UserStoryStatus.PLANNED,
        1,
        RiskLevel.LOW,
        () if dependency is None else (dependency,),
        UserStoryScope(("src",), ()),
        (AcceptanceCriterion(f"AC-{identifier[-1]}", "Proven behavior", True),),
        (),
        HumanApproval(False, False, None, None),
        UserStoryMetadata(NOW, "Codex/Architect", NOW),
    )


class Admission:
    def __init__(self, value):
        self.value = value

    def evaluate(self, request):
        return self.value


class Planning:
    def __init__(self, mission_id):
        self.mission_id = mission_id

    def start(self, request, admission, *, updated_at):
        return MissionPlanningResult(
            MissionPlanningStatus.PLANNED,
            self.mission_id,
            "US-0001",
            (),
            None,
        )

    def resume(self, mission_id, *, updated_at):
        return self.start(None, None, updated_at=updated_at)


class Integration:
    def __init__(self, project, records):
        self.project = project
        self.records = records

    def resume(self, mission_id, *, updated_at):
        if self.records.value.parallel_integration is None:
            current = self.records.value
            self.records.replace(
                current.with_parallel_integration(
                    ParallelIntegrationReference(
                        "b" * 64, 0, 0, ("assignment-1", "assignment-2"), "c" * 64, HEAD
                    )
                ),
                expected_fingerprint=current.fingerprint,
            )
            self.project.value.user_stories = [
                replace(item, status=UserStoryStatus.TESTING)
                for item in self.project.value.user_stories
            ]
        return MissionIntegrationResult(
            MissionIntegrationStatus.READY_FOR_TESTER,
            mission_id,
            0,
            ("US-0001", "US-0002"),
            HEAD,
            (),
            MissionRole.TESTER,
            (object(), object()),  # type: ignore[arg-type]
            (object(), object()),  # type: ignore[arg-type]
        )


class Certification:
    def __init__(self, project):
        self.project = project

    def resume(self, mission_id, story_id, *, updated_at):
        item = next(value for value in self.project.value.user_stories if value.id == story_id)
        status = {
            UserStoryStatus.TESTING: MissionCertificationStatus.WAITING_FOR_TESTER,
            UserStoryStatus.REVIEW: MissionCertificationStatus.WAITING_FOR_REVIEWER,
            UserStoryStatus.CERTIFICATION: MissionCertificationStatus.WAITING_FOR_CERTIFIER,
            UserStoryStatus.CERTIFIED: MissionCertificationStatus.CERTIFIED,
        }[item.status]
        stage = {
            UserStoryStatus.TESTING: ParallelStoryStage.TESTING,
            UserStoryStatus.REVIEW: ParallelStoryStage.REVIEW,
            UserStoryStatus.CERTIFICATION: ParallelStoryStage.CERTIFICATION,
            UserStoryStatus.CERTIFIED: ParallelStoryStage.CERTIFIED,
        }[item.status]
        dossier = ParallelStoryDossier(
            mission_id, 0, story_id, HEAD, stage, object(), object()  # type: ignore[arg-type]
        )
        return MissionCertificationResult(status, mission_id, 0, story_id, HEAD, dossier)


class Roles:
    def __init__(self, project, *, fail_role=None):
        self.project = project
        self.fail_role = fail_role
        self.calls = []

    def execute(self, dossier, role, *, request_id, updated_at):
        self.calls.append((dossier.user_story_id, role))
        if role is self.fail_role:
            return MissionRoleLaunchResult(False, (f"{role.value}_FAILED",))
        target = {
            MissionRole.TESTER: UserStoryStatus.REVIEW,
            MissionRole.REVIEWER: UserStoryStatus.CERTIFICATION,
            MissionRole.CERTIFIER: UserStoryStatus.CERTIFIED,
        }[role]
        self.project.value.user_stories = [
            replace(item, status=target) if item.id == dossier.user_story_id else item
            for item in self.project.value.user_stories
        ]
        return MissionRoleLaunchResult(True)


class Verification:
    def __init__(self, blockers=()):
        self.calls = []
        self.blockers = blockers

    def verify(self, configuration, user_story, **values):
        self.calls.append(user_story.id)
        return SimpleNamespace(gates=(), blockers=self.blockers)


class HumanControl:
    def __init__(self, projects):
        self.projects = projects

    def record_evidence(self, observation, *, evidence_id=None, timestamp=None):
        evidence = Evidence(
            evidence_id,
            EvidenceType.HUMAN_APPROVAL,
            observation.subject,
            observation.result,
            observation.provenance.source,
            observation.command,
            observation.exit_code,
            observation.artifact,
            observation.commit,
            timestamp,
            observation.provenance.producer,
        )
        self.projects.value.evidence.append(evidence)
        return evidence

    def apply_human_approval(self, story_id, evidence_id, *, expected_commit):
        story_value = next(
            item for item in self.projects.value.user_stories if item.id == story_id
        )
        evidence = next(
            item for item in self.projects.value.evidence
            if item.evidence_id == evidence_id
        )
        story_value.human_approval.approved = True
        story_value.human_approval.approved_by = evidence.producer
        story_value.human_approval.approved_at = evidence.timestamp
        story_value.human_approval.evidence_ref = evidence_id
        return object()


class Workflow:
    def __init__(self, missions):
        self.missions = missions
        self.finalized = 0
        self.remediation_calls = []

    def finalize(self, *, current_commit, updated_at):
        self.finalized += 1
        self.missions.value = replace(
            self.missions.value,
            status=MissionStatus.COMPLETED,
            operating_step=OperatingStep.REPORT,
        )
        return ParallelMissionResult(
            self.missions.value.mission_id,
            0,
            MissionStatus.COMPLETED,
            None,
            None,
            (),
            "complete",
        )

    def remediate_integration(
        self, attempt, *, affected_user_story_ids, updated_at
    ):
        self.remediation_calls.append(tuple(affected_user_story_ids))
        generation = self.missions.value.workflow_generation + 1
        self.missions.value = replace(
            self.missions.value,
            workflow_generation=generation,
            observed_commit=HEAD,
        )
        return SimpleNamespace(new_generation=generation, baseline_commit=HEAD)


def harness(tmp_path: Path, *, fail_role=None, verification_blockers=()):
    request = MissionRequest("Complete two stories", str(tmp_path))
    admission = MissionAdmission(
        MissionAdmissionStatus.ADMITTED,
        request_fingerprint(request),
        "d" * 64,
        HEAD,
        "project",
        (),
        (),
        "start",
        MaintenanceAdmission(
            MaintenanceOperation.START_MISSION,
            MaintenanceState.NORMAL,
            MaintenanceAdmissionDecision.ADMITTED,
            (MaintenanceAdmissionReason.NORMAL_OPERATION,),
            "9" * 64,
            NOW,
        ),
    )
    mission = MissionState(
        "1.0", "mission-1", 0, MissionStatus.ACTIVE, MissionRole.ORCHESTRATOR,
        request.objective, "US-0001", OperatingStep.ACT, "continue", HEAD, NOW, [],
    )
    missions = Store(mission)
    projects = Store(ProjectState("1.0", project_id="project", user_stories=[story("US-0001"), story("US-0002", "US-0001")]))
    record = OrchestrationRecord(
        ORCHESTRATION_RECORD_VERSION,
        mission.mission_id,
        request,
        request_fingerprint(request),
        HEAD,
        0,
        "e" * 64,
        (
            RoleExecutionReference(
                MissionRole.ARCHITECT,
                "US-0001",
                0,
                "architect-request",
                "architect-execution",
                "f" * 64,
            ),
        ),
        ("US-0001", "US-0002"),
    )
    records = RecordStore(record)
    roles = Roles(projects, fail_role=fail_role)
    verification = Verification(verification_blockers)
    configuration = Store(
        ProjectConfiguration(
            "1.0",
            "project",
            RepositoryRootPolicy.CONFIG_PARENT_GIT_ROOT,
            (),
            (),
            ProjectPathPolicy(("src",), (), (".git",)),
            (),
            CodexProjectConstraints(
                CodexSandboxConstraint.WORKSPACE_WRITE,
                CodexApprovalConstraint.NEVER,
                True,
                1,
            ),
            MissionStateGitPolicy.IGNORED,
        )
    )
    workflow = Workflow(missions)
    runner = MissionRunner(
        admission=Admission(admission),
        planning=Planning(mission.mission_id),  # type: ignore[arg-type]
        integration=Integration(projects, records),  # type: ignore[arg-type]
        certification=Certification(projects),  # type: ignore[arg-type]
        verification=verification,  # type: ignore[arg-type]
        control_loop=HumanControl(projects),  # type: ignore[arg-type]
        role_executor=roles,
        workflow=workflow,  # type: ignore[arg-type]
        mission_store=missions,
        project_store=projects,
        record_store=records,
        configuration_store=configuration,
    )
    return request, runner, missions, projects, records, roles, workflow, verification


def test_runner_completes_every_role_and_finalizes_multi_story_mission(tmp_path: Path) -> None:
    request, runner, missions, _, records, roles, workflow, verification = harness(tmp_path)

    result = runner.run(request, updated_at=NOW)

    assert result.status is MissionRunStatus.COMPLETED
    assert result.phase is MissionPhase.REPORT
    assert result.completed_story_ids == ("US-0001", "US-0002")
    assert result.current_story_ids == ()
    assert missions.value.status is MissionStatus.COMPLETED
    assert records.value.parallel_integration is None
    assert roles.calls == [
        (story_id, role)
        for story_id in ("US-0001", "US-0002")
        for role in (MissionRole.TESTER, MissionRole.REVIEWER, MissionRole.CERTIFIER)
    ]
    assert workflow.finalized == 1
    assert verification.calls == ["US-0001", "US-0002"]


def test_runner_stops_without_retry_when_role_result_is_not_validated(tmp_path: Path) -> None:
    request, runner, missions, _, _, roles, workflow, _ = harness(
        tmp_path, fail_role=MissionRole.REVIEWER
    )

    result = runner.run(request, updated_at=NOW)

    assert result.status is MissionRunStatus.BLOCKED
    assert result.phase is MissionPhase.REVIEW
    assert result.blockers == ("REVIEWER_FAILED",)
    assert roles.calls.count(("US-0001", MissionRole.REVIEWER)) == 1
    assert missions.value.status is MissionStatus.ACTIVE
    assert workflow.finalized == 0


def test_runner_stops_before_tester_when_verification_is_not_positive(
    tmp_path: Path,
) -> None:
    request, runner, missions, _, _, roles, workflow, verification = harness(
        tmp_path, verification_blockers=("COMMAND_TIMEOUT",)
    )

    result = runner.run(request, updated_at=NOW)

    assert result.status is MissionRunStatus.REMEDIATION_REQUIRED
    assert result.phase is MissionPhase.VERIFICATION
    assert result.blockers == ("COMMAND_TIMEOUT",)
    assert verification.calls == ["US-0001"]
    assert roles.calls == []
    assert missions.value.status is MissionStatus.ACTIVE
    assert workflow.finalized == 0


def test_resume_records_and_applies_exact_human_evidence_before_continuing(
    tmp_path: Path,
) -> None:
    _, runner, _, projects, _, _, _, _ = harness(tmp_path)
    projects.value.user_stories[0].human_approval = HumanApproval(
        True, False, None, None
    )
    evidence = Evidence(
        "EV-HUMAN-1",
        EvidenceType.HUMAN_APPROVAL,
        "US-0001",
        True,
        "Human",
        None,
        None,
        None,
        HEAD,
        NOW,
        "Human/Alice",
    )

    result = runner.resume("mission-1", updated_at=NOW, human_evidence=evidence)

    assert result.status is MissionRunStatus.COMPLETED
    assert projects.value.user_stories[0].human_approval.approved
    assert projects.value.user_stories[0].human_approval.evidence_ref == "EV-HUMAN-1"
    assert projects.value.evidence == [evidence]


def test_controlled_divergence_is_reported_as_recovery_not_internal_error(
    tmp_path: Path,
) -> None:
    request, runner, *_ = harness(tmp_path)

    class DivergentIntegration:
        def resume(self, mission_id, *, updated_at):
            raise MissionIntegrationError("HEAD_DIVERGED", "controlled divergence")

    runner._integration = DivergentIntegration()  # type: ignore[attr-defined]

    result = runner.run(request, updated_at=NOW)

    assert result.status is MissionRunStatus.RECOVERY_REQUIRED
    assert result.blockers == ("HEAD_DIVERGED",)


class IntegrationGateFailure:
    def __init__(self) -> None:
        self.calls = 0

    def resume(self, mission_id, *, updated_at):
        self.calls += 1
        if self.calls > 1:
            return MissionIntegrationResult(
                MissionIntegrationStatus.BLOCKED,
                mission_id,
                1,
                ("US-0001", "US-0002"),
                None,
                ("IMPLEMENTER_GROUP_INCOMPLETE",),
                None,
            )
        attempt = SimpleNamespace(
            gate_result=SimpleNamespace(
                result=IntegrationGateClassification.FAIL,
                findings=(SimpleNamespace(members=("US-0001",)),),
            ),
            merge_result=None,
        )
        return MissionIntegrationResult(
            MissionIntegrationStatus.BLOCKED,
            mission_id,
            0,
            ("US-0001", "US-0002"),
            None,
            ("INTEGRATION_GATE_FAIL",),
            None,
            remediation_attempt=attempt,  # type: ignore[arg-type]
        )


def test_run_reports_gate_fail_without_opening_a_generation(tmp_path: Path) -> None:
    request, runner, missions, _, _, _, workflow, _ = harness(tmp_path)
    integration = IntegrationGateFailure()
    runner._integration = integration  # type: ignore[attr-defined]

    result = runner.run(request, updated_at=NOW)

    assert result.status is MissionRunStatus.REMEDIATION_REQUIRED
    assert result.blockers == ("INTEGRATION_GATE_FAIL",)
    assert missions.value.workflow_generation == 0
    assert workflow.remediation_calls == []
    assert integration.calls == 1


def test_explicit_resume_opens_exactly_one_gate_remediation_generation(
    tmp_path: Path,
) -> None:
    _, runner, missions, _, records, _, workflow, _ = harness(tmp_path)
    integration = IntegrationGateFailure()
    runner._integration = integration  # type: ignore[attr-defined]

    result = runner.resume("mission-1", updated_at=NOW)

    assert result.status is MissionRunStatus.BLOCKED
    assert result.blockers == ("IMPLEMENTER_GROUP_INCOMPLETE",)
    assert result.generation == 1
    assert missions.value.workflow_generation == 1
    assert records.value.workflow_generation == 1
    assert workflow.remediation_calls == [("US-0001",)]
    assert integration.calls == 2
