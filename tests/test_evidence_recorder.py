from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    ContractValidator,
    EvidenceObservation,
    EvidenceProvenance,
    EvidenceRecorder,
    EvidenceRecordingError,
    ProvenanceKind,
)
from agentic_engineering_os.domain import Evidence, EvidenceType, to_dict


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas"
COMMIT = "be11488dfeceebbf8614ec730dff51612d673478"
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)

PYTEST_TOOL = EvidenceProvenance(
    kind=ProvenanceKind.TOOL,
    source="pytest",
    producer="pytest",
)
CODEX_TESTER = EvidenceProvenance(
    kind=ProvenanceKind.CODEX,
    source="pytest",
    producer="Codex/Tester",
)
CODEX_REVIEWER = EvidenceProvenance(
    kind=ProvenanceKind.CODEX,
    source="review",
    producer="Codex/Reviewer",
)
HUMAN = EvidenceProvenance(
    kind=ProvenanceKind.HUMAN,
    source="Human",
    producer="human-operator",
)


def recorder(
    target: list[Evidence],
    *,
    validator: ContractValidator | None = None,
    evidence_id: str = "EV-0001",
    timestamp: datetime = NOW,
) -> EvidenceRecorder:
    return EvidenceRecorder(
        target,
        validator=validator,
        id_factory=lambda: evidence_id,
        clock=lambda: timestamp,
    )


def observation(
    evidence_type: EvidenceType | str,
    *,
    provenance: EvidenceProvenance = CODEX_TESTER,
    repository_dependent: bool = True,
    commit: str | None = COMMIT,
    command: str | None = None,
    exit_code: int | None = None,
    result: object = "observed result",
) -> EvidenceObservation:
    return EvidenceObservation(
        evidence_type=evidence_type,
        subject="US-0001",
        result=result,
        provenance=provenance,
        repository_dependent=repository_dependent,
        command=command,
        exit_code=exit_code,
        artifact="captured output",
        commit=commit,
    )


def test_records_valid_command_evidence_without_gate_decision() -> None:
    target: list[Evidence] = []
    item = recorder(target).record(
        observation(
            EvidenceType.COMMAND_RESULT,
            provenance=PYTEST_TOOL,
            command="python -m pytest -q",
            exit_code=0,
            result="85 passed",
        )
    )

    assert target == [item]
    assert item.command == "python -m pytest -q"
    assert item.exit_code == 0
    assert item.result == "85 passed"
    assert item.source == "pytest"
    assert item.producer == "pytest"
    assert not hasattr(item, "gate_result")


@pytest.mark.parametrize(
    ("evidence_type", "provenance", "command", "exit_code"),
    [
        (EvidenceType.TEST_RESULT, CODEX_TESTER, "pytest -q", 0),
        (EvidenceType.GIT_STATE, CODEX_TESTER, "git status", 0),
        (EvidenceType.REVIEW_RESULT, CODEX_REVIEWER, None, None),
        (EvidenceType.ACCEPTANCE_CRITERION_CHECK, CODEX_TESTER, None, None),
    ],
)
def test_records_other_repository_evidence_types(
    evidence_type: EvidenceType,
    provenance: EvidenceProvenance,
    command: str | None,
    exit_code: int | None,
) -> None:
    target: list[Evidence] = []

    item = recorder(target).record(
        observation(
            evidence_type,
            provenance=provenance,
            command=command,
            exit_code=exit_code,
        )
    )

    assert item.evidence_type is evidence_type
    assert item.commit == COMMIT
    assert target == [item]


def test_records_explicit_human_evidence() -> None:
    target: list[Evidence] = []

    item = recorder(target).record(
        observation(
            EvidenceType.HUMAN_APPROVAL,
            provenance=HUMAN,
            repository_dependent=False,
            commit=None,
            result="approved",
        )
    )

    assert item.evidence_type is EvidenceType.HUMAN_APPROVAL
    assert item.source == "Human"
    assert item.producer == "human-operator"


def test_timestamp_is_normalized_to_explicit_utc() -> None:
    target: list[Evidence] = []
    local_time = datetime(
        2026, 8, 27, 14, 0, tzinfo=timezone(timedelta(hours=2))
    )

    item = recorder(target, timestamp=local_time).record(
        observation(EvidenceType.TEST_RESULT)
    )

    assert item.timestamp == NOW
    assert item.timestamp.utcoffset() == timedelta(0)
    assert to_dict(item)["timestamp"] == "2026-08-27T12:00:00+00:00"


def test_recorder_validates_with_contract_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target: list[Evidence] = []
    validator = ContractValidator(SCHEMA_DIR)
    calls: list[tuple[str, object]] = []
    validate = validator.validate

    def observe_validation(contract: str, candidate: object) -> object:
        calls.append((contract, candidate))
        return validate(contract, candidate)

    monkeypatch.setattr(validator, "validate", observe_validation)

    item = recorder(target, validator=validator).record(
        observation(EvidenceType.TEST_RESULT)
    )

    assert item in target
    assert len(calls) == 1
    assert calls[0][0] == "evidence"


