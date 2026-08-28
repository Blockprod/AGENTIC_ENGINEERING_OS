from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_engineering_os._authoritative_write import _issue_authoritative_write
from agentic_engineering_os.application import (
    CertificationService,
    ContractValidator,
    ControlLoop,
    ControlLoopError,
    EvidenceObservation,
    EvidenceProvenance,
    EvidenceRecorder,
    GateEvaluator,
    HumanApprovalError,
    HumanApprovalService,
    Orchestrator,
    ProvenanceKind,
    StateTransitionService,
)
from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    Evidence,
    EvidenceType,
    HumanApproval,
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    ProjectState,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
    to_dict,
)
from agentic_engineering_os.infrastructure import (
    MissionStateStore,
    PersistenceError,
    ProjectStateStore,
)


COMMIT = "b7c485b8d885dfbbdab202fd2eb44a1f917936d2"
OTHER_COMMIT = "a" * 40
NOW = datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc)


def story(identifier: str = "US-0001") -> UserStory:
    return UserStory(
        schema_version="1.0",
        id=identifier,
        title="Apply a Human decision",
        description="Require an attributable Human approval.",
        status=UserStoryStatus.REVIEW,
        priority=1,
        risk=RiskLevel.HIGH,
        depends_on=(),
        scope=UserStoryScope(allowed_paths=("src/",), forbidden_paths=()),
        acceptance_criteria=(
            AcceptanceCriterion("AC-001", "Human decision is applied.", True),
        ),
        required_gates=(),
        human_approval=HumanApproval(True, False, None, None),
        metadata=UserStoryMetadata(NOW, "human-operator", NOW),
    )


def human_evidence(
    *,
    evidence_id: str = "EV-HUMAN",
    subject: str = "US-0001",
    result: object = True,
    producer: str = "Alice",
    commit: str | None = COMMIT,
    evidence_type: EvidenceType = EvidenceType.HUMAN_APPROVAL,
    source: str = "Human",
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=evidence_type,
        subject=subject,
        result=result,  # type: ignore[arg-type]
        source=source,
        command=None,
        exit_code=None,
        artifact="operator decision",
        commit=commit,
        timestamp=NOW,
        producer=producer,
    )


def loop(store: object) -> ControlLoop:
    validator = ContractValidator()
    return ControlLoop(
        state_store=store,  # type: ignore[arg-type]
        evidence_recorder_factory=lambda target: EvidenceRecorder(
            target,
            validator=validator,
            clock=lambda: NOW,
        ),
        gate_evaluator=GateEvaluator(validator=validator, clock=lambda: NOW),
        certification_service=CertificationService(
            validator=validator,
            clock=lambda: NOW,
        ),
        transition_service=StateTransitionService(),
    )


def initialize(tmp_path: Path) -> tuple[ProjectStateStore, ControlLoop]:
    store = ProjectStateStore(tmp_path)
    current = store.initialize()
    state = store.load()
    state.user_stories.append(story())
    operation = "TEST_SETUP_PROJECT_STATE"
    authorization = _issue_authoritative_write(
        store_kind="PROJECT_STATE",
        store=store,
        before_state=current,
        candidate_state=state,
        operation=operation,
    )
    store.save(state, authorization=authorization, operation=operation)
    return store, loop(store)


def record_human(loop_instance: ControlLoop, *, result: bool = True) -> Evidence:
    return loop_instance.record_evidence(
        EvidenceObservation(
            evidence_type=EvidenceType.HUMAN_APPROVAL,
            subject="US-0001",
            result=result,
            provenance=EvidenceProvenance(
                kind=ProvenanceKind.HUMAN,
                source="Human",
                producer="Alice",
            ),
            repository_dependent=True,
            artifact="operator decision",
            commit=COMMIT,
        ),
        evidence_id="EV-HUMAN",
        timestamp=NOW,
    )


