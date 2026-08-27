"""Deterministic integrity checks for persisted CERTIFIED dossiers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypeVar

from agentic_engineering_os.domain import (
    Certification,
    CertificationResult,
    Evidence,
    EvidenceType,
    Gate,
    GateResult,
    UserStory,
)

from ._identity import (
    has_attributable_codex_role,
    is_attributable_human_identity,
    is_codex_identity,
)


_Item = TypeVar("_Item")


@dataclass(frozen=True, slots=True)
class CertificationIntegrityIssue:
    """One locally provable inconsistency in a certified dossier."""

    code: str
    message: str


def certified_dossier_issues(
    story: UserStory,
    certification: Certification,
    gates: Iterable[Gate],
    evidence: Iterable[Evidence],
) -> tuple[CertificationIntegrityIssue, ...]:
    """Verify persisted dossier coherence without recomputing its verdict."""

    if certification.result is not CertificationResult.CERTIFIED:
        return ()

    issues: list[CertificationIntegrityIssue] = []
    supplied_gates = tuple(item for item in gates if isinstance(item, Gate))
    supplied_evidence = tuple(
        item for item in evidence if isinstance(item, Evidence)
    )
    evidence_by_id = _index_by_id(supplied_evidence, "evidence_id")
    gates_by_id = _index_by_id(supplied_gates, "gate_id")

    if certification.subject != story.id:
        _issue(
            issues,
            "SUBJECT_MISMATCH",
            "Certification subject differs from User Story",
        )

    if not _attributable_certifier(certification.certifier):
        _issue(issues, "CERTIFIER_NOT_ATTRIBUTABLE", "certifier is not attributable")

    referenced_evidence: list[Evidence] = []
    for evidence_id in certification.evidence_refs:
        matches = evidence_by_id.get(evidence_id, ())
        if len(matches) != 1:
            _issue(
                issues,
                "EVIDENCE_REFERENCE_INVALID",
                f"Evidence reference is missing or ambiguous: {evidence_id}",
            )
            continue
        item = matches[0]
        referenced_evidence.append(item)
        if item.commit is not None and item.commit != certification.commit:
            _issue(
                issues,
                "EVIDENCE_COMMIT_MISMATCH",
                f"Evidence {evidence_id} does not match certified commit",
            )

    criteria_by_id = {
        criterion.id: criterion for criterion in story.acceptance_criteria
    }
    allowed_evidence_subjects = {story.id, *criteria_by_id}
    for item in referenced_evidence:
        if item.subject not in allowed_evidence_subjects:
            _issue(
                issues,
                "EVIDENCE_SUBJECT_MISMATCH",
                f"Certification Evidence has unrelated subject: {item.evidence_id}",
            )
    if set(certification.acceptance_results) != set(criteria_by_id):
        _issue(
            issues,
            "ACCEPTANCE_RESULTS_INCOMPLETE",
            "acceptance results do not exactly cover the User Story criteria",
        )
    for criterion in story.acceptance_criteria:
        result = certification.acceptance_results.get(criterion.id)
        if criterion.mandatory and result != GateResult.PASS.value:
            _issue(
                issues,
                "MANDATORY_ACCEPTANCE_NOT_PASS",
                f"mandatory Acceptance Criterion is not PASS: {criterion.id}",
            )
            continue
        if not criterion.mandatory or result != GateResult.PASS.value:
            continue
        proof = tuple(
            item
            for item in referenced_evidence
            if item.subject == criterion.id
            and item.evidence_type is EvidenceType.ACCEPTANCE_CRITERION_CHECK
        )
        if not proof or any(item.result is not True for item in proof):
            _issue(
                issues,
                "ACCEPTANCE_EVIDENCE_INVALID",
                f"mandatory Acceptance Criterion lacks positive Evidence: {criterion.id}",
            )

    required_gate_ids = set(story.required_gates)
    authorized_not_applicable_gates = (
        certification.authorized_not_applicable_gates
    )
    authorized_gate_ids = set(authorized_not_applicable_gates)
    if len(authorized_gate_ids) != len(authorized_not_applicable_gates):
        _issue(
            issues,
            "NOT_APPLICABLE_AUTHORITY_DUPLICATE",
            "persisted NOT_APPLICABLE Gate authority contains duplicates",
        )
    for gate_id in sorted(authorized_gate_ids - required_gate_ids):
        _issue(
            issues,
            "NOT_APPLICABLE_AUTHORITY_UNKNOWN",
            f"persisted NOT_APPLICABLE authority is not a required Gate: {gate_id}",
        )

    if set(certification.gate_results) != required_gate_ids:
        _issue(
            issues,
            "GATE_RESULTS_INCOMPLETE",
            "gate results do not exactly cover the required Gates",
        )
    for gate_id in story.required_gates:
        matches = gates_by_id.get(gate_id, ())
        if len(matches) != 1:
            _issue(
                issues,
                "REQUIRED_GATE_MISSING",
                f"required Gate is missing or ambiguous: {gate_id}",
            )
            continue
        gate = matches[0]
        if gate.subject != story.id or not gate.required:
            _issue(
                issues,
                "REQUIRED_GATE_MISMATCH",
                f"required Gate is not applicable to the User Story: {gate_id}",
            )
        recorded_result = certification.gate_results.get(gate_id)
        if recorded_result != gate.result.value:
            _issue(
                issues,
                "GATE_RESULT_MISMATCH",
                f"Certification and persisted Gate disagree: {gate_id}",
            )
        if gate.result not in {GateResult.PASS, GateResult.NOT_APPLICABLE}:
            _issue(
                issues,
                "REQUIRED_GATE_NOT_SATISFIED",
                f"required Gate cannot support CERTIFIED: {gate_id}",
            )
        if (
            gate.result is GateResult.NOT_APPLICABLE
            and gate_id not in authorized_gate_ids
        ):
            _issue(
                issues,
                "NOT_APPLICABLE_AUTHORITY_MISSING",
                f"required NOT_APPLICABLE Gate lacks persisted authority: {gate_id}",
            )
        if (
            gate.result is not GateResult.NOT_APPLICABLE
            and gate_id in authorized_gate_ids
        ):
            _issue(
                issues,
                "NOT_APPLICABLE_AUTHORITY_UNUSED",
                f"persisted NOT_APPLICABLE authority does not match Gate result: {gate_id}",
            )
        if gate.result is GateResult.PASS and not gate.evidence_refs:
            _issue(
                issues,
                "GATE_EVIDENCE_MISSING",
                f"PASS Gate has no Evidence: {gate_id}",
            )
        for evidence_id in gate.evidence_refs:
            if evidence_id not in certification.evidence_refs:
                _issue(
                    issues,
                    "GATE_EVIDENCE_NOT_CERTIFIED",
                    f"Gate Evidence is absent from Certification dossier: {evidence_id}",
                )
            matches = evidence_by_id.get(evidence_id, ())
            if len(matches) == 1 and matches[0].subject != story.id:
                _issue(
                    issues,
                    "GATE_EVIDENCE_SUBJECT_MISMATCH",
                    f"Gate Evidence has wrong subject: {evidence_id}",
                )

    _validate_human_approval(
        story,
        certification,
        evidence_by_id,
        issues,
    )
    return tuple(issues)


def _validate_human_approval(
    story: UserStory,
    certification: Certification,
    evidence_by_id: dict[str, tuple[Evidence, ...]],
    issues: list[CertificationIntegrityIssue],
) -> None:
    approval = story.human_approval
    recorded = certification.human_approval
    if not approval.required:
        if (
            recorded.get("required") is not False
            or recorded.get("evidence_ref") is not None
        ):
            _issue(
                issues,
                "HUMAN_APPROVAL_MISMATCH",
                "Certification records unexpected Human Approval authority",
            )
        return

    evidence_id = recorded.get("evidence_ref")
    if (
        not approval.approved
        or approval.approved_by is None
        or approval.approved_at is None
        or approval.approved_at.tzinfo is None
        or recorded.get("required") is not True
        or recorded.get("approved") is not True
        or recorded.get("result") != GateResult.PASS.value
        or not isinstance(evidence_id, str)
        or evidence_id not in certification.evidence_refs
    ):
        _issue(
            issues,
            "HUMAN_APPROVAL_INCOMPLETE",
            "required Human Approval is not completely represented",
        )
        return

    matches = evidence_by_id.get(evidence_id, ())
    if len(matches) != 1:
        _issue(
            issues,
            "HUMAN_EVIDENCE_INVALID",
            "Human Approval Evidence is missing or ambiguous",
        )
        return
    item = matches[0]
    if not (
        item.evidence_type is EvidenceType.HUMAN_APPROVAL
        and item.subject == story.id
        and item.result is True
        and item.source.casefold() == "human"
        and item.producer == approval.approved_by
        and is_attributable_human_identity(item.producer)
        and (item.commit is None or item.commit == certification.commit)
    ):
        _issue(
            issues,
            "HUMAN_EVIDENCE_INVALID",
            "Human Approval Evidence is not attributable or applicable",
        )


def _attributable_certifier(value: object) -> bool:
    if is_codex_identity(value):
        return has_attributable_codex_role(value)
    return is_attributable_human_identity(value)


def _index_by_id(
    items: Iterable[_Item], field: str
) -> dict[str, tuple[_Item, ...]]:
    indexed: dict[str, list[_Item]] = {}
    for item in items:
        identifier = getattr(item, field)
        indexed.setdefault(identifier, []).append(item)
    return {key: tuple(values) for key, values in indexed.items()}


def _issue(
    issues: list[CertificationIntegrityIssue], code: str, message: str
) -> None:
    issues.append(CertificationIntegrityIssue(code=code, message=message))
