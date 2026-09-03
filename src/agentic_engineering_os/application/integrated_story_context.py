"""Non-authoritative bindings between historical role results and an integration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentic_engineering_os.domain import (
    MissionRole,
    WorktreeAssignment,
    WorktreeStatus,
    to_dict,
)

from .execution_state import (
    CodexExecutionRecord,
    CodexExecutionStatus,
    canonical_result_json,
    result_json_fingerprint,
)
from .implementer import ImplementerResult
from .integration_gate import IntegrationGateResult, integration_gate_fingerprint

if TYPE_CHECKING:
    from .orchestration_record import OrchestrationRecord


_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class IntegratedStoryContextError(ValueError):
    """The integration cannot be attributed to the supplied durable authorities."""


@dataclass(frozen=True, slots=True)
class IntegratedStoryContext:
    """Immutable provenance links; never a role verdict or control decision."""

    mission_id: str
    workflow_generation: int
    user_story_id: str
    assignment_id: str
    architect_subject: str
    architect_baseline_commit: str
    architect_result_fingerprint: str
    implementer_execution_id: str
    implementer_result_fingerprint: str
    worktree_baseline_commit: str
    implementation_commit: str
    integration_gate_fingerprint: str
    integrated_commit: str

    def __post_init__(self) -> None:
        identities = (
            self.mission_id,
            self.user_story_id,
            self.assignment_id,
            self.architect_subject,
            self.implementer_execution_id,
        )
        if not all(isinstance(value, str) and value.strip() for value in identities):
            raise IntegratedStoryContextError("integration identities are required")
        if (
            not isinstance(self.workflow_generation, int)
            or isinstance(self.workflow_generation, bool)
            or self.workflow_generation < 0
        ):
            raise IntegratedStoryContextError("workflow generation is invalid")
        if not all(
            _SHA40.fullmatch(value)
            for value in (
                self.architect_baseline_commit,
                self.worktree_baseline_commit,
                self.implementation_commit,
                self.integrated_commit,
            )
        ):
            raise IntegratedStoryContextError("integration commits must be lowercase Git SHAs")
        if not all(
            _SHA256.fullmatch(value)
            for value in (
                self.architect_result_fingerprint,
                self.implementer_result_fingerprint,
                self.integration_gate_fingerprint,
            )
        ):
            raise IntegratedStoryContextError("integration fingerprints must be lowercase SHA-256")

    @classmethod
    def reconstruct(
        cls,
        *,
        orchestration: OrchestrationRecord,
        assignment: WorktreeAssignment,
        execution: CodexExecutionRecord,
        implementer_result: ImplementerResult,
        gate_result: IntegrationGateResult,
        primary_commit: str,
        primary_clean: bool,
    ) -> IntegratedStoryContext:
        """Rebuild exact provenance from persistent references and observed Git."""

        from .orchestration_record import OrchestrationRecord

        if not isinstance(orchestration, OrchestrationRecord):
            raise IntegratedStoryContextError("canonical OrchestrationRecord is required")
        if not isinstance(assignment, WorktreeAssignment):
            raise IntegratedStoryContextError("canonical WorktreeAssignment is required")
        if not isinstance(execution, CodexExecutionRecord):
            raise IntegratedStoryContextError("canonical execution ledger record is required")
        if not isinstance(implementer_result, ImplementerResult):
            raise IntegratedStoryContextError("canonical ImplementerResult is required")
        if not isinstance(gate_result, IntegrationGateResult):
            raise IntegratedStoryContextError("canonical IntegrationGateResult is required")
        progress = orchestration.parallel_integration
        if progress is None or progress.integrated_commit is None or progress.gate_fingerprint is None:
            raise IntegratedStoryContextError("durable integrated reference is incomplete")
        architect = tuple(
            item
            for item in orchestration.execution_references
            if item.role is MissionRole.ARCHITECT
            and item.workflow_generation == orchestration.workflow_generation
        )
        implementer = tuple(
            item
            for item in orchestration.execution_references
            if item.role is MissionRole.IMPLEMENTER
            and item.subject == assignment.user_story_id
            and item.workflow_generation == orchestration.workflow_generation
        )
        if len(architect) != 1 or len(implementer) != 1:
            raise IntegratedStoryContextError("role execution references do not resolve exactly once")
        implementation_reference = implementer[0]
        result_fingerprint = result_json_fingerprint(
            canonical_result_json(to_dict(implementer_result))
        )
        expected_gate_fingerprint = integration_gate_fingerprint(gate_result)
        bindings_match = (
            assignment.assignment_id in progress.assignment_ids
            and assignment.mission_id == orchestration.mission_id
            and assignment.workflow_generation == orchestration.workflow_generation
            and assignment.baseline_commit == execution.expected_commit
            and assignment.status is WorktreeStatus.COMPLETED
            and assignment.result_commit is not None
            and execution.status is CodexExecutionStatus.VALIDATED
            and execution.execution_id == implementation_reference.execution_id
            and execution.mission_id == orchestration.mission_id
            and execution.workflow_generation == orchestration.workflow_generation
            and execution.role is MissionRole.IMPLEMENTER
            and execution.subject == assignment.user_story_id
            and execution.validated_result_fingerprint == implementation_reference.result_fingerprint
            and result_fingerprint == implementation_reference.result_fingerprint
            and implementer_result.observed_commit.casefold() == assignment.baseline_commit
            and progress.gate_fingerprint == expected_gate_fingerprint
            and progress.integrated_commit == primary_commit.casefold()
            and primary_clean is True
        )
        if not bindings_match:
            raise IntegratedStoryContextError(
                "integration differs from orchestration, ledger, assignment, Gate, or observed Git"
            )
        return cls(
            mission_id=orchestration.mission_id,
            workflow_generation=orchestration.workflow_generation,
            user_story_id=assignment.user_story_id,
            assignment_id=assignment.assignment_id,
            architect_subject=architect[0].subject,
            architect_baseline_commit=orchestration.baseline_commit,
            architect_result_fingerprint=architect[0].result_fingerprint,
            implementer_execution_id=implementation_reference.execution_id,
            implementer_result_fingerprint=implementation_reference.result_fingerprint,
            worktree_baseline_commit=assignment.baseline_commit,
            implementation_commit=assignment.result_commit,
            integration_gate_fingerprint=expected_gate_fingerprint,
            integrated_commit=progress.integrated_commit,
        )


def role_result_fingerprint(result: object) -> str:
    """Fingerprint a canonical RoleResult using the execution-ledger encoding."""

    return result_json_fingerprint(canonical_result_json(to_dict(result)))
