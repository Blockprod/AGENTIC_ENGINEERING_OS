"""Non-authoritative durable references for mission composition."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace

from agentic_engineering_os.domain import MissionRole

from .mission_admission import MissionRequest


ORCHESTRATION_RECORD_VERSION = "1.0"
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
    }


def _fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
