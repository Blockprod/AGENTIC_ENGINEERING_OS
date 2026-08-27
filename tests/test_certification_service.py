from datetime import datetime, timezone

import pytest

from agentic_engineering_os.application import (
    AcceptanceResult,
    CertificationContext,
    CertificationError,
    CertificationService,
    ContractValidator,
    StateTransitionService,
    ValidationIssue,
    ValidationResult,
)
from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    CertificationResult,
    Evidence,
    EvidenceType,
    Gate,
    GateResult,
    HumanApproval,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
    to_dict,
)


COMMIT = "772e3c94f914ab8d3eec9cd486aeac5c93c03808"
OTHER_COMMIT = "935fa67dac3030a4cd7f7702b142718b751a7eac"
NOW = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)

FORMAT_CHARACTERS = ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff")

CODEX_IDENTITY_VARIANTS = (
    "Codex/FakeHuman",
    "codex/FakeHuman",
    "CODEX/FakeHuman",
    "CoDeX/FakeHuman",
    *(f"Co{character}dex/FakeHuman" for character in FORMAT_CHARACTERS),
    *(f"{character}Codex{character}/FakeHuman" for character in FORMAT_CHARACTERS),
    "Codex\u200b/FakeHuman",
    "  Codex / FakeHuman  ",
)

REMAINING_HUMAN_BYPASS_IDENTITIES = (
    "Co\u200bdex/FakeHuman",
    "Codex\u200b/FakeHuman",
    "/",
)

AMBIGUOUS_HUMAN_IDENTITIES = (
    "",
    "   ",
    "/",
    "///",
    "\u200b\u200c\u200d\u2060\ufeff",
)

LEGITIMATE_HUMAN_IDENTITIES = (
    "human-operator",
    "Alice",
    "Ali\u200cReza/Approver",
    "équipe-qualité/Approver",
    "李雷/Reviewer",
)


def story(
    *,
    criteria: tuple[tuple[str, bool], ...] = (("AC-001", True),),
    required_gates: tuple[str, ...] = ("GATE-001",),
    human_required: bool = False,
    human_approved: bool = False,
    approved_by: str | None = None,
) -> UserStory:
    return UserStory(
        schema_version="1.0",
        id="US-0001",
        title="Certify one User Story",
        description="Produce a deterministic certification verdict.",
        status=UserStoryStatus.CERTIFICATION,
        priority=1,
        risk=RiskLevel.HIGH,
        depends_on=(),
        scope=UserStoryScope(allowed_paths=("src/",), forbidden_paths=()),
        acceptance_criteria=tuple(
            AcceptanceCriterion(
                id=criterion_id,
                description=f"Observable condition {criterion_id}",
                mandatory=mandatory,
            )
            for criterion_id, mandatory in criteria
        ),
        required_gates=required_gates,
        human_approval=HumanApproval(
            required=human_required,
            approved=human_approved,
            approved_by=approved_by,
            approved_at=NOW if approved_by is not None else None,
        ),
        metadata=UserStoryMetadata(
            created_at=NOW,
            created_by="human-operator",
            updated_at=NOW,
        ),
    )


def evidence(
    evidence_id: str,
    subject: str,
    *,
    result: object = True,
    commit: str | None = COMMIT,
    evidence_type: EvidenceType | None = None,
    source: str = "pytest",
    producer: str = "pytest",
) -> Evidence:
    resolved_type = evidence_type or (
        EvidenceType.ACCEPTANCE_CRITERION_CHECK
        if subject.startswith("AC-")
        else EvidenceType.TEST_RESULT
    )
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=resolved_type,
        subject=subject,
        result=result,
        source=source,
        command="pytest -q" if resolved_type is not EvidenceType.HUMAN_APPROVAL else None,
        exit_code=0 if resolved_type is not EvidenceType.HUMAN_APPROVAL else None,
        artifact="captured output",
        commit=commit,
        timestamp=NOW,
        producer=producer,
    )


def gate(
    gate_id: str = "GATE-001",
    *,
    result: GateResult = GateResult.PASS,
    evidence_refs: tuple[str, ...] = ("EV-GATE-001",),
    subject: str = "US-0001",
    required: bool = True,
) -> Gate:
    return Gate(
        gate_id=gate_id,
        subject=subject,
        required=required,
        result=result,
        evidence_refs=evidence_refs,
        evaluated_at=NOW,
        evaluator="Codex/Reviewer",
    )


