from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from agentic_engineering_os.application import (
    ContractValidator,
    GateContract,
    GateEvaluationContext,
    GateEvaluationError,
    GateEvaluator,
    ValidationIssue,
    ValidationResult,
)
from agentic_engineering_os.domain import Evidence, EvidenceType, GateResult


COMMIT = "935fa67dac3030a4cd7f7702b142718b751a7eac"
OTHER_COMMIT = "be11488dfeceebbf8614ec730dff51612d673478"
NOW = datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc)


def boolean_condition(items: tuple[Evidence, ...]) -> GateResult:
    results = tuple(item.result for item in items)
    if any(result is False for result in results):
        return GateResult.FAIL
    if results and all(result is True for result in results):
        return GateResult.PASS
    return GateResult.UNKNOWN


def evidence(
    evidence_id: str = "EV-001",
    *,
    subject: str = "US-001",
    result: object = True,
    commit: str | None = COMMIT,
    source: str = "pytest",
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.TEST_RESULT,
        subject=subject,
        result=result,
        source=source,
        command="pytest -q",
        exit_code=0,
        artifact="test output",
        commit=commit,
        timestamp=NOW,
        producer="pytest",
    )


def contract(
    *evidence_ids: str,
    required: bool = True,
    condition=boolean_condition,
) -> GateContract:
    return GateContract(
        gate_id="GATE-001",
        subject="US-001",
        required=required,
        evidence_ids=tuple(evidence_ids),
        condition=condition,
        repository_dependent=True,
        evaluator="Codex/Reviewer",
    )


def context(
    *,
    commit: str | None = COMMIT,
    stale: frozenset[str] = frozenset(),
    not_applicable_reason: str | None = None,
) -> GateEvaluationContext:
    return GateEvaluationContext(
        expected_commit=commit,
        stale_evidence_ids=stale,
        not_applicable_reason=not_applicable_reason,
    )


def evaluator(*, validator: ContractValidator | None = None) -> GateEvaluator:
    return GateEvaluator(validator=validator, clock=lambda: NOW)


def test_pass_requires_sufficient_applicable_evidence() -> None:
    result = evaluator().evaluate(contract("EV-001"), [evidence()], context=context())

    assert result.gate_id == "GATE-001"
    assert result.result is GateResult.PASS
    assert result.evidence_refs == ("EV-001",)
    assert result.reasons[0].code == "CONDITION_PROVEN"


def test_proven_failure_remains_fail() -> None:
    result = evaluator().evaluate(
        contract("EV-001"), [evidence(result=False)], context=context()
    )

    assert result.result is GateResult.FAIL
    assert result.evidence_refs == ("EV-001",)
    assert result.reasons[0].code == "CONDITION_FAILED"


def test_explicit_not_applicable_reason_is_accepted_without_evidence() -> None:
    result = evaluator().evaluate(
        contract("EV-001"),
        [],
        context=context(not_applicable_reason="Gate excluded by the contract."),
    )

    assert result.result is GateResult.NOT_APPLICABLE
    assert result.evidence_refs == ()
    assert result.reasons[0].code == "EXPLICITLY_NOT_APPLICABLE"


def test_multiple_evidence_are_used_in_contract_order() -> None:
    result = evaluator().evaluate(
        contract("EV-002", "EV-001"),
        [evidence("EV-001"), evidence("EV-002")],
        context=context(),
    )

    assert result.result is GateResult.PASS
    assert result.evidence_refs == ("EV-002", "EV-001")


def test_optional_gate_is_evaluated_without_certification_policy() -> None:
    result = evaluator().evaluate(
        contract("EV-001", required=False),
        [evidence(result=False)],
        context=context(),
    )

    assert result.gate.required is False
    assert result.result is GateResult.FAIL


def test_no_evidence_produces_unknown_not_pass_or_not_applicable() -> None:
    result = evaluator().evaluate(contract("EV-001"), [], context=context())

    assert result.result is GateResult.UNKNOWN
    assert result.evidence_refs == ()
    assert result.reasons[0].code == "EVIDENCE_MISSING"


def test_contract_without_evidence_requirements_has_no_implicit_pass_path() -> None:
    result = evaluator().evaluate(contract(), [], context=context())

    assert result.result is GateResult.UNKNOWN
    assert result.evidence_refs == ()
    assert result.reasons[0].code == "EVIDENCE_REQUIRED"


def test_unrelated_evidence_does_not_replace_missing_required_evidence() -> None:
    result = evaluator().evaluate(
        contract("EV-MISSING"), [evidence("EV-OTHER")], context=context()
    )

    assert result.result is GateResult.UNKNOWN
    assert result.evidence_refs == ()
    assert result.reasons[0].code == "EVIDENCE_MISSING"


