"""Proof-dossier contracts and deterministic validation for the Certifier role."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import cast

from agentic_engineering_os.domain import (
    Evidence,
    EvidenceType,
    Gate,
    GateResult,
    MissionRole,
    OperatingStep,
    UserStory,
    UserStoryStatus,
    to_dict,
)

from ._identity import is_attributable_human_identity
from .architect import ArchitectResult, ArchitectVerdict
from .contract_validator import ContractValidator, ValidationIssue, ValidationResult
from .implementer import ImplementerResult, ImplementerVerdict
from .orchestrator import RoleHandoff
from .reviewer import ReviewerResult, ReviewerVerdict
from .tester import TesterResult, TesterVerdict


_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
_ARTIFACT_ROLES = ("ARCHITECT", "IMPLEMENTER", "TESTER", "REVIEWER")


class CertifierVerdict(str, Enum):
    READY_FOR_CONTROL_PLANE = "READY_FOR_CONTROL_PLANE"
    REMEDIATION_REQUIRED = "REMEDIATION_REQUIRED"
    BLOCKED = "BLOCKED"


class CertifierRecommendedAction(str, Enum):
    SUBMIT_TO_CONTROL_PLANE = "SUBMIT_TO_CONTROL_PLANE"
    RETURN_FOR_REMEDIATION = "RETURN_FOR_REMEDIATION"
    RESOLVE_BLOCKERS = "RESOLVE_BLOCKERS"


@dataclass(frozen=True, slots=True)
class ArtifactCheck:
    artifact_role: MissionRole
    present: bool
    coherent: bool
    notes: str


@dataclass(frozen=True, slots=True)
class AcceptanceCheck:
    acceptance_criterion_id: str
    mandatory: bool
    result: GateResult
    evidence_refs: tuple[str, ...]
    notes: str


@dataclass(frozen=True, slots=True)
class GateCheck:
    gate_id: str
    present: bool
    subject_matches: bool
    result: GateResult
    evidence_refs: tuple[str, ...]
    commit_matches: bool
    not_applicable_authorized: bool
    notes: str


@dataclass(frozen=True, slots=True)
class HumanApprovalCheck:
    required: bool
    present: bool
    valid: bool
    evidence_ref: str | None
    notes: str


@dataclass(frozen=True, slots=True)
class CertifierFinding:
    code: str
    summary: str
    demonstrated_failure: bool


@dataclass(frozen=True, slots=True)
class CertifierInput:
    """Immutable dossier supplied by the Orchestrator, without control authority."""

    mission_id: str
    workflow_generation: int
    user_story: UserStory
    architect_result: ArchitectResult | None
    implementer_result: ImplementerResult | None
    tester_result: TesterResult | None
    reviewer_result: ReviewerResult | None
    evidence: tuple[Evidence, ...]
    gates: tuple[Gate, ...]
    observed_commit: str
    authorized_not_applicable_gate_ids: frozenset[str]
    objective: str
    blockers: tuple[str, ...]
    instructions: str
    _assignment_snapshot: str = field(default="", init=False, repr=False, compare=False)

    @classmethod
    def from_handoff(
        cls,
        handoff: RoleHandoff,
        user_story: UserStory,
        architect_result: ArchitectResult | None,
        implementer_result: ImplementerResult | None,
        tester_result: TesterResult | None,
        reviewer_result: ReviewerResult | None,
        evidence: tuple[Evidence, ...],
        gates: tuple[Gate, ...],
        *,
        authorized_not_applicable_gate_ids: frozenset[str] = frozenset(),
        validator: ContractValidator | None = None,
    ) -> CertifierInput:
        resolved = validator if validator is not None else ContractValidator()
        _require_handoff(handoff)
        _require_story(user_story, resolved)
        if handoff.subject != user_story.id:
            raise CertifierInputError("handoff subject must identify the UserStory")
        if handoff.blockers:
            raise CertifierInputError("handoff with active blockers is not assignable")
        if not isinstance(evidence, tuple) or not all(isinstance(x, Evidence) for x in evidence):
            raise CertifierInputError("evidence must be an explicit tuple of Evidence")
        if not isinstance(gates, tuple) or not all(isinstance(x, Gate) for x in gates):
            raise CertifierInputError("gates must be an explicit tuple of Gate")
        expected_types = (
            (architect_result, ArchitectResult),
            (implementer_result, ImplementerResult),
            (tester_result, TesterResult),
            (reviewer_result, ReviewerResult),
        )
        if any(value is not None and not isinstance(value, expected) for value, expected in expected_types):
            raise CertifierInputError("prior artifacts must use their explicit result contracts or be absent")
        if not isinstance(authorized_not_applicable_gate_ids, frozenset) or not all(
            isinstance(x, str) and x.strip() for x in authorized_not_applicable_gate_ids
        ):
            raise CertifierInputError("NOT_APPLICABLE authority must be explicit Gate ids")
        _require_unique_valid_domain_items(evidence, gates, resolved)
        result = cls(
            mission_id=handoff.mission_id,
            workflow_generation=handoff.workflow_generation,
            user_story=deepcopy(user_story),
            architect_result=deepcopy(architect_result),
            implementer_result=deepcopy(implementer_result),
            tester_result=deepcopy(tester_result),
            reviewer_result=deepcopy(reviewer_result),
            evidence=deepcopy(evidence),
            gates=deepcopy(gates),
            observed_commit=handoff.observed_commit,
            authorized_not_applicable_gate_ids=frozenset(authorized_not_applicable_gate_ids),
            objective=handoff.objective,
            blockers=tuple(handoff.blockers),
            instructions=handoff.instructions,
        )
        object.__setattr__(result, "_assignment_snapshot", _input_snapshot(result))
        return result


@dataclass(frozen=True, slots=True)
class CertifierResult:
    """A dossier-readiness opinion; never an authoritative Certification."""

    mission_id: str
    workflow_generation: int
    role: MissionRole = field(default=MissionRole.CERTIFIER, init=False)
    subject: str
    user_story_id: str
    observed_commit: str
    summary: str
    artifact_checks: tuple[ArtifactCheck, ...]
    acceptance_checks: tuple[AcceptanceCheck, ...]
    gate_checks: tuple[GateCheck, ...]
    evidence_refs: tuple[str, ...]
    human_approval_check: HumanApprovalCheck
    findings: tuple[CertifierFinding, ...]
    blockers: tuple[str, ...]
    recommended_action: CertifierRecommendedAction
    verdict: CertifierVerdict


class CertifierInputError(ValueError):
    """The supplied dossier cannot safely authorize Certifier activity."""


class CertifierResultValidator:
    """Validate dossier consistency without calling the CertificationService."""

    def __init__(self, validator: ContractValidator | None = None) -> None:
        self._validator = validator if validator is not None else ContractValidator()

    def validate(
        self,
        candidate: CertifierResult | Mapping[str, object],
        *,
        certifier_input: CertifierInput,
    ) -> ValidationResult:
        if not isinstance(certifier_input, CertifierInput):
            return _invalid("INVALID_VALIDATION_CONTEXT", "certifier_input is required")
        if not certifier_input._assignment_snapshot or (
            certifier_input._assignment_snapshot != _input_snapshot(certifier_input)
        ):
            return _invalid("CERTIFIER_INPUT_TAMPERED", "CertifierInput differs from its snapshot")
        if isinstance(candidate, CertifierResult):
            serialized = cast(dict[str, object], to_dict(candidate))
        elif isinstance(candidate, Mapping):
            serialized = dict(candidate)
        else:
            return _invalid("INVALID_CERTIFIER_OUTPUT", "candidate must be a CertifierResult or mapping")
        schema_result = self._validator.validate("certifier-result", serialized)
        if not schema_result.is_valid:
            return schema_result

        issues: list[ValidationIssue] = []
        _validate_context(serialized, certifier_input, issues)
        truth = _dossier_truth(certifier_input, self._validator)
        _validate_artifact_checks(serialized, truth, issues)
        _validate_acceptance_checks(serialized, truth, issues)
        _validate_gate_checks(serialized, truth, issues)
        _validate_evidence_and_human(serialized, truth, issues)
        _validate_verdict(serialized, truth, issues)
        return ValidationResult(contract="certifier-result", errors=tuple(issues))


def _require_handoff(handoff: RoleHandoff) -> None:
    if not isinstance(handoff, RoleHandoff):
        raise CertifierInputError("input must be an explicit RoleHandoff")
    if handoff.from_role is not MissionRole.ORCHESTRATOR or handoff.to_role is not MissionRole.CERTIFIER:
        raise CertifierInputError("RoleHandoff must be ORCHESTRATOR to CERTIFIER")
    if handoff.operating_step is not OperatingStep.CONTROLLED_TRANSITION:
        raise CertifierInputError("Certifier handoff must target CONTROLLED_TRANSITION")
    if not all(isinstance(x, str) and x.strip() for x in (handoff.mission_id, handoff.subject, handoff.objective, handoff.instructions)):
        raise CertifierInputError("handoff text fields must be non-empty")
    if not _COMMIT_PATTERN.fullmatch(handoff.observed_commit):
        raise CertifierInputError("observed_commit must be a full Git SHA")
    if (
        not isinstance(handoff.workflow_generation, int)
        or isinstance(handoff.workflow_generation, bool)
        or handoff.workflow_generation < 0
    ):
        raise CertifierInputError("workflow_generation must be a non-negative integer")


def _require_story(story: UserStory, validator: ContractValidator) -> None:
    if not isinstance(story, UserStory) or not validator.validate("user-story", to_dict(story)).is_valid:
        raise CertifierInputError("UserStory does not satisfy its canonical contract")
    if story.status is not UserStoryStatus.CERTIFICATION:
        raise CertifierInputError("UserStory must be CERTIFICATION for Certifier activity")


def _require_unique_valid_domain_items(evidence: tuple[Evidence, ...], gates: tuple[Gate, ...], validator: ContractValidator) -> None:
    evidence_ids = [x.evidence_id for x in evidence]
    gate_ids = [x.gate_id for x in gates]
    if len(evidence_ids) != len(set(evidence_ids)) or len(gate_ids) != len(set(gate_ids)):
        raise CertifierInputError("Evidence and Gate ids must be unique")
    if any(not validator.validate("evidence", to_dict(x)).is_valid for x in evidence):
        raise CertifierInputError("Evidence does not satisfy its canonical contract")
    if any(not validator.validate("gate", to_dict(x)).is_valid for x in gates):
        raise CertifierInputError("Gate does not satisfy its canonical contract")


def _artifact_state(value: object, role: MissionRole, contract: str, expected_verdict: object, context: CertifierInput, validator: ContractValidator) -> tuple[bool, bool]:
    if value is None:
        return False, False
    valid = validator.validate(contract, to_dict(value)).is_valid
    coherent = valid and getattr(value, "role", None) is role and getattr(value, "mission_id", None) == context.mission_id and getattr(value, "observed_commit", "").casefold() == context.observed_commit.casefold() and getattr(value, "verdict", None) is expected_verdict
    if role is MissionRole.ARCHITECT:
        coherent = coherent and getattr(value, "workflow_generation", -1) <= context.workflow_generation
        stories = [x for x in cast(ArchitectResult, value).user_stories if x.id == context.user_story.id]
        coherent = coherent and len(stories) == 1 and _same_story_contract(stories[0], context.user_story)
    else:
        coherent = coherent and getattr(value, "workflow_generation", None) == context.workflow_generation and getattr(value, "subject", None) == context.user_story.id and getattr(value, "user_story_id", None) == context.user_story.id
    if role is MissionRole.IMPLEMENTER and isinstance(value, ImplementerResult):
        coherent = coherent and not value.blockers and all(
            not item.required or item.result.value == "PASS"
            for item in value.verification_results
        )
    if role is MissionRole.TESTER and isinstance(value, TesterResult):
        mandatory = {x.id for x in context.user_story.acceptance_criteria if x.mandatory}
        observed = {x.acceptance_criterion_id: x.result for x in value.acceptance_results}
        coherent = (
            coherent
            and not value.blockers
            and all(observed.get(x) is GateResult.PASS for x in mandatory)
            and all(not x.required or (x.executed and x.verdict is GateResult.PASS) for x in value.test_cases)
            and all(not x.required or (x.executed and x.result is GateResult.PASS) for x in value.verification_results)
        )
    if role is MissionRole.REVIEWER and isinstance(value, ReviewerResult):
        coherent = coherent and not value.blockers and not any(x.blocking for x in value.findings)
    return True, bool(coherent)


def _same_story_contract(candidate: UserStory, current: UserStory) -> bool:
    """Compare the immutable engineering contract while allowing lifecycle fields."""

    return (
        candidate.id == current.id
        and candidate.title == current.title
        and candidate.description == current.description
        and candidate.priority == current.priority
        and candidate.risk is current.risk
        and candidate.depends_on == current.depends_on
        and candidate.scope == current.scope
        and candidate.acceptance_criteria == current.acceptance_criteria
        and candidate.required_gates == current.required_gates
        and candidate.human_approval.required is current.human_approval.required
    )


def _dossier_truth(context: CertifierInput, validator: ContractValidator) -> dict[str, object]:
    artifacts = {
        "ARCHITECT": _artifact_state(context.architect_result, MissionRole.ARCHITECT, "architect-result", ArchitectVerdict.READY, context, validator),
        "IMPLEMENTER": _artifact_state(context.implementer_result, MissionRole.IMPLEMENTER, "implementer-result", ImplementerVerdict.READY_FOR_TEST, context, validator),
        "TESTER": _artifact_state(context.tester_result, MissionRole.TESTER, "tester-result", TesterVerdict.READY_FOR_REVIEW, context, validator),
        "REVIEWER": _artifact_state(context.reviewer_result, MissionRole.REVIEWER, "reviewer-result", ReviewerVerdict.READY_FOR_CERTIFICATION, context, validator),
    }
    evidence = {x.evidence_id: x for x in context.evidence}
    tester_results = {} if context.tester_result is None else {x.acceptance_criterion_id: x.result for x in context.tester_result.acceptance_results}
    acceptance: dict[str, tuple[bool, str]] = {}
    for criterion in context.user_story.acceptance_criteria:
        result = tester_results.get(criterion.id)
        acceptance[criterion.id] = (criterion.mandatory, result.value if isinstance(result, GateResult) else "UNKNOWN")
    gates_by_id = {x.gate_id: x for x in context.gates}
    human_matches = [x for x in context.evidence if x.evidence_type is EvidenceType.HUMAN_APPROVAL and x.subject == context.user_story.id]
    approval = context.user_story.human_approval
    human_valid = (not approval.required) or (len(human_matches) == 1 and approval.approved and approval.approved_at is not None and approval.evidence_ref == human_matches[0].evidence_id and is_attributable_human_identity(approval.approved_by) and human_matches[0].result is True and human_matches[0].source.casefold() == "human" and human_matches[0].producer == approval.approved_by and is_attributable_human_identity(human_matches[0].producer) and (human_matches[0].commit is None or human_matches[0].commit.casefold() == context.observed_commit.casefold()))
    return {"context": context, "artifacts": artifacts, "acceptance": acceptance, "gates": gates_by_id, "evidence": evidence, "human_matches": human_matches, "human_valid": human_valid}


def _validate_context(data: Mapping[str, object], context: CertifierInput, issues: list[ValidationIssue]) -> None:
    expected = {"mission_id": context.mission_id, "workflow_generation": context.workflow_generation, "subject": context.user_story.id, "user_story_id": context.user_story.id, "observed_commit": context.observed_commit.casefold()}
    actual = {**{k: data[k] for k in ("mission_id", "workflow_generation", "subject", "user_story_id")}, "observed_commit": cast(str, data["observed_commit"]).casefold()}
    for key, value in expected.items():
        if actual[key] != value:
            issues.append(ValidationIssue("CERTIFIER_CONTEXT_MISMATCH", (key,), f"{key} differs from CertifierInput"))


def _validate_artifact_checks(data: Mapping[str, object], truth: Mapping[str, object], issues: list[ValidationIssue]) -> None:
    checks = cast(list[Mapping[str, object]], data["artifact_checks"])
    if [x["artifact_role"] for x in checks] != list(_ARTIFACT_ROLES):
        issues.append(ValidationIssue("ARTIFACT_CHAIN_INCOMPLETE", ("artifact_checks",), "artifact checks must contain the ordered prior-role chain"))
        return
    states = cast(Mapping[str, tuple[bool, bool]], truth["artifacts"])
    for index, item in enumerate(checks):
        expected = states[cast(str, item["artifact_role"])]
        if (item["present"], item["coherent"]) != expected:
            issues.append(ValidationIssue("ARTIFACT_CHECK_MISMATCH", ("artifact_checks", index), "artifact check contradicts the supplied dossier"))


def _validate_acceptance_checks(data: Mapping[str, object], truth: Mapping[str, object], issues: list[ValidationIssue]) -> None:
    checks = cast(list[Mapping[str, object]], data["acceptance_checks"])
    expected = cast(Mapping[str, tuple[bool, str]], truth["acceptance"])
    if [x["acceptance_criterion_id"] for x in checks] != list(expected):
        issues.append(ValidationIssue("ACCEPTANCE_CHECKS_INCOMPLETE", ("acceptance_checks",), "checks must exactly cover the UserStory criteria"))
        return
    for index, item in enumerate(checks):
        mandatory, result = expected[cast(str, item["acceptance_criterion_id"])]
        if item["mandatory"] is not mandatory or item["result"] != result:
            issues.append(ValidationIssue("ACCEPTANCE_CHECK_MISMATCH", ("acceptance_checks", index), "Acceptance check contradicts TesterResult"))
        available = cast(Mapping[str, Evidence], truth["evidence"])
        for ref in cast(list[str], item["evidence_refs"]):
            evidence = available.get(ref)
            context = cast(CertifierInput, truth["context"])
            expected_payload = (
                True
                if result == GateResult.PASS.value
                else False if result == GateResult.FAIL.value else None
            )
            if evidence is not None and (
                evidence.subject != item["acceptance_criterion_id"]
                or evidence.evidence_type is not EvidenceType.ACCEPTANCE_CRITERION_CHECK
                or not isinstance(evidence.result, bool)
                or expected_payload is None
                or evidence.result is not expected_payload
                or evidence.commit is None
                or evidence.commit.casefold() != context.observed_commit.casefold()
            ):
                issues.append(ValidationIssue("ACCEPTANCE_EVIDENCE_INAPPLICABLE", ("acceptance_checks", index, "evidence_refs"), "Acceptance Evidence must exist and support the declared criterion result"))


def _validate_gate_checks(data: Mapping[str, object], truth: Mapping[str, object], issues: list[ValidationIssue]) -> None:
    checks = cast(list[Mapping[str, object]], data["gate_checks"])
    context = cast(CertifierInput, truth["context"])
    gate_map = cast(Mapping[str, Gate], truth["gates"])
    if len({cast(str, x["gate_id"]) for x in checks}) != len(checks):
        issues.append(ValidationIssue("DUPLICATE_GATE_CHECK", ("gate_checks",), "Gate checks must be unique"))
    if [cast(str, x["gate_id"]) for x in checks] != list(context.user_story.required_gates):
        issues.append(ValidationIssue("GATE_CHECKS_INCOMPLETE", ("gate_checks",), "Gate checks must exactly cover required Gates"))
    for index, item in enumerate(checks):
        gate = gate_map.get(cast(str, item["gate_id"]))
        if gate is None and (
            item["present"] is not False
            or item["subject_matches"] is not False
            or item["result"] != GateResult.UNKNOWN.value
            or item["evidence_refs"]
            or item["commit_matches"] is not False
        ):
            issues.append(ValidationIssue("GATE_CHECK_MISMATCH", ("gate_checks", index), "missing Gate must remain an explicit UNKNOWN check"))
        if gate is not None and (item["present"] is not True or item["result"] != gate.result.value or item["evidence_refs"] != list(gate.evidence_refs)):
            issues.append(ValidationIssue("GATE_CHECK_MISMATCH", ("gate_checks", index), "Gate check contradicts the supplied Gate"))
        if gate is not None:
            subject_matches = gate.subject == context.user_story.id and gate.required
            refs = [cast(Mapping[str, Evidence], truth["evidence"]).get(ref) for ref in gate.evidence_refs]
            commit_matches = all(x is not None and x.commit is not None and x.commit.casefold() == context.observed_commit.casefold() for x in refs)
            authorized = gate.gate_id in context.authorized_not_applicable_gate_ids
            if item["subject_matches"] is not subject_matches or item["commit_matches"] is not commit_matches or item["not_applicable_authorized"] is not authorized:
                issues.append(ValidationIssue("GATE_CHECK_MISMATCH", ("gate_checks", index), "Gate applicability, commit, or authority check is incorrect"))


def _validate_evidence_and_human(data: Mapping[str, object], truth: Mapping[str, object], issues: list[ValidationIssue]) -> None:
    available = cast(Mapping[str, Evidence], truth["evidence"])
    refs = cast(list[str], data["evidence_refs"])
    for index, ref in enumerate(refs):
        if ref not in available:
            issues.append(ValidationIssue("EVIDENCE_REFERENCE_MISSING", ("evidence_refs", index), "Evidence reference is absent from CertifierInput"))
    nested_refs: set[str] = set()
    for check in cast(list[Mapping[str, object]], data["acceptance_checks"]):
        nested_refs.update(cast(list[str], check["evidence_refs"]))
    for check in cast(list[Mapping[str, object]], data["gate_checks"]):
        nested_refs.update(cast(list[str], check["evidence_refs"]))
    human = cast(Mapping[str, object], data["human_approval_check"])
    if human["evidence_ref"] is not None:
        nested_refs.add(cast(str, human["evidence_ref"]))
    if set(refs) != {ref for ref in nested_refs if ref in available}:
        issues.append(ValidationIssue("EVIDENCE_REFERENCE_SET_MISMATCH", ("evidence_refs",), "evidence_refs must exactly collect all dossier check references"))
    matches = cast(list[Evidence], truth["human_matches"])
    context = cast(CertifierInput, truth["context"])
    expected_present = bool(matches)
    expected_valid = cast(bool, truth["human_valid"])
    if human["required"] is not context.user_story.human_approval.required or human["present"] is not expected_present or human["valid"] is not expected_valid:
        issues.append(ValidationIssue("HUMAN_APPROVAL_CHECK_MISMATCH", ("human_approval_check",), "Human approval check contradicts canonical identity and Evidence rules"))
    expected_ref = matches[0].evidence_id if len(matches) == 1 else None
    if human["evidence_ref"] != expected_ref:
        issues.append(ValidationIssue("HUMAN_APPROVAL_REFERENCE_MISMATCH", ("human_approval_check", "evidence_ref"), "Human Evidence reference is missing or ambiguous"))
    if human["required"] is False and human["evidence_ref"] is not None:
        issues.append(ValidationIssue("UNEXPECTED_HUMAN_AUTHORITY", ("human_approval_check", "evidence_ref"), "non-required approval cannot introduce authority"))


def _validate_verdict(data: Mapping[str, object], truth: Mapping[str, object], issues: list[ValidationIssue]) -> None:
    verdict = cast(str, data["verdict"])
    blockers = cast(list[str], data["blockers"])
    findings = cast(list[Mapping[str, object]], data["findings"])
    demonstrated = any(x["demonstrated_failure"] is True for x in findings)
    artifacts_ok = all(present and coherent for present, coherent in cast(Mapping[str, tuple[bool, bool]], truth["artifacts"]).values())
    acceptance = cast(Mapping[str, tuple[bool, str]], truth["acceptance"])
    mandatory_results = [result for mandatory, result in acceptance.values() if mandatory]
    data_gates = cast(list[Mapping[str, object]], data["gate_checks"])
    gates_ready = all(
        x["present"] is True
        and x["subject_matches"] is True
        and x["commit_matches"] is True
        and (
            (x["result"] == "PASS" and bool(x["evidence_refs"]))
            or (
                x["result"] == "NOT_APPLICABLE"
                and x["not_applicable_authorized"] is True
            )
        )
        for x in data_gates
    )
    context = cast(CertifierInput, truth["context"])
    actual_na = {cast(str, x["gate_id"]) for x in data_gates if x["result"] == "NOT_APPLICABLE"}
    authority_coherent = context.authorized_not_applicable_gate_ids == actual_na
    nested_refs = [ref for x in cast(list[Mapping[str, object]], data["acceptance_checks"]) for ref in cast(list[str], x["evidence_refs"])] + [ref for x in data_gates for ref in cast(list[str], x["evidence_refs"])]
    refs_exist = all(ref in cast(Mapping[str, Evidence], truth["evidence"]) for ref in nested_refs)
    mandatory_check_refs = all(cast(list[str], x["evidence_refs"]) for x in cast(list[Mapping[str, object]], data["acceptance_checks"]) if x["mandatory"] is True)
    human_ok = cast(bool, truth["human_valid"])
    if verdict == CertifierVerdict.READY_FOR_CONTROL_PLANE.value:
        if not artifacts_ok or any(x != "PASS" for x in mandatory_results) or not mandatory_check_refs or not gates_ready or not authority_coherent or not refs_exist or not human_ok or blockers:
            issues.append(ValidationIssue("DOSSIER_NOT_READY", ("verdict",), "incomplete, failed, unknown, or unauthorized dossier cannot be READY_FOR_CONTROL_PLANE"))
        if any(x in {"FAIL", "UNKNOWN"} for x in mandatory_results) or any(x["result"] in {"FAIL", "UNKNOWN"} for x in data_gates):
            issues.append(ValidationIssue("FAIL_OR_UNKNOWN_FORBIDS_READY", ("verdict",), "FAIL and UNKNOWN never permit readiness"))
        if demonstrated:
            issues.append(ValidationIssue("DEMONSTRATED_FAILURE_FORBIDS_READY", ("verdict",), "a demonstrated failure requires remediation"))
    elif verdict == CertifierVerdict.REMEDIATION_REQUIRED.value and not demonstrated:
        issues.append(ValidationIssue("REMEDIATION_WITHOUT_DEMONSTRATED_FAILURE", ("verdict",), "remediation requires an explicit demonstrated failure"))
    elif verdict == CertifierVerdict.BLOCKED.value and demonstrated:
        issues.append(ValidationIssue("BLOCKED_WITH_DEMONSTRATED_FAILURE", ("verdict",), "a demonstrated failure requires remediation, not BLOCKED"))


def _input_snapshot(value: CertifierInput) -> str:
    def item(candidate: object | None) -> object:
        return None if candidate is None else to_dict(candidate)
    return json.dumps({
        "mission_id": value.mission_id, "workflow_generation": value.workflow_generation, "user_story": to_dict(value.user_story),
        "architect_result": item(value.architect_result), "implementer_result": item(value.implementer_result),
        "tester_result": item(value.tester_result), "reviewer_result": item(value.reviewer_result),
        "evidence": [to_dict(x) for x in value.evidence], "gates": [to_dict(x) for x in value.gates],
        "observed_commit": value.observed_commit, "authorized_not_applicable_gate_ids": sorted(value.authorized_not_applicable_gate_ids),
        "objective": value.objective, "blockers": list(value.blockers), "instructions": value.instructions,
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _invalid(code: str, message: str) -> ValidationResult:
    return ValidationResult("certifier-result", (ValidationIssue(code, (), message),))