def acceptance(
    criterion_id: str = "AC-001",
    *,
    result: GateResult | str = GateResult.PASS,
    evidence_refs: tuple[str, ...] | None = None,
) -> AcceptanceResult:
    return AcceptanceResult(
        criterion_id=criterion_id,
        result=result,
        evidence_refs=(
            evidence_refs
            if evidence_refs is not None
            else (f"EV-{criterion_id}",)
        ),
    )


def service(*, validator: ContractValidator | None = None) -> CertificationService:
    return CertificationService(
        validator=validator,
        id_factory=lambda: "CERT-001",
        clock=lambda: NOW,
    )


def certify(
    user_story: UserStory | None = None,
    acceptance_results: tuple[AcceptanceResult, ...] | None = None,
    gates: tuple[Gate, ...] | None = None,
    available_evidence: tuple[Evidence, ...] | None = None,
    *,
    context: CertificationContext | None = None,
    certifier: str = "Codex/Certifier",
):
    current_story = user_story if user_story is not None else story()
    current_acceptance = (
        acceptance_results
        if acceptance_results is not None
        else (acceptance(),)
    )
    current_gates = gates if gates is not None else (gate(),)
    current_evidence = (
        available_evidence
        if available_evidence is not None
        else (
            evidence("EV-AC-001", "AC-001"),
            evidence("EV-GATE-001", "US-0001"),
        )
    )
    return service().certify(
        current_story,
        COMMIT,
        current_acceptance,
        current_gates,
        current_evidence,
        certifier=certifier,
        context=context,
    )


def test_complete_nominal_certification_with_multiple_criteria_and_gates() -> None:
    current_story = story(
        criteria=(("AC-001", True), ("AC-002", True)),
        required_gates=("GATE-001", "GATE-002"),
    )
    result = certify(
        current_story,
        (acceptance("AC-001"), acceptance("AC-002")),
        (
            gate("GATE-001", evidence_refs=("EV-GATE-001",)),
            gate("GATE-002", evidence_refs=("EV-GATE-002",)),
        ),
        (
            evidence("EV-AC-001", "AC-001"),
            evidence("EV-AC-002", "AC-002"),
            evidence("EV-GATE-001", "US-0001"),
            evidence("EV-GATE-002", "US-0001"),
        ),
    )

    assert result.result is CertificationResult.CERTIFIED
    assert result.certification_id == "CERT-001"
    assert result.commit == COMMIT
    assert result.certified_at == NOW
    assert result.acceptance_results == {"AC-001": "PASS", "AC-002": "PASS"}
    assert result.gate_results == {"GATE-001": "PASS", "GATE-002": "PASS"}
    assert result.human_approval["required"] is False
    assert result.evidence_refs == (
        "EV-AC-001",
        "EV-AC-002",
        "EV-GATE-001",
        "EV-GATE-002",
    )


def test_valid_required_human_approval_certifies() -> None:
    current_story = story(
        human_required=True,
        human_approved=True,
        approved_by="human-operator",
    )
    human = evidence(
        "EV-HUMAN",
        "US-0001",
        evidence_type=EvidenceType.HUMAN_APPROVAL,
        source="Human",
        producer="human-operator",
    )

    result = certify(
        current_story,
        available_evidence=(
            evidence("EV-AC-001", "AC-001"),
            evidence("EV-GATE-001", "US-0001"),
            human,
        ),
        context=CertificationContext(human_approval_evidence_id="EV-HUMAN"),
    )

    assert result.result is CertificationResult.CERTIFIED
    assert result.human_approval["result"] == "PASS"
    assert "EV-HUMAN" in result.evidence_refs