def test_insufficient_evidence_result_produces_unknown() -> None:
    result = evaluator().evaluate(
        contract("EV-001"), [evidence(result="ambiguous")], context=context()
    )

    assert result.result is GateResult.UNKNOWN
    assert result.evidence_refs == ("EV-001",)
    assert result.reasons[0].code == "CONDITION_UNKNOWN"


@pytest.mark.parametrize(
    ("candidate", "evaluation_context", "reason"),
    [
        (
            evidence(),
            context(stale=frozenset({"EV-001"})),
            "EVIDENCE_STALE",
        ),
        (evidence(subject="US-OTHER"), context(), "SUBJECT_MISMATCH"),
        (evidence(commit=OTHER_COMMIT), context(), "COMMIT_MISMATCH"),
    ],
)
def test_inapplicable_evidence_cannot_produce_pass(
    candidate: Evidence,
    evaluation_context: GateEvaluationContext,
    reason: str,
) -> None:
    result = evaluator().evaluate(
        contract("EV-001"), [candidate], context=evaluation_context
    )

    assert result.result is GateResult.UNKNOWN
    assert result.evidence_refs == ()
    assert result.reasons[0].code == reason


def test_duplicate_candidate_id_is_ambiguous() -> None:
    result = evaluator().evaluate(
        contract("EV-001"),
        [evidence(result=True), evidence(result=False)],
        context=context(),
    )

    assert result.result is GateResult.UNKNOWN
    assert result.evidence_refs == ()
    assert result.reasons[0].code == "EVIDENCE_AMBIGUOUS"


def test_unproven_not_applicable_condition_becomes_unknown() -> None:
    result = evaluator().evaluate(
        contract("EV-001", condition=lambda items: GateResult.NOT_APPLICABLE),
        [evidence()],
        context=context(),
    )

    assert result.result is GateResult.UNKNOWN
    assert result.reasons[0].code == "NOT_APPLICABLE_NOT_PROVEN"


def test_empty_not_applicable_reason_becomes_unknown() -> None:
    result = evaluator().evaluate(
        contract("EV-001"),
        [evidence()],
        context=context(not_applicable_reason=" "),
    )

    assert result.result is GateResult.UNKNOWN
    assert result.evidence_refs == ()


def test_repository_gate_without_expected_commit_is_unknown() -> None:
    result = evaluator().evaluate(
        contract("EV-001"), [evidence()], context=context(commit=None)
    )

    assert result.result is GateResult.UNKNOWN
    assert result.reasons[0].code == "EXPECTED_COMMIT_REQUIRED"


def test_schema_invalid_evidence_is_unknown() -> None:
    result = evaluator().evaluate(
        contract("EV-001"), [evidence(source="")], context=context()
    )

    assert result.result is GateResult.UNKNOWN
    assert result.evidence_refs == ()
    assert result.reasons[0].code == "EVIDENCE_INVALID"


def test_invalid_condition_result_never_becomes_pass() -> None:
    result = evaluator().evaluate(
        contract("EV-001", condition=lambda items: "MAGIC"),
        [evidence()],
        context=context(),
    )

    assert result.result is GateResult.UNKNOWN
    assert result.reasons[0].code == "AMBIGUOUS_RESULT"


def test_condition_technical_error_becomes_unknown() -> None:
    def explode(items: tuple[Evidence, ...]) -> GateResult:
        raise RuntimeError("condition unavailable")

    result = evaluator().evaluate(
        contract("EV-001", condition=explode), [evidence()], context=context()
    )

    assert result.result is GateResult.UNKNOWN
    assert result.reasons[0].code == "TECHNICAL_ERROR"


def test_gate_validation_failure_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = ContractValidator()
    validate = validator.validate

    def reject_gate(contract_name: str, candidate: object) -> ValidationResult:
        if contract_name == "gate":
            return ValidationResult(
                contract="gate",
                errors=(ValidationIssue("TEST_REJECTION", (), "rejected"),),
            )
        return validate(contract_name, candidate)

    monkeypatch.setattr(validator, "validate", reject_gate)

    with pytest.raises(GateEvaluationError) as captured:
        evaluator(validator=validator).evaluate(
            contract("EV-001"), [evidence()], context=context()
        )

    assert captured.value.code == "VALIDATION_FAILED"
    assert captured.value.validation_errors


def test_evaluated_gate_is_immutable_and_timestamp_is_utc() -> None:
    local_time = datetime(
        2026, 8, 27, 16, 0, tzinfo=timezone(timedelta(hours=2))
    )
    result = evaluator().evaluate(
        contract("EV-001"),
        [evidence()],
        context=context(),
        evaluated_at=local_time,
    )

    assert result.gate.evaluated_at == NOW
    assert result.gate.evaluated_at.utcoffset() == timedelta(0)
    with pytest.raises(FrozenInstanceError):
        result.gate.result = GateResult.FAIL
