"""Deterministic evaluation of Gates from explicit Evidence and context."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TypeAlias

from agentic_engineering_os.domain import Evidence, Gate, GateResult, to_dict

from .contract_validator import ContractValidator, ValidationIssue


GateCondition: TypeAlias = Callable[[tuple[Evidence, ...]], GateResult | str]


@dataclass(frozen=True, slots=True)
class GateContract:
    gate_id: str
    subject: str
    required: bool
    evidence_ids: tuple[str, ...]
    condition: GateCondition
    repository_dependent: bool
    evaluator: str


@dataclass(frozen=True, slots=True)
class GateEvaluationContext:
    expected_commit: str | None = None
    stale_evidence_ids: frozenset[str] = frozenset()
    not_applicable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class GateReason:
    code: str
    message: str
    evidence_id: str | None = None


@dataclass(frozen=True, slots=True)
class GateEvaluation:
    gate: Gate
    reasons: tuple[GateReason, ...]

    @property
    def gate_id(self) -> str:
        return self.gate.gate_id

    @property
    def result(self) -> GateResult:
        return self.gate.result

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        return self.gate.evidence_refs


class GateEvaluationError(RuntimeError):
    """A canonical Gate result could not be constructed or validated."""

    def __init__(
        self,
        code: str,
        message: str,
        validation_errors: tuple[ValidationIssue, ...] = (),
    ) -> None:
        self.code = code
        self.message = message
        self.validation_errors = validation_errors
        super().__init__(f"{code}: {message}")


class GateEvaluator:
    """Evaluate one explicit Gate contract without making certification decisions."""

    def __init__(
        self,
        *,
        validator: ContractValidator | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._validator = validator if validator is not None else ContractValidator()
        self._clock = clock if clock is not None else _utc_now

    def evaluate(
        self,
        contract: GateContract,
        evidence: Iterable[Evidence],
        *,
        context: GateEvaluationContext | None = None,
        evaluated_at: datetime | None = None,
    ) -> GateEvaluation:
        """Return a validated Gate evaluation, with no implicit success path."""

        self._validate_contract(contract)
        resolved_context = context or GateEvaluationContext()
        timestamp = _utc_timestamp(
            evaluated_at if evaluated_at is not None else self._clock()
        )

        if resolved_context.not_applicable_reason is not None:
            if resolved_context.not_applicable_reason.strip():
                return self._result(
                    contract,
                    GateResult.NOT_APPLICABLE,
                    (),
                    timestamp,
                    (
                        GateReason(
                            "EXPLICITLY_NOT_APPLICABLE",
                            resolved_context.not_applicable_reason,
                        ),
                    ),
                )
            return self._unknown(
                contract,
                timestamp,
                "NOT_APPLICABLE_NOT_PROVEN",
                "non-applicability requires an explicit non-empty reason",
            )

        if not contract.evidence_ids:
            return self._unknown(
                contract,
                timestamp,
                "EVIDENCE_REQUIRED",
                "no Evidence is required by the Gate contract, so PASS is unproven",
            )

        if contract.repository_dependent and (
            resolved_context.expected_commit is None
            or not resolved_context.expected_commit.strip()
        ):
            return self._unknown(
                contract,
                timestamp,
                "EXPECTED_COMMIT_REQUIRED",
                "repository-dependent Gate requires an explicit expected commit",
            )

        try:
            candidates = tuple(evidence)
        except Exception as error:
            return self._unknown(
                contract,
                timestamp,
                "TECHNICAL_ERROR",
                f"Evidence collection could not be read: {type(error).__name__}: {error}",
            )

        selected, issue = self._select_evidence(contract, candidates, resolved_context)
        if issue is not None:
            return self._result(
                contract,
                GateResult.UNKNOWN,
                (),
                timestamp,
                (issue,),
            )

        try:
            condition_result = GateResult(contract.condition(selected))
        except (TypeError, ValueError) as error:
            return self._result(
                contract,
                GateResult.UNKNOWN,
                tuple(item.evidence_id for item in selected),
                timestamp,
                (
                    GateReason(
                        "AMBIGUOUS_RESULT",
                        f"Gate condition returned no canonical result: {error}",
                    ),
                ),
            )
        except Exception as error:
            return self._result(
                contract,
                GateResult.UNKNOWN,
                tuple(item.evidence_id for item in selected),
                timestamp,
                (
                    GateReason(
                        "TECHNICAL_ERROR",
                        f"Gate condition failed: {type(error).__name__}: {error}",
                    ),
                ),
            )

        evidence_refs = tuple(item.evidence_id for item in selected)
        if condition_result is GateResult.NOT_APPLICABLE:
            return self._result(
                contract,
                GateResult.UNKNOWN,
                evidence_refs,
                timestamp,
                (
                    GateReason(
                        "NOT_APPLICABLE_NOT_PROVEN",
                        "NOT_APPLICABLE must be justified explicitly by context",
                    ),
                ),
            )
        if condition_result is GateResult.PASS:
            reason = GateReason(
                "CONDITION_PROVEN",
                "applicable Evidence explicitly satisfies the Gate condition",
            )
        elif condition_result is GateResult.FAIL:
            reason = GateReason(
                "CONDITION_FAILED",
                "applicable Evidence explicitly disproves the Gate condition",
            )
        else:
            reason = GateReason(
                "CONDITION_UNKNOWN",
                "applicable Evidence does not determine the Gate condition",
            )
        return self._result(
            contract,
            condition_result,
            evidence_refs,
            timestamp,
            (reason,),
        )

    def _select_evidence(
        self,
        contract: GateContract,
        candidates: tuple[Evidence, ...],
        context: GateEvaluationContext,
    ) -> tuple[tuple[Evidence, ...], GateReason | None]:
        by_id: dict[str, list[Evidence]] = {}
        for item in candidates:
            if isinstance(item, Evidence):
                by_id.setdefault(item.evidence_id, []).append(item)

        selected: list[Evidence] = []
        for evidence_id in contract.evidence_ids:
            matches = by_id.get(evidence_id, [])
            if not matches:
                return (), GateReason(
                    "EVIDENCE_MISSING",
                    "required Evidence is absent",
                    evidence_id,
                )
            if len(matches) != 1:
                return (), GateReason(
                    "EVIDENCE_AMBIGUOUS",
                    "multiple candidate Evidence share the required id",
                    evidence_id,
                )

            item = matches[0]
            validity_issue = self._evidence_issue(item)
            if validity_issue is not None:
                return (), validity_issue
            if item.subject != contract.subject:
                return (), GateReason(
                    "SUBJECT_MISMATCH",
                    "Evidence subject does not match the Gate subject",
                    evidence_id,
                )
            if evidence_id in context.stale_evidence_ids:
                return (), GateReason(
                    "EVIDENCE_STALE",
                    "stale Evidence cannot determine the current Gate",
                    evidence_id,
                )
            if contract.repository_dependent and item.commit != context.expected_commit:
                return (), GateReason(
                    "COMMIT_MISMATCH",
                    "Evidence commit does not match the expected commit",
                    evidence_id,
                )
            selected.append(item)
        return tuple(selected), None

    def _evidence_issue(self, evidence: Evidence) -> GateReason | None:
        try:
            validation = self._validator.validate("evidence", to_dict(evidence))
        except Exception as error:
            return GateReason(
                "EVIDENCE_VALIDATION_UNAVAILABLE",
                f"Evidence validity cannot be proven: {type(error).__name__}: {error}",
                evidence.evidence_id,
            )
        if validation.is_valid:
            return None
        return GateReason(
            "EVIDENCE_INVALID",
            "Evidence violates the canonical contract",
            evidence.evidence_id,
        )

    def _unknown(
        self,
        contract: GateContract,
        timestamp: datetime,
        code: str,
        message: str,
    ) -> GateEvaluation:
        return self._result(
            contract,
            GateResult.UNKNOWN,
            (),
            timestamp,
            (GateReason(code, message),),
        )

    def _result(
        self,
        contract: GateContract,
        result: GateResult,
        evidence_refs: tuple[str, ...],
        timestamp: datetime,
        reasons: tuple[GateReason, ...],
    ) -> GateEvaluation:
        gate = Gate(
            gate_id=contract.gate_id,
            subject=contract.subject,
            required=contract.required,
            result=result,
            evidence_refs=evidence_refs,
            evaluated_at=timestamp,
            evaluator=contract.evaluator,
        )
        try:
            validation = self._validator.validate("gate", to_dict(gate))
        except Exception as error:
            raise GateEvaluationError(
                "VALIDATION_UNAVAILABLE",
                f"Gate validation could not be completed: {error}",
            ) from error
        if not validation.is_valid:
            raise GateEvaluationError(
                "VALIDATION_FAILED",
                "evaluated Gate violates the canonical contract",
                validation.errors,
            )
        return GateEvaluation(gate=gate, reasons=reasons)

    @staticmethod
    def _validate_contract(contract: GateContract) -> None:
        if not isinstance(contract, GateContract):
            raise GateEvaluationError(
                "INVALID_GATE_CONTRACT", "an explicit GateContract is required"
            )
        if len(set(contract.evidence_ids)) != len(contract.evidence_ids):
            raise GateEvaluationError(
                "DUPLICATE_EVIDENCE_REQUIREMENT",
                "Gate contract Evidence ids must be unique",
            )
        if not callable(contract.condition):
            raise GateEvaluationError(
                "INVALID_GATE_CONDITION", "Gate condition must be callable"
            )
        if not isinstance(contract.repository_dependent, bool):
            raise GateEvaluationError(
                "INVALID_GATE_CONTRACT",
                "repository dependence must be stated explicitly",
            )


def _utc_timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise GateEvaluationError(
            "UTC_TIMESTAMP_REQUIRED", "Gate evaluation timestamp must be timezone-aware"
        )
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
