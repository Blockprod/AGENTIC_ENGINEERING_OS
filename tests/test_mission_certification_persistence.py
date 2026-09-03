from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentic_engineering_os.application import (
    AcceptanceResult,
    CertificationContext,
    CertificationReference,
    ControlLoopError,
    GateEvaluationContext,
    MissionRequest,
    MissionCertificationCoordinator,
    MissionCertificationError,
    ORCHESTRATION_RECORD_VERSION,
    OrchestrationRecord,
    RoleExecutionReference,
    ParallelMissionWorkflow,
    ParallelStoryDossier,
    ParallelStoryStage,
)
from agentic_engineering_os.application.orchestration_record import record_to_data
from agentic_engineering_os.application.execution_state import (
    CodexExecutionRecord,
    CodexExecutionStatus,
    ExecutionExecutableIdentity,
    canonical_result_json,
    result_json_fingerprint,
)
from agentic_engineering_os.domain import (
    Certification,
    CertificationResult,
    EvidenceType,
    GateResult,
    MissionRole,
    ProjectState,
    to_dict,
)
from agentic_engineering_os.application.certifier import CertifierInput, CertifierVerdict
from agentic_engineering_os.infrastructure import OrchestrationRecordStore
from tests.test_control_loop import COMMIT, NOW, initialized_loop, observation, pass_contract
from agentic_engineering_os.application import request_fingerprint
from tests.test_parallel_mission_workflow import make_story, make_tester_result


class _MemoryRecordStore:
    def __init__(self, value):
        self.value = value
        self.writes = 0

    def load(self):
        return self.value

    def replace(self, value, *, expected_fingerprint):
        assert self.value.fingerprint == expected_fingerprint
        self.value = value
        self.writes += 1


def _tester_execution(result, commit: str, *, request_id: str = "request-tester"):
    result_json = canonical_result_json(to_dict(result))
    return CodexExecutionRecord(
        "cx-" + "0" * 24,
        "a" * 64,
        request_id,
        "b" * 64,
        result.mission_id,
        result.workflow_generation,
        MissionRole.TESTER,
        result.subject,
        "C:/repository",
        None,
        "C:/repository",
        commit,
        "c" * 64,
        "tester-result@1.0",
        ExecutionExecutableIdentity("C:/codex.exe", "test", "d" * 64),
        CodexExecutionStatus.VALIDATED,
        NOW,
        NOW,
        validated_result_json=result_json,
        validated_result_fingerprint=result_json_fingerprint(result_json),
    )


