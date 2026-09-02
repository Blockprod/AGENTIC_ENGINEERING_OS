from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic_engineering_os.application import (
    CODEX_V1_ALWAYS_REQUIRED,
    MissionAdmissionStatus,
    MissionCapabilitySnapshot,
    MissionReadinessPrecheck,
    MissionRequest,
    create_operational_capability_proof,
)
from agentic_engineering_os.application.codex_capabilities import (
    CodexCapability,
    CodexCapabilityFinding,
    CodexCapabilityStatus,
    CodexDiscoveryProvenance,
    CodexOperationalCapabilityClass,
    CodexOperationalCapabilityStatus,
    create_discovered_assessment,
)
from agentic_engineering_os.application.execution_state import (
    EXECUTION_LEDGER_VERSION,
    CodexExecutionLedger,
)
from agentic_engineering_os.domain import (
    AgenticOsInitializationState,
    CodexApprovalConstraint,
    CodexProjectConstraints,
    CodexSandboxConstraint,
    MaintenanceAdmission,
    MaintenanceAdmissionDecision,
    MaintenanceAdmissionReason,
    MaintenanceOperation,
    MaintenanceState,
    MissionRole,
    MissionState,
    MissionStateGitPolicy,
    MissionStatus,
    ObservationClassification,
    OperatingStep,
    ObservedValue,
    ProjectConfiguration,
    ProjectPathPolicy,
    ProjectState,
    RepositoryRootPolicy,
    RepositorySupportStatus,
)


NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
HEAD = "a" * 40
DIGEST = "b" * 64
ENVIRONMENT = "c" * 64
PROJECT = "project-one"


class StoreError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ValueStore:
    def __init__(self, value=None, error: str | None = None) -> None:
        self.value = value
        self.error = error

    def load(self):
        if self.error:
            raise StoreError(self.error)
        return self.value


class FakeReconnaissance:
    def __init__(self, profile) -> None:
        self.profile = profile
        self.calls = 0

    def inspect(self, repository_root: str):
        self.calls += 1
        return self.profile


class FakeMaintenance:
    def __init__(self, decision=MaintenanceAdmissionDecision.ADMITTED) -> None:
        self.decision = decision
        self.calls = 0

    def evaluate_start_mission(self, **values):
        self.calls += 1
        reason = (
            MaintenanceAdmissionReason.GOVERNANCE_REQUIRES_HUMAN
            if self.decision is MaintenanceAdmissionDecision.HUMAN_REQUIRED
            else MaintenanceAdmissionReason.NORMAL_OPERATION
        )
        return MaintenanceAdmission(
            MaintenanceOperation.START_MISSION,
            MaintenanceState.NORMAL,
            self.decision,
            (reason,),
            "d" * 64,
            values["evaluated_at"],
        )