def test_evaluate_is_non_mutating_and_apply_changes_only_human_approval() -> None:
    service = HumanApprovalService()
    current = story()
    before = to_dict(current)

    evaluated = service.evaluate(current, human_evidence(), expected_commit=COMMIT)

    assert evaluated.subject == "US-0001"
    assert evaluated.evidence_id == "EV-HUMAN"
    assert evaluated.applied is True
    assert evaluated.approved is True
    assert evaluated.approved_by == "Alice"
    assert evaluated.reason == "HUMAN_APPROVED"
    assert to_dict(current) == before

    applied = service.apply(current, human_evidence(), expected_commit=COMMIT)
    after = to_dict(current)
    assert applied == evaluated
    assert after["human_approval"] == {
        "required": True,
        "approved": True,
        "approved_by": "Alice",
        "approved_at": NOW.isoformat(),
        "evidence_ref": "EV-HUMAN",
    }
    before["human_approval"] = after["human_approval"]
    assert after == before


def test_false_decision_remains_observable_without_becoming_approval() -> None:
    service = HumanApprovalService()
    current = story()

    result = service.apply(
        current,
        human_evidence(result=False),
        expected_commit=COMMIT,
    )

    assert result.applied is False
    assert result.approved is False
    assert result.approved_by is None
    assert result.reason == "HUMAN_REFUSED"
    assert current.human_approval == HumanApproval(True, False, None, None)


@pytest.mark.parametrize("result", ["True", "PASS", 1, 0])
def test_non_boolean_decisions_are_refused_without_mutation(result: object) -> None:
    current = story()

    with pytest.raises(HumanApprovalError):
        HumanApprovalService().apply(
            current,
            human_evidence(result=result),
            expected_commit=COMMIT,
        )

    assert current.human_approval == HumanApproval(True, False, None, None)


@pytest.mark.parametrize(
    "producer",
    [
        "Codex/FakeHuman",
        "cOdEx/FakeHuman",
        "Co\u200bdex/FakeHuman",
        "",
        "/",
        "\u200b\u2060",
    ],
)
def test_non_human_or_ambiguous_identities_are_refused(producer: str) -> None:
    current = story()

    with pytest.raises(HumanApprovalError):
        HumanApprovalService().apply(
            current,
            human_evidence(producer=producer),
            expected_commit=COMMIT,
        )

    assert current.human_approval.approved is False


@pytest.mark.parametrize(
    "candidate",
    [
        human_evidence(evidence_type=EvidenceType.TEST_RESULT),
        human_evidence(subject="US-0002"),
        human_evidence(commit=OTHER_COMMIT),
        human_evidence(source="Codex"),
    ],
)
def test_inapplicable_evidence_is_refused(candidate: Evidence) -> None:
    current = story()

    with pytest.raises(HumanApprovalError):
        HumanApprovalService().apply(current, candidate, expected_commit=COMMIT)

    assert current.human_approval.approved is False


def test_repository_independent_human_evidence_is_applicable() -> None:
    result = HumanApprovalService().apply(
        story(),
        human_evidence(commit=None),
        expected_commit=COMMIT,
    )

    assert result.applied is True


def test_replacement_and_approval_when_not_required_fail_closed() -> None:
    service = HumanApprovalService()
    not_required = story()
    not_required.human_approval.required = False
    already_approved = story()
    already_approved.human_approval = HumanApproval(
        True,
        True,
        "Alice",
        NOW,
        "EV-OLD",
    )

    with pytest.raises(HumanApprovalError):
        service.apply(not_required, human_evidence(), expected_commit=COMMIT)
    with pytest.raises(HumanApprovalError):
        service.apply(already_approved, human_evidence(), expected_commit=COMMIT)


def test_control_loop_applies_only_already_persisted_evidence_and_reloads(
    tmp_path: Path,
) -> None:
    store, control_loop = initialize(tmp_path)
    recorded = record_human(control_loop)
    assert store.load().user_stories[0].human_approval.approved is False

    result = control_loop.apply_human_approval(
        "US-0001",
        recorded.evidence_id,
        expected_commit=COMMIT,
    )
    reloaded = ProjectStateStore(tmp_path).load()

    assert result.applied is True
    assert reloaded.user_stories[0].human_approval == HumanApproval(
        True,
        True,
        "Alice",
        NOW,
        "EV-HUMAN",
    )


