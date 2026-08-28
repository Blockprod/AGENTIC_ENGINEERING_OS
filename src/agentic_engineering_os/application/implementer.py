"""Contracts and deterministic validation for the Codex Implementer role."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from copy import deepcopy
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

from ._identity import is_attributable_human_identity
from .contract_validator import ContractValidator, ValidationIssue, ValidationResult
from .orchestrator import RoleHandoff


_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
_CONTROL_PATHS = (
    ".agentic-engineering-os/mission.json",
    ".agentic-engineering-os/state.json",
)


class ImplementerVerdict(str, Enum):
    READY_FOR_TEST = "READY_FOR_TEST"
    BLOCKED = "BLOCKED"


class VerificationOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True, slots=True)
class VerificationResult:
    command: str
    required: bool
    result: VerificationOutcome
    exit_code: int | None
    details: str


@dataclass(frozen=True, slots=True)
class ImplementerInput:
    """Validated implementation assignment derived from an explicit handoff."""

    mission_id: str
    user_story: UserStory
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
        *,
        validator: ContractValidator | None = None,
    ) -> ImplementerInput:
        if not isinstance(handoff, RoleHandoff):
            raise ImplementerInputError("input must be an explicit RoleHandoff")
        if handoff.from_role is not MissionRole.ORCHESTRATOR:
            raise ImplementerInputError("RoleHandoff must originate from ORCHESTRATOR")
        if handoff.to_role is not MissionRole.IMPLEMENTER:
            raise ImplementerInputError("RoleHandoff must target IMPLEMENTER")
        if handoff.operating_step is not OperatingStep.ACT:
            raise ImplementerInputError("Implementer handoff must target ACT")
        values = (
            handoff.mission_id,
            handoff.subject,
            handoff.objective,
            handoff.instructions,
        )
        if not all(isinstance(value, str) and value.strip() for value in values):
            raise ImplementerInputError("handoff text fields must be non-empty")
        if not _COMMIT_PATTERN.fullmatch(handoff.observed_commit):
            raise ImplementerInputError("observed_commit must be a full Git SHA")
        if not isinstance(handoff.blockers, tuple) or not all(
            isinstance(blocker, str) and blocker.strip() for blocker in handoff.blockers
        ):
            raise ImplementerInputError("blockers must be explicit non-empty strings")
        if handoff.blockers:
            raise ImplementerInputError("handoff with active blockers is not assignable")
        if not isinstance(user_story, UserStory):
            raise ImplementerInputError("assignment must contain a UserStory")
        if handoff.subject != user_story.id:
            raise ImplementerInputError("handoff subject must identify the UserStory")

        _require_assignable_story(
            user_story,
            validator if validator is not None else ContractValidator(),
        )
        result = cls(
            mission_id=handoff.mission_id,
            user_story=deepcopy(user_story),
            observed_commit=handoff.observed_commit,
            objective=handoff.objective,
            blockers=tuple(handoff.blockers),
            instructions=handoff.instructions,
        )
        object.__setattr__(result, "_assignment_snapshot", _input_snapshot(result))
        return result


@dataclass(frozen=True, slots=True)
class ImplementerResult:
    """Structured report; it is neither Evidence nor Certification."""

    mission_id: str
    role: MissionRole = field(default=MissionRole.IMPLEMENTER, init=False)
    subject: str
    user_story_id: str
    observed_commit: str
    summary: str
    files_changed: tuple[str, ...]
    tests_added_or_modified: tuple[str, ...]
    verification_commands: tuple[str, ...]
    verification_results: tuple[VerificationResult, ...]
    assumptions: tuple[str, ...]
    findings: tuple[str, ...]
    blockers: tuple[str, ...]
    recommended_next_role: MissionRole
    verdict: ImplementerVerdict


class ImplementerInputError(ValueError):
    """A handoff or User Story cannot safely authorize implementation."""


class ImplementerResultValidator:
    """Validate an Implementer report against its immutable assignment."""

    def __init__(self, validator: ContractValidator | None = None) -> None:
        self._validator = validator if validator is not None else ContractValidator()

    def validate(
        self,
        candidate: ImplementerResult | Mapping[str, object],
        *,
        implementer_input: ImplementerInput,
    ) -> ValidationResult:
        if not isinstance(implementer_input, ImplementerInput):
            return _invalid("INVALID_VALIDATION_CONTEXT", "implementer_input is required")
        if (
            not implementer_input._assignment_snapshot
            or implementer_input._assignment_snapshot != _input_snapshot(implementer_input)
        ):
            return _invalid(
                "IMPLEMENTER_INPUT_TAMPERED",
                "ImplementerInput differs from its authorized assignment snapshot",
            )
        try:
            _require_assignable_story(implementer_input.user_story, self._validator)
        except ImplementerInputError as error:
            return _invalid("INVALID_IMPLEMENTER_INPUT", str(error))

        if isinstance(candidate, ImplementerResult):
            serialized = cast(dict[str, object], to_dict(candidate))
        elif isinstance(candidate, Mapping):
            serialized = dict(candidate)
        else:
            return _invalid(
                "INVALID_IMPLEMENTER_OUTPUT",
                "candidate must be an ImplementerResult or mapping",
            )

        result = self._validator.validate("implementer-result", serialized)
        if not result.is_valid:
            return result

        story = implementer_input.user_story
        issues: list[ValidationIssue] = []
        expected = {
            "mission_id": implementer_input.mission_id,
            "subject": story.id,
            "user_story_id": story.id,
            "observed_commit": implementer_input.observed_commit.casefold(),
        }
        actual = {
            "mission_id": serialized["mission_id"],
            "subject": serialized["subject"],
            "user_story_id": serialized["user_story_id"],
            "observed_commit": cast(str, serialized["observed_commit"]).casefold(),
        }
        for name, expected_value in expected.items():
            if actual[name] != expected_value:
                issues.append(
                    ValidationIssue(
                        code="IMPLEMENTER_CONTEXT_MISMATCH",
                        path=(name,),
                        message=f"{name} differs from ImplementerInput",
                    )
                )

        files = cast(list[str], serialized["files_changed"])
        tests = cast(list[str], serialized["tests_added_or_modified"])
        normalized_files = _validate_changed_paths(files, story, issues, "files_changed")
        normalized_tests = _validate_changed_paths(
            tests, story, issues, "tests_added_or_modified"
        )
        file_set = set(normalized_files)
        for index, test_path in enumerate(normalized_tests):
            if test_path not in file_set:
                issues.append(
                    ValidationIssue(
                        code="TEST_NOT_IN_CHANGED_FILES",
                        path=("tests_added_or_modified", index),
                        message="test path must also be declared in files_changed",
                    )
                )

        commands = cast(list[str], serialized["verification_commands"])
        records = cast(list[Mapping[str, object]], serialized["verification_results"])
        _validate_verification(
            commands,
            records,
            issues,
            require_success=(
                serialized["verdict"] == ImplementerVerdict.READY_FOR_TEST.value
            ),
        )
        return ValidationResult(contract="implementer-result", errors=tuple(issues))


def _require_assignable_story(story: UserStory, validator: ContractValidator) -> None:
    result = validator.validate("user-story", to_dict(story))
    if not result.is_valid:
        raise ImplementerInputError("UserStory does not satisfy its canonical contract")
    if story.status is not UserStoryStatus.IN_PROGRESS:
        raise ImplementerInputError("UserStory must be IN_PROGRESS for implementation")
    approval = story.human_approval
    if approval.required and (
        not approval.approved
        or not isinstance(approval.approved_by, str)
        or not approval.approved_by.strip()
        or approval.approved_at is None
        or not is_attributable_human_identity(approval.approved_by)
    ):
        raise ImplementerInputError("required Human approval is not satisfied")
    if not story.scope.allowed_paths:
        raise ImplementerInputError("UserStory allowed_paths must be non-empty")
    for path in (*story.scope.allowed_paths, *story.scope.forbidden_paths):
        _normalize_path(path, allow_directory=True)


def _validate_changed_paths(
    paths: list[str],
    story: UserStory,
    issues: list[ValidationIssue],
    field_name: str,
) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for index, path in enumerate(paths):
        try:
            canonical = _normalize_path(path, allow_directory=False)
        except ImplementerInputError as error:
            issues.append(
                ValidationIssue(
                    code="UNSAFE_REPOSITORY_PATH",
                    path=(field_name, index),
                    message=str(error),
                )
            )
            continue
        if canonical in seen:
            issues.append(
                ValidationIssue(
                    code="DUPLICATE_REPOSITORY_PATH",
                    path=(field_name, index),
                    message="repository path is duplicated after normalization",
                )
            )
        seen.add(canonical)
        normalized.append(canonical)
        if any(_scope_matches(canonical, path) for path in _CONTROL_PATHS):
            issues.append(
                ValidationIssue(
                    code="CONTROL_STATE_MODIFICATION_FORBIDDEN",
                    path=(field_name, index),
                    message="Implementer cannot modify persistent control state",
                )
            )
            continue
        forbidden = any(
            _scope_matches(canonical, path) for path in story.scope.forbidden_paths
        )
        allowed = any(
            _scope_matches(canonical, path) for path in story.scope.allowed_paths
        )
        if forbidden:
            issues.append(
                ValidationIssue(
                    code="FORBIDDEN_PATH",
                    path=(field_name, index),
                    message="forbidden_paths has priority over allowed_paths",
                )
            )
        elif not allowed:
            issues.append(
                ValidationIssue(
                    code="PATH_OUTSIDE_SCOPE",
                    path=(field_name, index),
                    message="path is not covered by allowed_paths",
                )
            )
    return normalized


def _validate_verification(
    commands: list[str],
    records: list[Mapping[str, object]],
    issues: list[ValidationIssue],
    *,
    require_success: bool,
) -> None:
    normalized_commands = [command.strip() for command in commands]
    if len(set(normalized_commands)) != len(normalized_commands):
        issues.append(
            ValidationIssue(
                code="DUPLICATE_VERIFICATION_COMMAND",
                path=("verification_commands",),
                message="verification commands must be unique",
            )
        )
    result_commands = [cast(str, record["command"]).strip() for record in records]
    if len(set(result_commands)) != len(result_commands):
        issues.append(
            ValidationIssue(
                code="DUPLICATE_VERIFICATION_RESULT",
                path=("verification_results",),
                message="each command must have exactly one result",
            )
        )
    if set(normalized_commands) != set(result_commands):
        issues.append(
            ValidationIssue(
                code="VERIFICATION_RESULT_MISMATCH",
                path=("verification_results",),
                message="every declared command must have exactly one matching result",
            )
        )
    for index, record in enumerate(records):
        if (
            require_success
            and record["required"] is True
            and record["result"] != VerificationOutcome.PASS.value
        ):
            issues.append(
                ValidationIssue(
                    code="REQUIRED_VERIFICATION_NOT_PASS",
                    path=("verification_results", index, "result"),
                    message="required verification must be PASS for READY_FOR_TEST",
                )
            )


def _normalize_path(path: object, *, allow_directory: bool) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ImplementerInputError("repository path must be a non-empty string")
    if path != path.strip() or "\\" in path or path.startswith("/") or _DRIVE_PATTERN.match(path):
        raise ImplementerInputError("repository path must be an unambiguous relative POSIX path")
    directory = path.endswith("/")
    if directory and not allow_directory:
        raise ImplementerInputError("changed file path cannot identify a directory")
    value = path[:-1] if directory else path
    parts = value.split("/")
    if not value or any(part in {"", ".", ".."} for part in parts):
        raise ImplementerInputError("repository path cannot contain empty or traversal segments")
    canonical = unicodedata.normalize("NFC", value).casefold()
    return f"{canonical}/" if directory else canonical


def _scope_matches(file_path: str, scope_path: str) -> bool:
    scope = _normalize_path(scope_path, allow_directory=True)
    if scope.endswith("/"):
        return file_path.startswith(scope)
    return file_path == scope


def _invalid(code: str, message: str) -> ValidationResult:
    return ValidationResult(
        contract="implementer-result",
        errors=(ValidationIssue(code=code, path=(), message=message),),
    )


def _input_snapshot(value: ImplementerInput) -> str:
    return json.dumps(
        {
            "mission_id": value.mission_id,
            "user_story": to_dict(value.user_story),
            "observed_commit": value.observed_commit,
            "objective": value.objective,
            "blockers": list(value.blockers),
            "instructions": value.instructions,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
