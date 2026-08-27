import ast
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import agentic_engineering_os.application.control_loop as control_loop_module
from agentic_engineering_os.application import (
    AcceptanceResult,
    CertificationService,
    ControlLoop,
    ControlLoopError,
    ContractValidator,
    EvidenceObservation,
    EvidenceProvenance,
    EvidenceRecorder,
    EvidenceRecordingError,
    GateContract,
    GateEvaluationContext,
    GateEvaluator,
    ProvenanceKind,
    StateTransitionService,
    TransitionContext,
)
from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    Certification,
    CertificationResult,
    Evidence,
    EvidenceType,
    GateResult,
    HumanApproval,
    ProjectState,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
    to_dict,
)
from agentic_engineering_os.infrastructure import PersistenceError, ProjectStateStore


COMMIT = "2c65f6b79c75a8986171abb94d386f022ea23988"
OTHER_COMMIT = "29c518ea0130bccddb0c0ee5f2d9daf55ea79b80"
NOW = datetime(2026, 8, 27, 18, 0, tzinfo=timezone.utc)


def story(*, status: UserStoryStatus = UserStoryStatus.CERTIFICATION) -> UserStory:
    return UserStory(
        schema_version="1.0",
        id="US-0001",
        title="Integrated deterministic cycle",
        description="Coordinate the certified deterministic services.",
        status=status,
        priority=1,
        risk=RiskLevel.HIGH,
        depends_on=(),
        scope=UserStoryScope(allowed_paths=("src/",), forbidden_paths=()),
        acceptance_criteria=(
            AcceptanceCriterion(
                id="AC-001",
                description="The integrated cycle is proven.",
                mandatory=True,
            ),
        ),
        required_gates=("GATE-001",),
        human_approval=HumanApproval(
            required=False,
            approved=False,
            approved_by=None,
            approved_at=None,
        ),
        metadata=UserStoryMetadata(
            created_at=NOW,
            created_by="human-operator",
            updated_at=NOW,
        ),
    )


def observation(
    subject: str,
    *,
    result: object = True,
    evidence_type: EvidenceType = EvidenceType.TEST_RESULT,
    source: str = "pytest",
) -> EvidenceObservation:
    return EvidenceObservation(
        evidence_type=evidence_type,
        subject=subject,
        result=result,
        provenance=EvidenceProvenance(
            kind=ProvenanceKind.TOOL,
            source=source,
            producer="pytest",
        ),
        repository_dependent=True,
        artifact="captured test output",
        commit=COMMIT,
    )


def make_loop(store: object) -> ControlLoop:
    validator = ContractValidator()
    return ControlLoop(
        state_store=store,
        evidence_recorder_factory=lambda target: EvidenceRecorder(
            target,
            validator=validator,
            id_factory=lambda: "EV-GENERATED",
            clock=lambda: NOW,
        ),
        gate_evaluator=GateEvaluator(validator=validator, clock=lambda: NOW),
        certification_service=CertificationService(
            validator=validator,
            id_factory=lambda: "CERT-GENERATED",
            clock=lambda: NOW,
        ),
        transition_service=StateTransitionService(),
    )


def initialized_loop(tmp_path: Path) -> tuple[ProjectStateStore, ControlLoop]:
    store = ProjectStateStore(tmp_path)
    store.initialize()
    state = store.load()
    state.user_stories.append(story())
    store.save(state)
    return store, make_loop(store)


def pass_contract(
    evidence_id: str = "EV-GATE-001",
    *,
    gate_id: str = "GATE-001",
) -> GateContract:
    return GateContract(
        gate_id=gate_id,
        subject="US-0001",
        required=True,
        evidence_ids=(evidence_id,),
        condition=lambda items: (
            GateResult.PASS if items[0].result is True else GateResult.FAIL
        ),
        repository_dependent=True,
        evaluator="Codex/Reviewer",
    )


def record_certification_inputs(loop: ControlLoop, *, result: bool = True) -> None:
    loop.record_evidence(
        observation(
            "AC-001",
            result=result,
            evidence_type=EvidenceType.ACCEPTANCE_CRITERION_CHECK,
        ),
        evidence_id="EV-AC-001",
        timestamp=NOW,
    )
    loop.record_evidence(
        observation("US-0001", result=result),
        evidence_id="EV-GATE-001",
        timestamp=NOW,
    )