def test_legacy_record_hydrates_empty_certification_references(tmp_path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    state = repository / ".agentic-engineering-os"
    state.mkdir()
    request = MissionRequest("Objective", str(repository))
    legacy = record_to_data(
        OrchestrationRecord(
            ORCHESTRATION_RECORD_VERSION,
            "mission-one",
            request,
            request_fingerprint(request),
            COMMIT,
            0,
        )
    )
    legacy["schema_version"] = "1.1"
    legacy.pop("certification_references")
    (state / "orchestration.json").write_text(
        json.dumps(legacy, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    restored = OrchestrationRecordStore(repository).load()

    assert restored.schema_version == ORCHESTRATION_RECORD_VERSION
    assert restored.certification_references == ()


def test_certification_reference_is_story_generation_scoped(tmp_path) -> None:
    request = MissionRequest("Objective", str(tmp_path))
    # Construct the smallest valid planned record.
    current = OrchestrationRecord(
        ORCHESTRATION_RECORD_VERSION,
        "mission-one",
        request,
        request_fingerprint(request),
        COMMIT,
        0,
        "a" * 64,
        (RoleExecutionReference(MissionRole.ARCHITECT, "US-1", 0, "r", "e", "b" * 64),),
        ("US-1",),
    )
    reference = CertificationReference("US-1", 0, "CERT-1", "c" * 64, COMMIT)

    assert current.with_certification_reference(reference).certification_references == (reference,)
    with pytest.raises(ValueError, match="active planning"):
        current.with_certification_reference(
            CertificationReference("US-2", 0, "CERT-2", "d" * 64, COMMIT)
        )


def test_certification_reference_round_trips_canonically(tmp_path) -> None:
    request = MissionRequest("Objective", str(tmp_path))
    record = OrchestrationRecord(
        ORCHESTRATION_RECORD_VERSION,
        "mission-one",
        request,
        request_fingerprint(request),
        COMMIT,
        0,
        "a" * 64,
        (RoleExecutionReference(MissionRole.ARCHITECT, "US-1", 0, "r", "e", "b" * 64),),
        ("US-1",),
        certification_references=(
            CertificationReference("US-1", 0, "CERT-1", "c" * 64, COMMIT),
        ),
    )
    store = OrchestrationRecordStore(tmp_path)
    store.initialize(record)

    assert store.load() == record


def test_control_loop_recognizes_exact_deterministic_certification(tmp_path) -> None:
    _, loop = initialized_loop(tmp_path)
    loop.record_evidence(
        observation("AC-001", evidence_type=EvidenceType.ACCEPTANCE_CRITERION_CHECK),
        evidence_id="EV-AC-001",
        timestamp=NOW,
    )
    loop.record_evidence(
        observation("US-0001"), evidence_id="EV-GATE-001", timestamp=NOW
    )
    loop.evaluate_gate(
        pass_contract(),
        context=GateEvaluationContext(expected_commit=COMMIT),
        evaluated_at=NOW,
    )
    inputs = (AcceptanceResult("AC-001", GateResult.PASS, ("EV-AC-001",)),)

    first = loop.certify_user_story(
        "US-0001",
        COMMIT,
        inputs,
        certifier="Codex/Certifier",
        context=CertificationContext(),
        certification_id="CERT-DETERMINISTIC",
        certified_at=NOW,
    )
    second = loop.certify_user_story_idempotent(
        "US-0001",
        COMMIT,
        inputs,
        certifier="Codex/Certifier",
        context=CertificationContext(),
        certification_id="CERT-DETERMINISTIC",
        certified_at=None,
    )

    assert second == first
    assert len(loop.load_state().certifications) == 1


def test_control_loop_rejects_divergent_deterministic_certification(tmp_path) -> None:
    _, loop = initialized_loop(tmp_path)
    loop.record_evidence(
        observation("AC-001", evidence_type=EvidenceType.ACCEPTANCE_CRITERION_CHECK),
        evidence_id="EV-AC-001",
        timestamp=NOW,
    )
    loop.record_evidence(
        observation("US-0001"), evidence_id="EV-GATE-001", timestamp=NOW
    )
    loop.evaluate_gate(
        pass_contract(),
        context=GateEvaluationContext(expected_commit=COMMIT),
        evaluated_at=NOW,
    )
    inputs = (AcceptanceResult("AC-001", GateResult.PASS, ("EV-AC-001",)),)
    loop.certify_user_story(
        "US-0001",
        COMMIT,
        inputs,
        certifier="Codex/Certifier",
        certification_id="CERT-COLLISION",
        certified_at=NOW,
    )

    with pytest.raises(ControlLoopError, match="CERTIFICATION_COLLISION"):
        loop.certify_user_story_idempotent(
            "US-0001",
            COMMIT,
            inputs,
            certifier="Human/Other",
            certification_id="CERT-COLLISION",
            certified_at=NOW,
        )


def test_validated_role_reference_is_persisted_once_and_reused(tmp_path) -> None:
    request = MissionRequest("Objective", str(tmp_path))
    architect = RoleExecutionReference(
        MissionRole.ARCHITECT, "US-0001", 0, "architect-request", "architect-execution", "e" * 64
    )
    record = OrchestrationRecord(
        ORCHESTRATION_RECORD_VERSION,
        "P3.11",
        request,
        request_fingerprint(request),
        COMMIT,
        0,
        "f" * 64,
        (architect,),
        ("US-0001",),
    )
    result = make_tester_result(make_story("US-0001"), COMMIT)
    execution = _tester_execution(result, COMMIT)
    records = _MemoryRecordStore(record)
    coordinator = object.__new__(MissionCertificationCoordinator)
    coordinator._executions = SimpleNamespace(load=lambda: SimpleNamespace(records=(execution,)))
    coordinator._records = records

    restored, persisted = coordinator._restore_role(
        record, MissionRole.TESTER, "US-0001", COMMIT
    )
    replayed, replay_record = coordinator._restore_role(
        persisted, MissionRole.TESTER, "US-0001", COMMIT
    )

    assert restored == replayed == result
    assert replay_record == persisted == records.value
    assert records.writes == 1


def test_ambiguous_validated_role_execution_fails_closed(tmp_path) -> None:
    request = MissionRequest("Objective", str(tmp_path))
    record = OrchestrationRecord(
        ORCHESTRATION_RECORD_VERSION,
        "P3.11",
        request,
        request_fingerprint(request),
        COMMIT,
        0,
        "f" * 64,
        (RoleExecutionReference(MissionRole.ARCHITECT, "US-0001", 0, "a", "b", "e" * 64),),
        ("US-0001",),
    )
    result = make_tester_result(make_story("US-0001"), COMMIT)
    first = _tester_execution(result, COMMIT)
    second = replace(first, request_id="another-request", execution_id="cx-" + "1" * 24)
    coordinator = object.__new__(MissionCertificationCoordinator)
    coordinator._executions = SimpleNamespace(load=lambda: SimpleNamespace(records=(first, second)))
    coordinator._records = _MemoryRecordStore(record)

    with pytest.raises(MissionCertificationError, match="ROLE_EXECUTION_AMBIGUOUS"):
        coordinator._restore_role(record, MissionRole.TESTER, "US-0001", COMMIT)


def test_certification_reference_callback_precedes_terminal_transition(monkeypatch) -> None:
    events: list[str] = []
    certification = Certification(
        "CERT-1",
        "US-0001",
        CertificationResult.CERTIFIED,
        COMMIT,
        {},
        {},
        {},
        (),
        NOW,
        "Codex/Certifier",
    )

    class _Control:
        def certify_user_story_idempotent(self, *args, **kwargs):
            events.append("certification")
            return certification

        def transition_user_story(self, *args, **kwargs):
            events.append("transition")

    workflow = object.__new__(ParallelMissionWorkflow)
    workflow._require_no_pending_transaction = lambda: None
    workflow._require_dossier = lambda *args: None
    workflow._story = lambda identifier: make_story(identifier)
    workflow._project_store = SimpleNamespace(load=lambda: ProjectState("1.0"))
    workflow._dossier_handoff = lambda *args: object()
    workflow._certifier_validator = SimpleNamespace(
        validate=lambda *args, **kwargs: SimpleNamespace(is_valid=True)
    )
    workflow._control_loop = _Control()
    monkeypatch.setattr(
        CertifierInput, "from_integrated_handoff", staticmethod(lambda *args, **kwargs: object())
    )
    dossier = ParallelStoryDossier(
        "P3.11",
        0,
        "US-0001",
        COMMIT,
        ParallelStoryStage.CERTIFICATION,
        object(),
        object(),
        tester_result=object(),
        reviewer_result=object(),
    )
    candidate = SimpleNamespace(
        verdict=CertifierVerdict.READY_FOR_CONTROL_PLANE,
        blockers=(),
        findings=(),
    )

    def interrupted(_):
        events.append("reference")
        raise RuntimeError("simulated persistence interruption")

    with pytest.raises(RuntimeError, match="simulated persistence interruption"):
        workflow.submit_certifier(
            dossier,
            candidate,
            architect_result=object(),
            acceptance_results=(),
            certification_context=CertificationContext(),
            certifier="Codex/Certifier",
            updated_at=NOW,
            certification_id="CERT-1",
            certification_persisted=interrupted,
        )

    assert events == ["certification", "reference"]

    events.clear()
    completed = workflow.submit_certifier(
        dossier,
        candidate,
        architect_result=object(),
        acceptance_results=(),
        certification_context=CertificationContext(),
        certifier="Codex/Certifier",
        updated_at=NOW,
        certification_id="CERT-1",
        certification_persisted=lambda _: events.append("reference"),
    )

    assert completed.stage is ParallelStoryStage.CERTIFIED
    assert events == ["certification", "reference", "transition"]