class FakeCapabilities:
    def __init__(self, snapshot: MissionCapabilitySnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def inspect(self, request, configuration):
        self.calls += 1
        return self.snapshot


def configuration() -> ProjectConfiguration:
    return ProjectConfiguration(
        "1.0",
        PROJECT,
        RepositoryRootPolicy.CONFIG_PARENT_GIT_ROOT,
        (),
        (),
        ProjectPathPolicy(("src", "tests"), ("docs",), (".git",)),
        (),
        CodexProjectConstraints(
            CodexSandboxConstraint.WORKSPACE_WRITE,
            CodexApprovalConstraint.NEVER,
            True,
            2,
        ),
        MissionStateGitPolicy.IGNORED,
    )


def profile(root: Path, config: ProjectConfiguration, **overrides):
    fact = lambda value: ObservedValue(ObservationClassification.FACT, value, "test", "test")
    values = {
        "support_status": RepositorySupportStatus.SUPPORTED,
        "scan_complete": True,
        "agentic_state": AgenticOsInitializationState.INITIALIZED,
        "head": HEAD,
        "clean": True,
        "detached": False,
    }
    values.update(overrides)
    from agentic_engineering_os.application import project_configuration_fingerprint

    return SimpleNamespace(
        requested_root=str(root.resolve()),
        support_status=values["support_status"],
        scan_complete=values["scan_complete"],
        agentic_os=SimpleNamespace(
            state=values["agentic_state"],
            config_semantic_fingerprint=project_configuration_fingerprint(config),
        ),
        git=SimpleNamespace(
            is_repository=fact(True),
            top_level=fact(str(root.resolve())),
            head_commit=fact(values["head"]),
            clean=fact(values["clean"]),
            detached=fact(values["detached"]),
        ),
    )


def capability_snapshot(
    root: Path,
    *,
    statuses: dict[CodexOperationalCapabilityClass, CodexOperationalCapabilityStatus] | None = None,
    observed_at: datetime = NOW,
) -> MissionCapabilitySnapshot:
    executable = str((root / "codex.exe").resolve())
    findings = tuple(
        CodexCapabilityFinding(item, CodexCapabilityStatus.SUPPORTED, "test")
        for item in CodexCapability
    )
    assessment = create_discovered_assessment(
        executable_path=executable,
        executable_sha256=DIGEST,
        executable_version="codex 1.0",
        discovery_provenance=CodexDiscoveryProvenance.TEST_INJECTION_STATIC_HELP,
        platform="win32",
        findings=findings,
        observed_at=observed_at,
    )
    status_by_class = statuses or {}
    proofs = tuple(
        create_operational_capability_proof(
            executable_path=executable,
            executable_sha256=DIGEST,
            executable_version="codex 1.0",
            capability_class=item,
            sandbox=CodexSandboxConstraint.WORKSPACE_WRITE.value,
            approval_policy=CodexApprovalConstraint.NEVER.value,
            environment_fingerprint=ENVIRONMENT,
            status=status_by_class.get(item, CodexOperationalCapabilityStatus.PROVEN),
            detail="test operational fact",
            diagnostic_code=(
                "CAPABILITY_BLOCKED_BY_HOST_POLICY"
                if status_by_class.get(item) is CodexOperationalCapabilityStatus.UNPROVEN
                else "OPERATIONAL_PROBE_RESULT"
            ),
            observed_at=observed_at,
        )
        for item in (
            CodexOperationalCapabilityClass.REPOSITORY_READ,
            CodexOperationalCapabilityClass.WORKSPACE_EDIT,
            CodexOperationalCapabilityClass.COMMAND_EXECUTION,
            CodexOperationalCapabilityClass.GIT_OBSERVATION,
        )
    )
    return MissionCapabilitySnapshot(assessment, proofs, ENVIRONMENT)


def harness(
    tmp_path: Path,
    *,
    profile_overrides=None,
    project_state=None,
    mission_state=None,
    mission_error="MISSION_ABSENT",
    ledger_error="LEDGER_ABSENT",
    maintenance=None,
    capabilities=None,
):
    config = configuration()
    recon = FakeReconnaissance(profile(tmp_path, config, **(profile_overrides or {})))
    maintenance = maintenance or FakeMaintenance()
    capabilities = capabilities or FakeCapabilities(capability_snapshot(tmp_path))
    service = MissionReadinessPrecheck(
        capability_provider=capabilities,
        maintenance_provider=maintenance,
        reconnaissance=recon,
        configuration_loader_factory=lambda root: ValueStore(config),
        project_state_store_factory=lambda root: ValueStore(
            project_state or ProjectState("1.0", project_id=PROJECT)
        ),
        mission_state_store_factory=lambda root: ValueStore(
            mission_state, error=mission_error
        ),
        execution_state_store_factory=lambda root: ValueStore(
            CodexExecutionLedger(EXECUTION_LEDGER_VERSION, ()), error=ledger_error
        ),
        now=lambda: NOW,
    )
    return service, recon, maintenance, capabilities


def request(tmp_path: Path, **values) -> MissionRequest:
    return MissionRequest(
        values.get("objective", "Add a bounded feature"),
        str(tmp_path),
        values.get("requested_scope", ("src",)),
        values.get("verification_command_ids", ()),
    )


def codes(result) -> set[str]:
    return {item.code for item in result.blockers}


def test_nominal_fake_admission_is_read_only_and_defers_structured_result(tmp_path: Path) -> None:
    service, recon, maintenance, capabilities = harness(tmp_path)
    result = service.evaluate(request(tmp_path))
    assert result.status is MissionAdmissionStatus.ADMITTED
    assert result.repository_head == HEAD
    assert result.project_id == PROJECT
    assert not result.blockers
    assert not result.missing_capabilities
    assert recon.calls == maintenance.calls == capabilities.calls == 1
    assert CodexOperationalCapabilityClass.STRUCTURED_RESULT not in result.missing_capabilities


def test_non_adopted_repository_blocks_before_state_or_capability_work(tmp_path: Path) -> None:
    service, _, maintenance, capabilities = harness(
        tmp_path,
        profile_overrides={"agentic_state": AgenticOsInitializationState.UNINITIALIZED},
    )
    result = service.evaluate(request(tmp_path))
    assert result.status is MissionAdmissionStatus.BLOCKED
    assert "REPOSITORY_NOT_ADOPTED" in codes(result)
    assert maintenance.calls == capabilities.calls == 0


@pytest.mark.parametrize(
    ("overrides", "expected"),
    (({"clean": False}, "GIT_NOT_CLEAN"), ({"head": None}, "GIT_HEAD_UNKNOWN"), ({"detached": True}, "GIT_DETACHED_OR_UNKNOWN")),
)
def test_incompatible_git_facts_block_before_governance(tmp_path: Path, overrides, expected) -> None:
    service, _, maintenance, capabilities = harness(tmp_path, profile_overrides=overrides)
    result = service.evaluate(request(tmp_path))
    assert expected in codes(result)
    assert maintenance.calls == capabilities.calls == 0


def test_invalid_project_state_binding_fails_closed(tmp_path: Path) -> None:
    service, _, maintenance, capabilities = harness(
        tmp_path, project_state=ProjectState("1.0", project_id="other")
    )
    result = service.evaluate(request(tmp_path))
    assert "PROJECT_STATE_BINDING_MISMATCH" in codes(result)
    assert maintenance.calls == capabilities.calls == 0


@pytest.mark.parametrize("status", (MissionStatus.ACTIVE, MissionStatus.BLOCKED))
def test_nonterminal_or_stale_mission_blocks_before_governance(
    tmp_path: Path, status: MissionStatus
) -> None:
    mission = MissionState(
        "1.0",
        "mission-old",
        0,
        status,
        MissionRole.ORCHESTRATOR,
        "Old objective",
        "mission-old",
        OperatingStep.RECONSTRUCT,
        "resume",
        "e" * 40,
        NOW,
        [],
    )
    service, _, maintenance, capabilities = harness(
        tmp_path, mission_state=mission, mission_error=None
    )
    result = service.evaluate(request(tmp_path))
    assert {"MISSION_NOT_TERMINAL", "STALE_MISSION_BASELINE"} <= codes(result)
    assert maintenance.calls == capabilities.calls == 0


def test_recovery_pending_blocks_before_capability_inspection(tmp_path: Path) -> None:
    maintenance = FakeMaintenance(MaintenanceAdmissionDecision.REFUSED)
    service, _, maintenance, capabilities = harness(tmp_path, maintenance=maintenance)
    result = service.evaluate(request(tmp_path))
    assert result.status is MissionAdmissionStatus.BLOCKED
    assert "MAINTENANCE_REFUSED" in codes(result)
    assert maintenance.calls == 1
    assert capabilities.calls == 0


def test_human_required_is_distinct_and_never_synthesized(tmp_path: Path) -> None:
    maintenance = FakeMaintenance(MaintenanceAdmissionDecision.HUMAN_REQUIRED)
    service, _, _, capabilities = harness(tmp_path, maintenance=maintenance)
    result = service.evaluate(request(tmp_path))
    assert result.status is MissionAdmissionStatus.HUMAN_REQUIRED
    assert codes(result) == {"HUMAN_AUTHORITY_REQUIRED"}
    assert capabilities.calls == 0


def test_current_host_mutating_capabilities_block_before_any_role(tmp_path: Path) -> None:
    blocked = {
        CodexOperationalCapabilityClass.WORKSPACE_EDIT: CodexOperationalCapabilityStatus.UNPROVEN,
        CodexOperationalCapabilityClass.COMMAND_EXECUTION: CodexOperationalCapabilityStatus.UNPROVEN,
    }
    capabilities = FakeCapabilities(capability_snapshot(tmp_path, statuses=blocked))
    service, _, maintenance, capabilities = harness(tmp_path, capabilities=capabilities)
    result = service.evaluate(request(tmp_path))
    assert result.status is MissionAdmissionStatus.BLOCKED
    assert result.missing_capabilities == (
        CodexOperationalCapabilityClass.COMMAND_EXECUTION,
        CodexOperationalCapabilityClass.WORKSPACE_EDIT,
    )
    assert all("CAPABILITY_BLOCKED_BY_HOST_POLICY" in item.detail for item in result.blockers)
    assert maintenance.calls == capabilities.calls == 1


def test_stale_operational_proofs_are_not_reinterpreted_as_supported(tmp_path: Path) -> None:
    snapshot = capability_snapshot(tmp_path, observed_at=NOW - timedelta(minutes=6))
    service, _, _, _ = harness(tmp_path, capabilities=FakeCapabilities(snapshot))
    result = service.evaluate(request(tmp_path))
    assert result.status is MissionAdmissionStatus.BLOCKED
    assert set(result.missing_capabilities) == {
        CodexOperationalCapabilityClass.REPOSITORY_READ,
        CodexOperationalCapabilityClass.WORKSPACE_EDIT,
        CodexOperationalCapabilityClass.COMMAND_EXECUTION,
        CodexOperationalCapabilityClass.GIT_OBSERVATION,
    }
    assert "CODEX_CAPABILITY_ASSESSMENT_STALE_OR_FORGED" in codes(result)


def test_forged_or_wrong_class_proof_is_refused(tmp_path: Path) -> None:
    valid = capability_snapshot(tmp_path)
    forged = replace(valid.operational_proofs[0], _attestation="")
    duplicate_wrong_class = replace(
        valid,
        operational_proofs=(forged, *valid.operational_proofs[1:]),
    )
    service, _, _, _ = harness(
        tmp_path, capabilities=FakeCapabilities(duplicate_wrong_class)
    )
    result = service.evaluate(request(tmp_path))
    assert result.missing_capabilities == (
        CodexOperationalCapabilityClass.REPOSITORY_READ,
    )


def test_scope_and_verification_ids_are_bound_to_project_policy(tmp_path: Path) -> None:
    service, _, maintenance, capabilities = harness(tmp_path)
    result = service.evaluate(
        request(
            tmp_path,
            requested_scope=("docs",),
            verification_command_ids=("unknown",),
        )
    )
    assert codes(result) == {"REQUEST_SCOPE_NOT_ALLOWED", "REQUEST_SCOPE_PROTECTED", "UNKNOWN_VERIFICATION_COMMAND"}
    assert maintenance.calls == capabilities.calls == 0


@pytest.mark.parametrize(
    "values",
    (
        {"objective": "  "},
        {"requested_scope": ("../outside",)},
        {"requested_scope": ("tests", "src")},
        {"verification_command_ids": ("bad id",)},
    ),
)
def test_request_contract_rejects_noncanonical_input(tmp_path: Path, values) -> None:
    with pytest.raises(ValueError):
        request(tmp_path, **values)


def test_static_contract_is_checked_even_when_operational_proofs_exist(tmp_path: Path) -> None:
    snapshot = capability_snapshot(tmp_path)
    unsupported = tuple(
        CodexCapabilityFinding(
            item.capability,
            CodexCapabilityStatus.UNSUPPORTED if item.capability is CodexCapability.JSONL else item.status,
            item.detail,
        )
        for item in snapshot.assessment.findings
    )
    altered = replace(
        snapshot,
        assessment=create_discovered_assessment(
            executable_path=snapshot.assessment.executable_path,
            executable_sha256=DIGEST,
            executable_version="codex 1.0",
            discovery_provenance=CodexDiscoveryProvenance.TEST_INJECTION_STATIC_HELP,
            platform="win32",
            findings=unsupported,
            observed_at=NOW,
        ),
    )
    service, _, _, _ = harness(tmp_path, capabilities=FakeCapabilities(altered))
    result = service.evaluate(request(tmp_path))
    assert "CODEX_STATIC_CAPABILITY_UNAVAILABLE" in codes(result)
    assert CodexCapability.JSONL in CODEX_V1_ALWAYS_REQUIRED
