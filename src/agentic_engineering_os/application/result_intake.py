"""Strict intake of schema-constrained Codex role results."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TypeAlias, cast

from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    GateResult,
    HumanApproval,
    MissionRole,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
)
from .architect import (
    ArchitectDecision,
    ArchitectDecisionKind,
    ArchitectInput,
    ArchitectResult,
    ArchitectResultValidator,
    ArchitectVerdict,
)
from .certifier import (
    AcceptanceCheck,
    ArtifactCheck,
    CertifierFinding,
    CertifierInput,
    CertifierRecommendedAction,
    CertifierResult,
    CertifierResultValidator,
    CertifierVerdict,
    GateCheck,
    HumanApprovalCheck,
)
from .codex_runtime import CodexExecutionObservation, CodexJsonlEvent
from .codex_output_schema import (
    CodexOutputSchemaError,
    codex_output_schema_path,
)
from .contract_validator import ContractValidator, ValidationIssue, ValidationResult
from .implementer import (
    ImplementerInput,
    ImplementerResult,
    ImplementerResultValidator,
    ImplementerVerdict,
    VerificationOutcome,
    VerificationResult,
    _normalize_path,
)
from .prompt_compiler import CompiledPrompt
from .reviewer import (
    ReviewDimension,
    ReviewFinding,
    ReviewerInput,
    ReviewerResult,
    ReviewerResultValidator,
    ReviewerVerdict,
    ReviewSeverity,
)
from .tester import (
    TestCaseType,
    TesterAcceptanceResult,
    TesterInput,
    TesterPlan,
    TesterResult,
    TesterResultValidator,
    TesterTestCase,
    TesterVerdict,
    TesterVerificationResult,
)


RoleResult: TypeAlias = (
    ArchitectResult
    | ImplementerResult
    | TesterResult
    | ReviewerResult
    | CertifierResult
)
RoleValidationInput: TypeAlias = (
    ArchitectInput | ImplementerInput | TesterInput | ReviewerInput | CertifierInput
)


class PersistedRoleResultError(ValueError):
    """Persisted validated JSON no longer reconstructs as its declared role."""


def reconstruct_persisted_role_result(value: str, role: MissionRole) -> RoleResult:
    """Rebuild one canonical role result from strict execution-ledger JSON."""

    if not isinstance(role, MissionRole) or role is MissionRole.ORCHESTRATOR:
        raise PersistedRoleResultError("persisted result role is invalid")
    if not isinstance(value, str) or not value:
        raise PersistedRoleResultError("validated RoleResult JSON is absent")
    try:
        payload = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise PersistedRoleResultError("validated RoleResult is not strict JSON") from error
    if not isinstance(payload, dict):
        raise PersistedRoleResultError("validated RoleResult must be an object")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if value != canonical or payload.get("role") != role.value:
        raise PersistedRoleResultError(
            "persisted RoleResult is non-canonical or has the wrong role"
        )
    contract = f"{role.value.casefold()}-result"
    contracts = ContractValidator()
    if not contracts.validate(contract, payload).is_valid:
        raise PersistedRoleResultError("persisted RoleResult fails its schema")
    try:
        candidate = _build_role_result(role, payload)
    except (KeyError, TypeError, ValueError) as error:
        raise PersistedRoleResultError(
            "persisted RoleResult cannot rebuild the canonical model"
        ) from error
    if getattr(candidate, "role", None) is not role:
        raise PersistedRoleResultError("persisted RoleResult rebuilt as the wrong role")
    return candidate


def reconstruct_persisted_architect_result(value: str) -> ArchitectResult:
    """Rebuild a canonical ArchitectResult from execution-ledger JSON only."""

    if not isinstance(value, str) or not value:
        raise PersistedRoleResultError("validated ArchitectResult JSON is absent")
    try:
        payload = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise PersistedRoleResultError(
            "validated ArchitectResult is not strict JSON"
        ) from error
    if not isinstance(payload, dict):
        raise PersistedRoleResultError("validated ArchitectResult must be an object")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if value != canonical:
        raise PersistedRoleResultError("validated ArchitectResult is not canonical")
    if payload.get("role") != MissionRole.ARCHITECT.value:
        raise PersistedRoleResultError("persisted result role is not ARCHITECT")
    contracts = ContractValidator()
    schema = contracts.validate("architect-result", payload)
    if not schema.is_valid:
        raise PersistedRoleResultError("persisted ArchitectResult fails its schema")
    try:
        candidate = _build_role_result(MissionRole.ARCHITECT, payload)
    except (KeyError, TypeError, ValueError) as error:
        raise PersistedRoleResultError(
            "persisted ArchitectResult cannot rebuild the canonical model"
        ) from error
    if not isinstance(candidate, ArchitectResult):
        raise PersistedRoleResultError("persisted result did not rebuild as ArchitectResult")
    validation = ArchitectResultValidator(contracts).validate(candidate)
    if not validation.is_valid:
        raise PersistedRoleResultError("persisted ArchitectResult is canonically invalid")
    return candidate


def reconstruct_persisted_implementer_result(value: str) -> ImplementerResult:
    """Rebuild schema-valid canonical ImplementerResult ledger JSON."""

    if not isinstance(value, str) or not value:
        raise PersistedRoleResultError("validated ImplementerResult JSON is absent")
    try:
        payload = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise PersistedRoleResultError(
            "validated ImplementerResult is not strict JSON"
        ) from error
    if not isinstance(payload, dict):
        raise PersistedRoleResultError("validated ImplementerResult must be an object")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if value != canonical or payload.get("role") != MissionRole.IMPLEMENTER.value:
        raise PersistedRoleResultError(
            "persisted ImplementerResult is non-canonical or has the wrong role"
        )
    contracts = ContractValidator()
    if not contracts.validate("implementer-result", payload).is_valid:
        raise PersistedRoleResultError("persisted ImplementerResult fails its schema")
    try:
        candidate = _build_role_result(MissionRole.IMPLEMENTER, payload)
    except (KeyError, TypeError, ValueError) as error:
        raise PersistedRoleResultError(
            "persisted ImplementerResult cannot rebuild the canonical model"
        ) from error
    if not isinstance(candidate, ImplementerResult):
        raise PersistedRoleResultError(
            "persisted result did not rebuild as ImplementerResult"
        )
    return candidate


_CONTRACTS = {
    MissionRole.ARCHITECT: "architect-result@1.0",
    MissionRole.IMPLEMENTER: "implementer-result@1.0",
    MissionRole.TESTER: "tester-result@1.0",
    MissionRole.REVIEWER: "reviewer-result@1.0",
    MissionRole.CERTIFIER: "certifier-result@1.0",
}
_INPUT_ROLES = {
    ArchitectInput: MissionRole.ARCHITECT,
    ImplementerInput: MissionRole.IMPLEMENTER,
    TesterInput: MissionRole.TESTER,
    ReviewerInput: MissionRole.REVIEWER,
    CertifierInput: MissionRole.CERTIFIER,
}
_READ_ONLY_ROLES = frozenset(
    {MissionRole.ARCHITECT, MissionRole.REVIEWER, MissionRole.CERTIFIER}
)
_ALLOWED_TRANSPORT_ISSUES = frozenset({"STDERR_OBSERVED", "GIT_STATE_CHANGED"})


class ResultIntakeRefusalCode(str, Enum):
    INVALID_INPUT = "INVALID_INPUT"
    OBSERVATION_BINDING_MISMATCH = "OBSERVATION_BINDING_MISMATCH"
    VALIDATION_CONTEXT_MISMATCH = "VALIDATION_CONTEXT_MISMATCH"
    EXPECTED_CONTRACT_MISMATCH = "EXPECTED_CONTRACT_MISMATCH"
    STRUCTURED_CHANNEL_MISSING = "STRUCTURED_CHANNEL_MISSING"
    STRUCTURED_CHANNEL_AMBIGUOUS = "STRUCTURED_CHANNEL_AMBIGUOUS"
    TRANSPORT_FAILED = "TRANSPORT_FAILED"
    PAYLOAD_MISSING = "PAYLOAD_MISSING"
    PAYLOAD_MALFORMED = "PAYLOAD_MALFORMED"
    PAYLOAD_AMBIGUOUS = "PAYLOAD_AMBIGUOUS"
    ROLE_MISMATCH = "ROLE_MISMATCH"
    PAYLOAD_BINDING_MISMATCH = "PAYLOAD_BINDING_MISMATCH"
    ROLE_VALIDATION_FAILED = "ROLE_VALIDATION_FAILED"
    CANONICAL_MODEL_BUILD_FAILED = "CANONICAL_MODEL_BUILD_FAILED"
    GIT_OBSERVATION_REQUIRED = "GIT_OBSERVATION_REQUIRED"
    GIT_COMMIT_MISMATCH = "GIT_COMMIT_MISMATCH"
    GIT_SIDE_EFFECT_MISMATCH = "GIT_SIDE_EFFECT_MISMATCH"


@dataclass(frozen=True, slots=True)
class ResultIntakeRefusal:
    code: ResultIntakeRefusalCode
    path: tuple[str | int, ...]
    message: str


@dataclass(frozen=True, slots=True)
class ResultIntakeValidationContext:
    """Existing role authority plus the exact schema channel selected for execution."""

    role_input: RoleValidationInput
    output_schema_path: str
    known_user_story_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResultIntakeDiagnostics:
    exit_code: int | None
    timed_out: bool
    interrupted: bool
    tool_failure_observed: bool
    stderr_observed: bool
    stdout_truncated: bool
    stderr_truncated: bool
    transport_issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResultIntakeOutcome:
    """Intake fact only; acceptance carries no Control Plane authority."""

    accepted: bool
    role: MissionRole
    validated_result: RoleResult | None
    refusal_reasons: tuple[ResultIntakeRefusal, ...]
    diagnostics: ResultIntakeDiagnostics

    def __post_init__(self) -> None:
        if self.accepted != (self.validated_result is not None):
            raise ValueError("accepted must exactly match presence of a validated result")
        if self.accepted == bool(self.refusal_reasons):
            raise ValueError("accepted outcomes cannot carry refusal reasons")


class CodexResultIntake:
    """Convert one bound, schema-constrained observation into a validated result."""

    def __init__(self, contract_validator: ContractValidator | None = None) -> None:
        self._contracts = contract_validator or ContractValidator()

    def process(
        self,
        compiled_prompt: CompiledPrompt,
        observation: CodexExecutionObservation,
        validation_context: ResultIntakeValidationContext,
    ) -> ResultIntakeOutcome:
        if not isinstance(compiled_prompt, CompiledPrompt):
            raise TypeError("compiled_prompt must use CompiledPrompt")
        if not isinstance(observation, CodexExecutionObservation):
            raise TypeError("observation must use CodexExecutionObservation")
        diagnostics = _diagnostics(observation)
        if not isinstance(validation_context, ResultIntakeValidationContext):
            return _refused(
                compiled_prompt.role,
                diagnostics,
                ResultIntakeRefusalCode.INVALID_INPUT,
                "validation_context must use ResultIntakeValidationContext",
            )

        preflight = _validate_execution_binding(
            compiled_prompt, observation, validation_context
        )
        if preflight:
            return _refused_many(compiled_prompt.role, diagnostics, preflight)

        transport = _validate_transport(compiled_prompt.role, observation)
        if transport:
            return _refused_many(compiled_prompt.role, diagnostics, transport)

        payload_text, source_reasons = _structured_payload_text(observation)
        if source_reasons:
            return _refused_many(compiled_prompt.role, diagnostics, source_reasons)
        if payload_text is None:
            return _refused(
                compiled_prompt.role,
                diagnostics,
                ResultIntakeRefusalCode.PAYLOAD_MISSING,
                "schema-constrained payload is absent",
            )

        try:
            payload = json.loads(
                payload_text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as error:
            return _refused(
                compiled_prompt.role,
                diagnostics,
                ResultIntakeRefusalCode.PAYLOAD_MALFORMED,
                f"structured payload is not strict JSON: {error}",
            )
        if not isinstance(payload, dict):
            return _refused(
                compiled_prompt.role,
                diagnostics,
                ResultIntakeRefusalCode.PAYLOAD_MALFORMED,
                "structured payload must be a JSON object",
            )

        expected_role = compiled_prompt.role.value
        if payload.get("role") != expected_role:
            return _refused(
                compiled_prompt.role,
                diagnostics,
                ResultIntakeRefusalCode.ROLE_MISMATCH,
                "payload role differs from the compiled role",
                path=("role",),
            )
        payload_binding = _validate_payload_binding(compiled_prompt, payload)
        if payload_binding:
            return _refused_many(compiled_prompt.role, diagnostics, payload_binding)

        contract_name = compiled_prompt.expected_result_contract.removesuffix("@1.0")
        schema_result = self._contracts.validate(contract_name, payload)
        if not schema_result.is_valid:
            return _refused_many(
                compiled_prompt.role,
                diagnostics,
                _validation_refusals(schema_result),
            )
        try:
            candidate = _build_role_result(compiled_prompt.role, payload)
        except (KeyError, TypeError, ValueError) as error:
            return _refused(
                compiled_prompt.role,
                diagnostics,
                ResultIntakeRefusalCode.CANONICAL_MODEL_BUILD_FAILED,
                f"canonical RoleResult construction failed: {error}",
            )

        role_validation = _validate_role_result(
            candidate, validation_context, self._contracts
        )
        if not role_validation.is_valid:
            return _refused_many(
                compiled_prompt.role,
                diagnostics,
                _validation_refusals(role_validation),
            )
        git_reasons = _validate_git_consistency(
            compiled_prompt.role, candidate, observation
        )
        if git_reasons:
            return _refused_many(compiled_prompt.role, diagnostics, git_reasons)
        return ResultIntakeOutcome(
            accepted=True,
            role=compiled_prompt.role,
            validated_result=candidate,
            refusal_reasons=(),
            diagnostics=diagnostics,
        )


def _diagnostics(observation: CodexExecutionObservation) -> ResultIntakeDiagnostics:
    return ResultIntakeDiagnostics(
        exit_code=observation.exit_code,
        timed_out=observation.timed_out,
        interrupted=observation.interrupted,
        tool_failure_observed=observation.tool_failure_observed,
        stderr_observed=bool(observation.stderr),
        stdout_truncated=observation.stdout_truncated,
        stderr_truncated=observation.stderr_truncated,
        transport_issues=observation.issues,
    )


def _validate_execution_binding(
    compiled: CompiledPrompt,
    observation: CodexExecutionObservation,
    context: ResultIntakeValidationContext,
) -> tuple[ResultIntakeRefusal, ...]:
    reasons: list[ResultIntakeRefusal] = []
    if (
        observation.request_id != compiled.request_id
        or observation.context_fingerprint != compiled.context_fingerprint
    ):
        reasons.append(
            _reason(
                ResultIntakeRefusalCode.OBSERVATION_BINDING_MISMATCH,
                "observation request/context differs from CompiledPrompt",
            )
        )
    expected_cwd = compiled.worktree_path or compiled.repository_root
    if not _same_existing_path(observation.cwd, expected_cwd):
        reasons.append(
            _reason(
                ResultIntakeRefusalCode.OBSERVATION_BINDING_MISMATCH,
                "observation cwd differs from compiled repository/worktree",
                ("cwd",),
            )
        )
    role = _INPUT_ROLES.get(type(context.role_input))
    if role is not compiled.role:
        reasons.append(
            _reason(
                ResultIntakeRefusalCode.VALIDATION_CONTEXT_MISMATCH,
                "role validation input differs from compiled role",
            )
        )
    else:
        role_input = context.role_input
        input_subject = (
            role_input.subject
            if isinstance(role_input, ArchitectInput)
            else role_input.user_story.id
        )
        expected = (
            ("mission_id", role_input.mission_id, compiled.mission_id),
            (
                "workflow_generation",
                role_input.workflow_generation,
                compiled.workflow_generation,
            ),
            ("subject", input_subject, compiled.subject),
            ("observed_commit", role_input.observed_commit.casefold(), compiled.observed_commit.casefold()),
        )
        for name, actual, required in expected:
            if actual != required:
                reasons.append(
                    _reason(
                        ResultIntakeRefusalCode.VALIDATION_CONTEXT_MISMATCH,
                        f"{name} differs from CompiledPrompt",
                        (name,),
                    )
                )
    expected_contract = _CONTRACTS.get(compiled.role)
    if compiled.expected_result_contract != expected_contract:
        reasons.append(
            _reason(
                ResultIntakeRefusalCode.EXPECTED_CONTRACT_MISMATCH,
                "compiled expected RoleResult contract is not canonical for the role",
                ("expected_result_contract",),
            )
        )
    reasons.extend(_validate_schema_channel(compiled, observation, context))
    reasons.extend(_validate_git_before(compiled, observation))
    return tuple(reasons)


def _validate_schema_channel(
    compiled: CompiledPrompt,
    observation: CodexExecutionObservation,
    context: ResultIntakeValidationContext,
) -> tuple[ResultIntakeRefusal, ...]:
    cwd_values = _argument_values(observation.invocation, "-C")
    if (
        len(cwd_values) != 1
        or not _same_existing_path(cwd_values[0], observation.cwd)
        or "exec" not in observation.invocation
        or "--json" not in observation.invocation
        or not observation.invocation
        or observation.invocation[-1] != "-"
    ):
        return (
            _reason(
                ResultIntakeRefusalCode.STRUCTURED_CHANNEL_AMBIGUOUS,
                "invocation does not prove one explicit JSONL stdin/cwd channel",
            ),
        )
    values = _argument_values(observation.invocation, "--output-schema")
    if not values:
        return (
            _reason(
                ResultIntakeRefusalCode.STRUCTURED_CHANNEL_MISSING,
                "invocation did not bind an output schema",
            ),
        )
    if len(values) != 1:
        return (
            _reason(
                ResultIntakeRefusalCode.STRUCTURED_CHANNEL_AMBIGUOUS,
                "invocation contains multiple output schemas",
            ),
        )
    if not isinstance(context.output_schema_path, str):
        return (
            _reason(
                ResultIntakeRefusalCode.STRUCTURED_CHANNEL_AMBIGUOUS,
                "output schema binding must be an explicit path",
            ),
        )
    supplied = Path(context.output_schema_path)
    try:
        expected_schema = codex_output_schema_path(compiled.role)
    except CodexOutputSchemaError:
        return (
            _reason(
                ResultIntakeRefusalCode.STRUCTURED_CHANNEL_AMBIGUOUS,
                "role has no canonical Codex transport schema",
            ),
        )
    if (
        not supplied.is_absolute()
        or supplied.is_symlink()
        or not supplied.is_file()
        or not _same_existing_path(str(supplied), str(expected_schema))
        or not _same_existing_path(values[0], str(supplied))
    ):
        return (
            _reason(
                ResultIntakeRefusalCode.STRUCTURED_CHANNEL_AMBIGUOUS,
                "output schema does not match the packaged role transport schema binding",
            ),
        )
    return ()


def _validate_git_before(
    compiled: CompiledPrompt, observation: CodexExecutionObservation
) -> tuple[ResultIntakeRefusal, ...]:
    before = observation.git_before
    if (
        before is None
        or before.error is not None
        or before.clean is not True
        or before.head_commit is None
        or before.changed_paths != ()
    ):
        return (
            _reason(
                ResultIntakeRefusalCode.GIT_OBSERVATION_REQUIRED,
                "complete clean pre-execution Git observation is required",
            ),
        )
    if before.head_commit.casefold() != compiled.observed_commit.casefold():
        return (
            _reason(
                ResultIntakeRefusalCode.GIT_COMMIT_MISMATCH,
                "pre-execution Git HEAD differs from compiled commit",
            ),
        )
    return ()


def _validate_transport(
    role: MissionRole, observation: CodexExecutionObservation
) -> tuple[ResultIntakeRefusal, ...]:
    unexpected_issues = tuple(
        issue for issue in observation.issues if issue not in _ALLOWED_TRANSPORT_ISSUES
    )
    changed_issue_invalid = (
        "GIT_STATE_CHANGED" in observation.issues and role in _READ_ONLY_ROLES
    )
    if (
        observation.process_id is None
        or observation.exit_code != 0
        or observation.timed_out
        or observation.interrupted
        or observation.tool_failure_observed
        or observation.stdout_truncated
        or observation.stderr_truncated
        or observation.invalid_jsonl_lines
        or not _events_match_stdout(observation)
        or unexpected_issues
        or changed_issue_invalid
    ):
        return (
            _reason(
                ResultIntakeRefusalCode.TRANSPORT_FAILED,
                "transport/process diagnostics do not permit RoleResult intake",
            ),
        )
    return ()


def _structured_payload_text(
    observation: CodexExecutionObservation,
) -> tuple[str | None, tuple[ResultIntakeRefusal, ...]]:
    if not observation.events:
        return None, (
            _reason(
                ResultIntakeRefusalCode.PAYLOAD_MISSING,
                "no schema-constrained completed agent message was observed",
            ),
        )
    messages: list[tuple[str, str]] = []
    payloads: list[dict[str, object]] = []
    seen_item_ids: set[str] = set()
    for event in observation.events:
        try:
            payload = _event_payload(event)
        except (AttributeError, json.JSONDecodeError, TypeError, ValueError) as error:
            return None, (
                _reason(
                    ResultIntakeRefusalCode.PAYLOAD_MALFORMED,
                    f"stored JSONL event is not strict: {error}",
                ),
            )
        payloads.append(payload)
    starts = [index for index, payload in enumerate(payloads) if payload.get("type") == "turn.started"]
    completions = [index for index, payload in enumerate(payloads) if payload.get("type") == "turn.completed"]
    if len(starts) != 1 or len(completions) != 1:
        return None, (
            _reason(
                ResultIntakeRefusalCode.PAYLOAD_AMBIGUOUS,
                "exactly one completed transport turn is required",
            ),
        )
    start, completion = starts[0], completions[0]
    if start >= completion or completion != len(payloads) - 1:
        return None, (
            _reason(
                ResultIntakeRefusalCode.PAYLOAD_MALFORMED,
                "JSONL turn ordering does not identify a terminal boundary",
            ),
        )
    for index, payload in enumerate(payloads):
        item = payload.get("item")
        if isinstance(item, dict) and item.get("id") is not None:
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id or item_id in seen_item_ids:
                return None, (
                    _reason(
                        ResultIntakeRefusalCode.PAYLOAD_AMBIGUOUS,
                        "item identity is absent, duplicate, or replayed",
                    ),
                )
            seen_item_ids.add(item_id)
        if (
            payload.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
        ):
            item_id = item.get("id")
            text = item.get("text")
            if (
                not isinstance(item_id, str)
                or not item_id
                or not isinstance(text, str)
                or not text
                or not (start < index < completion)
            ):
                return None, (
                    _reason(
                        ResultIntakeRefusalCode.PAYLOAD_MALFORMED,
                        "completed agent message is outside the terminal turn or incomplete",
                    ),
                )
            messages.append((item_id, text))
    if not messages:
        return None, (
            _reason(
                ResultIntakeRefusalCode.PAYLOAD_MISSING,
                "no schema-constrained completed agent message was observed",
            ),
        )
    terminal = messages[-1][1]
    if observation.final_output != terminal:
        return None, (
            _reason(
                ResultIntakeRefusalCode.PAYLOAD_AMBIGUOUS,
                "final output contradicts the canonical JSONL message",
            ),
        )
    return terminal, ()


def _validate_payload_binding(
    compiled: CompiledPrompt, payload: Mapping[str, object]
) -> tuple[ResultIntakeRefusal, ...]:
    values = (
        ("mission_id", compiled.mission_id),
        ("workflow_generation", compiled.workflow_generation),
        ("subject", compiled.subject),
        ("observed_commit", compiled.observed_commit.casefold()),
    )
    reasons: list[ResultIntakeRefusal] = []
    for name, expected in values:
        actual = payload.get(name)
        if name == "observed_commit" and isinstance(actual, str):
            actual = actual.casefold()
        if actual != expected:
            reasons.append(
                _reason(
                    ResultIntakeRefusalCode.PAYLOAD_BINDING_MISMATCH,
                    f"payload {name} differs from immutable execution binding",
                    (name,),
                )
            )
    if compiled.role is not MissionRole.ARCHITECT and payload.get("user_story_id") != compiled.subject:
        reasons.append(
            _reason(
                ResultIntakeRefusalCode.PAYLOAD_BINDING_MISMATCH,
                "payload user_story_id differs from immutable subject",
                ("user_story_id",),
            )
        )
    return tuple(reasons)


def _validate_role_result(
    candidate: RoleResult,
    context: ResultIntakeValidationContext,
    contract_validator: ContractValidator,
) -> ValidationResult:
    role_input = context.role_input
    if isinstance(candidate, ArchitectResult) and isinstance(role_input, ArchitectInput):
        return ArchitectResultValidator(contract_validator).validate(
            candidate,
            architect_input=role_input,
            known_user_story_ids=context.known_user_story_ids,
        )
    if isinstance(candidate, ImplementerResult) and isinstance(role_input, ImplementerInput):
        return ImplementerResultValidator(contract_validator).validate(
            candidate, implementer_input=role_input
        )
    if isinstance(candidate, TesterResult) and isinstance(role_input, TesterInput):
        return TesterResultValidator(contract_validator).validate(
            candidate, tester_input=role_input
        )
    if isinstance(candidate, ReviewerResult) and isinstance(role_input, ReviewerInput):
        return ReviewerResultValidator(contract_validator).validate(
            candidate, reviewer_input=role_input
        )
    if isinstance(candidate, CertifierResult) and isinstance(role_input, CertifierInput):
        return CertifierResultValidator(contract_validator).validate(
            candidate, certifier_input=role_input
        )
    return ValidationResult(
        contract="result-intake",
        errors=(
            ValidationIssue(
                code="ROLE_CONTEXT_TYPE_MISMATCH",
                path=(),
                message="RoleResult and validation context types differ",
            ),
        ),
    )


def _validate_git_consistency(
    role: MissionRole,
    candidate: RoleResult,
    observation: CodexExecutionObservation,
) -> tuple[ResultIntakeRefusal, ...]:
    before = observation.git_before
    after = observation.git_after
    if (
        before is None
        or after is None
        or after.error is not None
        or after.head_commit is None
        or after.clean is None
        or after.changed_paths is None
    ):
        return (
            _reason(
                ResultIntakeRefusalCode.GIT_OBSERVATION_REQUIRED,
                "complete post-execution Git observation is required",
            ),
        )
    if candidate.observed_commit.casefold() != after.head_commit.casefold():
        return (
            _reason(
                ResultIntakeRefusalCode.GIT_COMMIT_MISMATCH,
                "RoleResult commit differs from post-execution Git HEAD",
                ("observed_commit",),
            ),
        )
    if role in _READ_ONLY_ROLES and before != after:
        return (
            _reason(
                ResultIntakeRefusalCode.GIT_SIDE_EFFECT_MISMATCH,
                "read-only role changed or obscured Git state",
            ),
        )
    declared_changes: tuple[str, ...] = ()
    if isinstance(candidate, ImplementerResult):
        declared_changes = candidate.files_changed
    elif isinstance(candidate, TesterResult):
        declared_changes = candidate.test_files_changed
    if role in {MissionRole.IMPLEMENTER, MissionRole.TESTER}:
        if bool(declared_changes) == bool(after.clean):
            return (
                _reason(
                    ResultIntakeRefusalCode.GIT_SIDE_EFFECT_MISMATCH,
                    "declared file changes contradict observable Git cleanliness",
                ),
            )
        try:
            declared = tuple(
                sorted(
                    (
                        _normalize_path(path, allow_directory=False)
                        for path in declared_changes
                    ),
                    key=lambda value: (value.casefold(), value),
                )
            )
            observed = tuple(
                sorted(
                    (
                        _normalize_path(path, allow_directory=False)
                        for path in after.changed_paths
                    ),
                    key=lambda value: (value.casefold(), value),
                )
            )
        except ValueError:
            return (
                _reason(
                    ResultIntakeRefusalCode.GIT_SIDE_EFFECT_MISMATCH,
                    "observed Git paths are unsafe or non-canonical",
                ),
            )
        if declared != observed:
            return (
                _reason(
                    ResultIntakeRefusalCode.GIT_SIDE_EFFECT_MISMATCH,
                    "declared file changes differ from observed Git paths",
                ),
            )
    return ()


def _build_role_result(role: MissionRole, data: Mapping[str, object]) -> RoleResult:
    if role is MissionRole.ARCHITECT:
        return ArchitectResult(
            mission_id=cast(str, data["mission_id"]),
            workflow_generation=cast(int, data["workflow_generation"]),
            subject=cast(str, data["subject"]),
            observed_commit=cast(str, data["observed_commit"]),
            summary=cast(str, data["summary"]),
            assumptions=_strings(data["assumptions"]),
            decisions=tuple(
                ArchitectDecision(
                    ArchitectDecisionKind(cast(str, item["kind"])),
                    cast(str, item["description"]),
                )
                for item in _mappings(data["decisions"])
            ),
            risks=_strings(data["risks"]),
            blockers=_strings(data["blockers"]),
            user_stories=tuple(_user_story(item) for item in _mappings(data["user_stories"])),
            recommended_next_role=MissionRole(cast(str, data["recommended_next_role"])),
            verdict=ArchitectVerdict(cast(str, data["verdict"])),
        )
    if role is MissionRole.IMPLEMENTER:
        return ImplementerResult(
            mission_id=cast(str, data["mission_id"]),
            workflow_generation=cast(int, data["workflow_generation"]),
            subject=cast(str, data["subject"]),
            user_story_id=cast(str, data["user_story_id"]),
            observed_commit=cast(str, data["observed_commit"]),
            summary=cast(str, data["summary"]),
            files_changed=_strings(data["files_changed"]),
            tests_added_or_modified=_strings(data["tests_added_or_modified"]),
            verification_commands=_strings(data["verification_commands"]),
            verification_results=tuple(
                VerificationResult(
                    cast(str, item["command"]),
                    cast(bool, item["required"]),
                    VerificationOutcome(cast(str, item["result"])),
                    cast(int | None, item["exit_code"]),
                    cast(str, item["details"]),
                )
                for item in _mappings(data["verification_results"])
            ),
            assumptions=_strings(data["assumptions"]),
            findings=_strings(data["findings"]),
            blockers=_strings(data["blockers"]),
            recommended_next_role=MissionRole(cast(str, data["recommended_next_role"])),
            verdict=ImplementerVerdict(cast(str, data["verdict"])),
        )
    if role is MissionRole.TESTER:
        plan = _mapping(data["test_plan"])
        return TesterResult(
            mission_id=cast(str, data["mission_id"]),
            workflow_generation=cast(int, data["workflow_generation"]),
            subject=cast(str, data["subject"]),
            user_story_id=cast(str, data["user_story_id"]),
            observed_commit=cast(str, data["observed_commit"]),
            summary=cast(str, data["summary"]),
            test_plan=TesterPlan(*(_strings(plan[name]) for name in (
                "acceptance_criteria", "positive_tests", "negative_tests",
                "edge_cases", "regressions", "commands"
            ))),
            acceptance_results=tuple(
                TesterAcceptanceResult(
                    cast(str, item["acceptance_criterion_id"]),
                    GateResult(cast(str, item["result"])),
                    _strings(item["evidence"]),
                    cast(str, item["notes"]),
                )
                for item in _mappings(data["acceptance_results"])
            ),
            test_cases=tuple(
                TesterTestCase(
                    cast(str, item["id"]), TestCaseType(cast(str, item["type"])),
                    cast(str, item["objective"]), cast(str, item["expected_result"]),
                    cast(str, item["observed_result"]), cast(bool, item["required"]),
                    cast(bool, item["executed"]), GateResult(cast(str, item["verdict"])),
                )
                for item in _mappings(data["test_cases"])
            ),
            test_files_changed=_strings(data["test_files_changed"]),
            verification_commands=_strings(data["verification_commands"]),
            verification_results=tuple(
                TesterVerificationResult(
                    cast(str, item["command"]), cast(bool, item["required"]),
                    cast(bool, item["executed"]), GateResult(cast(str, item["result"])),
                    cast(int | None, item["exit_code"]), cast(str, item["details"]),
                )
                for item in _mappings(data["verification_results"])
            ),
            findings=_strings(data["findings"]), blockers=_strings(data["blockers"]),
            recommended_next_role=MissionRole(cast(str, data["recommended_next_role"])),
            verdict=TesterVerdict(cast(str, data["verdict"])),
        )
    if role is MissionRole.REVIEWER:
        return ReviewerResult(
            mission_id=cast(str, data["mission_id"]),
            workflow_generation=cast(int, data["workflow_generation"]),
            subject=cast(str, data["subject"]), user_story_id=cast(str, data["user_story_id"]),
            observed_commit=cast(str, data["observed_commit"]), summary=cast(str, data["summary"]),
            dimensions_reviewed=tuple(ReviewDimension(cast(str, item)) for item in cast(list[object], data["dimensions_reviewed"])),
            reviewed_paths=_strings(data["reviewed_paths"]),
            findings=tuple(
                ReviewFinding(
                    cast(str, item["id"]), ReviewDimension(cast(str, item["dimension"])),
                    ReviewSeverity(cast(str, item["severity"])), cast(str, item["summary"]),
                    _strings(item["evidence"]), _strings(item["affected_paths"]),
                    cast(bool, item["blocking"]),
                )
                for item in _mappings(data["findings"])
            ),
            blockers=_strings(data["blockers"]),
            recommended_next_role=MissionRole(cast(str, data["recommended_next_role"])),
            verdict=ReviewerVerdict(cast(str, data["verdict"])),
        )
    if role is MissionRole.CERTIFIER:
        human = _mapping(data["human_approval_check"])
        return CertifierResult(
            mission_id=cast(str, data["mission_id"]),
            workflow_generation=cast(int, data["workflow_generation"]),
            subject=cast(str, data["subject"]), user_story_id=cast(str, data["user_story_id"]),
            observed_commit=cast(str, data["observed_commit"]), summary=cast(str, data["summary"]),
            artifact_checks=tuple(
                ArtifactCheck(MissionRole(cast(str, item["artifact_role"])), cast(bool, item["present"]), cast(bool, item["coherent"]), cast(str, item["notes"]))
                for item in _mappings(data["artifact_checks"])
            ),
            acceptance_checks=tuple(
                AcceptanceCheck(cast(str, item["acceptance_criterion_id"]), cast(bool, item["mandatory"]), GateResult(cast(str, item["result"])), _strings(item["evidence_refs"]), cast(str, item["notes"]))
                for item in _mappings(data["acceptance_checks"])
            ),
            gate_checks=tuple(
                GateCheck(cast(str, item["gate_id"]), cast(bool, item["present"]), cast(bool, item["subject_matches"]), GateResult(cast(str, item["result"])), _strings(item["evidence_refs"]), cast(bool, item["commit_matches"]), cast(bool, item["not_applicable_authorized"]), cast(str, item["notes"]))
                for item in _mappings(data["gate_checks"])
            ),
            evidence_refs=_strings(data["evidence_refs"]),
            human_approval_check=HumanApprovalCheck(cast(bool, human["required"]), cast(bool, human["present"]), cast(bool, human["valid"]), cast(str | None, human["evidence_ref"]), cast(str, human["notes"])),
            findings=tuple(CertifierFinding(cast(str, item["code"]), cast(str, item["summary"]), cast(bool, item["demonstrated_failure"])) for item in _mappings(data["findings"])),
            blockers=_strings(data["blockers"]),
            recommended_action=CertifierRecommendedAction(cast(str, data["recommended_action"])),
            verdict=CertifierVerdict(cast(str, data["verdict"])),
        )
    raise ValueError("unsupported RoleResult role")


def _user_story(data: Mapping[str, object]) -> UserStory:
    scope = _mapping(data["scope"])
    approval = _mapping(data["human_approval"])
    metadata = _mapping(data["metadata"])
    return UserStory(
        schema_version=cast(str, data["schema_version"]), id=cast(str, data["id"]),
        title=cast(str, data["title"]), description=cast(str, data["description"]),
        status=UserStoryStatus(cast(str, data["status"])), priority=cast(int, data["priority"]),
        risk=RiskLevel(cast(str, data["risk"])), depends_on=_strings(data["depends_on"]),
        scope=UserStoryScope(_strings(scope["allowed_paths"]), _strings(scope["forbidden_paths"])),
        acceptance_criteria=tuple(AcceptanceCriterion(cast(str, item["id"]), cast(str, item["description"]), cast(bool, item["mandatory"])) for item in _mappings(data["acceptance_criteria"])),
        required_gates=_strings(data["required_gates"]),
        human_approval=HumanApproval(cast(bool, approval["required"]), cast(bool, approval["approved"]), cast(str | None, approval["approved_by"]), _optional_datetime(approval["approved_at"]), cast(str | None, approval.get("evidence_ref"))),
        metadata=UserStoryMetadata(_datetime(metadata["created_at"]), cast(str, metadata["created_by"]), _datetime(metadata["updated_at"])),
    )


def _validation_refusals(result: ValidationResult) -> tuple[ResultIntakeRefusal, ...]:
    return tuple(
        _reason(ResultIntakeRefusalCode.ROLE_VALIDATION_FAILED, f"{issue.code}: {issue.message}", issue.path)
        for issue in result.errors
    )


def _argument_values(arguments: tuple[str, ...], flag: str) -> tuple[str, ...]:
    values: list[str] = []
    for index, argument in enumerate(arguments):
        if argument == flag:
            if index + 1 >= len(arguments):
                values.append("")
            else:
                values.append(arguments[index + 1])
    return tuple(values)


def _same_existing_path(left: str, right: str) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        left_path = Path(left).resolve(strict=True)
        right_path = Path(right).resolve(strict=True)
    except OSError:
        return False
    return os.path.normcase(str(left_path)).casefold() == os.path.normcase(str(right_path)).casefold()


def _path_is_within(root: str, candidate: str) -> bool:
    try:
        root_path = Path(root).resolve(strict=True)
        candidate_path = Path(candidate).resolve(strict=True)
        candidate_path.relative_to(root_path)
    except (OSError, ValueError):
        return False
    return True


def _event_payload(event: CodexJsonlEvent) -> dict[str, object]:
    value = json.loads(
        event.payload_json,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_non_json_constant,
    )
    raw_value = json.loads(
        event.raw_line,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_non_json_constant,
    )
    if not isinstance(value, dict) or not isinstance(raw_value, dict):
        raise ValueError("JSONL event payload must be an object")
    if raw_value != value or event.event_type != value.get("type"):
        raise ValueError("JSONL event representations contradict each other")
    return value


def _events_match_stdout(observation: CodexExecutionObservation) -> bool:
    lines = tuple(observation.stdout.splitlines())
    if len(lines) != len(observation.events):
        return False
    return all(
        event.line_number == index and event.raw_line == line
        for index, (event, line) in enumerate(zip(observation.events, lines), 1)
    )


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> object:
    raise ValueError(f"non-JSON numeric constant: {value}")


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("expected mapping")
    return cast(Mapping[str, object], value)


def _mappings(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        raise TypeError("expected list")
    return tuple(_mapping(item) for item in value)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError("expected string list")
    return tuple(cast(list[str], value))


def _datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("expected datetime string")
    return datetime.fromisoformat(value)


def _optional_datetime(value: object) -> datetime | None:
    return None if value is None else _datetime(value)


def _reason(
    code: ResultIntakeRefusalCode,
    message: str,
    path: tuple[str | int, ...] = (),
) -> ResultIntakeRefusal:
    return ResultIntakeRefusal(code, path, message)


def _refused(
    role: MissionRole,
    diagnostics: ResultIntakeDiagnostics,
    code: ResultIntakeRefusalCode,
    message: str,
    *,
    path: tuple[str | int, ...] = (),
) -> ResultIntakeOutcome:
    return _refused_many(role, diagnostics, (_reason(code, message, path),))


def _refused_many(
    role: MissionRole,
    diagnostics: ResultIntakeDiagnostics,
    reasons: tuple[ResultIntakeRefusal, ...],
) -> ResultIntakeOutcome:
    return ResultIntakeOutcome(False, role, None, reasons, diagnostics)
