"""Adversarial Tester role contracts and deterministic result validation."""

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
    _CONTROL_PATHS,
    _normalize_path,
    _scope_matches,
)
from .orchestrator import RoleHandoff


_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")


class TestCaseType(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    EDGE = "EDGE"
    REGRESSION = "REGRESSION"


class TesterVerdict(str, Enum):
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    REMEDIATION_REQUIRED = "REMEDIATION_REQUIRED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class TesterPlan:
    acceptance_criteria: tuple[str, ...]
    positive_tests: tuple[str, ...]
    negative_tests: tuple[str, ...]
    edge_cases: tuple[str, ...]
    regressions: tuple[str, ...]
    commands: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TesterAcceptanceResult:
    acceptance_criterion_id: str
    result: GateResult
    evidence: tuple[str, ...]
    notes: str


@dataclass(frozen=True, slots=True)
class TesterTestCase:
    id: str
    type: TestCaseType
    objective: str
    expected_result: str
    observed_result: str
    required: bool
    executed: bool
    verdict: GateResult


@dataclass(frozen=True, slots=True)
class TesterVerificationResult:
    command: str
    required: bool
    executed: bool
    result: GateResult
    exit_code: int | None
    details: str


@dataclass(frozen=True, slots=True)
class TesterInput:
    """Immutable testing assignment derived from an Orchestrator handoff."""

    mission_id: str
    workflow_generation: int
    user_story: UserStory
    implementer_result: ImplementerResult
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
        *,
        validator: ContractValidator | None = None,
    ) -> TesterInput:
        resolved_validator = validator if validator is not None else ContractValidator()
        _require_tester_handoff(handoff)
        _require_testable_story(user_story, resolved_validator)
        if handoff.subject != user_story.id:
            raise TesterInputError("handoff subject must identify the UserStory")
        if handoff.blockers:
            raise TesterInputError("handoff with active blockers is not assignable")
        _require_implementer_result(
            implementer_result,
            handoff=handoff,
            user_story=user_story,
            validator=resolved_validator,
        )
        result = cls(
            mission_id=handoff.mission_id,
            workflow_generation=handoff.workflow_generation,
            user_story=deepcopy(user_story),
            implementer_result=deepcopy(implementer_result),
            observed_commit=handoff.observed_commit,
            objective=handoff.objective,
            blockers=tuple(handoff.blockers),
            instructions=handoff.instructions,
        )
        object.__setattr__(result, "_assignment_snapshot", _input_snapshot(result))
        return result


@dataclass(frozen=True, slots=True)
class TesterResult:
    """Structured adversarial test report without Control Plane authority."""

    mission_id: str
    workflow_generation: int
    role: MissionRole = field(default=MissionRole.TESTER, init=False)
    subject: str
    user_story_id: str
    observed_commit: str
    summary: str
    test_plan: TesterPlan
    acceptance_results: tuple[TesterAcceptanceResult, ...]
    test_cases: tuple[TesterTestCase, ...]
    test_files_changed: tuple[str, ...]
    verification_commands: tuple[str, ...]
    verification_results: tuple[TesterVerificationResult, ...]
    findings: tuple[str, ...]
    blockers: tuple[str, ...]
    recommended_next_role: MissionRole
    verdict: TesterVerdict


class TesterInputError(ValueError):
    """The supplied context cannot safely authorize Tester activity."""