@pytest.mark.parametrize("failure", ["acceptance", "gate"])
def test_mandatory_acceptance_or_gate_failure_rejects(failure: str) -> None:
    acceptance_results = (
        acceptance(result=GateResult.FAIL)
        if failure == "acceptance"
        else acceptance(),
    )
    gates = (
        gate(result=GateResult.FAIL) if failure == "gate" else gate(),
    )

    available_evidence = (
        evidence("EV-AC-001", "AC-001", result=failure != "acceptance"),
        evidence("EV-GATE-001", "US-0001"),
    )

    result = certify(
        acceptance_results=acceptance_results,
        gates=gates,
        available_evidence=available_evidence,
    )

    assert result.result is CertificationResult.REJECTED


def test_explicit_human_refusal_rejects() -> None:
    current_story = story(
        human_required=True,
        human_approved=False,
        approved_by="human-operator",
    )
    human = evidence(
        "EV-HUMAN",
        "US-0001",
        result=False,
        evidence_type=EvidenceType.HUMAN_APPROVAL,
        source="Human",
        producer="human-operator",
    )

    result = certify(
        current_story,
        available_evidence=(
            evidence("EV-AC-001", "AC-001"),
            evidence("EV-GATE-001", "US-0001"),
            human,
        ),
        context=CertificationContext(human_approval_evidence_id="EV-HUMAN"),
    )

    assert result.result is CertificationResult.REJECTED
    assert result.human_approval["result"] == "FAIL"


def test_rejected_dominates_blocked() -> None:
    result = certify(
        acceptance_results=(acceptance(result=GateResult.FAIL),),
        gates=(gate(result=GateResult.UNKNOWN, evidence_refs=()),),
        available_evidence=(
            evidence("EV-AC-001", "AC-001", result=False),
            evidence("EV-GATE-001", "US-0001"),
        ),
    )

    assert result.result is CertificationResult.REJECTED


@pytest.mark.parametrize(
    "acceptance_results",
    [
        (),
        (acceptance(result=GateResult.UNKNOWN),),
        (acceptance(result="MAGIC"),),
    ],
)
def test_missing_unknown_or_ambiguous_mandatory_criterion_blocks(
    acceptance_results: tuple[AcceptanceResult, ...],
) -> None:
    result = certify(acceptance_results=acceptance_results)

    assert result.result is CertificationResult.BLOCKED
    assert result.acceptance_results["AC-001"] == "UNKNOWN"


@pytest.mark.parametrize(
    "gates",
    [
        (),
        (gate(result=GateResult.UNKNOWN, evidence_refs=()),),
        (gate(result=GateResult.NOT_APPLICABLE, evidence_refs=()),),
    ],
)
def test_missing_unknown_or_unapproved_not_applicable_gate_blocks(
    gates: tuple[Gate, ...],
) -> None:
    result = certify(gates=gates)

    assert result.result is CertificationResult.BLOCKED


def test_explicitly_allowed_not_applicable_required_gate_is_accepted() -> None:
    result = certify(
        gates=(gate(result=GateResult.NOT_APPLICABLE, evidence_refs=()),),
        context=CertificationContext(
            allowed_not_applicable_gate_ids=frozenset({"GATE-001"})
        ),
    )

    assert result.result is CertificationResult.CERTIFIED
    assert result.authorized_not_applicable_gates == ("GATE-001",)


def test_unknown_not_applicable_gate_authority_blocks_without_persisting_it() -> None:
    result = certify(
        context=CertificationContext(
            allowed_not_applicable_gate_ids=frozenset({"GATE-UNKNOWN"})
        ),
    )

    assert result.result is CertificationResult.BLOCKED
    assert result.authorized_not_applicable_gates == ()


def test_unused_not_applicable_gate_authority_is_not_persisted() -> None:
    result = certify(
        context=CertificationContext(
            allowed_not_applicable_gate_ids=frozenset({"GATE-001"})
        ),
    )

    assert result.result is CertificationResult.CERTIFIED
    assert result.authorized_not_applicable_gates == ()


@pytest.mark.parametrize(
    ("gate_result", "evidence_refs", "expected_verdict"),
    [
        (GateResult.UNKNOWN, (), CertificationResult.BLOCKED),
        (GateResult.FAIL, ("EV-GATE-001",), CertificationResult.REJECTED),
    ],
)
def test_not_applicable_authority_cannot_override_unknown_or_fail(
    gate_result: GateResult,
    evidence_refs: tuple[str, ...],
    expected_verdict: CertificationResult,
) -> None:
    result = certify(
        gates=(gate(result=gate_result, evidence_refs=evidence_refs),),
        context=CertificationContext(
            allowed_not_applicable_gate_ids=frozenset({"GATE-001"})
        ),
    )

    assert result.result is expected_verdict
    assert result.authorized_not_applicable_gates == ()


