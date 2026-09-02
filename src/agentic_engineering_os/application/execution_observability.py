"""Read-only projection of durable Codex execution outcomes into observability."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from agentic_engineering_os.domain import (
    OPERATIONAL_EVENT_SCHEMA_VERSION,
    OperationalCorrelation,
    OperationalEvent,
    OperationalEventPayload,
    OperationalEventType,
    OperationalProvenance,
    OperationalProvenanceKind,
    OperationalSeverity,
)

from .execution_state import (
    CodexExecutionLedger,
    CodexExecutionRecord,
    CodexExecutionStatus,
    ExecutionLedgerStorePort,
)


class OperationalEventSourcePort(Protocol):
    def read(self) -> tuple[OperationalEvent, ...]: ...


class ExecutionObservabilityError(RuntimeError):
    """The execution ledger cannot be projected without ambiguity."""


_TERMINAL_OPERATIONS = {
    CodexExecutionStatus.VALIDATED: "FINISHED",
    CodexExecutionStatus.FAILED: "FAILED",
    CodexExecutionStatus.INTERRUPTED: "INTERRUPTED",
}


class ExecutionOperationalEventReader:
    """Combine stored events with a stable, non-authoritative ledger projection."""

    def __init__(
        self,
        event_source: OperationalEventSourcePort,
        execution_store: ExecutionLedgerStorePort,
        *,
        project_id: str,
        repository_root: Path,
    ) -> None:
        self._event_source = event_source
        self._execution_store = execution_store
        self._project_id = project_id
        self._repository_root = repository_root.resolve()
        self._snapshot: tuple[OperationalEvent, ...] | None = None

    def read(self) -> tuple[OperationalEvent, ...]:
        if self._snapshot is not None:
            return self._snapshot
        stored = self._event_source.read()
        ledger = self._execution_store.load()
        projected = project_terminal_execution_events(
            ledger,
            project_id=self._project_id,
            repository_root=self._repository_root,
        )
        records = {item.execution_id: item for item in ledger.records}
        retained: list[OperationalEvent] = []
        projected_keys = {_lifecycle_key(item) for item in projected}
        for event in stored:
            if event.event_type is not OperationalEventType.CODEX_EXECUTION:
                retained.append(event)
                continue
            execution_id = event.correlation.execution_id
            record = records.get(execution_id or "")
            if record is None or not _correlation_matches_record(event, record):
                raise ExecutionObservabilityError(
                    "stored Codex event has no exact durable execution binding"
                )
            if _lifecycle_key(event) not in projected_keys:
                retained.append(event)
        self._snapshot = tuple(
            sorted(
                (*retained, *projected),
                key=lambda item: (
                    item.occurred_at,
                    0 if item.payload.operation == "STARTED" else 1,
                    item.event_id,
                ),
            )
        )
        return self._snapshot

    def retention_exhausted(self) -> bool:
        probe = getattr(self._event_source, "retention_exhausted", None)
        if not callable(probe):
            return False
        return bool(probe())


def project_terminal_execution_events(
    ledger: CodexExecutionLedger,
    *,
    project_id: str,
    repository_root: Path,
) -> tuple[OperationalEvent, ...]:
    """Project only factual terminal records, with deterministic event identities."""

    expected_root = _path_key(repository_root)
    events: list[OperationalEvent] = []
    for record in ledger.records:
        if _path_key(Path(record.repository_root)) != expected_root:
            raise ExecutionObservabilityError(
                "execution record belongs to a foreign repository"
            )
        operation = _TERMINAL_OPERATIONS.get(record.status)
        if operation is None:
            continue
        if record.observation is None:
            raise ExecutionObservabilityError(
                "terminal execution is missing its durable observation"
            )
        events.append(_event(record, project_id, "STARTED"))
        events.append(_event(record, project_id, operation))
    return tuple(events)


def _event(
    record: CodexExecutionRecord,
    project_id: str,
    operation: str,
) -> OperationalEvent:
    observation = record.observation
    assert observation is not None
    terminal = operation != "STARTED"
    reason = _reason_code(record) if terminal and operation != "FINISHED" else None
    identity = json.dumps(
        {
            "project_id": project_id,
            "execution_id": record.execution_id,
            "operation": operation,
            "semantic_fingerprint": record.semantic_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return OperationalEvent(
        schema_version=OPERATIONAL_EVENT_SCHEMA_VERSION,
        event_id=str(uuid5(NAMESPACE_URL, identity)),
        event_type=OperationalEventType.CODEX_EXECUTION,
        occurred_at=(observation.ended_at if terminal else observation.started_at),
        severity=(
            OperationalSeverity.ERROR
            if operation == "FAILED"
            else OperationalSeverity.WARNING
            if operation == "INTERRUPTED"
            else OperationalSeverity.INFO
        ),
        source_component="execution-state-projector",
        project_id=project_id,
        correlation=OperationalCorrelation(
            mission_id=record.mission_id,
            workflow_generation=record.workflow_generation,
            role=record.role,
            execution_id=record.execution_id,
            repository_commit=_repository_commit(
                observation.git_after.head_commit
                if terminal
                else observation.git_before.head_commit,
                record.expected_commit,
            ),
        ),
        payload=OperationalEventPayload(operation=operation, reason_code=reason),
        provenance=OperationalProvenance(
            OperationalProvenanceKind.DETERMINISTIC_COMPONENT,
            "ExecutionStateProjector",
            f"execution-state:{record.execution_id}",
        ),
    )


def _reason_code(record: CodexExecutionRecord) -> str:
    observation = record.observation
    assert observation is not None
    if observation.timed_out:
        return "TIMEOUT"
    if observation.interrupted:
        return "INTERRUPTED"
    if observation.tool_failure_observed:
        return "TOOL_FAILURE_EXIT_ZERO" if observation.exit_code == 0 else "TOOL_FAILURE"
    if observation.exit_code is None:
        return "PROCESS_OUTCOME_MISSING"
    if observation.exit_code != 0:
        return "PROCESS_FAILURE"
    return "RESULT_INTAKE_FAILED"


def _correlation_matches_record(
    event: OperationalEvent, record: CodexExecutionRecord
) -> bool:
    correlation = event.correlation
    observation = record.observation
    commits = {record.expected_commit}
    if observation is not None:
        commits.update(
            item
            for item in (
                observation.git_before.head_commit,
                observation.git_after.head_commit,
            )
            if isinstance(item, str)
        )
    return (
        correlation.mission_id == record.mission_id
        and correlation.workflow_generation == record.workflow_generation
        and correlation.role is record.role
        and correlation.execution_id == record.execution_id
        and correlation.repository_commit in commits
    )


def _lifecycle_key(event: OperationalEvent) -> tuple[object, ...]:
    correlation = event.correlation
    return (
        event.project_id,
        correlation.mission_id,
        correlation.workflow_generation,
        correlation.role,
        correlation.execution_id,
        event.payload.operation,
    )


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path)).casefold()


def _repository_commit(observed: str | None, expected: str) -> str:
    return observed if isinstance(observed, str) else expected
