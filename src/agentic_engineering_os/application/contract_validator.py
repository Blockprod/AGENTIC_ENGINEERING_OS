"""Deterministic validation of candidate data against Phase 0 contracts."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypeAlias, cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource


ErrorPathPart: TypeAlias = str | int

_SCHEMA_FILES = {
    "user-story": "user-story.schema.json",
    "evidence": "evidence.schema.json",
    "gate": "gate.schema.json",
    "audit-event": "audit-event.schema.json",
    "certification": "certification.schema.json",
    "project-state": "project-state.schema.json",
    "mission-state": "mission-state.schema.json",
    "architect-result": "architect-result.schema.json",
    "implementer-result": "implementer-result.schema.json",
    "tester-result": "tester-result.schema.json",
    "reviewer-result": "reviewer-result.schema.json",
    "certifier-result": "certifier-result.schema.json",
    "dag-snapshot": "dag-snapshot.schema.json",
    "readiness-snapshot": "readiness-snapshot.schema.json",
    "wave-plan": "wave-plan.schema.json",
    "conflict-analysis": "conflict-analysis.schema.json",
}


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    path: tuple[ErrorPathPart, ...]
    message: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    contract: str
    errors: tuple[ValidationIssue, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.errors


class ValidationError(RuntimeError):
    """The requested validation could not be completed reliably."""

    def __init__(self, contract: str, message: str) -> None:
        self.contract = contract
        self.message = message
        super().__init__(f"{contract}: {message}")


class ParseError(RuntimeError):
    """A required local JSON Schema could not be parsed."""

    def __init__(self, contract: str, message: str) -> None:
        self.contract = contract
        self.message = message
        super().__init__(f"{contract}: {message}")


class ContractValidator:
    """Validate structure and object-local semantics without project state."""

    def __init__(self, schema_directory: Path | None = None) -> None:
        self._schema_directory = schema_directory or _default_schema_directory()

    def validate(self, contract: str, candidate: object) -> ValidationResult:
        """Return all provable candidate issues, or raise on validator failure."""

        if contract not in _SCHEMA_FILES:
            raise ValidationError(contract, "unknown contract type")

        try:
            validator = self._load_validator(contract)
            json_issues = tuple(_json_data_issues(candidate))
            if json_issues:
                return ValidationResult(contract=contract, errors=json_issues)

            structural_issues = tuple(
                ValidationIssue(
                    code="SCHEMA_VIOLATION",
                    path=tuple(error.absolute_path),
                    message=error.message,
                )
                for error in sorted(
                    validator.iter_errors(candidate), key=_schema_error_sort_key
                )
            )
            if structural_issues:
                return ValidationResult(contract=contract, errors=structural_issues)

            semantic_issues = self._local_semantic_issues(contract, candidate)
            return ValidationResult(contract=contract, errors=semantic_issues)
        except (ParseError, ValidationError):
            raise
        except Exception as error:
            raise ValidationError(
                contract,
                f"validation could not be completed: {type(error).__name__}: {error}",
            ) from error

    def _load_validator(self, contract: str) -> Draft202012Validator:
        schema_path = self._schema_directory / _SCHEMA_FILES[contract]
        try:
            schema_text = schema_path.read_text(encoding="utf-8")
        except OSError as error:
            raise ValidationError(
                contract, f"schema cannot be resolved at {schema_path}"
            ) from error

        try:
            schema = json.loads(schema_text)
        except json.JSONDecodeError as error:
            raise ParseError(contract, f"schema is not valid JSON: {schema_path}") from error

        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as error:
            raise ValidationError(contract, "schema is not valid Draft 2020-12") from error

        registry = self._schema_registry()
        return Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
            registry=registry,
        )

    def _schema_registry(self) -> Registry:
        resources: list[tuple[str, Resource[object]]] = []
        for schema_file in _SCHEMA_FILES.values():
            schema_path = self._schema_directory / schema_file
            try:
                candidate = json.loads(schema_path.read_text(encoding="utf-8"))
                resource = Resource.from_contents(candidate)
                identifier = candidate["$id"]
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
                raise ValidationError(
                    "schema-registry",
                    f"schema cannot be registered: {schema_path}",
                ) from error
            resources.append((identifier, resource))
        return Registry().with_resources(resources)

    @staticmethod
    def _local_semantic_issues(
        contract: str, candidate: object
    ) -> tuple[ValidationIssue, ...]:
        if contract == "mission-state":
            mission_state = cast(Mapping[str, object], candidate)
            timestamp = cast(str, mission_state["updated_at"])
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
            if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
                return (
                    ValidationIssue(
                        code="INVALID_TIMESTAMP",
                        path=("updated_at",),
                        message="updated_at must be an ISO 8601 datetime with timezone",
                    ),
                )
            return ()

        if contract != "user-story":
            return ()

        user_story = cast(Mapping[str, object], candidate)
        issues: list[ValidationIssue] = []
        story_id = user_story["id"]
        dependencies = cast(list[object], user_story["depends_on"])
        if story_id in dependencies:
            issues.append(
                ValidationIssue(
                    code="SELF_DEPENDENCY",
                    path=("depends_on",),
                    message="a User Story cannot depend on itself",
                )
            )

        criteria = cast(list[Mapping[str, object]], user_story["acceptance_criteria"])
        criterion_ids = [criterion["id"] for criterion in criteria]
        seen_ids: set[object] = set()
        duplicate_ids: set[object] = set()
        for criterion_id in criterion_ids:
            if criterion_id in seen_ids:
                duplicate_ids.add(criterion_id)
            seen_ids.add(criterion_id)
        for criterion_id in sorted(duplicate_ids, key=str):
            issues.append(
                ValidationIssue(
                    code="DUPLICATE_ACCEPTANCE_CRITERION_ID",
                    path=("acceptance_criteria",),
                    message=f"duplicate Acceptance Criterion id: {criterion_id}",
                )
            )
        return tuple(issues)


def _default_schema_directory() -> Path:
    return Path(__file__).resolve().parents[3] / "schemas"


def _schema_error_sort_key(error: object) -> tuple[tuple[str, ...], str]:
    path = getattr(error, "absolute_path")
    message = getattr(error, "message")
    return tuple(f"{type(part).__name__}:{part}" for part in path), message


def _json_data_issues(
    value: object, path: tuple[ErrorPathPart, ...] = ()
) -> list[ValidationIssue]:
    if value is None or isinstance(value, (str, bool, int)):
        return []
    if isinstance(value, float):
        if math.isfinite(value):
            return []
        return [
            ValidationIssue(
                code="INVALID_JSON_DATA",
                path=path,
                message="non-finite numbers are not valid JSON data",
            )
        ]
    if isinstance(value, list):
        issues: list[ValidationIssue] = []
        for index, item in enumerate(value):
            issues.extend(_json_data_issues(item, (*path, index)))
        return issues
    if isinstance(value, Mapping):
        issues = []
        for key, item in sorted(value.items(), key=lambda pair: repr(pair[0])):
            if not isinstance(key, str):
                issues.append(
                    ValidationIssue(
                        code="INVALID_JSON_DATA",
                        path=path,
                        message=f"JSON object key must be a string: {key!r}",
                    )
                )
                continue
            issues.extend(_json_data_issues(item, (*path, key)))
        return issues
    return [
        ValidationIssue(
            code="INVALID_JSON_DATA",
            path=path,
            message=f"unsupported JSON value: {type(value).__name__}",
        )
    ]