def test_required_human_approval_absence_blocks() -> None:
    current_story = story(human_required=True)

    result = certify(current_story)

    assert result.result is CertificationResult.BLOCKED


def test_ambiguous_human_provenance_blocks() -> None:
    current_story = story(
        human_required=True,
        human_approved=True,
        approved_by="Codex/Certifier",
    )
    fake = evidence(
        "EV-HUMAN",
        "US-0001",
        evidence_type=EvidenceType.HUMAN_APPROVAL,
        source="Human",
        producer="Codex/Certifier",
    )

    result = certify(
        current_story,
        available_evidence=(
            evidence("EV-AC-001", "AC-001"),
            evidence("EV-GATE-001", "US-0001"),
            fake,
        ),
        context=CertificationContext(human_approval_evidence_id="EV-HUMAN"),
    )

    assert result.result is CertificationResult.BLOCKED


@pytest.mark.parametrize("producer", CODEX_IDENTITY_VARIANTS)
def test_codex_identity_case_variants_cannot_satisfy_human_approval(
    producer: str,
) -> None:
    current_story = story(
        human_required=True,
        human_approved=True,
        approved_by=producer,
    )
    fake = evidence(
        "EV-HUMAN",
        "US-0001",
        evidence_type=EvidenceType.HUMAN_APPROVAL,
        source="Human",
        producer=producer,
    )

    result = certify(
        current_story,
        available_evidence=(
            evidence("EV-AC-001", "AC-001"),
            evidence("EV-GATE-001", "US-0001"),
            fake,
        ),
        context=CertificationContext(human_approval_evidence_id="EV-HUMAN"),
    )

    assert result.result is CertificationResult.BLOCKED


@pytest.mark.parametrize("producer", REMAINING_HUMAN_BYPASS_IDENTITIES)
def test_invisible_codex_and_ambiguous_identity_cannot_certify(
    producer: str,
) -> None:
    current_story = story(
        human_required=True,
        human_approved=True,
        approved_by=producer,
    )
    fake = evidence(
        "EV-HUMAN",
        "US-0001",
        evidence_type=EvidenceType.HUMAN_APPROVAL,
        source="Human",
        producer=producer,
    )

    result = certify(
        current_story,
        available_evidence=(
            evidence("EV-AC-001", "AC-001"),
            evidence("EV-GATE-001", "US-0001"),
            fake,
        ),
        context=CertificationContext(human_approval_evidence_id="EV-HUMAN"),
    )

    assert result.result is CertificationResult.BLOCKED


@pytest.mark.parametrize("producer", AMBIGUOUS_HUMAN_IDENTITIES)
def test_non_attributable_human_identity_cannot_certify(producer: str) -> None:
    current_story = story(
        human_required=True,
        human_approved=True,
        approved_by=producer,
    )
    ambiguous = evidence(
        "EV-HUMAN",
        "US-0001",
        evidence_type=EvidenceType.HUMAN_APPROVAL,
        source="Human",
        producer=producer,
    )

    if not producer:
        with pytest.raises(CertificationError) as captured:
            certify(
                current_story,
                available_evidence=(
                    evidence("EV-AC-001", "AC-001"),
                    evidence("EV-GATE-001", "US-0001"),
                    ambiguous,
                ),
                context=CertificationContext(
                    human_approval_evidence_id="EV-HUMAN"
                ),
            )
        assert captured.value.code == "INVALID_USER_STORY"
    else:
        result = certify(
            current_story,
            available_evidence=(
                evidence("EV-AC-001", "AC-001"),
                evidence("EV-GATE-001", "US-0001"),
                ambiguous,
            ),
            context=CertificationContext(human_approval_evidence_id="EV-HUMAN"),
        )
        assert result.result is CertificationResult.BLOCKED