def test_recorded_evidence_is_detached_and_immutable() -> None:
    target: list[Evidence] = []
    raw_result = {"tests": ["passed"]}
    item = recorder(target).record(
        observation(EvidenceType.TEST_RESULT, result=raw_result)
    )

    raw_result["tests"].append("changed")

    assert to_dict(item)["result"] == {"tests": ["passed"]}
    with pytest.raises(FrozenInstanceError):
        item.subject = "changed"
    with pytest.raises(TypeError):
        item.result["tests"] = []


def test_unknown_evidence_type_is_refused_without_write() -> None:
    target: list[Evidence] = []

    with pytest.raises(EvidenceRecordingError) as captured:
        recorder(target).record(observation("MAGIC"))

    assert captured.value.code == "UNKNOWN_EVIDENCE_TYPE"
    assert target == []


def test_missing_provenance_is_refused_without_write() -> None:
    target: list[Evidence] = []
    candidate = observation(EvidenceType.TEST_RESULT)
    object.__setattr__(candidate, "provenance", None)

    with pytest.raises(EvidenceRecordingError) as captured:
        recorder(target).record(candidate)

    assert captured.value.code == "PROVENANCE_REQUIRED"
    assert target == []


def test_repository_evidence_without_commit_is_refused() -> None:
    target: list[Evidence] = []

    with pytest.raises(EvidenceRecordingError) as captured:
        recorder(target).record(
            observation(EvidenceType.TEST_RESULT, commit=None)
        )

    assert captured.value.code == "COMMIT_REQUIRED"
    assert target == []


@pytest.mark.parametrize(
    "provenance",
    [
        CODEX_TESTER,
        EvidenceProvenance(
            kind=ProvenanceKind.HUMAN,
            source="Human",
            producer="Codex/Reviewer",
        ),
    ],
)
def test_human_evidence_without_human_actor_is_refused(
    provenance: EvidenceProvenance,
) -> None:
    target: list[Evidence] = []

    with pytest.raises(EvidenceRecordingError) as captured:
        recorder(target).record(
            observation(
                EvidenceType.HUMAN_APPROVAL,
                provenance=provenance,
                repository_dependent=False,
                commit=None,
            )
        )

    assert captured.value.code in {
        "HUMAN_PROVENANCE_REQUIRED",
        "HUMAN_ACTOR_REQUIRED",
    }
    assert target == []


def test_schema_invalid_evidence_is_refused_with_details() -> None:
    target: list[Evidence] = []
    candidate = observation(EvidenceType.TEST_RESULT)
    object.__setattr__(candidate, "subject", "")

    with pytest.raises(EvidenceRecordingError) as captured:
        recorder(target).record(candidate)

    assert captured.value.code == "VALIDATION_FAILED"
    assert captured.value.validation_errors
    assert target == []


def test_duplicate_id_never_overwrites_existing_evidence() -> None:
    target: list[Evidence] = []
    service = recorder(target)
    original = service.record(observation(EvidenceType.TEST_RESULT))

    with pytest.raises(EvidenceRecordingError) as captured:
        service.record(
            observation(EvidenceType.REVIEW_RESULT, provenance=CODEX_REVIEWER)
        )

    assert captured.value.code == "DUPLICATE_EVIDENCE_ID"
    assert target == [original]
    assert target[0] is original


def test_validation_failure_never_writes_partial_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target: list[Evidence] = []
    validator = ContractValidator(SCHEMA_DIR)

    def fail_validation(*args: object, **kwargs: object) -> None:
        raise RuntimeError("validator unavailable")

    monkeypatch.setattr(validator, "validate", fail_validation)

    with pytest.raises(EvidenceRecordingError) as captured:
        recorder(target, validator=validator).record(
            observation(EvidenceType.TEST_RESULT)
        )

    assert captured.value.code == "VALIDATION_UNAVAILABLE"
    assert target == []


def test_unexpected_technical_error_never_writes_partial_evidence() -> None:
    target: list[Evidence] = []

    def fail_id_generation() -> str:
        raise RuntimeError("id generation unavailable")

    service = EvidenceRecorder(
        target,
        id_factory=fail_id_generation,
        clock=lambda: NOW,
    )

    with pytest.raises(EvidenceRecordingError) as captured:
        service.record(observation(EvidenceType.TEST_RESULT))

    assert captured.value.code == "TECHNICAL_ERROR"
    assert target == []


def test_command_evidence_requires_command_and_exit_code() -> None:
    target: list[Evidence] = []

    with pytest.raises(EvidenceRecordingError) as captured:
        recorder(target).record(observation(EvidenceType.COMMAND_RESULT))

    assert captured.value.code == "COMMAND_DETAILS_REQUIRED"
    assert target == []


def test_naive_timestamp_is_refused() -> None:
    target: list[Evidence] = []

    with pytest.raises(EvidenceRecordingError) as captured:
        recorder(target, timestamp=datetime(2026, 8, 27, 12, 0)).record(
            observation(EvidenceType.TEST_RESULT)
        )

    assert captured.value.code == "UTC_TIMESTAMP_REQUIRED"
    assert target == []
