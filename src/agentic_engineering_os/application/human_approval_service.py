"""Controlled application of already-recorded Human approval Evidence."""

from dataclasses import dataclass

from agentic_engineering_os.domain import Evidence, EvidenceType, UserStory, to_dict

from ._identity import is_attributable_human_identity
from .contract_validator import ContractValidator


@dataclass(frozen=True, slots=True)
class HumanApprovalResult:
    subject: str
    evidence_id: str
    applied: bool
    approved: bool
    approved_by: str | None
    reason: str


class HumanApprovalError(RuntimeError):
    """The proposed Human decision cannot be applied authoritatively."""


class HumanApprovalService:
    def __init__(self, validator: ContractValidator | None = None) -> None:
        self._validator = validator or ContractValidator()

    def evaluate(
        self,
        story: UserStory,
        evidence: Evidence,
        *,
        expected_commit: str,
    ) -> HumanApprovalResult:
        if not isinstance(story, UserStory) or not isinstance(evidence, Evidence):
            raise HumanApprovalError("explicit UserStory and Evidence are required")
        if not isinstance(expected_commit, str) or not expected_commit.strip():
            raise HumanApprovalError("an explicit expected commit is required")
        if not self._validator.validate("evidence", to_dict(evidence)).is_valid:
            raise HumanApprovalError("Human Evidence violates its canonical contract")
        valid = (
            story.human_approval.required
            and evidence.evidence_type is EvidenceType.HUMAN_APPROVAL
            and evidence.subject == story.id
            and isinstance(evidence.result, bool)
            and evidence.source.casefold() == "human"
            and is_attributable_human_identity(evidence.producer)
            and (evidence.commit is None or evidence.commit == expected_commit)
        )
        if not valid:
            raise HumanApprovalError("Human Evidence is not attributable or applicable")
        if evidence.result is False:
            return HumanApprovalResult(
                story.id,
                evidence.evidence_id,
                False,
                False,
                None,
                "HUMAN_REFUSED",
            )
        if story.human_approval.approved:
            raise HumanApprovalError("replacement of an existing approval is undefined")
        return HumanApprovalResult(
            story.id,
            evidence.evidence_id,
            True,
            True,
            evidence.producer,
            "HUMAN_APPROVED",
        )

    def apply(
        self,
        story: UserStory,
        evidence: Evidence,
        *,
        expected_commit: str,
    ) -> HumanApprovalResult:
        result = self.evaluate(story, evidence, expected_commit=expected_commit)
        if result.applied:
            approval = story.human_approval
            approval.approved = True
            approval.approved_by = result.approved_by
            approval.approved_at = evidence.timestamp
            approval.evidence_ref = evidence.evidence_id
        return result
