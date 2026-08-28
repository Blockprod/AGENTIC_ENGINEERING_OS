"""Contracts and deterministic validation for the Codex Architect role."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import cast

from agentic_engineering_os.domain import (
    MissionRole,
    OperatingStep,
    UserStory,
    UserStoryStatus,
    to_dict,
)

from .contract_validator import (
    ContractValidator,
    ValidationIssue,
    ValidationResult,
)
from .orchestrator import RoleHandoff


_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
_USER_STORY_ID_PATTERN = re.compile(r"^US-[0-9]{4}$")


class ArchitectVerdict(str, Enum):
    READY = "READY"
    BLOCKED = "BLOCKED"


class ArchitectDecisionKind(str, Enum):
    ARCHITECTURAL = "ARCHITECTURAL"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"


@dataclass(frozen=True, slots=True)
class ArchitectDecision:
    kind: ArchitectDecisionKind
    description: str


@dataclass(frozen=True, slots=True)
class ArchitectInput:
    """Repository-local Architect context derived from an Orchestrator handoff."""

    mission_id: str
    objective: str
    subject: str
    observed_commit: str
    blockers: tuple[str, ...]
    instructions: str
    constraints: tuple[str, ...]

    @classmethod
    def from_handoff(
        cls,
        handoff: RoleHandoff,
        *,
        constraints: Iterable[str] = (),
    ) -> ArchitectInput:
        if not isinstance(handoff, RoleHandoff):
            raise ArchitectInputError("input must be an explicit RoleHandoff")
        if handoff.to_role is not MissionRole.ARCHITECT:
            raise ArchitectInputError("RoleHandoff must target ARCHITECT")
        if handoff.operating_step is not OperatingStep.UNDERSTAND_CONTRACT:
            raise ArchitectInputError(
                "Architect handoff must target UNDERSTAND_CONTRACT"
            )
        values = (
            handoff.mission_id,
            handoff.objective,
            handoff.subject,
            handoff.instructions,
        )
        if not all(isinstance(value, str) and value.strip() for value in values):
            raise ArchitectInputError("handoff text fields must be non-empty")
        if not _COMMIT_PATTERN.fullmatch(handoff.observed_commit):
            raise ArchitectInputError("observed_commit must be a full Git SHA")
        if not isinstance(handoff.blockers, tuple) or not all(
            isinstance(blocker, str) and blocker.strip()
            for blocker in handoff.blockers
        ):
            raise ArchitectInputError("blockers must be explicit non-empty strings")
        if isinstance(constraints, (str, bytes)):
            raise ArchitectInputError("constraints must be a collection of strings")
        resolved_constraints = tuple(constraints)
        if not all(
            isinstance(constraint, str) and constraint.strip()
            for constraint in resolved_constraints
        ):
            raise ArchitectInputError(
                "constraints must be explicit non-empty strings"
            )
        return cls(
            mission_id=handoff.mission_id,
            objective=handoff.objective,
            subject=handoff.subject,
            observed_commit=handoff.observed_commit,
            blockers=tuple(handoff.blockers),
            instructions=handoff.instructions,
            constraints=resolved_constraints,
        )


@dataclass(frozen=True, slots=True)
class ArchitectResult:
    """Structured Architect output; READY is not a Certification."""

    mission_id: str
    role: MissionRole = field(default=MissionRole.ARCHITECT, init=False)
    subject: str
    observed_commit: str
    summary: str
    assumptions: tuple[str, ...]
    decisions: tuple[ArchitectDecision, ...]
    risks: tuple[str, ...]
    blockers: tuple[str, ...]
    user_stories: tuple[UserStory, ...]
    recommended_next_role: MissionRole
    verdict: ArchitectVerdict


class ArchitectInputError(ValueError):
    """A handoff cannot safely be used as Architect input."""


class ArchitectResultValidator:
    """Validate output shape and User Story candidates without generating them."""

    def __init__(self, validator: ContractValidator | None = None) -> None:
        self._validator = validator if validator is not None else ContractValidator()

    def validate(
        self,
        candidate: ArchitectResult | Mapping[str, object],
        *,
        architect_input: ArchitectInput | None = None,
        known_user_story_ids: Iterable[str] = (),
    ) -> ValidationResult:
        if isinstance(candidate, ArchitectResult):
            serialized = cast(dict[str, object], to_dict(candidate))
        elif isinstance(candidate, Mapping):
            serialized = dict(candidate)
        else:
            return ValidationResult(
                contract="architect-result",
                errors=(
                    ValidationIssue(
                        code="INVALID_ARCHITECT_OUTPUT",
                        path=(),
                        message="candidate must be an ArchitectResult or mapping",
                    ),
                ),
            )

        result = self._validator.validate("architect-result", serialized)
        if not result.is_valid:
            return result

        issues: list[ValidationIssue] = []
        if architect_input is not None:
            if not isinstance(architect_input, ArchitectInput):
                issues.append(
                    ValidationIssue(
                        code="INVALID_VALIDATION_CONTEXT",
                        path=(),
                        message="architect_input must be an ArchitectInput",
                    )
                )
            else:
                expected = {
                    "mission_id": architect_input.mission_id,
                    "subject": architect_input.subject,
                    "observed_commit": architect_input.observed_commit.casefold(),
                }
                actual = {
                    "mission_id": serialized["mission_id"],
                    "subject": serialized["subject"],
                    "observed_commit": cast(
                        str, serialized["observed_commit"]
                    ).casefold(),
                }
                for field_name in expected:
                    if actual[field_name] != expected[field_name]:
                        issues.append(
                            ValidationIssue(
                                code="ARCHITECT_CONTEXT_MISMATCH",
                                path=(field_name,),
                                message=f"{field_name} differs from ArchitectInput",
                            )
                        )

        if isinstance(known_user_story_ids, (str, bytes)):
            return ValidationResult(
                contract="architect-result",
                errors=(
                    ValidationIssue(
                        code="INVALID_VALIDATION_CONTEXT",
                        path=(),
                        message="known User Story ids must be a collection",
                    ),
                ),
            )
        known_ids = tuple(known_user_story_ids)
        if not all(
            isinstance(identifier, str)
            and _USER_STORY_ID_PATTERN.fullmatch(identifier)
            for identifier in known_ids
        ):
            return ValidationResult(
                contract="architect-result",
                errors=(
                    ValidationIssue(
                        code="INVALID_VALIDATION_CONTEXT",
                        path=(),
                        message="known User Story ids must use the canonical format",
                    ),
                ),
            )

        stories = cast(list[Mapping[str, object]], serialized["user_stories"])
        for index, story in enumerate(stories):
            story_result = self._validator.validate("user-story", story)
            issues.extend(
                ValidationIssue(
                    code=issue.code,
                    path=("user_stories", index, *issue.path),
                    message=issue.message,
                )
                for issue in story_result.errors
            )
        if issues:
            return ValidationResult(contract="architect-result", errors=tuple(issues))

        story_ids = [cast(str, story["id"]) for story in stories]
        duplicate_ids = sorted(
            {identifier for identifier in story_ids if story_ids.count(identifier) > 1}
        )
        for identifier in duplicate_ids:
            issues.append(
                ValidationIssue(
                    code="DUPLICATE_USER_STORY_ID",
                    path=("user_stories",),
                    message=f"duplicate candidate User Story id: {identifier}",
                )
            )

        available_ids = set(story_ids) | set(known_ids)
        for index, story in enumerate(stories):
            if story["status"] != UserStoryStatus.PROPOSED.value:
                issues.append(
                    ValidationIssue(
                        code="INVALID_CANDIDATE_STATUS",
                        path=("user_stories", index, "status"),
                        message="Architect User Story candidates must be PROPOSED",
                    )
                )
            dependencies = cast(list[str], story["depends_on"])
            for dependency in dependencies:
                if dependency not in available_ids:
                    issues.append(
                        ValidationIssue(
                            code="UNRESOLVED_LOCAL_DEPENDENCY",
                            path=("user_stories", index, "depends_on"),
                            message=f"unresolved User Story dependency: {dependency}",
                        )
                    )
            approval = cast(Mapping[str, object], story["human_approval"])
            if (
                approval["approved"] is not False
                or approval["approved_by"] is not None
                or approval["approved_at"] is not None
                or approval["evidence_ref"] is not None
            ):
                issues.append(
                    ValidationIssue(
                        code="ARCHITECT_CANNOT_APPROVE_HUMAN",
                        path=("user_stories", index, "human_approval"),
                        message=(
                            "Architect may require Human approval but cannot grant it"
                        ),
                    )
                )

        return ValidationResult(contract="architect-result", errors=tuple(issues))