def produce_certified_record(
    loop: ControlLoop,
    *,
    certification_id: str = "CERT-001",
) -> Certification:
    record_certification_inputs(loop)
    loop.evaluate_gate(
        pass_contract(),
        context=GateEvaluationContext(expected_commit=COMMIT),
        evaluated_at=NOW,
    )
    return loop.certify_user_story(
        "US-0001",
        COMMIT,
        (
            AcceptanceResult(
                criterion_id="AC-001",
                result=GateResult.PASS,
                evidence_refs=("EV-AC-001",),
            ),
        ),
        certifier="Codex/Certifier",
        certification_id=certification_id,
        certified_at=NOW,
    )


def certification_record(
    *,
    certification_id: str,
    subject: str,
    result: CertificationResult,
    commit: str = COMMIT,
) -> Certification:
    return Certification(
        certification_id=certification_id,
        subject=subject,
        result=result,
        commit=commit,
        acceptance_results={"AC-001": "PASS"},
        gate_results={},
        human_approval={
            "required": False,
            "approved": False,
            "evidence_ref": None,
        },
        evidence_refs=(),
        certified_at=NOW,
        certifier="Codex/Certifier",
    )


def assert_certified_promotion_refused(
    store: ProjectStateStore,
    loop: ControlLoop,
    *,
    context: TransitionContext,
    code: str,
) -> None:
    before = store.state_path.read_bytes()

    with pytest.raises(ControlLoopError) as captured:
        loop.transition_user_story(
            "US-0001",
            UserStoryStatus.CERTIFIED,
            context=context,
        )

    assert captured.value.code == code
    assert store.state_path.read_bytes() == before
    assert store.load().user_stories[0].status is UserStoryStatus.CERTIFICATION


