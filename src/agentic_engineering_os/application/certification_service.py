"""Deterministic certification of one User Story at one explicit commit."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from uuid import uuid4

from agentic_engineering_os.domain import (
    Certification,
    CertificationResult,
    Evidence,
    EvidenceType,
    Gate,
    GateResult,
    UserStory,
    to_dict,
)

from ._identity import (
    has_attributable_codex_role,
    is_attributable_human_identity,
    is_codex_identity,
)
from .certification_integrity import certified_dossier_issues
from .contract_validator import ContractValidator, ValidationIssue


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    criterion_id: str
    result: GateResult | str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CertificationContext:
    stale_evidence_ids: frozenset[str] = frozenset()
    repository_independent_evidence_ids: frozenset[str] = frozenset()
    allowed_not_applicable_gate_ids: frozenset[str] = frozenset()
    human_approval_evidence_id: str | None = None


class CertificationError(RuntimeError):
    """A canonical Certification could not be evaluated or validated."""

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


class CertificationService:
    """Apply Phase 0 certification rules without changing project state."""

    def __init__(
        self,
        *,
        validator: ContractValidator | None = None,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._validator = validator if validator is not None else ContractValidator()
        self._id_factory = id_factory if id_factory is not None else _new_id
        self._clock = clock if clock is not None else _utc_now

    def certify(
        self,
        user_story: UserStory,
        commit: str,
        acceptance_results: Iterable[AcceptanceResult],
        gates: Iterable[Gate],
        evidence: Iterable[Evidence],
        *,
        certifier: str,
        context: CertificationContext | None = None,
        certification_id: str | None = None,
        certified_at: datetime | None = None,
    ) -> Certification:
        """Return one validated verdict; never mutate the supplied User Story."""

        self._validate_request(user_story, commit, certifier)
        resolved_context = context or CertificationContext()
        try:
            timestamp = _utc_timestamp(
                certified_at if certified_at is not None else self._clock()
            )
            resolved_id = (
                certification_id
                if certification_id is not None
                else self._id_factory()
            )
        except CertificationError:
            raise
        except Exception as error:
            raise CertificationError(
                "TECHNICAL_ERROR",
                f"certification metadata could not be created: {error}",
            ) from error

        try:
            supplied_acceptance = tuple(acceptance_results)
            supplied_gates = tuple(gates)
            supplied_evidence = tuple(evidence)
        except Exception as error:
            raise CertificationError(
                "TECHNICAL_ERROR",
                f"certification inputs could not be read: {type(error).__name__}: {error}",
            ) from error

        evidence_index = _index_evidence(supplied_evidence)
        failures: list[str] = []
        blockers: list[str] = []
        used_evidence: list[str] = []
        authorized_not_applicable_gates: list[str] = []

        unknown_gate_authorities = (
            resolved_context.allowed_not_applicable_gate_ids
            - set(user_story.required_gates)
        )
        blockers.extend(
            f"gate-authority:{gate_id}:unknown-or-not-required"
            for gate_id in sorted(unknown_gate_authorities)
        )

        acceptance_map = self._evaluate_acceptance(
            user_story,
            supplied_acceptance,
            evidence_index,
            commit,
            resolved_context,
            failures,
            blockers,
            used_evidence,
        )
        gate_map = self._evaluate_gates(
            user_story,
            supplied_gates,
            evidence_index,
            commit,
            resolved_context,
            failures,
            blockers,
            used_evidence,
            authorized_not_applicable_gates,
        )
        human_map = self._evaluate_human_approval(
            user_story,
            evidence_index,
            commit,
            resolved_context,
            failures,
            blockers,
            used_evidence,
        )

        # Normative priority: an explicitly proven mandatory failure dominates
        # uncertainty; uncertainty dominates success.
        if failures:
            verdict = CertificationResult.REJECTED
        elif blockers:
            verdict = CertificationResult.BLOCKED
        else:
            verdict = CertificationResult.CERTIFIED

        certification = Certification(
            certification_id=resolved_id,
            subject=user_story.id,
            result=verdict,
            commit=commit,
            acceptance_results=MappingProxyType(acceptance_map),
            gate_results=MappingProxyType(gate_map),
            human_approval=MappingProxyType(human_map),
            evidence_refs=tuple(dict.fromkeys(used_evidence)),
            certified_at=timestamp,
            certifier=certifier,
            authorized_not_applicable_gates=tuple(
                authorized_not_applicable_gates
            ),
        )
        self._validate_certification(certification)
        integrity_issues = certified_dossier_issues(
            user_story,
            certification,
            supplied_gates,
            supplied_evidence,
        )
        if integrity_issues:
            details = "; ".join(
                f"{issue.code}: {issue.message}" for issue in integrity_issues
            )
            raise CertificationError(
                "INVALID_CERTIFICATION_DOSSIER",
                f"CERTIFIED dossier is not persistently coherent: {details}",
            )
        return certification

    def _validate_request(
        self, user_story: UserStory, commit: str, certifier: str
    ) -> None:
        if not isinstance(user_story, UserStory):
            raise CertificationError(
                "INVALID_USER_STORY", "an explicit UserStory is required"
            )
        if not isinstance(commit, str) or not commit.strip():
            raise CertificationError(
                "COMMIT_REQUIRED", "an explicit commit is required for certification"
            )
        if not isinstance(certifier, str) or not certifier.strip():
            raise CertificationError(
                "CERTIFIER_REQUIRED", "an explicit certifier is required"
            )
        if is_codex_identity(certifier) and not has_attributable_codex_role(
            certifier
        ):
            raise CertificationError(
                "CERTIFIER_ROLE_REQUIRED",
                "Codex certification requires an explicit Codex/<role> identity",
            )
        try:
            validation = self._validator.validate("user-story", to_dict(user_story))
        except Exception as error:
            raise CertificationError(
                "VALIDATION_UNAVAILABLE",
                f"User Story validation could not be completed: {error}",
            ) from error
        if not validation.is_valid:
            raise CertificationError(
                "INVALID_USER_STORY",
                "User Story violates the canonical contract",
                validation.errors,
            )

    def _evaluate_acceptance(
        self,
        user_story: UserStory,
        supplied: tuple[AcceptanceResult, ...],
        evidence_index: Mapping[str, tuple[Evidence, ...]],
        commit: str,
        context: CertificationContext,
        failures: list[str],
        blockers: list[str],
        used_evidence: list[str],
    ) -> dict[str, str]:
        by_id: dict[str, list[AcceptanceResult]] = {}
        for item in supplied:
            if isinstance(item, AcceptanceResult):
                by_id.setdefault(item.criterion_id, []).append(item)

        results: dict[str, str] = {}
        for criterion in user_story.acceptance_criteria:
            matches = by_id.get(criterion.id, [])
            if len(matches) != 1:
                results[criterion.id] = GateResult.UNKNOWN.value
                if criterion.mandatory:
                    blockers.append(f"acceptance:{criterion.id}:missing-or-ambiguous")
                continue

            item = matches[0]
            result = _gate_result(item.result)
            if result is None:
                results[criterion.id] = GateResult.UNKNOWN.value
                if criterion.mandatory:
                    blockers.append(f"acceptance:{criterion.id}:unknown-result")
                continue
            results[criterion.id] = result.value

            refs_valid, resolved_refs = self._resolve_evidence(
                item.evidence_refs,
                evidence_index,
                subject=criterion.id,
                commit=commit,
                context=context,
            )
            refs_valid = refs_valid and _acceptance_evidence_supports(
                result, resolved_refs, evidence_index
            )
            if refs_valid:
                used_evidence.extend(resolved_refs)

            if not criterion.mandatory:
                continue
            if result is GateResult.FAIL and refs_valid:
                failures.append(f"acceptance:{criterion.id}:failed")
            elif result is not GateResult.PASS or not refs_valid:
                blockers.append(f"acceptance:{criterion.id}:not-proven")
        return results

    def _evaluate_gates(
        self,
        user_story: UserStory,
        supplied: tuple[Gate, ...],
        evidence_index: Mapping[str, tuple[Evidence, ...]],
        commit: str,
        context: CertificationContext,
        failures: list[str],
        blockers: list[str],
        used_evidence: list[str],
        authorized_not_applicable_gates: list[str],
    ) -> dict[str, str]:
        by_id: dict[str, list[Gate]] = {}
        for gate in supplied:
            if isinstance(gate, Gate):
                by_id.setdefault(gate.gate_id, []).append(gate)

        results: dict[str, str] = {}
        for gate_id in user_story.required_gates:
            matches = by_id.get(gate_id, [])
            if len(matches) != 1:
                results[gate_id] = GateResult.UNKNOWN.value
                blockers.append(f"gate:{gate_id}:missing-or-ambiguous")
                continue

            gate = matches[0]
            if not self._gate_is_valid(gate):
                results[gate_id] = GateResult.UNKNOWN.value
                blockers.append(f"gate:{gate_id}:invalid")
                continue
            results[gate_id] = gate.result.value

            refs_valid, resolved_refs = self._resolve_evidence(
                gate.evidence_refs,
                evidence_index,
                subject=user_story.id,
                commit=commit,
                context=context,
            )
            if refs_valid:
                used_evidence.extend(resolved_refs)

            if gate.subject != user_story.id or not gate.required:
                blockers.append(f"gate:{gate_id}:contract-mismatch")
            elif gate.result is GateResult.FAIL and refs_valid:
                failures.append(f"gate:{gate_id}:failed")
            elif gate.result is GateResult.PASS and refs_valid:
                continue
            elif (
                gate.result is GateResult.NOT_APPLICABLE
                and gate_id in context.allowed_not_applicable_gate_ids
            ):
                authorized_not_applicable_gates.append(gate_id)
                continue
            else:
                blockers.append(f"gate:{gate_id}:not-proven")
        return results

    def _evaluate_human_approval(
        self,
        user_story: UserStory,
        evidence_index: Mapping[str, tuple[Evidence, ...]],
        commit: str,
        context: CertificationContext,
        failures: list[str],
        blockers: list[str],
        used_evidence: list[str],
    ) -> dict[str, object]:
        approval = user_story.human_approval
        if not approval.required:
            return {
                "required": False,
                "approved": approval.approved,
                "evidence_ref": None,
            }

        evidence_id = context.human_approval_evidence_id
        result: dict[str, object] = {
            "required": True,
            "approved": approval.approved,
            "evidence_ref": evidence_id,
        }
        if evidence_id is None:
            blockers.append("human-approval:missing")
            return result

        matches = evidence_index.get(evidence_id, ())
        if len(matches) != 1:
            blockers.append("human-approval:missing-or-ambiguous")
            return result
        item = matches[0]
        if not self._human_evidence_is_valid(item, user_story, commit, context):
            blockers.append("human-approval:invalid-or-ambiguous")
            return result

        used_evidence.append(evidence_id)
        if approval.approved and item.result is True:
            result["result"] = GateResult.PASS.value
        elif not approval.approved and item.result is False:
            result["result"] = GateResult.FAIL.value
            failures.append("human-approval:refused")
        else:
            result["result"] = GateResult.UNKNOWN.value
            blockers.append("human-approval:conflicting")
        return result

    def _resolve_evidence(
        self,
        evidence_refs: tuple[str, ...],
        evidence_index: Mapping[str, tuple[Evidence, ...]],
        *,
        subject: str,
        commit: str,
        context: CertificationContext,
    ) -> tuple[bool, tuple[str, ...]]:
        if not evidence_refs or len(set(evidence_refs)) != len(evidence_refs):
            return False, ()

        resolved: list[str] = []
        for evidence_id in evidence_refs:
            matches = evidence_index.get(evidence_id, ())
            if len(matches) != 1:
                return False, tuple(resolved)
            item = matches[0]
            if not self._evidence_is_valid(item):
                return False, tuple(resolved)
            if item.subject != subject or evidence_id in context.stale_evidence_ids:
                return False, tuple(resolved)
            repository_independent = (
                evidence_id in context.repository_independent_evidence_ids
            )
            if repository_independent:
                if item.commit is not None and item.commit != commit:
                    return False, tuple(resolved)
            elif item.commit != commit:
                return False, tuple(resolved)
            resolved.append(evidence_id)
        return True, tuple(resolved)

    def _human_evidence_is_valid(
        self,
        evidence: Evidence,
        user_story: UserStory,
        commit: str,
        context: CertificationContext,
    ) -> bool:
        approval = user_story.human_approval
        return (
            self._evidence_is_valid(evidence)
            and evidence.evidence_type is EvidenceType.HUMAN_APPROVAL
            and evidence.subject == user_story.id
            and evidence.evidence_id not in context.stale_evidence_ids
            and (evidence.commit is None or evidence.commit == commit)
            and evidence.source.casefold() == "human"
            and is_attributable_human_identity(evidence.producer)
            and approval.approved_by is not None
            and approval.approved_by == evidence.producer
            and (
                evidence.result is False
                or approval.evidence_ref == evidence.evidence_id
            )
            and approval.approved_at is not None
            and approval.approved_at.tzinfo is not None
            and isinstance(evidence.result, bool)
        )

    def _evidence_is_valid(self, evidence: Evidence) -> bool:
        try:
            validation = self._validator.validate("evidence", to_dict(evidence))
        except Exception as error:
            raise CertificationError(
                "VALIDATION_UNAVAILABLE",
                f"Evidence validation could not be completed: {error}",
            ) from error
        return validation.is_valid

    def _gate_is_valid(self, gate: Gate) -> bool:
        try:
            validation = self._validator.validate("gate", to_dict(gate))
        except Exception as error:
            raise CertificationError(
                "VALIDATION_UNAVAILABLE",
                f"Gate validation could not be completed: {error}",
            ) from error
        return validation.is_valid

    def _validate_certification(self, certification: Certification) -> None:
        try:
            validation = self._validator.validate(
                "certification", to_dict(certification)
            )
        except Exception as error:
            raise CertificationError(
                "VALIDATION_UNAVAILABLE",
                f"Certification validation could not be completed: {error}",
            ) from error
        if not validation.is_valid:
            raise CertificationError(
                "VALIDATION_FAILED",
                "Certification violates the canonical contract",
                validation.errors,
            )


def _index_evidence(
    evidence: tuple[Evidence, ...],
) -> dict[str, tuple[Evidence, ...]]:
    indexed: dict[str, list[Evidence]] = {}
    for item in evidence:
        if isinstance(item, Evidence):
            indexed.setdefault(item.evidence_id, []).append(item)
    return {key: tuple(items) for key, items in indexed.items()}


def _gate_result(value: GateResult | str) -> GateResult | None:
    try:
        return GateResult(value)
    except (TypeError, ValueError):
        return None


def _acceptance_evidence_supports(
    result: GateResult,
    evidence_refs: tuple[str, ...],
    evidence_index: Mapping[str, tuple[Evidence, ...]],
) -> bool:
    if result not in {GateResult.PASS, GateResult.FAIL}:
        return True
    items = tuple(evidence_index[evidence_id][0] for evidence_id in evidence_refs)
    if not items or any(
        item.evidence_type is not EvidenceType.ACCEPTANCE_CRITERION_CHECK
        or not isinstance(item.result, bool)
        for item in items
    ):
        return False
    if result is GateResult.PASS:
        return all(item.result is True for item in items)
    return any(item.result is False for item in items)


def _utc_timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CertificationError(
            "UTC_TIMESTAMP_REQUIRED",
            "Certification timestamp must be timezone-aware",
        )
    return value.astimezone(timezone.utc)


def _new_id() -> str:
    return f"CERT-{uuid4()}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