class TesterResultValidator:
    """Validate a Tester report without choosing or executing creative tests."""

    def __init__(self, validator: ContractValidator | None = None) -> None:
        self._validator = validator if validator is not None else ContractValidator()

    def validate(
        self,
        candidate: TesterResult | Mapping[str, object],
        *,
        tester_input: TesterInput,
    ) -> ValidationResult:
        if not isinstance(tester_input, TesterInput):
            return _invalid("INVALID_VALIDATION_CONTEXT", "tester_input is required")
        if (
            not tester_input._assignment_snapshot
            or tester_input._assignment_snapshot != _input_snapshot(tester_input)
        ):
            return _invalid(
                "TESTER_INPUT_TAMPERED",
                "TesterInput differs from its authorized assignment snapshot",
            )
        try:
            _require_testable_story(tester_input.user_story, self._validator)
        except TesterInputError as error:
            return _invalid("INVALID_TESTER_INPUT", str(error))

        if isinstance(candidate, TesterResult):
            serialized = cast(dict[str, object], to_dict(candidate))
        elif isinstance(candidate, Mapping):
            serialized = dict(candidate)
        else:
            return _invalid(
                "INVALID_TESTER_OUTPUT",
                "candidate must be a TesterResult or mapping",
            )

        schema_result = self._validator.validate("tester-result", serialized)
        if not schema_result.is_valid:
            return schema_result

        issues: list[ValidationIssue] = []
        _validate_context(serialized, tester_input, issues)
        story = tester_input.user_story
        criteria = {criterion.id: criterion for criterion in story.acceptance_criteria}
        plan = cast(Mapping[str, object], serialized["test_plan"])
        targeted = cast(list[str], plan["acceptance_criteria"])
        for index, criterion_id in enumerate(targeted):
            if criterion_id not in criteria:
                issues.append(
                    ValidationIssue(
                        code="UNKNOWN_ACCEPTANCE_CRITERION",
                        path=("test_plan", "acceptance_criteria", index),
                        message=f"unknown Acceptance Criterion: {criterion_id}",
                    )
                )

        acceptance_results = cast(
            list[Mapping[str, object]], serialized["acceptance_results"]
        )
        acceptance_by_id: dict[str, Mapping[str, object]] = {}
        for index, acceptance in enumerate(acceptance_results):
            identifier = cast(str, acceptance["acceptance_criterion_id"])
            if identifier not in criteria:
                issues.append(
                    ValidationIssue(
                        code="UNKNOWN_ACCEPTANCE_CRITERION",
                        path=("acceptance_results", index, "acceptance_criterion_id"),
                        message=f"unknown Acceptance Criterion: {identifier}",
                    )
                )
            if identifier not in targeted:
                issues.append(
                    ValidationIssue(
                        code="UNTARGETED_ACCEPTANCE_RESULT",
                        path=("acceptance_results", index),
                        message="Acceptance result must be declared in the test plan",
                    )
                )
            if identifier in acceptance_by_id:
                issues.append(
                    ValidationIssue(
                        code="DUPLICATE_ACCEPTANCE_RESULT",
                        path=("acceptance_results", index),
                        message=f"duplicate Acceptance result: {identifier}",
                    )
                )
            acceptance_by_id[identifier] = acceptance
            if acceptance["result"] in {GateResult.PASS.value, GateResult.FAIL.value} and not cast(
                list[str], acceptance["evidence"]
            ):
                issues.append(
                    ValidationIssue(
                        code="ACCEPTANCE_OBSERVATION_REQUIRED",
                        path=("acceptance_results", index, "evidence"),
                        message="PASS or FAIL requires an observable supporting result",
                    )
                )

        test_cases = cast(list[Mapping[str, object]], serialized["test_cases"])
        _validate_unique_test_ids(test_cases, issues)
        _validate_execution_claims(test_cases, issues)
        _validate_test_paths(
            cast(list[str], serialized["test_files_changed"]), story, issues
        )
        commands = cast(list[str], serialized["verification_commands"])
        records = cast(list[Mapping[str, object]], serialized["verification_results"])
        _validate_verification(
            commands,
            cast(list[str], plan["commands"]),
            records,
            issues,
        )
        _validate_verdict(
            serialized,
            criteria,
            acceptance_by_id,
            test_cases,
            records,
            targeted,
            issues,
        )
        return ValidationResult(contract="tester-result", errors=tuple(issues))


def _require_tester_handoff(handoff: RoleHandoff) -> None:
    if not isinstance(handoff, RoleHandoff):
        raise TesterInputError("input must be an explicit RoleHandoff")
    if handoff.from_role is not MissionRole.ORCHESTRATOR:
        raise TesterInputError("RoleHandoff must originate from ORCHESTRATOR")
    if handoff.to_role is not MissionRole.TESTER:
        raise TesterInputError("RoleHandoff must target TESTER")
    if handoff.operating_step is not OperatingStep.VERIFY:
        raise TesterInputError("Tester handoff must target VERIFY")
    values = (
        handoff.mission_id,
        handoff.subject,
        handoff.objective,
        handoff.instructions,
    )
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise TesterInputError("handoff text fields must be non-empty")
    if not _COMMIT_PATTERN.fullmatch(handoff.observed_commit):
        raise TesterInputError("observed_commit must be a full Git SHA")
    if (
        not isinstance(handoff.workflow_generation, int)
        or isinstance(handoff.workflow_generation, bool)
        or handoff.workflow_generation < 0
    ):
        raise TesterInputError("workflow_generation must be a non-negative integer")
    if not isinstance(handoff.blockers, tuple) or not all(
        isinstance(blocker, str) and blocker.strip() for blocker in handoff.blockers
    ):
        raise TesterInputError("blockers must be explicit non-empty strings")