@pytest.mark.parametrize("producer", LEGITIMATE_HUMAN_IDENTITIES)
def test_attributable_human_identity_can_certify(producer: str) -> None:
    current_story = story(
        human_required=True,
        human_approved=True,
        approved_by=producer,
    )
    human = evidence(
        "EV-HUMAN",
        "US-0001",
        evidence_type=EvidenceType.HUMAN_APPROVAL,
        source="Human",
        producer=producer,
    )

    result = certify(
        current_story,
        available_evidence=(
            evidence("EV-AC-001", "AC-001"),
            evidence("EV-GATE-001", "US-0001"),
            human,
        ),
        context=CertificationContext(human_approval_evidence_id="EV-HUMAN"),
    )

    assert result.result is CertificationResult.CERTIFIED


def test_blank_identity_cannot_satisfy_human_approval() -> None:
    producer = "   "
    current_story = story(
        human_required=True,
        human_approved=True,
        approved_by=producer,
    )
    fake = evidence(
        "EV-HUMAN",
        "US-0001",
        evidence_type=EvidenceType.HUMAN_APPROVAL,
        source="Human",
        producer=producer,
    )

    result = certify(
        current_story,
        available_evidence=(
            evidence("EV-AC-001", "AC-001"),
            evidence("EV-GATE-001", "US-0001"),
            fake,
        ),
        context=CertificationContext(human_approval_evidence_id="EV-HUMAN"),
    )

    assert result.result is CertificationResult.BLOCKED


@pytest.mark.parametrize(
    "certifier", ("Codex", "codex", "CODEX", "CoDeX")
)
def test_codex_certifier_case_variants_require_an_explicit_role(
    certifier: str,
) -> None:
    with pytest.raises(CertificationError) as captured:
        service().certify(
            story(),
            COMMIT,
            (acceptance(),),
            (gate(),),
            (
                evidence("EV-AC-001", "AC-001"),
                evidence("EV-GATE-001", "US-0001"),
            ),
            certifier=certifier,
        )

    assert captured.value.code == "CERTIFIER_ROLE_REQUIRED"


@pytest.mark.parametrize(
    "available_evidence",
    [
        (evidence("EV-GATE-001", "US-0001"),),
        (
            evidence("EV-AC-001", "AC-001", commit=OTHER_COMMIT),
            evidence("EV-GATE-001", "US-0001"),
        ),
        (
            evidence("EV-AC-001", "AC-001", commit=None),
            evidence("EV-GATE-001", "US-0001"),
        ),
    ],
)
def test_missing_or_wrong_commit_evidence_blocks(
    available_evidence: tuple[Evidence, ...],
) -> None:
    result = certify(available_evidence=available_evidence)

    assert result.result is CertificationResult.BLOCKED


def test_explicit_repository_independent_evidence_can_omit_commit() -> None:
    result = certify(
        available_evidence=(
            evidence("EV-AC-001", "AC-001", commit=None),
            evidence("EV-GATE-001", "US-0001"),
        ),
        context=CertificationContext(
            repository_independent_evidence_ids=frozenset({"EV-AC-001"})
        ),
    )

    assert result.result is CertificationResult.CERTIFIED


@pytest.mark.parametrize(
    ("candidate", "certification_context"),
    [
        (evidence("EV-AC-001", "AC-OTHER"), CertificationContext()),
        (
            evidence("EV-AC-001", "AC-001"),
            CertificationContext(stale_evidence_ids=frozenset({"EV-AC-001"})),
        ),
    ],
)
def test_wrong_subject_or_stale_evidence_blocks(
    candidate: Evidence,
    certification_context: CertificationContext,
) -> None:
    result = certify(
        available_evidence=(
            candidate,
            evidence("EV-GATE-001", "US-0001"),
        ),
        context=certification_context,
    )

    assert result.result is CertificationResult.BLOCKED


@pytest.mark.parametrize(
    "required_gate",
    [gate(subject="US-OTHER"), gate(required=False)],
)
def test_required_gate_contract_mismatch_blocks(required_gate: Gate) -> None:
    result = certify(gates=(required_gate,))

    assert result.result is CertificationResult.BLOCKED


