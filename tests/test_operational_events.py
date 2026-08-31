from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_engineering_os.application.contract_validator import ContractValidator
from agentic_engineering_os.domain import (
    AuditEvent,
    MissionRole,
    OperationalAttribute,
    OperationalCorrelation,
    OperationalEvent,
    OperationalEventError,
    OperationalEventPayload,
    OperationalEventType,
    OperationalProvenance,
    OperationalProvenanceKind,
    OperationalSeverity,
    canonical_operational_event_json,
    operational_event_fingerprint,
    operational_event_from_dict,
    operational_event_to_dict,
)


ROOT = Path(__file__).resolve().parents[1]
EVENT_ID = "12345678-1234-4234-8234-123456789abc"


def _correlation(**changes: object) -> OperationalCorrelation:
    values: dict[str, object] = {
        "mission_id": None,
        "workflow_generation": None,
        "user_story_id": None,
        "role": None,
        "execution_id": None,
        "assignment_id": None,
        "wave_index": None,
        "group_index": None,
        "gate_id": None,
        "certification_id": None,
        "repository_commit": None,
    }
    values.update(changes)
    return OperationalCorrelation(**values)  # type: ignore[arg-type]


def _event(
    *,
    event_type: OperationalEventType = OperationalEventType.OPERATIONAL_ANOMALY,
    operation: str = "DETECTED",
    correlation: OperationalCorrelation | None = None,
    payload: OperationalEventPayload | None = None,
    provenance: OperationalProvenance | None = None,
) -> OperationalEvent:
    return OperationalEvent(
        schema_version="1.0",
        event_id=EVENT_ID,
        event_type=event_type,
        occurred_at=datetime(2026, 8, 31, 10, 30, tzinfo=timezone.utc),
        severity=OperationalSeverity.WARNING,
        source_component="runtime.observer",
        project_id="agentic-engineering-os",
        correlation=correlation or OperationalCorrelation(),
        payload=payload or OperationalEventPayload(operation=operation),
        provenance=provenance
        or OperationalProvenance(
            kind=OperationalProvenanceKind.DETERMINISTIC_COMPONENT,
            producer="RuntimeObserver",
        ),
    )


def _mission_correlation(**changes: object) -> OperationalCorrelation:
    return _correlation(mission_id="P6.2", workflow_generation=2, **changes)


def test_minimal_event_is_immutable_and_round_trips() -> None:
    event = _event()

    with pytest.raises(FrozenInstanceError):
        event.project_id = "changed"  # type: ignore[misc]

    assert operational_event_from_dict(operational_event_to_dict(event)) == event


@pytest.mark.parametrize(
    ("event_type", "operation", "correlation"),
    [
        (OperationalEventType.MISSION_LIFECYCLE, "STARTED", _mission_correlation()),
        (
            OperationalEventType.ROLE_EXECUTION,
            "FINISHED",
            _mission_correlation(role=MissionRole.TESTER),
        ),
        (
            OperationalEventType.CODEX_EXECUTION,
            "FINISHED",
            _mission_correlation(role=MissionRole.IMPLEMENTER, execution_id="exec-1"),
        ),
        (
            OperationalEventType.WORKTREE_LIFECYCLE,
            "CREATED",
            _mission_correlation(user_story_id="US-1", assignment_id="assignment-1"),
        ),
        (
            OperationalEventType.INTEGRATION_GATE,
            "EVALUATED",
            _mission_correlation(wave_index=1, group_index=0, gate_id="gate-1"),
        ),
        (
            OperationalEventType.MERGE_OPERATION,
            "FINISHED",
            _mission_correlation(wave_index=1, group_index=0),
        ),
        (
            OperationalEventType.CONTROL_PLANE_DECISION,
            "GATE_EVALUATED",
            _mission_correlation(gate_id="gate-1"),
        ),
        (
            OperationalEventType.REMEDIATION_RECOVERY,
            "RECOVERY_REQUIRED",
            _mission_correlation(),
        ),
        (
            OperationalEventType.HUMAN_WAITING,
            "WAITING_STARTED",
            _mission_correlation(),
        ),
        (OperationalEventType.PERSISTENCE_FAILURE, "WRITE_FAILED", _correlation()),
        (OperationalEventType.ADOPTION_MIGRATION, "REFUSED", _correlation()),
        (OperationalEventType.OPERATIONAL_ANOMALY, "DETECTED", _correlation()),
    ],
)
def test_closed_event_families_accept_their_canonical_operation(
    event_type: OperationalEventType,
    operation: str,
    correlation: OperationalCorrelation,
) -> None:
    assert _event(
        event_type=event_type,
        operation=operation,
        correlation=correlation,
    ).event_type is event_type