def _require_testable_story(story: UserStory, validator: ContractValidator) -> None:
    if not isinstance(story, UserStory):
        raise TesterInputError("assignment must contain a UserStory")
    if not validator.validate("user-story", to_dict(story)).is_valid:
        raise TesterInputError("UserStory does not satisfy its canonical contract")
    if story.status is not UserStoryStatus.TESTING:
        raise TesterInputError("UserStory must be TESTING for Tester activity")
    approval = story.human_approval
    if approval.required and (
        not approval.approved
        or not is_attributable_human_identity(approval.approved_by)
        or approval.approved_at is None
    ):
        raise TesterInputError("required Human approval is not satisfied")
    if not story.scope.allowed_paths:
        raise TesterInputError("UserStory allowed_paths must be non-empty")
    for path in (*story.scope.allowed_paths, *story.scope.forbidden_paths):
        try:
            _normalize_path(path, allow_directory=True)
        except ImplementerInputError as error:
            raise TesterInputError(str(error)) from error


def _require_implementer_result(
    result: ImplementerResult,
    *,
    handoff: RoleHandoff,
    user_story: UserStory,
    validator: ContractValidator,
) -> None:
    if not isinstance(result, ImplementerResult):
        raise TesterInputError("input must contain an ImplementerResult")
    serialized = cast(dict[str, object], to_dict(result))
    if not validator.validate("implementer-result", serialized).is_valid:
        raise TesterInputError("ImplementerResult does not satisfy its schema")
    if result.verdict is not ImplementerVerdict.READY_FOR_TEST:
        raise TesterInputError("ImplementerResult must be READY_FOR_TEST")
    if (
        result.mission_id != handoff.mission_id
        or result.workflow_generation != handoff.workflow_generation
        or result.subject != user_story.id
        or result.user_story_id != user_story.id
        or result.observed_commit.casefold() != handoff.observed_commit.casefold()
    ):
        raise TesterInputError("ImplementerResult is incoherent with Tester context")
    normalized_files = _require_paths_in_story_scope(result.files_changed, user_story)
    normalized_tests = _require_paths_in_story_scope(
        result.tests_added_or_modified, user_story
    )
    if not set(normalized_tests).issubset(set(normalized_files)):
        raise TesterInputError("ImplementerResult test files must be changed files")
    commands = [command.strip() for command in result.verification_commands]
    result_commands = [item.command.strip() for item in result.verification_results]
    if (
        len(commands) != len(set(commands))
        or len(result_commands) != len(set(result_commands))
        or set(commands) != set(result_commands)
    ):
        raise TesterInputError("ImplementerResult command results are incoherent")
    if any(item.required and item.result.value != GateResult.PASS.value for item in result.verification_results):
        raise TesterInputError("ImplementerResult required verification is not PASS")


def _require_paths_in_story_scope(paths: tuple[str, ...], story: UserStory) -> list[str]:
    normalized: list[str] = []
    for path in paths:
        try:
            canonical = _normalize_path(path, allow_directory=False)
        except ImplementerInputError as error:
            raise TesterInputError(str(error)) from error
        if canonical in normalized:
            raise TesterInputError("repository path is duplicated after normalization")
        if any(_scope_matches(canonical, item) for item in _CONTROL_PATHS):
            raise TesterInputError("ImplementerResult modifies persistent control state")
        if any(_scope_matches(canonical, item) for item in story.scope.forbidden_paths):
            raise TesterInputError("ImplementerResult contains a forbidden path")
        if not any(_scope_matches(canonical, item) for item in story.scope.allowed_paths):
            raise TesterInputError("ImplementerResult contains an out-of-scope path")
        normalized.append(canonical)
    return normalized