@pytest.mark.parametrize(
    ("commit", "certifier", "code"),
    [
        ("", "Codex/Certifier", "COMMIT_REQUIRED"),
        (COMMIT, "", "CERTIFIER_REQUIRED"),
        (COMMIT, "Codex", "CERTIFIER_ROLE_REQUIRED"),
    ],
)
def test_missing_commit_or_certifier_is_explicitly_refused(
    commit: str, certifier: str, code: str
) -> None:
    with pytest.raises(CertificationError) as captured:
        service().certify(
            story(),
            commit,
            (acceptance(),),
            (gate(),),
            (
                evidence("EV-AC-001", "AC-001"),
                evidence("EV-GATE-001", "US-0001"),
            ),
            certifier=certifier,
        )

    assert captured.value.code == code


def test_duplicate_mandatory_information_is_ambiguous_and_blocks() -> None:
    result = certify(
        acceptance_results=(acceptance(), acceptance(result=GateResult.FAIL))
    )

    assert result.result is CertificationResult.BLOCKED


def test_optional_gate_never_replaces_missing_required_gate() -> None:
    result = certify(
        gates=(gate("GATE-OPTIONAL", required=False),),
    )

    assert result.result is CertificationResult.BLOCKED
    assert result.gate_results == {"GATE-001": "UNKNOWN"}


def test_agent_assertion_without_evidence_cannot_certify() -> None:
    result = certify(
        acceptance_results=(acceptance(evidence_refs=()),),
        available_evidence=(evidence("EV-GATE-001", "US-0001"),),
    )

    assert result.result is CertificationResult.BLOCKED


def test_unrelated_evidence_type_cannot_prove_acceptance() -> None:
    assertion = evidence(
        "EV-AC-001",
        "AC-001",
        evidence_type=EvidenceType.REVIEW_RESULT,
        producer="Codex/Reviewer",
    )

    result = certify(
        available_evidence=(
            assertion,
            evidence("EV-GATE-001", "US-0001"),
        )
    )

    assert result.result is CertificationResult.BLOCKED


def test_service_never_mutates_story_or_calls_state_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_story = story()
    before = to_dict(current_story)

    def forbidden_transition(*args: object, **kwargs: object) -> None:
        raise AssertionError("StateTransitionService must not be called")

    monkeypatch.setattr(StateTransitionService, "apply", forbidden_transition)

    result = certify(current_story)

    assert result.result is CertificationResult.CERTIFIED
    assert to_dict(current_story) == before
    assert current_story.status is UserStoryStatus.CERTIFICATION


def test_final_certification_is_validated_by_contract_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = ContractValidator()
    validate = validator.validate
    calls: list[str] = []

    def observe(contract_name: str, candidate: object) -> ValidationResult:
        calls.append(contract_name)
        return validate(contract_name, candidate)

    monkeypatch.setattr(validator, "validate", observe)
    result = CertificationService(
        validator=validator,
        id_factory=lambda: "CERT-001",
        clock=lambda: NOW,
    ).certify(
        story(),
        COMMIT,
        (acceptance(),),
        (gate(),),
        (
            evidence("EV-AC-001", "AC-001"),
            evidence("EV-GATE-001", "US-0001"),
        ),
        certifier="Codex/Certifier",
    )

    assert result.result is CertificationResult.CERTIFIED
    assert calls[-1] == "certification"
    assert validate("certification", to_dict(result)).is_valid


def test_final_validation_failure_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = ContractValidator()
    validate = validator.validate

    def reject_certification(
        contract_name: str, candidate: object
    ) -> ValidationResult:
        if contract_name == "certification":
            return ValidationResult(
                contract="certification",
                errors=(ValidationIssue("TEST_REJECTION", (), "rejected"),),
            )
        return validate(contract_name, candidate)

    monkeypatch.setattr(validator, "validate", reject_certification)

    with pytest.raises(CertificationError) as captured:
        CertificationService(
            validator=validator,
            id_factory=lambda: "CERT-001",
            clock=lambda: NOW,
        ).certify(
            story(),
            COMMIT,
            (acceptance(),),
            (gate(),),
            (
                evidence("EV-AC-001", "AC-001"),
                evidence("EV-GATE-001", "US-0001"),
            ),
            certifier="Codex/Certifier",
        )

    assert captured.value.code == "VALIDATION_FAILED"
