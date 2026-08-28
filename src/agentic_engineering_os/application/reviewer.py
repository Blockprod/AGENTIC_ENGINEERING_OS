"""Independent engineering-quality review contracts and validation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import cast

from agentic_engineering_os.domain import (
    GateResult,
    MissionRole,
    OperatingStep,
    UserStory,
    UserStoryStatus,
    to_dict,
)

from ._identity import is_attributable_human_identity
from .contract_validator import ContractValidator, ValidationIssue, ValidationResult
from .implementer import (
    ImplementerInputError,
    ImplementerResult,
    ImplementerVerdict,
    _normalize_path,
    _scope_matches,
)
from .orchestrator import RoleHandoff
from .tester import (
    TestCaseType,
    TesterResult,
    TesterVerdict,
    _require_implementer_result,
)


_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")


class ReviewDimension(str, Enum):
    SCOPE = "SCOPE"
    ARCHITECTURE = "ARCHITECTURE"
    MAINTAINABILITY = "MAINTAINABILITY"
    COMPLEXITY = "COMPLEXITY"
    DUPLICATION = "DUPLICATION"
    TEST_QUALITY = "TEST_QUALITY"
    CONTRACT_COMPLIANCE = "CONTRACT_COMPLIANCE"
    AUTHORITY_SAFETY = "AUTHORITY_SAFETY"


class ReviewSeverity(str, Enum):
    INFO = "INFO"
    MINOR = "MINOR"
    MAJOR = "MAJOR"
    CRITICAL = "CRITICAL"


class ReviewerVerdict(str, Enum):
    READY_FOR_CERTIFICATION = "READY_FOR_CERTIFICATION"
    REMEDIATION_REQUIRED = "REMEDIATION_REQUIRED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    id: str
    dimension: ReviewDimension
    severity: ReviewSeverity
    summary: str
    evidence: tuple[str, ...]
    affected_paths: tuple[str, ...]
    blocking: bool


@dataclass(frozen=True, slots=True)
class ReviewerInput:
    """Immutable review assignment derived from all required prior artifacts."""

    mission_id: str
    workflow_generation: int
    user_story: UserStory
    implementer_result: ImplementerResult
    tester_result: TesterResult
    observed_commit: str
    objective: str
    blockers: tuple[str, ...]
    instructions: str
    _assignment_snapshot: str = field(default="", init=False, repr=False, compare=False)

    @classmethod
    def from_handoff(
        cls,
        handoff: RoleHandoff,
        user_story: UserStory,
        implementer_result: ImplementerResult,
        tester_result: TesterResult,
        *,
        validator: ContractValidator | None = None,
    ) -> ReviewerInput:
        resolved_validator = validator if validator is not None else ContractValidator()
        _require_reviewer_handoff(handoff)
        _require_reviewable_story(user_story, resolved_validator)
        if handoff.subject != user_story.id:
            raise ReviewerInputError("handoff subject must identify the UserStory")
        if handoff.blockers:
            raise ReviewerInputError("handoff with active blockers is not assignable")
        try:
            _require_implementer_result(
                implementer_result,
                handoff=handoff,
                user_story=user_story,
                validator=resolved_validator,
            )
        except ValueError as error:
            raise ReviewerInputError(str(error)) from error
        _require_tester_result(
            tester_result,
            handoff=handoff,
            user_story=user_story,
            validator=resolved_validator,
        )
        result = cls(
            mission_id=handoff.mission_id,
            workflow_generation=handoff.workflow_generation,
            user_story=deepcopy(user_story),
            implementer_result=deepcopy(implementer_result),
            tester_result=deepcopy(tester_result),
            observed_commit=handoff.observed_commit,
            objective=handoff.objective,
            blockers=tuple(handoff.blockers),
            instructions=handoff.instructions,
        )
        object.__setattr__(result, "_assignment_snapshot", _input_snapshot(result))
        return result


@dataclass(frozen=True, slots=True)
class ReviewerResult:
    """Structured quality review without mutation or Certification authority."""

    mission_id: str
    workflow_generation: int
    role: MissionRole = field(default=MissionRole.REVIEWER, init=False)
    subject: str
    user_story_id: str
    observed_commit: str
    summary: str
    dimensions_reviewed: tuple[ReviewDimension, ...]
    reviewed_paths: tuple[str, ...]
    findings: tuple[ReviewFinding, ...]
    blockers: tuple[str, ...]
    recommended_next_role: MissionRole
    verdict: ReviewerVerdict


class ReviewerInputError(ValueError):
    """The supplied context cannot safely authorize an engineering review."""


class ReviewerResultValidator:
    """Validate review structure and consistency, not engineering quality itself."""

    def __init__(self, validator: ContractValidator | None = None) -> None:
        self._validator = validator if validator is not None else ContractValidator()

    def validate(
        self,
        candidate: ReviewerResult | Mapping[str, object],
        *,
        reviewer_input: ReviewerInput,
    ) -> ValidationResult:
        if not isinstance(reviewer_input, ReviewerInput):
            return _invalid("INVALID_VALIDATION_CONTEXT", "reviewer_input is required")
        if (
            not reviewer_input._assignment_snapshot
            or reviewer_input._assignment_snapshot != _input_snapshot(reviewer_input)
        ):
            return _invalid(
                "REVIEWER_INPUT_TAMPERED",
                "ReviewerInput differs from its authorized assignment snapshot",
            )
        try:
            _require_reviewable_story(reviewer_input.user_story, self._validator)
        except ReviewerInputError as error:
            return _invalid("INVALID_REVIEWER_INPUT", str(error))

        if isinstance(candidate, ReviewerResult):
            serialized = cast(dict[str, object], to_dict(candidate))
        elif isinstance(candidate, Mapping):
            serialized = dict(candidate)
        else:
            return _invalid(
                "INVALID_REVIEWER_OUTPUT",
                "candidate must be a ReviewerResult or mapping",
            )
        schema_result = self._validator.validate("reviewer-result", serialized)
        if not schema_result.is_valid:
            return schema_result

        issues: list[ValidationIssue] = []
        _validate_context(serialized, reviewer_input, issues)
        reviewed_paths = _validate_reviewed_paths(
            cast(list[str], serialized["reviewed_paths"]), issues
        )
        findings = cast(list[Mapping[str, object]], serialized["findings"])
        finding_paths = _validate_findings(findings, set(reviewed_paths), issues)
        _validate_review_coverage(
            serialized,
            reviewer_input,
            set(reviewed_paths),
            finding_paths,
            findings,
            issues,
        )
        return ValidationResult(contract="reviewer-result", errors=tuple(issues))


def _require_reviewer_handoff(handoff: RoleHandoff) -> None:
    if not isinstance(handoff, RoleHandoff):
        raise ReviewerInputError("input must be an explicit RoleHandoff")
    if handoff.from_role is not MissionRole.ORCHESTRATOR:
        raise ReviewerInputError("RoleHandoff must originate from ORCHESTRATOR")
    if handoff.to_role is not MissionRole.REVIEWER:
        raise ReviewerInputError("RoleHandoff must target REVIEWER")
    if handoff.operating_step is not OperatingStep.REPORT:
        raise ReviewerInputError("Reviewer handoff must target REPORT")
    values = (
        handoff.mission_id,
        handoff.subject,
        handoff.objective,
        handoff.instructions,
    )
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ReviewerInputError("handoff text fields must be non-empty")
    if not _COMMIT_PATTERN.fullmatch(handoff.observed_commit):
        raise ReviewerInputError("observed_commit must be a full Git SHA")
    if (
        not isinstance(handoff.workflow_generation, int)
        or isinstance(handoff.workflow_generation, bool)
        or handoff.workflow_generation < 0
    ):
        raise ReviewerInputError("workflow_generation must be a non-negative integer")
    if not isinstance(handoff.blockers, tuple) or not all(
        isinstance(blocker, str) and blocker.strip() for blocker in handoff.blockers
    ):
        raise ReviewerInputError("blockers must be explicit non-empty strings")


def _require_reviewable_story(story: UserStory, validator: ContractValidator) -> None:
    if not isinstance(story, UserStory):
        raise ReviewerInputError("assignment must contain a UserStory")
    if not validator.validate("user-story", to_dict(story)).is_valid:
        raise ReviewerInputError("UserStory does not satisfy its canonical contract")
    if story.status is not UserStoryStatus.REVIEW:
        raise ReviewerInputError("UserStory must be REVIEW for Reviewer activity")
    approval = story.human_approval
    if approval.required and (
        not approval.approved
        or not is_attributable_human_identity(approval.approved_by)
        or approval.approved_at is None
    ):
        raise ReviewerInputError("required Human approval is not satisfied")


def _require_tester_result(
    result: TesterResult,
    *,
    handoff: RoleHandoff,
    user_story: UserStory,
    validator: ContractValidator,
) -> None:
    if not isinstance(result, TesterResult):
        raise ReviewerInputError("input must contain a TesterResult")
    serialized = cast(dict[str, object], to_dict(result))
    if not validator.validate("tester-result", serialized).is_valid:
        raise ReviewerInputError("TesterResult does not satisfy its schema")
    if result.verdict is not TesterVerdict.READY_FOR_REVIEW:
        raise ReviewerInputError("TesterResult must be READY_FOR_REVIEW")
    if (
        result.mission_id != handoff.mission_id
        or result.workflow_generation != handoff.workflow_generation
        or result.subject != user_story.id
        or result.user_story_id != user_story.id
        or result.observed_commit.casefold() != handoff.observed_commit.casefold()
    ):
        raise ReviewerInputError("TesterResult is incoherent with Reviewer context")
    mandatory = {
        criterion.id for criterion in user_story.acceptance_criteria if criterion.mandatory
    }
    acceptance = {
        item.acceptance_criterion_id: item.result for item in result.acceptance_results
    }
    acceptance_ids = [
        item.acceptance_criterion_id for item in result.acceptance_results
    ]
    if len(acceptance_ids) != len(set(acceptance_ids)):
        raise ReviewerInputError("TesterResult duplicates an Acceptance result")
    if any(acceptance.get(identifier) is not GateResult.PASS for identifier in mandatory):
        raise ReviewerInputError("TesterResult does not prove every mandatory criterion")
    if not mandatory.issubset(set(result.test_plan.acceptance_criteria)):
        raise ReviewerInputError("TesterResult does not target every mandatory criterion")
    test_ids = [item.id for item in result.test_cases]
    if len(test_ids) != len(set(test_ids)):
        raise ReviewerInputError("TesterResult duplicates a test case id")
    if any(item.required and item.verdict is not GateResult.PASS for item in result.test_cases):
        raise ReviewerInputError("TesterResult contains a required test not at PASS")
    if any(
        item.required and (not item.executed or item.result is not GateResult.PASS)
        for item in result.verification_results
    ):
        raise ReviewerInputError("TesterResult contains unproven required verification")
    if {item.type for item in result.test_cases} != set(TestCaseType):
        raise ReviewerInputError("TesterResult lacks required adversarial coverage")
    commands = [command.strip() for command in result.verification_commands]
    planned_commands = [command.strip() for command in result.test_plan.commands]
    observed_commands = [item.command.strip() for item in result.verification_results]
    if (
        not all((*commands, *planned_commands, *observed_commands))
        or len(commands) != len(set(commands))
        or len(observed_commands) != len(set(observed_commands))
        or set(commands) != set(planned_commands)
        or set(commands) != set(observed_commands)
    ):
        raise ReviewerInputError("TesterResult verification commands are incoherent")
    for path in result.test_files_changed:
        try:
            canonical = _normalize_path(path, allow_directory=False)
        except ImplementerInputError as error:
            raise ReviewerInputError(str(error)) from error
        if "tests" not in canonical.split("/")[:-1]:
            raise ReviewerInputError("TesterResult declares a production file change")
        if any(_scope_matches(canonical, item) for item in user_story.scope.forbidden_paths):
            raise ReviewerInputError("TesterResult declares a forbidden test path")
        if not any(_scope_matches(canonical, item) for item in user_story.scope.allowed_paths):
            raise ReviewerInputError("TesterResult declares an out-of-scope test path")


def _validate_context(
    serialized: Mapping[str, object],
    reviewer_input: ReviewerInput,
    issues: list[ValidationIssue],
) -> None:
    expected = {
        "mission_id": reviewer_input.mission_id,
        "workflow_generation": reviewer_input.workflow_generation,
        "subject": reviewer_input.user_story.id,
        "user_story_id": reviewer_input.user_story.id,
        "observed_commit": reviewer_input.observed_commit.casefold(),
    }
    actual = {
        "mission_id": serialized["mission_id"],
        "workflow_generation": serialized["workflow_generation"],
        "subject": serialized["subject"],
        "user_story_id": serialized["user_story_id"],
        "observed_commit": cast(str, serialized["observed_commit"]).casefold(),
    }
    for field_name, value in expected.items():
        if actual[field_name] != value:
            issues.append(
                ValidationIssue(
                    "REVIEWER_CONTEXT_MISMATCH",
                    (field_name,),
                    f"{field_name} differs from ReviewerInput",
                )
            )


def _validate_reviewed_paths(
    paths: list[str], issues: list[ValidationIssue]
) -> list[str]:
    normalized: list[str] = []
    for index, path in enumerate(paths):
        try:
            canonical = _normalize_path(path, allow_directory=False)
        except ImplementerInputError as error:
            issues.append(
                ValidationIssue(
                    "UNSAFE_REPOSITORY_PATH",
                    ("reviewed_paths", index),
                    str(error),
                )
            )
            continue
        if canonical in normalized:
            issues.append(
                ValidationIssue(
                    "DUPLICATE_REPOSITORY_PATH",
                    ("reviewed_paths", index),
                    "repository path is duplicated after normalization",
                )
            )
        normalized.append(canonical)
    return normalized


def _validate_findings(
    findings: list[Mapping[str, object]],
    reviewed_paths: set[str],
    issues: list[ValidationIssue],
) -> set[str]:
    identifiers: set[str] = set()
    all_paths: set[str] = set()
    for index, finding in enumerate(findings):
        identifier = cast(str, finding["id"])
        if identifier in identifiers:
            issues.append(
                ValidationIssue(
                    "DUPLICATE_REVIEW_FINDING_ID",
                    ("findings", index, "id"),
                    f"duplicate ReviewFinding id: {identifier}",
                )
            )
        identifiers.add(identifier)
        severity = cast(str, finding["severity"])
        blocking = cast(bool, finding["blocking"])
        if severity == ReviewSeverity.CRITICAL.value and not blocking:
            issues.append(
                ValidationIssue(
                    "CRITICAL_FINDING_MUST_BLOCK",
                    ("findings", index, "blocking"),
                    "a CRITICAL finding must be blocking in V1",
                )
            )
        if severity == ReviewSeverity.INFO.value and blocking:
            issues.append(
                ValidationIssue(
                    "INFO_FINDING_CANNOT_BLOCK",
                    ("findings", index, "blocking"),
                    "an INFO finding cannot be blocking in V1",
                )
            )
        finding_seen_paths: set[str] = set()
        for path_index, path in enumerate(cast(list[str], finding["affected_paths"])):
            try:
                canonical = _normalize_path(path, allow_directory=False)
            except ImplementerInputError as error:
                issues.append(
                    ValidationIssue(
                        "UNSAFE_REPOSITORY_PATH",
                        ("findings", index, "affected_paths", path_index),
                        str(error),
                    )
                )
                continue
            if canonical in finding_seen_paths:
                issues.append(
                    ValidationIssue(
                        "DUPLICATE_REPOSITORY_PATH",
                        ("findings", index, "affected_paths", path_index),
                        "affected path is duplicated after normalization",
                    )
                )
            finding_seen_paths.add(canonical)
            all_paths.add(canonical)
            if canonical not in reviewed_paths:
                issues.append(
                    ValidationIssue(
                        "FINDING_PATH_NOT_REVIEWED",
                        ("findings", index, "affected_paths", path_index),
                        "finding path must be declared in reviewed_paths",
                    )
                )
    return all_paths


def _validate_review_coverage(
    serialized: Mapping[str, object],
    reviewer_input: ReviewerInput,
    reviewed_paths: set[str],
    finding_paths: set[str],
    findings: list[Mapping[str, object]],
    issues: list[ValidationIssue],
) -> None:
    artifact_paths = {
        _normalize_path(path, allow_directory=False)
        for path in (
            *reviewer_input.implementer_result.files_changed,
            *reviewer_input.tester_result.test_files_changed,
        )
    }
    story = reviewer_input.user_story
    for path in reviewed_paths - artifact_paths:
        in_scope = any(_scope_matches(path, item) for item in story.scope.allowed_paths)
        if not in_scope and path not in finding_paths:
            issues.append(
                ValidationIssue(
                    "UNRELATED_REVIEW_PATH",
                    ("reviewed_paths",),
                    f"out-of-scope path has no attributable finding: {path}",
                )
            )

    dimensions = set(cast(list[str], serialized["dimensions_reviewed"]))
    blocking_findings = [finding for finding in findings if finding["blocking"] is True]
    verdict = serialized["verdict"]
    if verdict == ReviewerVerdict.READY_FOR_CERTIFICATION.value:
        for path in sorted(artifact_paths - reviewed_paths):
            issues.append(
                ValidationIssue(
                    "IMPLEMENTATION_PATH_NOT_REVIEWED",
                    ("reviewed_paths",),
                    f"implementation artifact was not reviewed: {path}",
                )
            )
        required_dimensions = {dimension.value for dimension in ReviewDimension}
        if dimensions != required_dimensions:
            issues.append(
                ValidationIssue(
                    "REQUIRED_REVIEW_DIMENSION_MISSING",
                    ("dimensions_reviewed",),
                    "all eight V1 dimensions are required",
                )
            )
        if blocking_findings:
            issues.append(
                ValidationIssue(
                    "BLOCKING_FINDING_FORBIDS_READY",
                    ("verdict",),
                    "a blocking finding forbids READY_FOR_CERTIFICATION",
                )
            )
    elif verdict == ReviewerVerdict.REMEDIATION_REQUIRED.value:
        if not blocking_findings:
            issues.append(
                ValidationIssue(
                    "REMEDIATION_WITHOUT_BLOCKING_FINDING",
                    ("verdict",),
                    "remediation requires at least one blocking finding",
                )
            )


def _input_snapshot(value: ReviewerInput) -> str:
    return json.dumps(
        {
            "mission_id": value.mission_id,
            "workflow_generation": value.workflow_generation,
            "user_story": to_dict(value.user_story),
            "implementer_result": to_dict(value.implementer_result),
            "tester_result": to_dict(value.tester_result),
            "observed_commit": value.observed_commit,
            "objective": value.objective,
            "blockers": list(value.blockers),
            "instructions": value.instructions,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _invalid(code: str, message: str) -> ValidationResult:
    return ValidationResult(
        contract="reviewer-result",
        errors=(ValidationIssue(code=code, path=(), message=message),),
    )