def test_golden_path_persists_and_reloads_the_same_authoritative_state(
    tmp_path: Path,
) -> None:
    store, loop = initialized_loop(tmp_path)
    certification = produce_certified_record(loop)
    before_transition = store.load()
    transition = loop.transition_user_story(
        "US-0001",
        UserStoryStatus.CERTIFIED,
        context=TransitionContext(
            preconditions_proven=False,
            target_commit=COMMIT,
        ),
    )
    reloaded = loop.load_state()

    assert certification.result is CertificationResult.CERTIFIED
    assert before_transition.user_stories[0].status is UserStoryStatus.CERTIFICATION
    assert transition.allowed
    assert reloaded.user_stories[0].status is UserStoryStatus.CERTIFIED
    assert [item.evidence_id for item in reloaded.evidence] == [
        "EV-AC-001",
        "EV-GATE-001",
    ]
    assert reloaded.gates[0].evidence_refs == ("EV-GATE-001",)
    assert reloaded.certifications[0].evidence_refs == (
        "EV-AC-001",
        "EV-GATE-001",
    )
    assert reloaded.evidence[0].timestamp == NOW
    assert reloaded.gates[0].evaluated_at == NOW
    assert reloaded.certifications[0].certified_at == NOW
    assert to_dict(reloaded) == json.loads(store.state_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("corrupt", [False, True], ids=["absent", "corrupt"])
def test_load_state_fails_closed_when_state_is_unavailable(
    tmp_path: Path, corrupt: bool
) -> None:
    store = ProjectStateStore(tmp_path)
    if corrupt:
        store.state_path.parent.mkdir()
        store.state_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(PersistenceError) as caught:
        make_loop(store).load_state()

    assert caught.value.code == ("INVALID_JSON" if corrupt else "STATE_ABSENT")


def test_unknown_and_ambiguous_user_story_ids_are_explicit_refusals(
    tmp_path: Path,
) -> None:
    store, loop = initialized_loop(tmp_path)
    before = store.state_path.read_bytes()

    with pytest.raises(ControlLoopError, match="USER_STORY_NOT_FOUND"):
        loop.transition_user_story(
            "US-UNKNOWN",
            UserStoryStatus.CERTIFIED,
            context=TransitionContext(preconditions_proven=True),
        )
    assert store.state_path.read_bytes() == before

    class AmbiguousStore:
        def load(self) -> ProjectState:
            return ProjectState(user_stories=[story(), story()], schema_version="1.0")

        def save(self, state: ProjectState) -> Path:
            raise AssertionError("ambiguous state must not be saved")

    with pytest.raises(ControlLoopError, match="AMBIGUOUS_USER_STORY"):
        make_loop(AmbiguousStore()).transition_user_story(
            "US-0001",
            UserStoryStatus.CERTIFIED,
            context=TransitionContext(preconditions_proven=True),
        )


def test_invalid_and_duplicate_evidence_do_not_partially_persist(tmp_path: Path) -> None:
    store, loop = initialized_loop(tmp_path)
    baseline = store.state_path.read_bytes()

    with pytest.raises(EvidenceRecordingError, match="PROVENANCE_REQUIRED"):
        loop.record_evidence(
            observation("US-0001", source=""),
            evidence_id="EV-INVALID",
            timestamp=NOW,
        )
    assert store.state_path.read_bytes() == baseline

    loop.record_evidence(
        observation("US-0001"), evidence_id="EV-001", timestamp=NOW
    )
    after_first = store.state_path.read_bytes()
    with pytest.raises(EvidenceRecordingError, match="DUPLICATE_EVIDENCE_ID"):
        loop.record_evidence(
            observation("US-0001"), evidence_id="EV-001", timestamp=NOW
        )
    assert store.state_path.read_bytes() == after_first
    assert [item.evidence_id for item in store.load().evidence] == ["EV-001"]


@pytest.mark.parametrize(
    ("evidence_id", "evidence_result", "expected"),
    [
        ("EV-MISSING", None, GateResult.UNKNOWN),
        ("EV-GATE-001", False, GateResult.FAIL),
    ],
)
def test_gate_unknown_and_fail_are_service_results_persisted_without_promotion(
    tmp_path: Path,
    evidence_id: str,
    evidence_result: bool | None,
    expected: GateResult,
) -> None:
    store, loop = initialized_loop(tmp_path)
    if evidence_result is not None:
        loop.record_evidence(
            observation("US-0001", result=evidence_result),
            evidence_id=evidence_id,
            timestamp=NOW,
        )

    evaluation = loop.evaluate_gate(
        pass_contract(evidence_id),
        context=GateEvaluationContext(expected_commit=COMMIT),
        evaluated_at=NOW,
    )
    reloaded = store.load()

    assert evaluation.result is expected
    assert reloaded.gates[0].result is expected
    assert reloaded.user_stories[0].status is UserStoryStatus.CERTIFICATION


def test_duplicate_gate_id_is_refused_without_re_evaluation(tmp_path: Path) -> None:
    store, loop = initialized_loop(tmp_path)
    loop.evaluate_gate(
        pass_contract("EV-MISSING"),
        context=GateEvaluationContext(expected_commit=COMMIT),
        evaluated_at=NOW,
    )
    before = store.state_path.read_bytes()

    with pytest.raises(ControlLoopError, match="DUPLICATE_GATE_ID"):
        loop.evaluate_gate(
            pass_contract("EV-MISSING"),
            context=GateEvaluationContext(expected_commit=COMMIT),
            evaluated_at=NOW,
        )

    assert store.state_path.read_bytes() == before


def test_blocked_certification_is_persisted_without_status_mutation(
    tmp_path: Path,
) -> None:
    store, loop = initialized_loop(tmp_path)
    loop.evaluate_gate(
        pass_contract("EV-MISSING"),
        context=GateEvaluationContext(expected_commit=COMMIT),
        evaluated_at=NOW,
    )

    certification = loop.certify_user_story(
        "US-0001",
        COMMIT,
        (),
        certifier="Codex/Certifier",
        certification_id="CERT-BLOCKED",
        certified_at=NOW,
    )
    reloaded = store.load()

    assert certification.result is CertificationResult.BLOCKED
    assert reloaded.certifications[0].result is CertificationResult.BLOCKED
    assert reloaded.user_stories[0].status is UserStoryStatus.CERTIFICATION


def test_failed_inputs_produce_rejected_certification_without_status_mutation(
    tmp_path: Path,
) -> None:
    store, loop = initialized_loop(tmp_path)
    record_certification_inputs(loop, result=False)
    loop.evaluate_gate(
        pass_contract(),
        context=GateEvaluationContext(expected_commit=COMMIT),
        evaluated_at=NOW,
    )

    certification = loop.certify_user_story(
        "US-0001",
        COMMIT,
        (
            AcceptanceResult(
                criterion_id="AC-001",
                result=GateResult.FAIL,
                evidence_refs=("EV-AC-001",),
            ),
        ),
        certifier="Codex/Certifier",
        certification_id="CERT-REJECTED",
        certified_at=NOW,
    )
    reloaded = store.load()

    assert certification.result is CertificationResult.REJECTED
    assert reloaded.certifications[0].result is CertificationResult.REJECTED
    assert reloaded.user_stories[0].status is UserStoryStatus.CERTIFICATION


def test_duplicate_certification_id_is_refused_without_overwrite(tmp_path: Path) -> None:
    store, loop = initialized_loop(tmp_path)
    loop.evaluate_gate(
        pass_contract("EV-MISSING"),
        context=GateEvaluationContext(expected_commit=COMMIT),
        evaluated_at=NOW,
    )
    loop.certify_user_story(
        "US-0001",
        COMMIT,
        (),
        certifier="Codex/Certifier",
        certification_id="CERT-001",
        certified_at=NOW,
    )
    before = store.state_path.read_bytes()

    with pytest.raises(ControlLoopError, match="DUPLICATE_CERTIFICATION_ID"):
        loop.certify_user_story(
            "US-0001",
            COMMIT,
            (),
            certifier="Codex/Certifier",
            certification_id="CERT-001",
            certified_at=NOW,
        )

    assert store.state_path.read_bytes() == before


def test_forbidden_transition_preserves_authoritative_state(tmp_path: Path) -> None:
    store, loop = initialized_loop(tmp_path)
    before = store.state_path.read_bytes()

    with pytest.raises(ControlLoopError, match="TRANSITION_REFUSED"):
        loop.transition_user_story(
            "US-0001",
            UserStoryStatus.READY,
            context=TransitionContext(preconditions_proven=True),
        )

    assert store.state_path.read_bytes() == before
    assert store.load().user_stories[0].status is UserStoryStatus.CERTIFICATION


def test_caller_boolean_cannot_promote_without_authoritative_certification(
    tmp_path: Path,
) -> None:
    store, loop = initialized_loop(tmp_path)
    before = store.state_path.read_bytes()
    state = store.load()
    assert state.evidence == []
    assert state.gates == []
    assert state.certifications == []

    with pytest.raises(ControlLoopError, match="CERTIFICATION_COMMIT_REQUIRED"):
        loop.transition_user_story(
            "US-0001",
            UserStoryStatus.CERTIFIED,
            context=TransitionContext(preconditions_proven=True),
        )

    assert store.state_path.read_bytes() == before
    assert store.load().user_stories[0].status is UserStoryStatus.CERTIFICATION


def test_explicit_commit_cannot_promote_without_authoritative_certification(
    tmp_path: Path,
) -> None:
    store, loop = initialized_loop(tmp_path)

    assert_certified_promotion_refused(
        store,
        loop,
        context=TransitionContext(
            preconditions_proven=True,
            target_commit=COMMIT,
        ),
        code="CERTIFICATION_NOT_FOUND",
    )


@pytest.mark.parametrize(
    "result", (CertificationResult.BLOCKED, CertificationResult.REJECTED)
)
def test_non_certified_verdict_cannot_authorize_promotion(
    tmp_path: Path,
    result: CertificationResult,
) -> None:
    store, loop = initialized_loop(tmp_path)
    state = store.load()
    state.certifications.append(
        certification_record(
            certification_id=f"CERT-{result.value}",
            subject="US-0001",
            result=result,
        )
    )
    store.save(state)

    assert_certified_promotion_refused(
        store,
        loop,
        context=TransitionContext(
            preconditions_proven=True,
            target_commit=COMMIT,
        ),
        code="CERTIFICATION_NOT_CERTIFIED",
    )


def test_certification_for_another_story_cannot_authorize_promotion(
    tmp_path: Path,
) -> None:
    store, loop = initialized_loop(tmp_path)
    state = store.load()
    state.user_stories.append(
        replace(story(), id="US-0002", required_gates=())
    )
    state.certifications.append(
        certification_record(
            certification_id="CERT-OTHER-STORY",
            subject="US-0002",
            result=CertificationResult.CERTIFIED,
        )
    )
    store.save(state)

    assert_certified_promotion_refused(
        store,
        loop,
        context=TransitionContext(
            preconditions_proven=True,
            target_commit=COMMIT,
        ),
        code="CERTIFICATION_NOT_FOUND",
    )


def test_certification_for_another_commit_cannot_authorize_promotion(
    tmp_path: Path,
) -> None:
    store, loop = initialized_loop(tmp_path)
    state = store.load()
    state.certifications.append(
        certification_record(
            certification_id="CERT-WRONG-COMMIT",
            subject="US-0001",
            result=CertificationResult.CERTIFIED,
        )
    )
    store.save(state)

    assert_certified_promotion_refused(
        store,
        loop,
        context=TransitionContext(
            preconditions_proven=True,
            target_commit=OTHER_COMMIT,
        ),
        code="CERTIFICATION_NOT_FOUND",
    )


def test_multiple_applicable_certifications_are_ambiguous(
    tmp_path: Path,
) -> None:
    store, loop = initialized_loop(tmp_path)
    state = store.load()
    state.certifications.extend(
        (
            certification_record(
                certification_id="CERT-ONE",
                subject="US-0001",
                result=CertificationResult.CERTIFIED,
            ),
            certification_record(
                certification_id="CERT-TWO",
                subject="US-0001",
                result=CertificationResult.CERTIFIED,
            ),
        )
    )
    store.save(state)

    assert_certified_promotion_refused(
        store,
        loop,
        context=TransitionContext(
            preconditions_proven=True,
            target_commit=COMMIT,
        ),
        code="AMBIGUOUS_CERTIFICATION",
    )


def test_persistence_failure_returns_no_success_and_preserves_prior_state(
    tmp_path: Path,
) -> None:
    store, setup_loop = initialized_loop(tmp_path)
    produce_certified_record(setup_loop)
    before = store.state_path.read_bytes()

    class FailingSaveStore:
        def load(self) -> ProjectState:
            return store.load()

        def save(self, state: ProjectState) -> Path:
            raise PersistenceError("WRITE_FAILED", "injected save failure")

    loop = make_loop(FailingSaveStore())
    with pytest.raises(PersistenceError, match="injected save failure"):
        loop.transition_user_story(
            "US-0001",
            UserStoryStatus.CERTIFIED,
            context=TransitionContext(
                preconditions_proven=True,
                target_commit=COMMIT,
            ),
        )

    assert store.state_path.read_bytes() == before
    assert store.load().user_stories[0].status is UserStoryStatus.CERTIFICATION


def test_candidate_copy_prevents_partial_mutation_of_shared_loaded_state() -> None:
    authoritative = ProjectState(
        schema_version="1.0",
        user_stories=[story()],
        certifications=[
            certification_record(
                certification_id="CERT-001",
                subject="US-0001",
                result=CertificationResult.CERTIFIED,
            )
        ],
    )

    class InMemoryFailingStore:
        def load(self) -> ProjectState:
            return authoritative

        def save(self, state: ProjectState) -> Path:
            raise PersistenceError("WRITE_FAILED", "injected save failure")

    with pytest.raises(PersistenceError):
        make_loop(InMemoryFailingStore()).transition_user_story(
            "US-0001",
            UserStoryStatus.CERTIFIED,
            context=TransitionContext(
                preconditions_proven=True,
                target_commit=COMMIT,
            ),
        )

    assert authoritative.user_stories[0].status is UserStoryStatus.CERTIFICATION


def test_invalid_candidate_is_rejected_by_store_and_prior_state_is_preserved(
    tmp_path: Path,
) -> None:
    store, _ = initialized_loop(tmp_path)
    before = store.state_path.read_bytes()
    invalid = Evidence(
        evidence_id="EV-INVALID",
        evidence_type=EvidenceType.TEST_RESULT,
        subject="US-0001",
        result=True,
        source="",
        command=None,
        exit_code=None,
        artifact=None,
        commit=COMMIT,
        timestamp=NOW,
        producer="pytest",
    )

    class InvalidRecorder:
        def __init__(self, target: list[Evidence]) -> None:
            self.target = target

        def record(self, *args: object, **kwargs: object) -> Evidence:
            self.target.append(invalid)
            return invalid

    loop = ControlLoop(
        state_store=store,
        evidence_recorder_factory=InvalidRecorder,
        gate_evaluator=GateEvaluator(clock=lambda: NOW),
        certification_service=CertificationService(clock=lambda: NOW),
        transition_service=StateTransitionService(),
    )
    with pytest.raises(PersistenceError, match="INVALID_SCHEMA"):
        loop.record_evidence(observation("US-0001"))

    assert store.state_path.read_bytes() == before


def test_control_loop_contains_no_specialized_rule_catalog_or_direct_io() -> None:
    source = Path(control_loop_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    status_assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        and any(
            isinstance(target, ast.Attribute) and target.attr == "status"
            for target in (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
            )
        )
    ]

    assert "ALLOWED_TRANSITIONS" not in source
    assert "GateResult" not in source
    assert source.count("CertificationResult.CERTIFIED") == 1
    assert "state.json" not in source
    assert "jsonschema" not in source
    assert ".write_text(" not in source
    assert ".write_bytes(" not in source
    assert status_assignments == []
