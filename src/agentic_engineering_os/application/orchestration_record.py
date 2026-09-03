"""Non-authoritative durable references for mission composition."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace

from agentic_engineering_os.domain import MissionRole

from .mission_admission import MissionRequest


ORCHESTRATION_RECORD_VERSION = "1.2"
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class RoleExecutionReference:
    role: MissionRole
    subject: str
    workflow_generation: int
    request_id: str
    execution_id: str
    result_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, MissionRole) or self.role is MissionRole.ORCHESTRATOR:
            raise ValueError("reference role is invalid")
        if not all(isinstance(item, str) and item.strip() for item in (self.subject, self.request_id, self.execution_id)):
            raise ValueError("reference identities are required")
        if not isinstance(self.workflow_generation, int) or isinstance(self.workflow_generation, bool) or self.workflow_generation < 0:
            raise ValueError("reference generation is invalid")
        if not _SHA256.fullmatch(self.result_fingerprint):
            raise ValueError("result fingerprint must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class ParallelIntegrationReference:
    plan_fingerprint: str
    wave_index: int
    group_index: int
    assignment_ids: tuple[str, ...]
    gate_fingerprint: str | None = None
    integrated_commit: str | None = None

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.plan_fingerprint):
            raise ValueError("parallel plan fingerprint must be lowercase SHA-256")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (self.wave_index, self.group_index)
        ):
            raise ValueError("parallel wave/group identity is invalid")
        if (
            not self.assignment_ids
            or len(set(self.assignment_ids)) != len(self.assignment_ids)
            or any(not isinstance(item, str) or not item.strip() for item in self.assignment_ids)
        ):
            raise ValueError("parallel assignment references are invalid")
        if self.gate_fingerprint is not None and not _SHA256.fullmatch(self.gate_fingerprint):
            raise ValueError("gate fingerprint must be lowercase SHA-256")
        if self.integrated_commit is not None and not _SHA40.fullmatch(self.integrated_commit):
            raise ValueError("integrated commit must be lowercase SHA-1")
        if self.integrated_commit is not None and self.gate_fingerprint is None:
            raise ValueError("integrated commit requires a Gate reference")


@dataclass(frozen=True, slots=True)
class CertificationReference:
    user_story_id: str
    workflow_generation: int
    certification_id: str
    certification_fingerprint: str
    commit: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.user_story_id, self.certification_id)
        ):
            raise ValueError("Certification reference identities are required")
        if (
            not isinstance(self.workflow_generation, int)
            or isinstance(self.workflow_generation, bool)
            or self.workflow_generation < 0
        ):
            raise ValueError("Certification reference generation is invalid")
        if not _SHA256.fullmatch(self.certification_fingerprint):
            raise ValueError("Certification fingerprint must be lowercase SHA-256")
        if not _SHA40.fullmatch(self.commit):
            raise ValueError("Certification commit must be lowercase SHA-1")


@dataclass(frozen=True, slots=True)
class OrchestrationRecord:
    schema_version: str
    mission_id: str
    request: MissionRequest
    request_fingerprint: str
    baseline_commit: str
    workflow_generation: int
    plan_fingerprint: str | None = None
    execution_references: tuple[RoleExecutionReference, ...] = ()
    user_story_ids: tuple[str, ...] = ()
    parallel_integration: ParallelIntegrationReference | None = None
    certification_references: tuple[CertificationReference, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != ORCHESTRATION_RECORD_VERSION:
            raise ValueError("unsupported orchestration record version")
        if not self.mission_id.strip() or self.request_fingerprint != request_fingerprint(self.request):
            raise ValueError("orchestration request binding is invalid")
        if not _SHA40.fullmatch(self.baseline_commit):
            raise ValueError("baseline_commit must be lowercase SHA-1")
        if not isinstance(self.workflow_generation, int) or isinstance(self.workflow_generation, bool) or self.workflow_generation < 0:
            raise ValueError("workflow_generation is invalid")
        if self.plan_fingerprint is not None and not _SHA256.fullmatch(self.plan_fingerprint):
            raise ValueError("plan_fingerprint must be lowercase SHA-256")
        keys = tuple((item.role.value, item.subject, item.workflow_generation) for item in self.execution_references)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("execution references must be unique and canonical")
        if self.user_story_ids != tuple(sorted(set(self.user_story_ids))):
            raise ValueError("user_story_ids must be unique and canonical")
        planned = self.plan_fingerprint is not None
        architect_references = tuple(
            item for item in self.execution_references if item.role is MissionRole.ARCHITECT
        )
        if planned != bool(self.user_story_ids) or planned != bool(architect_references):
            raise ValueError(
                "planning fingerprint, User Story references, and Architect reference must agree"
            )
        if self.parallel_integration is not None:
            implementer_subjects = tuple(
                item.subject
                for item in self.execution_references
                if item.role is MissionRole.IMPLEMENTER
                and item.workflow_generation == self.workflow_generation
            )
            if not planned or not set(implementer_subjects).issubset(set(self.user_story_ids)):
                raise ValueError("parallel integration references differ from planning")
        certification_keys = tuple(
            (item.user_story_id, item.workflow_generation)
            for item in self.certification_references
        )
        if certification_keys != tuple(sorted(set(certification_keys))):
            raise ValueError("Certification references must be unique and canonical")
        if any(
            item.user_story_id not in self.user_story_ids
            or item.workflow_generation != self.workflow_generation
            for item in self.certification_references
        ):
            raise ValueError("Certification references differ from active planning")

    @property
    def fingerprint(self) -> str:
        return _fingerprint(record_to_data(self))

    def with_reference(
        self,
        reference: RoleExecutionReference,
        *,
        plan_fingerprint: str | None = None,
        user_story_ids: tuple[str, ...] | None = None,
    ) -> OrchestrationRecord:
        retained = tuple(
            item
            for item in self.execution_references
            if (item.role, item.subject, item.workflow_generation)
            != (reference.role, reference.subject, reference.workflow_generation)
        )
        return replace(
            self,
            plan_fingerprint=plan_fingerprint or self.plan_fingerprint,
            execution_references=tuple(
                sorted((*retained, reference), key=lambda item: (item.role.value, item.subject, item.workflow_generation))
            ),
            user_story_ids=(
                self.user_story_ids if user_story_ids is None else user_story_ids
            ),
        )

    def with_parallel_integration(
        self, reference: ParallelIntegrationReference
    ) -> OrchestrationRecord:
        if not isinstance(reference, ParallelIntegrationReference):
            raise TypeError("canonical parallel integration reference is required")
        return replace(self, parallel_integration=reference)

    def with_certification_reference(
        self, reference: CertificationReference
    ) -> OrchestrationRecord:
        if not isinstance(reference, CertificationReference):
            raise TypeError("canonical Certification reference is required")
        retained = tuple(
            item
            for item in self.certification_references
            if (item.user_story_id, item.workflow_generation)
            != (reference.user_story_id, reference.workflow_generation)
        )
        return replace(
            self,
            certification_references=tuple(
                sorted(
                    (*retained, reference),
                    key=lambda item: (
                        item.user_story_id,
                        item.workflow_generation,
                    ),
                )
            ),
        )


def request_fingerprint(request: MissionRequest) -> str:
    import os
    from pathlib import Path

    return _fingerprint(
        {
            "objective": request.objective,
            "repository_root": os.path.normcase(str(Path(request.repository_root).resolve(strict=False))).casefold(),
            "requested_scope": list(request.requested_scope),
            "verification_command_ids": list(request.verification_command_ids),
        }
    )


def record_to_data(record: OrchestrationRecord) -> dict[str, object]:
    return {
        "schema_version": record.schema_version,
        "mission_id": record.mission_id,
        "request": {
            "objective": record.request.objective,
            "repository_root": record.request.repository_root,
            "requested_scope": list(record.request.requested_scope),
            "verification_command_ids": list(record.request.verification_command_ids),
        },
        "request_fingerprint": record.request_fingerprint,
        "baseline_commit": record.baseline_commit,
        "workflow_generation": record.workflow_generation,
        "plan_fingerprint": record.plan_fingerprint,
        "execution_references": [
            {
                "role": item.role.value,
                "subject": item.subject,
                "workflow_generation": item.workflow_generation,
                "request_id": item.request_id,
                "execution_id": item.execution_id,
                "result_fingerprint": item.result_fingerprint,
            }
            for item in record.execution_references
        ],
        "user_story_ids": list(record.user_story_ids),
        "parallel_integration": (
            None
            if record.parallel_integration is None
            else {
                "plan_fingerprint": record.parallel_integration.plan_fingerprint,
                "wave_index": record.parallel_integration.wave_index,
                "group_index": record.parallel_integration.group_index,
                "assignment_ids": list(record.parallel_integration.assignment_ids),
                "gate_fingerprint": record.parallel_integration.gate_fingerprint,
                "integrated_commit": record.parallel_integration.integrated_commit,
            }
        ),
        "certification_references": [
            {
                "user_story_id": item.user_story_id,
                "workflow_generation": item.workflow_generation,
                "certification_id": item.certification_id,
                "certification_fingerprint": item.certification_fingerprint,
                "commit": item.commit,
            }
            for item in record.certification_references
        ],
    }


def _fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
