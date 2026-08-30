"""Restart-safe lifecycle around the single-provider Codex runtime."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from agentic_engineering_os.domain import to_dict

from .codex_runtime import CodexExecutionBinding, CodexExecutionObservation, CodexRuntimePort, GitExecutionObservation
from .execution_state import (
    EXECUTION_LEDGER_VERSION,
    CodexExecutionLedger,
    CodexExecutionRecord,
    CodexExecutionStatus,
    ExecutionExecutableIdentity,
    ExecutionGitObserverPort,
    ExecutionLedgerStorePort,
    ExecutionStateError,
    RestartDisposition,
    RestartInspection,
    _issue_execution_write,
    canonical_result_json,
    compiled_prompt_fingerprint,
    derive_execution_id,
    result_json_fingerprint,
    semantic_execution_fingerprint,
)
from .prompt_compiler import CompiledPrompt
from .result_intake import CodexResultIntake, ResultIntakeOutcome, ResultIntakeValidationContext


class RestartSafeCodexExecutionService:
    """Persist before effects, then classify restart without blind retry."""

    def __init__(
        self,
        store: ExecutionLedgerStorePort,
        runtime: CodexRuntimePort,
        git_observer: ExecutionGitObserverPort,
        *,
        intake: CodexResultIntake | None = None,
        clock=None,
    ) -> None:
        self._store = store
        self._runtime = runtime
        self._git = git_observer
        self._intake = intake or CodexResultIntake()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def plan(
        self,
        compiled: CompiledPrompt,
        binding: CodexExecutionBinding,
        executable: ExecutionExecutableIdentity,
    ) -> CodexExecutionRecord:
        _validate_compiled_binding(compiled, binding)
        _validate_executable_binding(executable)
        ledger = self._store.load()
        execution_id = derive_execution_id(compiled)
        for record in ledger.records:
            if record.execution_id == execution_id:
                return _require_exact_record(record, compiled, binding, executable)
        semantic = semantic_execution_fingerprint(compiled)
        if any(record.semantic_fingerprint == semantic or record.request_id == compiled.request_id for record in ledger.records):
            raise ExecutionStateError("DUPLICATE_EXECUTION", "request or semantic work already has an execution record")
        now = self._now()
        record = CodexExecutionRecord(
            execution_id=execution_id,
            semantic_fingerprint=semantic,
            request_id=compiled.request_id,
            context_fingerprint=compiled.context_fingerprint,
            mission_id=compiled.mission_id,
            workflow_generation=compiled.workflow_generation,
            role=compiled.role,
            subject=compiled.subject,
            repository_root=compiled.repository_root,
            worktree_path=compiled.worktree_path,
            cwd=binding.cwd,
            expected_commit=binding.expected_commit.casefold(),
            compiled_prompt_fingerprint=compiled_prompt_fingerprint(compiled),
            expected_result_contract=compiled.expected_result_contract,
            executable=executable,
            status=CodexExecutionStatus.PLANNED,
            created_at=now,
            updated_at=now,
        )
        candidate = _with_record(ledger, record)
        self._persist(ledger, candidate, "PLAN")
        return record

    def execute(
        self,
        execution_id: str,
        compiled: CompiledPrompt,
        binding: CodexExecutionBinding,
        *,
        cancellation: Event | None = None,
    ) -> CodexExecutionObservation:
        ledger, record = self._bound_record(execution_id, compiled, binding)
        if record.status is not CodexExecutionStatus.PLANNED:
            raise ExecutionStateError("BLIND_RETRY_FORBIDDEN", f"execution is already {record.status.value}")
        running = replace(record, status=CodexExecutionStatus.RUNNING, updated_at=self._now())
        self._persist(ledger, _with_record(ledger, running), "MARK_RUNNING")
        try:
            observation = self._runtime.execute(compiled, binding, cancellation=cancellation)
        except Exception as error:
            raise ExecutionStateError(
                "RUNTIME_OUTCOME_UNCERTAIN",
                f"runtime stopped without a durable observation: {type(error).__name__}: {error}",
            ) from error
        status, reasons = _observation_status(observation)
        current = self._store.load()
        recorded = replace(
            _find(current, execution_id),
            status=status,
            updated_at=self._now(),
            observation=observation,
            failure_reasons=reasons,
        )
        try:
            self._persist(current, _with_record(current, recorded), "RECORD_OBSERVATION")
        except Exception as error:
            raise ExecutionStateError(
                "OBSERVATION_PERSISTENCE_FAILED",
                "runtime observation was returned but could not be durably recorded",
            ) from error
        return observation

    def replay_intake(
        self,
        execution_id: str,
        compiled: CompiledPrompt,
        validation_context: ResultIntakeValidationContext,
    ) -> ResultIntakeOutcome:
        ledger = self._store.load()
        record = _find(ledger, execution_id)
        _require_compiled(record, compiled)
        if record.status is not CodexExecutionStatus.OBSERVED or record.observation is None:
            raise ExecutionStateError("INTAKE_REPLAY_FORBIDDEN", "only an OBSERVED execution can enter intake")
        outcome = self._intake.process(compiled, record.observation, validation_context)
        if outcome.accepted:
            assert outcome.validated_result is not None
            result_json = canonical_result_json(to_dict(outcome.validated_result))
            updated = replace(
                record,
                status=CodexExecutionStatus.VALIDATED,
                updated_at=self._now(),
                validated_result_json=result_json,
                validated_result_fingerprint=result_json_fingerprint(result_json),
            )
        else:
            reasons = tuple(f"{item.code.value}: {item.message}" for item in outcome.refusal_reasons)
            updated = replace(record, status=CodexExecutionStatus.FAILED, updated_at=self._now(), failure_reasons=reasons)
        try:
            self._persist(ledger, _with_record(ledger, updated), "RECORD_INTAKE")
        except Exception as error:
            raise ExecutionStateError("INTAKE_PERSISTENCE_FAILED", "intake outcome could not be durably recorded") from error
        return outcome

    def revalidate_completed(
        self,
        execution_id: str,
        compiled: CompiledPrompt,
        validation_context: ResultIntakeValidationContext,
    ) -> ResultIntakeOutcome:
        """Rehydrate a completed result through P4.6 without mutating or rerunning."""

        ledger = self._store.load()
        record = _find(ledger, execution_id)
        _require_compiled(record, compiled)
        if (
            record.status is not CodexExecutionStatus.VALIDATED
            or record.observation is None
            or record.validated_result_json is None
            or record.validated_result_fingerprint is None
        ):
            raise ExecutionStateError(
                "COMPLETED_REVALIDATION_FORBIDDEN",
                "only a complete VALIDATED execution can be revalidated",
            )
        outcome = self._intake.process(
            compiled, record.observation, validation_context
        )
        if not outcome.accepted or outcome.validated_result is None:
            raise ExecutionStateError(
                "COMPLETED_RESULT_STALE",
                "persisted completed result no longer passes deterministic intake",
            )
        canonical = canonical_result_json(to_dict(outcome.validated_result))
        if (
            canonical != record.validated_result_json
            or result_json_fingerprint(canonical)
            != record.validated_result_fingerprint
        ):
            raise ExecutionStateError(
                "COMPLETED_RESULT_STALE",
                "persisted completed result differs from its revalidated form",
            )
        return outcome

    def inspect_restart(
        self,
        execution_id: str,
        compiled: CompiledPrompt,
        binding: CodexExecutionBinding,
        executable: ExecutionExecutableIdentity,
        *,
        validation_context: ResultIntakeValidationContext | None = None,
    ) -> RestartInspection:
        ledger = self._store.load()
        record = _find(ledger, execution_id)
        current_git = self._git.observe(record.cwd)
        try:
            _validate_compiled_binding(compiled, binding)
            _require_exact_record(record, compiled, binding, executable)
        except ExecutionStateError as error:
            return _inspection(record, current_git, RestartDisposition.STALE_OR_INCONSISTENT, (str(error),))
        baseline_clean = _git_exact(current_git, record.expected_commit)
        if record.status is CodexExecutionStatus.PLANNED:
            disposition = RestartDisposition.SAFE_NOT_STARTED if baseline_clean else RestartDisposition.STALE_OR_INCONSISTENT
            return _inspection(record, current_git, disposition, (() if baseline_clean else ("Git no longer matches the planned baseline",)), can_execute=baseline_clean)
        if record.status is CodexExecutionStatus.RUNNING:
            disposition = RestartDisposition.NEW_REQUEST_REQUIRED if baseline_clean else RestartDisposition.RECOVERY_REQUIRED
            return _inspection(record, current_git, disposition, ("RUNNING has no durable terminal observation; same-request retry is forbidden",), operator=not baseline_clean)
        if record.status is CodexExecutionStatus.OBSERVED:
            replayable = record.observation is not None and _git_same(current_git, record.observation.git_after)
            disposition = RestartDisposition.INTAKE_REPLAY_AVAILABLE if replayable else RestartDisposition.RECOVERY_REQUIRED
            return _inspection(record, current_git, disposition, (() if replayable else ("Git differs from the persisted post-execution observation",)), can_replay=replayable, operator=not replayable)
        if record.status is CodexExecutionStatus.VALIDATED:
            if validation_context is None or record.observation is None:
                return _inspection(record, current_git, RestartDisposition.STALE_OR_INCONSISTENT, ("validated replay context is absent",))
            try:
                self.revalidate_completed(
                    execution_id, compiled, validation_context
                )
            except ExecutionStateError:
                return _inspection(record, current_git, RestartDisposition.STALE_OR_INCONSISTENT, ("persisted validated result no longer passes deterministic intake",))
            valid = _git_same(current_git, record.observation.git_after)
            disposition = RestartDisposition.VALIDATED_NO_RERUN if valid else RestartDisposition.STALE_OR_INCONSISTENT
            return _inspection(record, current_git, disposition, (() if valid else ("validated record or current Git is inconsistent",)))
        clean_after = record.observation is not None and _git_same(current_git, record.observation.git_after) and baseline_clean
        disposition = RestartDisposition.NEW_REQUEST_REQUIRED if clean_after else RestartDisposition.RECOVERY_REQUIRED
        return _inspection(record, current_git, disposition, ("terminal unsuccessful execution cannot be retried under the same request",), operator=not clean_after)

    def _bound_record(self, execution_id: str, compiled: CompiledPrompt, binding: CodexExecutionBinding) -> tuple[CodexExecutionLedger, CodexExecutionRecord]:
        _validate_compiled_binding(compiled, binding)
        ledger = self._store.load()
        record = _find(ledger, execution_id)
        _require_exact_record(record, compiled, binding, record.executable)
        return ledger, record

    def _persist(self, before: CodexExecutionLedger, after: CodexExecutionLedger, operation: str) -> None:
        authorization = _issue_execution_write(store=self._store, before=before, after=after, operation=operation)
        self._store._replace_authorized(after, authorization=authorization, operation=operation)

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ExecutionStateError("INVALID_CLOCK", "execution clock must return UTC")
        return value


def _validate_compiled_binding(compiled: CompiledPrompt, binding: CodexExecutionBinding) -> None:
    if not isinstance(compiled, CompiledPrompt) or not isinstance(binding, CodexExecutionBinding):
        raise ExecutionStateError("INVALID_INPUT", "compiled prompt and execution binding types are required")
    pairs = (
        (compiled.request_id, binding.request_id), (compiled.context_fingerprint, binding.context_fingerprint),
        (compiled.mission_id, binding.mission_id), (compiled.workflow_generation, binding.workflow_generation),
        (compiled.role, binding.role), (compiled.subject, binding.subject),
        (compiled.observed_commit.casefold(), binding.expected_commit.casefold()),
    )
    if any(left != right for left, right in pairs) or _path_key(binding.cwd) != _path_key(compiled.worktree_path or compiled.repository_root):
        raise ExecutionStateError("EXECUTION_BINDING_MISMATCH", "compiled prompt differs from execution binding")


def _validate_executable_binding(value: ExecutionExecutableIdentity) -> None:
    if not isinstance(value, ExecutionExecutableIdentity) or not Path(value.path).is_absolute() or not value.version or len(value.sha256) != 64 or any(c not in "0123456789abcdef" for c in value.sha256):
        raise ExecutionStateError("INVALID_EXECUTABLE_IDENTITY", "exact executable path, version and lowercase SHA-256 are required")


def _require_exact_record(record: CodexExecutionRecord, compiled: CompiledPrompt, binding: CodexExecutionBinding, executable: ExecutionExecutableIdentity) -> CodexExecutionRecord:
    _require_compiled(record, compiled)
    _validate_compiled_binding(compiled, binding)
    _validate_executable_binding(executable)
    if record.executable != executable or _path_key(record.cwd) != _path_key(binding.cwd) or record.expected_commit != binding.expected_commit.casefold():
        raise ExecutionStateError("EXECUTION_RECORD_MISMATCH", "persisted execution identity differs")
    return record


def _require_compiled(record: CodexExecutionRecord, compiled: CompiledPrompt) -> None:
    if record.execution_id != derive_execution_id(compiled) or record.semantic_fingerprint != semantic_execution_fingerprint(compiled) or record.compiled_prompt_fingerprint != compiled_prompt_fingerprint(compiled):
        raise ExecutionStateError("STALE_COMPILED_PROMPT", "compiled prompt does not match the execution record")


def _observation_status(observation: CodexExecutionObservation) -> tuple[CodexExecutionStatus, tuple[str, ...]]:
    if observation.timed_out or observation.interrupted:
        return CodexExecutionStatus.INTERRUPTED, ("execution timed out or was interrupted",)
    if observation.process_id is None or observation.exit_code is None or observation.exit_code != 0 or observation.tool_failure_observed:
        return CodexExecutionStatus.FAILED, ("transport did not complete successfully",)
    return CodexExecutionStatus.OBSERVED, ()


def _with_record(ledger: CodexExecutionLedger, record: CodexExecutionRecord) -> CodexExecutionLedger:
    records = {item.execution_id: item for item in ledger.records}
    records[record.execution_id] = record
    return CodexExecutionLedger(EXECUTION_LEDGER_VERSION, tuple(records[key] for key in sorted(records)))


def _find(ledger: CodexExecutionLedger, execution_id: str) -> CodexExecutionRecord:
    matches = [item for item in ledger.records if item.execution_id == execution_id]
    if len(matches) != 1:
        raise ExecutionStateError("EXECUTION_NOT_FOUND", "exactly one execution record is required")
    return matches[0]


def _git_exact(actual: GitExecutionObservation, expected: str) -> bool:
    return (
        actual.error is None
        and actual.head_commit == expected.casefold()
        and actual.clean is True
        and actual.changed_paths == ()
    )


def _git_same(actual: GitExecutionObservation, expected: GitExecutionObservation | None) -> bool:
    return (
        expected is not None
        and actual.error is None
        and expected.error is None
        and actual.head_commit == expected.head_commit
        and actual.clean == expected.clean
        and actual.clean is not None
        and actual.changed_paths == expected.changed_paths
        and actual.changed_paths is not None
    )


def _inspection(record: CodexExecutionRecord, git: GitExecutionObservation, disposition: RestartDisposition, reasons: tuple[str, ...], *, can_execute: bool = False, can_replay: bool = False, operator: bool = False) -> RestartInspection:
    return RestartInspection(record.execution_id, record.status, disposition, git, can_execute, can_replay, False, operator, reasons)


def _path_key(value: str) -> str:
    return os.path.normcase(str(Path(value).resolve(strict=False))).casefold()