def test_control_loop_refuses_unpersisted_and_other_story_evidence(
    tmp_path: Path,
) -> None:
    store, control_loop = initialize(tmp_path)
    current = store.load()
    state = store.load()
    state.evidence.append(human_evidence(evidence_id="EV-OTHER", subject="US-0002"))
    operation = "TEST_SETUP_OTHER_STORY_EVIDENCE"
    authorization = _issue_authoritative_write(
        store_kind="PROJECT_STATE",
        store=store,
        before_state=current,
        candidate_state=state,
        operation=operation,
    )
    store.save(state, authorization=authorization, operation=operation)

    with pytest.raises(ControlLoopError) as absent:
        control_loop.apply_human_approval(
            "US-0001",
            "EV-NOT-PERSISTED",
            expected_commit=COMMIT,
        )
    assert absent.value.code == "HUMAN_EVIDENCE_NOT_FOUND"

    with pytest.raises(HumanApprovalError):
        control_loop.apply_human_approval(
            "US-0001",
            "EV-OTHER",
            expected_commit=COMMIT,
        )
    assert store.load().user_stories[0].human_approval.approved is False


def test_store_refuses_direct_forged_approval_without_applicable_evidence(
    tmp_path: Path,
) -> None:
    store, _ = initialize(tmp_path)
    state = store.load()
    approval = state.user_stories[0].human_approval
    approval.approved = True
    approval.approved_by = "Alice"
    approval.approved_at = NOW

    with pytest.raises(PersistenceError) as captured:
        store.save(state)

    assert captured.value.code == "INVALID_HUMAN_APPROVAL"
    persisted = ProjectStateStore(tmp_path).load()
    assert persisted.user_stories[0].human_approval.approved is False


class FailingStore:
    def __init__(self, state: ProjectState) -> None:
        self.state = state

    def load(self) -> ProjectState:
        return self.state

    def save(self, state: ProjectState, **_: object) -> Path:
        raise OSError("simulated persistence failure")


def test_persistence_failure_produces_no_partial_mutation() -> None:
    authoritative = ProjectState(
        schema_version="1.0",
        user_stories=[story()],
        evidence=[human_evidence()],
    )
    control_loop = loop(FailingStore(authoritative))

    with pytest.raises(OSError, match="simulated persistence failure"):
        control_loop.apply_human_approval(
            "US-0001",
            "EV-HUMAN",
            expected_commit=COMMIT,
        )

    assert authoritative.user_stories[0].human_approval.approved is False


def test_orchestrator_continues_after_authoritative_approval_and_reload(
    tmp_path: Path,
) -> None:
    store, control_loop = initialize(tmp_path)
    record_human(control_loop)
    control_loop.apply_human_approval(
        "US-0001",
        "EV-HUMAN",
        expected_commit=COMMIT,
    )
    mission_store = MissionStateStore(tmp_path)
    mission_store.initialize(
        MissionState(
            schema_version="1.0",
            mission_id="P2.9-R2",
            workflow_generation=0,
            status=MissionStatus.ACTIVE,
            role=MissionRole.ORCHESTRATOR,
            objective="Continue after attributable Human approval.",
            subject="US-0001",
            operating_step=OperatingStep.PREFLIGHT,
            next_action="Inspect the authoritative state.",
            observed_commit=COMMIT,
            updated_at=NOW,
        )
    )
    orchestrator = Orchestrator(
        repository_root=tmp_path,
        mission_store=mission_store,
        project_state_store=ProjectStateStore(tmp_path),
    )

    result = orchestrator.orchestrate(current_commit=COMMIT, updated_at=NOW)

    assert result.success is True
    assert result.reason != "HUMAN_REQUIRED"