def test_canonical_serialization_and_fingerprint_are_deterministic_with_unicode() -> None:
    payload = OperationalEventPayload(
        operation="DETECTED",
        outcome="observé",
        attributes=(OperationalAttribute("label", "échec contrôlé"),),
    )
    event = _event(payload=payload)

    serialized = canonical_operational_event_json(event)

    assert "échec contrôlé" in serialized
    assert serialized == canonical_operational_event_json(event)
    assert operational_event_fingerprint(event) == operational_event_fingerprint(event)
    assert len(operational_event_fingerprint(event)) == 64
    assert operational_event_fingerprint(replace(event, event_id="22345678-1234-4234-8234-123456789abc")) != operational_event_fingerprint(event)


def test_schema_and_contract_validator_accept_valid_fixture() -> None:
    candidate = json.loads(
        (ROOT / "tests/fixtures/valid/operational-event.json").read_text(encoding="utf-8")
    )

    assert ContractValidator().validate("operational-event", candidate).is_valid


@pytest.mark.parametrize("field", ["project_id", "event_id", "severity", "event_type"])
def test_required_or_closed_root_values_are_rejected(field: str) -> None:
    candidate = operational_event_to_dict(_event())
    if field == "project_id":
        del candidate[field]
    else:
        candidate[field] = "invalid"

    assert not ContractValidator().validate("operational-event", candidate).is_valid


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-08-31T10:30:00",
        "2026-08-31T12:30:00+02:00",
        "2026-08-31 10:30:00Z",
        "2026-08-31T10:30:00.0000000Z",
        "not-a-date",
    ],
)
def test_non_utc_or_invalid_timestamp_is_rejected(timestamp: str) -> None:
    candidate = operational_event_to_dict(_event())
    candidate["occurred_at"] = timestamp
    assert not ContractValidator().validate("operational-event", candidate).is_valid

    with pytest.raises(OperationalEventError):
        replace(_event(), occurred_at=datetime(2026, 8, 31, 12, 30))


@pytest.mark.parametrize(
    "correlation",
    [
        {"workflow_generation": 1},
        {"mission_id": "mission", "execution_id": "exec"},
        {"mission_id": "mission", "assignment_id": "assignment"},
        {"mission_id": "mission", "wave_index": 1},
    ],
)
def test_impossible_correlation_combinations_are_rejected(correlation: dict[str, object]) -> None:
    with pytest.raises(OperationalEventError, match="INVALID_CORRELATION"):
        _correlation(**correlation)


def test_family_specific_correlation_and_operation_are_enforced() -> None:
    with pytest.raises(OperationalEventError, match="INVALID_CORRELATION"):
        _event(event_type=OperationalEventType.CODEX_EXECUTION, operation="STARTED")
    with pytest.raises(OperationalEventError, match="INVALID_EVENT_OPERATION"):
        _event(operation="STARTED")


def test_unknown_or_nested_payload_is_rejected_without_coercion() -> None:
    candidate = operational_event_to_dict(_event())
    candidate["payload"]["authority"] = "CERTIFIED"  # type: ignore[index]
    assert not ContractValidator().validate("operational-event", candidate).is_valid

    candidate = operational_event_to_dict(_event())
    candidate["payload"]["attributes"] = [{"name": "nested", "value": {"x": 1}}]  # type: ignore[index]
    assert not ContractValidator().validate("operational-event", candidate).is_valid