def _validate_context(
    serialized: Mapping[str, object],
    tester_input: TesterInput,
    issues: list[ValidationIssue],
) -> None:
    expected = {
        "mission_id": tester_input.mission_id,
        "workflow_generation": tester_input.workflow_generation,
        "subject": tester_input.user_story.id,
        "user_story_id": tester_input.user_story.id,
        "observed_commit": tester_input.observed_commit.casefold(),
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
                    code="TESTER_CONTEXT_MISMATCH",
                    path=(field_name,),
                    message=f"{field_name} differs from TesterInput",
                )
            )


def _validate_unique_test_ids(
    test_cases: list[Mapping[str, object]], issues: list[ValidationIssue]
) -> None:
    identifiers = [cast(str, item["id"]) for item in test_cases]
    for identifier in sorted({item for item in identifiers if identifiers.count(item) > 1}):
        issues.append(
            ValidationIssue(
                code="DUPLICATE_TEST_CASE_ID",
                path=("test_cases",),
                message=f"duplicate test case id: {identifier}",
            )
        )


def _validate_execution_claims(
    test_cases: list[Mapping[str, object]], issues: list[ValidationIssue]
) -> None:
    for index, item in enumerate(test_cases):
        if item["verdict"] in {GateResult.PASS.value, GateResult.FAIL.value} and item["executed"] is not True:
            issues.append(
                ValidationIssue(
                    code="TEST_NOT_EXECUTED",
                    path=("test_cases", index, "executed"),
                    message="PASS or FAIL requires an executed test case",
                )
            )


def _validate_test_paths(
    paths: list[str], story: UserStory, issues: list[ValidationIssue]
) -> None:
    seen: set[str] = set()
    for index, path in enumerate(paths):
        try:
            canonical = _normalize_path(path, allow_directory=False)
        except ImplementerInputError as error:
            issues.append(
                ValidationIssue("UNSAFE_REPOSITORY_PATH", ("test_files_changed", index), str(error))
            )
            continue
        if canonical in seen:
            issues.append(
                ValidationIssue(
                    "DUPLICATE_REPOSITORY_PATH",
                    ("test_files_changed", index),
                    "repository path is duplicated after normalization",
                )
            )
        seen.add(canonical)
        if "tests" not in canonical.split("/")[:-1]:
            issues.append(
                ValidationIssue(
                    "PRODUCTION_FILE_MODIFICATION_FORBIDDEN",
                    ("test_files_changed", index),
                    "Tester may modify only files below a tests directory",
                )
            )
        if any(_scope_matches(canonical, item) for item in _CONTROL_PATHS):
            issues.append(
                ValidationIssue(
                    "CONTROL_STATE_MODIFICATION_FORBIDDEN",
                    ("test_files_changed", index),
                    "Tester cannot modify persistent control state",
                )
            )
        elif any(_scope_matches(canonical, item) for item in story.scope.forbidden_paths):
            issues.append(
                ValidationIssue(
                    "FORBIDDEN_PATH",
                    ("test_files_changed", index),
                    "forbidden_paths has priority over allowed_paths",
                )
            )
        elif not any(_scope_matches(canonical, item) for item in story.scope.allowed_paths):
            issues.append(
                ValidationIssue(
                    "PATH_OUTSIDE_SCOPE",
                    ("test_files_changed", index),
                    "test path is not covered by allowed_paths",
                )
            )


