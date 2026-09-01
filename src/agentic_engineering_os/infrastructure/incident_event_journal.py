"""Persist incident snapshots through the existing OperationalEventStore."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from agentic_engineering_os.domain import (
    OPERATIONAL_EVENT_SCHEMA_VERSION,
    IncidentRecord,
    IncidentScope,
    OperationalAttribute,
    OperationalCorrelation,
    OperationalEvent,
    OperationalEventPayload,
    OperationalEventType,
    OperationalProvenance,
    OperationalProvenanceKind,
    OperationalSeverity,
    canonical_incident_record_json,
    incident_record_from_json,
)

from .operational_event_store import (
    OperationalEventAppendReceipt,
    OperationalEventQuery,
    OperationalEventStore,
)


class IncidentEventJournalError(RuntimeError):
    """Persisted incident history is malformed or inconsistent."""


class IncidentEventJournal:
    """Explicit append/read adapter; it owns no second persistence mechanism."""

    def __init__(self, event_store: OperationalEventStore) -> None:
        if not isinstance(event_store, OperationalEventStore):
            raise TypeError("event_store must be OperationalEventStore")
        self._store = event_store

    def append(self, record: IncidentRecord) -> OperationalEventAppendReceipt:
        return self._store.append(incident_record_to_operational_event(record))

    def read(self, scope: IncidentScope) -> tuple[IncidentRecord, ...]:
        if not isinstance(scope, IncidentScope):
            raise TypeError("scope must be IncidentScope")
        events = self._store.query(
            OperationalEventQuery(
                event_type=OperationalEventType.OPERATIONAL_ANOMALY,
                project_id=scope.project_id,
                mission_id=scope.mission_id,
            )
        )
        records = tuple(
            record
            for event in events
            if (record := incident_record_from_operational_event(event)) is not None
            and record.scope == scope
        )
        _validate_revision_chains(records)
        return records

    def latest(self, scope: IncidentScope) -> tuple[IncidentRecord, ...]:
        latest: dict[str, IncidentRecord] = {}
        for record in self.read(scope):
            latest[record.incident_id] = record
        return tuple(sorted(latest.values(), key=lambda item: item.incident_id))


def incident_record_to_operational_event(record: IncidentRecord) -> OperationalEvent:
    if not isinstance(record, IncidentRecord):
        raise TypeError("record must be IncidentRecord")
    provenance = (
        OperationalProvenance(
            OperationalProvenanceKind.OPERATOR_HUMAN,
            record.acknowledged_by,
            record.fingerprint,
        )
        if record.acknowledged_by is not None
        else OperationalProvenance(
            OperationalProvenanceKind.DETERMINISTIC_COMPONENT,
            "incident-manager",
            record.fingerprint,
        )
    )
    return OperationalEvent(
        OPERATIONAL_EVENT_SCHEMA_VERSION,
        str(uuid5(NAMESPACE_URL, record.fingerprint)),
        OperationalEventType.OPERATIONAL_ANOMALY,
        record.updated_at,
        (
            OperationalSeverity.CRITICAL
            if record.severity.value == "CRITICAL"
            else OperationalSeverity.ERROR
        ),
        "incident-manager",
        record.scope.project_id,
        OperationalCorrelation(
            mission_id=record.correlation.mission_id,
            workflow_generation=record.correlation.workflow_generation,
            user_story_id=record.correlation.user_story_id,
            role=record.correlation.role,
            execution_id=record.correlation.execution_id,
            assignment_id=record.correlation.assignment_id,
            repository_commit=record.scope.repository_head,
        ),
        OperationalEventPayload(
            "DETECTED",
            outcome=record.state.value,
            reason_code=record.classification.value,
            attributes=(
                OperationalAttribute(
                    "incident_record", canonical_incident_record_json(record)
                ),
            ),
        ),
        provenance,
    )


def incident_record_from_operational_event(
    event: OperationalEvent,
) -> IncidentRecord | None:
    if not isinstance(event, OperationalEvent):
        raise TypeError("event must be OperationalEvent")
    attributes = {item.name: item.value for item in event.payload.attributes}
    raw = attributes.get("incident_record")
    if raw is None:
        return None
    if (
        event.event_type is not OperationalEventType.OPERATIONAL_ANOMALY
        or event.payload.operation != "DETECTED"
        or not isinstance(raw, str)
    ):
        raise IncidentEventJournalError("incident event envelope is invalid")
    try:
        record = incident_record_from_json(raw)
    except ValueError as error:
        raise IncidentEventJournalError("persisted incident record is invalid") from error
    expected = incident_record_to_operational_event(record)
    if event != expected:
        raise IncidentEventJournalError("incident event envelope contradicts record")
    return record


def _validate_revision_chains(records: tuple[IncidentRecord, ...]) -> None:
    by_id: dict[str, list[IncidentRecord]] = {}
    for record in records:
        by_id.setdefault(record.incident_id, []).append(record)
    for revisions in by_id.values():
        if [item.revision for item in revisions] != list(range(1, len(revisions) + 1)):
            raise IncidentEventJournalError("incident revision sequence is incomplete")
        for before, after in zip(revisions, revisions[1:]):
            if after.previous_fingerprint != before.fingerprint:
                raise IncidentEventJournalError("incident revision chain is broken")