@pytest.mark.parametrize("value", [b"raw", object(), float("nan"), float("inf")])
def test_non_json_or_non_finite_attribute_is_rejected(value: object) -> None:
    with pytest.raises(OperationalEventError, match="INVALID_PAYLOAD"):
        OperationalAttribute("result", value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "-----BEGIN PRIVATE KEY-----",
        "AKIAABCDEFGHIJKLMNOP",
        "Bearer abcdefghijklmnopqrstuvwxyz",
        "token=topsecretvalue",
    ],
)
def test_secret_like_values_are_rejected_before_event_construction(value: str) -> None:
    with pytest.raises(OperationalEventError, match="SECRET_MATERIAL"):
        OperationalAttribute("result", value)


@pytest.mark.parametrize(
    "name", ["environment", "stdout", "stderr", "access_token", "client_secret"]
)
def test_environment_output_and_credential_fields_are_rejected(name: str) -> None:
    with pytest.raises(OperationalEventError, match="SENSITIVE_FIELD"):
        OperationalAttribute(name, "redacted")


def test_payload_bounds_and_canonical_attribute_order_are_enforced() -> None:
    with pytest.raises(OperationalEventError, match="PAYLOAD_TOO_LARGE"):
        OperationalAttribute("message", "x" * 2049)
    with pytest.raises(OperationalEventError, match="PAYLOAD_TOO_LARGE"):
        OperationalEventPayload(
            operation="DETECTED",
            attributes=tuple(OperationalAttribute(f"item_{index}", index) for index in range(33)),
        )
    with pytest.raises(OperationalEventError, match="NON_CANONICAL_PAYLOAD"):
        OperationalEventPayload(
            operation="DETECTED",
            attributes=(OperationalAttribute("z", 1), OperationalAttribute("a", 2)),
        )


def test_total_serialized_size_is_bounded() -> None:
    attributes = tuple(
        OperationalAttribute(f"item_{index:02d}", "x" * 2048) for index in range(9)
    )
    with pytest.raises(OperationalEventError, match="EVENT_TOO_LARGE"):
        _event(payload=OperationalEventPayload(operation="DETECTED", attributes=attributes))


@pytest.mark.parametrize("producer", ["Codex/FakeHuman", "codex/FakeHuman", "CODEX/FakeHuman"])
def test_codex_cannot_be_presented_as_human_provenance(producer: str) -> None:
    with pytest.raises(OperationalEventError, match="INVALID_PROVENANCE"):
        OperationalProvenance(
            kind=OperationalProvenanceKind.OPERATOR_HUMAN,
            producer=producer,
        )


def test_legitimate_human_and_codex_provenance_remain_non_authoritative() -> None:
    human = OperationalProvenance(
        kind=OperationalProvenanceKind.OPERATOR_HUMAN,
        producer="Human/Alice",
    )
    codex = OperationalProvenance(
        kind=OperationalProvenanceKind.CODEX_RUNTIME,
        producer="Codex/Implementer",
    )

    assert _event(provenance=human).provenance is human
    assert _event(provenance=codex).provenance is codex
    for forbidden in ("to_evidence", "to_audit_event", "to_gate", "to_certification"):
        assert not hasattr(_event(), forbidden)


def test_operational_event_is_distinct_from_authoritative_audit_event() -> None:
    event = _event()
    assert not isinstance(event, AuditEvent)
    assert "authority" not in operational_event_to_dict(event)
    assert "result" not in operational_event_to_dict(event)


def test_root_and_packaged_schemas_are_identical() -> None:
    root_schema = ROOT / "schemas/operational-event.schema.json"
    packaged_schema = ROOT / "src/agentic_engineering_os/resources/schemas/operational-event.schema.json"
    assert root_schema.read_bytes() == packaged_schema.read_bytes()