def _validate_verification(
    commands: list[str],
    planned_commands: list[str],
    records: list[Mapping[str, object]],
    issues: list[ValidationIssue],
) -> None:
    normalized = [command.strip() for command in commands]
    planned = [command.strip() for command in planned_commands]
    observed = [cast(str, item["command"]).strip() for item in records]
    if not all((*normalized, *planned, *observed)):
        issues.append(ValidationIssue("EMPTY_VERIFICATION_COMMAND", ("verification_commands",), "commands must contain non-whitespace text"))
    if len(normalized) != len(set(normalized)):
        issues.append(ValidationIssue("DUPLICATE_VERIFICATION_COMMAND", ("verification_commands",), "commands must be unique"))
    if len(observed) != len(set(observed)):
        issues.append(ValidationIssue("DUPLICATE_VERIFICATION_RESULT", ("verification_results",), "results must be unique by command"))
    if set(normalized) != set(planned):
        issues.append(ValidationIssue("TEST_PLAN_COMMAND_MISMATCH", ("test_plan", "commands"), "planned and declared commands must match"))
    if set(normalized) != set(observed):
        issues.append(ValidationIssue("VERIFICATION_RESULT_MISMATCH", ("verification_results",), "each command requires exactly one result"))
    for index, item in enumerate(records):
        if item["result"] in {GateResult.PASS.value, GateResult.FAIL.value} and item["executed"] is not True:
            issues.append(ValidationIssue("COMMAND_NOT_EXECUTED", ("verification_results", index, "executed"), "PASS or FAIL requires actual execution"))


def _validate_verdict(
    serialized: Mapping[str, object],
    criteria: Mapping[str, object],
    acceptance_by_id: Mapping[str, Mapping[str, object]],
    test_cases: list[Mapping[str, object]],
    records: list[Mapping[str, object]],
    targeted: list[str],
    issues: list[ValidationIssue],
) -> None:
    mandatory_ids = {
        identifier for identifier, criterion in criteria.items() if getattr(criterion, "mandatory")
    }
    missing_mandatory = mandatory_ids - set(acceptance_by_id)
    mandatory_ac_results = [
        acceptance_by_id[identifier]["result"]
        for identifier in mandatory_ids
        if identifier in acceptance_by_id
    ]
    fail_detected = (
        any(item["result"] == GateResult.FAIL.value for item in acceptance_by_id.values())
        or any(item["verdict"] == GateResult.FAIL.value for item in test_cases)
        or any(item["required"] is True and item["result"] == GateResult.FAIL.value for item in records)
    )
    unknown_required = (
        bool(missing_mandatory)
        or any(result in {GateResult.UNKNOWN.value, GateResult.NOT_APPLICABLE.value} for result in mandatory_ac_results)
        or any(item["required"] is True and item["verdict"] in {GateResult.UNKNOWN.value, GateResult.NOT_APPLICABLE.value} for item in test_cases)
        or any(item["required"] is True and item["result"] in {GateResult.UNKNOWN.value, GateResult.NOT_APPLICABLE.value} for item in records)
    )
    verdict = serialized["verdict"]
    if verdict == TesterVerdict.READY_FOR_REVIEW.value:
        required_types = {item.value for item in TestCaseType}
        actual_types = {cast(str, item["type"]) for item in test_cases}
        if fail_detected:
            issues.append(ValidationIssue("FAIL_FORBIDS_READY_FOR_REVIEW", ("verdict",), "an explicit failure requires remediation"))
        if unknown_required:
            issues.append(ValidationIssue("UNKNOWN_FORBIDS_READY_FOR_REVIEW", ("verdict",), "required UNKNOWN or missing result blocks review readiness"))
        if not mandatory_ids.issubset(set(targeted)):
            issues.append(ValidationIssue("MANDATORY_AC_NOT_TARGETED", ("test_plan", "acceptance_criteria"), "all mandatory criteria must be targeted"))
        if not required_types.issubset(actual_types):
            issues.append(ValidationIssue("INCOMPLETE_ADVERSARIAL_COVERAGE", ("test_cases",), "READY_FOR_REVIEW requires positive, negative, edge and regression cases"))
    elif verdict == TesterVerdict.REMEDIATION_REQUIRED.value:
        if not fail_detected:
            issues.append(ValidationIssue("REMEDIATION_WITHOUT_FAILURE", ("verdict",), "remediation requires an explicit failure"))
        if unknown_required:
            issues.append(ValidationIssue("UNKNOWN_REQUIRES_BLOCKED", ("verdict",), "required UNKNOWN requires BLOCKED"))


def _input_snapshot(value: TesterInput) -> str:
    return json.dumps(
        {
            "mission_id": value.mission_id,
            "workflow_generation": value.workflow_generation,
            "user_story": to_dict(value.user_story),
            "implementer_result": to_dict(value.implementer_result),
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
        contract="tester-result",
        errors=(ValidationIssue(code=code, path=(), message=message),),
    )
