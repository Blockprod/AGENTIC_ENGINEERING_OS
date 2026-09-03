"""Restart-safe post-merge role and Certification composition."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Protocol, cast

from agentic_engineering_os.domain import (
    Certification,
    EvidenceType,
    GateResult,
    MissionRole,
    MissionState,
    ProjectState,
    UserStory,
    UserStoryStatus,
    to_dict,
)

from .architect import ArchitectResult
from .certification_service import AcceptanceResult, CertificationContext
from .certifier import CertifierInput, CertifierResult, CertifierResultValidator
from .execution_state import CodexExecutionRecord, CodexExecutionStatus
from .integrated_story_context import role_result_fingerprint
from .mission_integration import MissionIntegrationCoordinator, MissionIntegrationStatus
from .orchestration_record import (
    CertificationReference,
    OrchestrationRecord,
    RoleExecutionReference,
)
from .parallel_mission_workflow import (
    ParallelMissionWorkflow,
    ParallelStoryDossier,
    ParallelStoryStage,
)
from .result_intake import reconstruct_persisted_role_result
from .reviewer import ReviewerInput, ReviewerResult, ReviewerResultValidator
from .tester import TesterInput, TesterResult, TesterResultValidator


class MissionCertificationStatus(str, Enum):
    WAITING_FOR_TESTER = "WAITING_FOR_TESTER"
    WAITING_FOR_REVIEWER = "WAITING_FOR_REVIEWER"
    WAITING_FOR_CERTIFIER = "WAITING_FOR_CERTIFIER"
    CERTIFIED = "CERTIFIED"
    BLOCKED = "BLOCKED"
    REMEDIATION_REQUIRED = "REMEDIATION_REQUIRED"


@dataclass(frozen=True, slots=True)
class MissionCertificationResult:
    status: MissionCertificationStatus
    mission_id: str
    workflow_generation: int
    user_story_id: str
    integration_commit: str
    dossier: ParallelStoryDossier
    certification_id: str | None = None
    blockers: tuple[str, ...] = ()


class MissionCertificationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class _Reader(Protocol):
    def load(self) -> object: ...


class _RecordStore(Protocol):
    def load(self) -> OrchestrationRecord: ...
    def replace(self, record: OrchestrationRecord, *, expected_fingerprint: str) -> object: ...


class MissionCertificationCoordinator:
    """Sequence existing authorities from an integrated story using durable facts."""

    def __init__(
        self,
        *,
        workflow: ParallelMissionWorkflow,
        integration: MissionIntegrationCoordinator,
        mission_store: _Reader,
        project_store: _Reader,
        execution_store: _Reader,
        record_store: _RecordStore,
    ) -> None:
        self._workflow = workflow
        self._integration = integration
        self._missions = mission_store
        self._projects = project_store
        self._executions = execution_store
        self._records = record_store
        self._tester = TesterResultValidator()
        self._reviewer = ReviewerResultValidator()
        self._certifier = CertifierResultValidator()

    def resume(
        self, mission_id: str, user_story_id: str, *, updated_at: datetime
    ) -> MissionCertificationResult:
        record, mission, project = self._context(mission_id, user_story_id)
        integrated = self._integration.resume(mission_id, updated_at=updated_at)
        if integrated.status is not MissionIntegrationStatus.READY_FOR_TESTER:
            raise MissionCertificationError(
                "INTEGRATION_INCOMPLETE", "durable integration is not ready"
            )
        try:
            index = integrated.user_story_ids.index(user_story_id)
        except ValueError as error:
            raise MissionCertificationError(
                "STORY_NOT_IN_INTEGRATION", "story is absent from integrated group"
            ) from error
        if integrated.integrated_commit is None:
            raise MissionCertificationError(
                "INTEGRATION_INCOMPLETE", "integrated commit is absent"
            )
        dossier = ParallelStoryDossier(
            mission_id,
            mission.workflow_generation,
            user_story_id,
            integrated.integrated_commit,
            ParallelStoryStage.TESTING,
            integrated.implementer_results[index],
            integrated.integrated_contexts[index],
        )

        story = _story(project, user_story_id)
        tester_result, record = self._restore_role(
            record, MissionRole.TESTER, user_story_id, integrated.integrated_commit
        )
        if tester_result is None:
            return self._waiting(dossier, MissionCertificationStatus.WAITING_FOR_TESTER)
        tester = cast(TesterResult, tester_result)
        if story.status is UserStoryStatus.TESTING:
            dossier = self._workflow.accept_tester(dossier, tester)
        else:
            self._validate_historical_tester(dossier, story, tester)
            dossier = replace(
                dossier, stage=ParallelStoryStage.REVIEW, tester_result=tester
            )
        if dossier.stage is not ParallelStoryStage.REVIEW:
            return self._non_positive(dossier)

        project = self._project()
        story = _story(project, user_story_id)
        reviewer_result, record = self._restore_role(
            record, MissionRole.REVIEWER, user_story_id, integrated.integrated_commit
        )
        if reviewer_result is None:
            return self._waiting(dossier, MissionCertificationStatus.WAITING_FOR_REVIEWER)
        reviewer = cast(ReviewerResult, reviewer_result)
        if story.status is UserStoryStatus.REVIEW:
            dossier = self._workflow.accept_reviewer(dossier, reviewer)
        else:
            self._validate_historical_reviewer(dossier, story, reviewer)
            dossier = replace(
                dossier, stage=ParallelStoryStage.CERTIFICATION, reviewer_result=reviewer
            )
        if dossier.stage is not ParallelStoryStage.CERTIFICATION:
            return self._non_positive(dossier)

        project = self._project()
        story = _story(project, user_story_id)
        certifier_result, record = self._restore_role(
            record, MissionRole.CERTIFIER, user_story_id, integrated.integrated_commit
        )
        if certifier_result is None:
            return self._waiting(dossier, MissionCertificationStatus.WAITING_FOR_CERTIFIER)
        certifier = cast(CertifierResult, certifier_result)
        architect = self._architect(record, dossier.integrated_context.architect_result_fingerprint)
        acceptance, certification_context = _certification_inputs(
            project, story, integrated.integrated_commit
        )
        self._validate_certifier(dossier, story, architect, certifier, project)
        certification_id = _certification_id(
            dossier, record, project, architect, tester, reviewer, certifier
        )

        if story.status is UserStoryStatus.CERTIFIED:
            self._require_existing_certification(
                record, project, story, integrated.integrated_commit, certification_id
            )
            dossier = replace(
                dossier,
                stage=ParallelStoryStage.CERTIFIED,
                certifier_result=certifier,
            )
        else:
            dossier = self._workflow.submit_certifier(
                dossier,
                certifier,
                architect_result=architect,
                acceptance_results=acceptance,
                certification_context=certification_context,
                certifier="Codex/Certifier",
                updated_at=updated_at,
                certification_id=certification_id,
                certification_persisted=lambda item: self._persist_certification(
                    record, item, integrated.integrated_commit
                ),
            )
        if dossier.stage is not ParallelStoryStage.CERTIFIED:
            return self._non_positive(dossier, certification_id=certification_id)
        return MissionCertificationResult(
            MissionCertificationStatus.CERTIFIED,
            mission_id,
            mission.workflow_generation,
            user_story_id,
            integrated.integrated_commit,
            dossier,
            certification_id,
        )

    def _context(
        self, mission_id: str, user_story_id: str
    ) -> tuple[OrchestrationRecord, MissionState, ProjectState]:
        record = self._records.load()
        mission = self._missions.load()
        project = self._project()
        if (
            not isinstance(record, OrchestrationRecord)
            or not isinstance(mission, MissionState)
            or record.mission_id != mission_id
            or mission.mission_id != mission_id
            or record.workflow_generation != mission.workflow_generation
            or user_story_id not in record.user_story_ids
        ):
            raise MissionCertificationError(
                "MISSION_BINDING_MISMATCH", "durable mission authorities disagree"
            )
        _story(project, user_story_id)
        return record, mission, project

    def _restore_role(
        self,
        record: OrchestrationRecord,
        role: MissionRole,
        subject: str,
        commit: str,
    ) -> tuple[object | None, OrchestrationRecord]:
        ledger = self._executions.load()
        candidates = tuple(
            item
            for item in getattr(ledger, "records", ())
            if isinstance(item, CodexExecutionRecord)
            and item.mission_id == record.mission_id
            and item.workflow_generation == record.workflow_generation
            and item.role is role
            and item.subject == subject
            and item.expected_commit == commit
            and item.status is CodexExecutionStatus.VALIDATED
            and item.validated_result_json is not None
            and item.validated_result_fingerprint is not None
        )
        references = tuple(
            item
            for item in record.execution_references
            if item.role is role
            and item.subject == subject
            and item.workflow_generation == record.workflow_generation
        )
        if references:
            if len(references) != 1:
                raise MissionCertificationError(
                    "ROLE_REFERENCE_AMBIGUOUS", "role reference is not unique"
                )
            candidates = tuple(
                item
                for item in candidates
                if _matches_reference(item, references[0])
            )
        if references and not candidates:
            raise MissionCertificationError(
                "ROLE_REFERENCE_STALE",
                "role reference has no exact validated ledger execution",
            )
        if not candidates:
            return None, record
        if len(candidates) != 1:
            raise MissionCertificationError(
                "ROLE_EXECUTION_AMBIGUOUS", "validated role execution is not unique"
            )
        execution = candidates[0]
        assert execution.validated_result_json is not None
        assert execution.validated_result_fingerprint is not None
        result = reconstruct_persisted_role_result(execution.validated_result_json, role)
        if (
            role_result_fingerprint(result) != execution.validated_result_fingerprint
            or getattr(result, "mission_id", None) != record.mission_id
            or getattr(result, "workflow_generation", None) != record.workflow_generation
            or getattr(result, "subject", None) != subject
            or getattr(result, "observed_commit", "").casefold() != commit
        ):
            raise MissionCertificationError(
                "ROLE_RESULT_FORGED", "ledger result bindings or fingerprint are inconsistent"
            )
        reference = RoleExecutionReference(
            role,
            subject,
            record.workflow_generation,
            execution.request_id,
            execution.execution_id,
            execution.validated_result_fingerprint,
        )
        if references and references[0] != reference:
            raise MissionCertificationError(
                "ROLE_REFERENCE_STALE", "role reference differs from its validated ledger"
            )
        if not references:
            record = self._replace(record, record.with_reference(reference))
        return result, record

    def _architect(self, record: OrchestrationRecord, fingerprint: str) -> ArchitectResult:
        references = tuple(
            item
            for item in record.execution_references
            if item.role is MissionRole.ARCHITECT
            and item.result_fingerprint == fingerprint
        )
        if len(references) != 1:
            raise MissionCertificationError(
                "ARCHITECT_REFERENCE_INVALID", "Architect reference is not exact"
            )
        ledger = self._executions.load()
        matches = tuple(
            item
            for item in getattr(ledger, "records", ())
            if isinstance(item, CodexExecutionRecord)
            and _matches_reference(item, references[0])
            and item.mission_id == record.mission_id
            and item.expected_commit == record.baseline_commit
            and item.status is CodexExecutionStatus.VALIDATED
            and item.validated_result_json is not None
        )
        if len(matches) != 1:
            raise MissionCertificationError(
                "ARCHITECT_LEDGER_INVALID", "Architect ledger result is not exact"
            )
        result = reconstruct_persisted_role_result(
            cast(str, matches[0].validated_result_json), MissionRole.ARCHITECT
        )
        if not isinstance(result, ArchitectResult) or role_result_fingerprint(result) != fingerprint:
            raise MissionCertificationError(
                "ARCHITECT_RESULT_FORGED", "Architect result differs from integration"
            )
        return result

    def _validate_historical_tester(
        self, dossier: ParallelStoryDossier, story: UserStory, result: TesterResult
    ) -> None:
        historical = replace(story, status=UserStoryStatus.TESTING)
        handoff = self._workflow.runtime_handoff(dossier, MissionRole.TESTER)
        assignment = TesterInput.from_integrated_handoff(
            handoff, historical, dossier.implementer_result, dossier.integrated_context
        )
        if not self._tester.validate(result, tester_input=assignment).is_valid:
            raise MissionCertificationError(
                "TESTER_RESULT_INVALID", "persisted TesterResult no longer validates"
            )

    def _validate_historical_reviewer(
        self, dossier: ParallelStoryDossier, story: UserStory, result: ReviewerResult
    ) -> None:
        historical = replace(story, status=UserStoryStatus.REVIEW)
        handoff = self._workflow.runtime_handoff(dossier, MissionRole.REVIEWER)
        assignment = ReviewerInput.from_integrated_handoff(
            handoff,
            historical,
            dossier.implementer_result,
            cast(TesterResult, dossier.tester_result),
            dossier.integrated_context,
        )
        if not self._reviewer.validate(result, reviewer_input=assignment).is_valid:
            raise MissionCertificationError(
                "REVIEWER_RESULT_INVALID", "persisted ReviewerResult no longer validates"
            )

    def _persist_certification(
        self, record: OrchestrationRecord, certification: Certification, commit: str
    ) -> None:
        reference = CertificationReference(
            certification.subject,
            record.workflow_generation,
            certification.certification_id,
            _fingerprint(to_dict(certification)),
            commit,
        )
        current = self._records.load()
        existing = tuple(
            item
            for item in current.certification_references
            if item.user_story_id == certification.subject
            and item.workflow_generation == record.workflow_generation
        )
        if existing:
            if existing != (reference,):
                raise MissionCertificationError(
                    "CERTIFICATION_REFERENCE_COLLISION",
                    "durable Certification reference is divergent",
                )
            return
        self._replace(current, current.with_certification_reference(reference))

    def _validate_certifier(
        self,
        dossier: ParallelStoryDossier,
        story: UserStory,
        architect: ArchitectResult,
        result: CertifierResult,
        project: ProjectState,
    ) -> None:
        historical = replace(story, status=UserStoryStatus.CERTIFICATION)
        handoff = self._workflow.runtime_handoff(dossier, MissionRole.CERTIFIER)
        assignment = CertifierInput.from_integrated_handoff(
            handoff,
            historical,
            architect,
            dossier.implementer_result,
            cast(TesterResult, dossier.tester_result),
            cast(ReviewerResult, dossier.reviewer_result),
            tuple(project.evidence),
            tuple(project.gates),
            dossier.integrated_context,
        )
        if not self._certifier.validate(result, certifier_input=assignment).is_valid:
            raise MissionCertificationError(
                "CERTIFIER_RESULT_INVALID", "persisted CertifierResult no longer validates"
            )

    def _require_existing_certification(
        self,
        record: OrchestrationRecord,
        project: ProjectState,
        story: UserStory,
        commit: str,
        certification_id: str,
    ) -> None:
        certifications = tuple(
            item
            for item in project.certifications
            if item.certification_id == certification_id
            and item.subject == story.id
            and item.commit == commit
        )
        references = tuple(
            item
            for item in record.certification_references
            if item.user_story_id == story.id
            and item.workflow_generation == record.workflow_generation
        )
        if len(certifications) != 1 or len(references) != 1:
            raise MissionCertificationError(
                "CERTIFICATION_RECOVERY_REQUIRED",
                "CERTIFIED story lacks one exact durable Certification",
            )
        expected = CertificationReference(
            story.id,
            record.workflow_generation,
            certification_id,
            _fingerprint(to_dict(certifications[0])),
            commit,
        )
        if references[0] != expected:
            raise MissionCertificationError(
                "CERTIFICATION_REFERENCE_STALE",
                "Certification reference differs from ProjectState",
            )

    def _replace(
        self, current: OrchestrationRecord, candidate: OrchestrationRecord
    ) -> OrchestrationRecord:
        self._records.replace(candidate, expected_fingerprint=current.fingerprint)
        return candidate

    def _project(self) -> ProjectState:
        value = self._projects.load()
        if not isinstance(value, ProjectState):
            raise MissionCertificationError(
                "PROJECT_STATE_INVALID", "canonical ProjectState is required"
            )
        return value

    @staticmethod
    def _waiting(
        dossier: ParallelStoryDossier, status: MissionCertificationStatus
    ) -> MissionCertificationResult:
        return MissionCertificationResult(
            status,
            dossier.mission_id,
            dossier.workflow_generation,
            dossier.user_story_id,
            dossier.integration_commit,
            dossier,
        )

    @staticmethod
    def _non_positive(
        dossier: ParallelStoryDossier, *, certification_id: str | None = None
    ) -> MissionCertificationResult:
        status = (
            MissionCertificationStatus.REMEDIATION_REQUIRED
            if dossier.stage is ParallelStoryStage.REMEDIATION_REQUIRED
            else MissionCertificationStatus.BLOCKED
        )
        return MissionCertificationResult(
            status,
            dossier.mission_id,
            dossier.workflow_generation,
            dossier.user_story_id,
            dossier.integration_commit,
            dossier,
            certification_id,
            dossier.blockers,
        )


def _matches_reference(
    execution: CodexExecutionRecord, reference: RoleExecutionReference
) -> bool:
    return (
        execution.execution_id == reference.execution_id
        and execution.request_id == reference.request_id
        and execution.role is reference.role
        and execution.subject == reference.subject
        and execution.workflow_generation == reference.workflow_generation
        and execution.validated_result_fingerprint == reference.result_fingerprint
    )


def _story(project: ProjectState, identifier: str) -> UserStory:
    matches = tuple(item for item in project.user_stories if item.id == identifier)
    if len(matches) != 1:
        raise MissionCertificationError(
            "STORY_MISSING_OR_AMBIGUOUS", "story must resolve exactly once"
        )
    return matches[0]


def _certification_inputs(
    project: ProjectState, story: UserStory, commit: str
) -> tuple[tuple[AcceptanceResult, ...], CertificationContext]:
    acceptance: list[AcceptanceResult] = []
    for criterion in story.acceptance_criteria:
        evidence = tuple(
            item
            for item in project.evidence
            if item.evidence_type is EvidenceType.ACCEPTANCE_CRITERION_CHECK
            and item.subject == criterion.id
            and item.commit in {commit, None}
        )
        passed = len(evidence) == 1 and evidence[0].result is True
        acceptance.append(
            AcceptanceResult(
                criterion.id,
                GateResult.PASS if passed else GateResult.UNKNOWN,
                (evidence[0].evidence_id,) if len(evidence) == 1 else (),
            )
        )
    stale = frozenset(
        item.evidence_id
        for item in project.evidence
        if item.commit is not None and item.commit != commit
    )
    independent = frozenset(
        item.evidence_id for item in project.evidence if item.commit is None
    )
    human = story.human_approval.evidence_ref if story.human_approval.approved else None
    return tuple(acceptance), CertificationContext(
        stale_evidence_ids=stale,
        repository_independent_evidence_ids=independent,
        human_approval_evidence_id=human,
    )


def _certification_id(
    dossier: ParallelStoryDossier,
    record: OrchestrationRecord,
    project: ProjectState,
    architect: ArchitectResult,
    tester: TesterResult,
    reviewer: ReviewerResult,
    certifier: CertifierResult,
) -> str:
    story = _story(project, dossier.user_story_id)
    gates = tuple(item for item in project.gates if item.subject == story.id)
    referenced_evidence = {
        evidence_id for gate in gates for evidence_id in gate.evidence_refs
    }
    referenced_evidence.update(
        item.evidence_id
        for item in project.evidence
        if item.subject in {criterion.id for criterion in story.acceptance_criteria}
        or item.evidence_id == story.human_approval.evidence_ref
    )
    payload = {
        "mission_id": dossier.mission_id,
        "workflow_generation": dossier.workflow_generation,
        "user_story_id": dossier.user_story_id,
        "integration_commit": dossier.integration_commit,
        "architect": role_result_fingerprint(architect),
        "implementer": role_result_fingerprint(dossier.implementer_result),
        "tester": role_result_fingerprint(tester),
        "reviewer": role_result_fingerprint(reviewer),
        "certifier": role_result_fingerprint(certifier),
        "evidence": [
            to_dict(item)
            for item in project.evidence
            if item.evidence_id in referenced_evidence
        ],
        "gates": [to_dict(item) for item in gates],
        "role_references": [
            {
                "role": item.role.value,
                "subject": item.subject,
                "generation": item.workflow_generation,
                "request_id": item.request_id,
                "execution_id": item.execution_id,
                "result_fingerprint": item.result_fingerprint,
            }
            for item in record.execution_references
            if item.workflow_generation == dossier.workflow_generation
            and item.subject in {dossier.user_story_id, dossier.integrated_context.architect_subject}
        ],
    }
    return f"CERT-{_fingerprint(payload)[:24]}"


def _fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
