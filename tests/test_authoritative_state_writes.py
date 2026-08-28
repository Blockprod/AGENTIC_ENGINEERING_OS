from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

import pytest

import agentic_engineering_os
import agentic_engineering_os.application as application
import agentic_engineering_os.infrastructure as infrastructure
from agentic_engineering_os._authoritative_write import _issue_authoritative_write
from agentic_engineering_os.domain import (
    AuditEvent,
    AuditEventType,
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    ProjectState,
    to_dict,
)
from agentic_engineering_os.infrastructure import (
    MissionStateStore,
    PersistenceError,
    ProjectStateStore,
)


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
COMMIT = "fe7b1fa76b8fc14826948459b9c0e28a1547a5c9"


def project_candidate() -> ProjectState:
    return ProjectState(
        schema_version="1.0",
        audit_events=[
            AuditEvent(
                event_id="EVENT-001",
                timestamp=NOW,
                event_type=AuditEventType.STATE_CHANGED,
                subject="P2.10-R2",
                actor="Codex",
                role="Orchestrator",
                repository_commit=COMMIT,
                payload={"result": "started"},
            )
        ],
    )


def mission_state(**overrides: object) -> MissionState:
    values: dict[str, object] = {
        "schema_version": "1.0",
        "mission_id": "P2.10-R2",
        "workflow_generation": 0,
        "status": MissionStatus.ACTIVE,
        "role": MissionRole.ORCHESTRATOR,
        "objective": "Enforce trusted authoritative writes.",
        "subject": "Trusted state writes",
        "operating_step": OperatingStep.RECONSTRUCT,
        "next_action": "Inspect authoritative state.",
        "observed_commit": COMMIT,
        "updated_at": NOW,
    }
    values.update(overrides)
    return MissionState(**values)  # type: ignore[arg-type]


def issue(
    store: object,
    before: ProjectState | MissionState,
    candidate: ProjectState | MissionState,
    operation: str,
) -> object:
    kind = "PROJECT_STATE" if isinstance(before, ProjectState) else "MISSION_STATE"
    return _issue_authoritative_write(
        store_kind=kind,
        store=store,
        before_state=before,
        candidate_state=candidate,
        operation=operation,
    )


@pytest.mark.parametrize("authorization", (None, object(), {"trusted": True}))
def test_project_store_rejects_arbitrary_valid_public_mutation(
    tmp_path, authorization: object
) -> None:
    store = ProjectStateStore(tmp_path)
    before = store.initialize()
    candidate = project_candidate()
    previous = store.state_path.read_bytes()

    with pytest.raises(PersistenceError) as captured:
        store.save(
            candidate,
            authorization=authorization,
            operation="FORGED_PUBLIC_WRITE",
        )

    assert captured.value.code == "WRITE_NOT_AUTHORIZED"
    assert store.state_path.read_bytes() == previous
    assert to_dict(store.load()) == to_dict(before)


@pytest.mark.parametrize("authorization", (None, object(), {"trusted": True}))
def test_mission_store_rejects_arbitrary_valid_public_mutation(
    tmp_path, authorization: object
) -> None:
    store = MissionStateStore(tmp_path)
    before = store.initialize(mission_state())
    candidate = replace(before, operating_step=OperatingStep.ACT)
    previous = store.mission_path.read_bytes()

    with pytest.raises(PersistenceError) as captured:
        store.save(
            candidate,
            authorization=authorization,
            operation="FORGED_PUBLIC_WRITE",
        )

    assert captured.value.code == "WRITE_NOT_AUTHORIZED"
    assert store.mission_path.read_bytes() == previous
    assert to_dict(store.load()) == to_dict(before)


def test_authorization_is_bound_to_operation_candidate_and_store_instance(
    tmp_path,
) -> None:
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    store = ProjectStateStore(tmp_path / "one")
    other = ProjectStateStore(tmp_path / "two")
    before = store.initialize()
    other.initialize()
    candidate = project_candidate()
    authorization = issue(store, before, candidate, "RECORD_EVENT")

    with pytest.raises(PersistenceError, match="WRITE_NOT_AUTHORIZED"):
        store.save(candidate, authorization=authorization, operation="OTHER_OPERATION")
    different = deepcopy(candidate)
    different.audit_events[0] = replace(
        different.audit_events[0], payload={"result": "different"}
    )
    with pytest.raises(PersistenceError, match="WRITE_NOT_AUTHORIZED"):
        store.save(different, authorization=authorization, operation="RECORD_EVENT")
    with pytest.raises(PersistenceError, match="WRITE_NOT_AUTHORIZED"):
        other.save(candidate, authorization=authorization, operation="RECORD_EVENT")


def test_authorization_is_bound_to_exact_before_state_and_cannot_be_replayed(
    tmp_path,
) -> None:
    store = ProjectStateStore(tmp_path)
    before = store.initialize()
    first = project_candidate()
    operation = "RECORD_EVENT"
    first_authorization = issue(store, before, first, operation)
    store.save(first, authorization=first_authorization, operation=operation)
    second = deepcopy(first)
    second.audit_events.append(
        replace(first.audit_events[0], event_id="EVENT-002")
    )

    with pytest.raises(PersistenceError, match="WRITE_NOT_AUTHORIZED"):
        store.save(second, authorization=first_authorization, operation=operation)

    assert [event.event_id for event in store.load().audit_events] == ["EVENT-001"]


def test_authorization_with_wrong_before_fingerprint_is_refused(tmp_path) -> None:
    store = ProjectStateStore(tmp_path)
    actual_before = store.initialize()
    wrong_before = project_candidate()
    candidate = deepcopy(wrong_before)
    candidate.audit_events.append(
        replace(candidate.audit_events[0], event_id="EVENT-002")
    )
    authorization = issue(store, wrong_before, candidate, "RECORD_EVENT")

    with pytest.raises(PersistenceError, match="WRITE_NOT_AUTHORIZED"):
        store.save(candidate, authorization=authorization, operation="RECORD_EVENT")

    assert to_dict(store.load()) == to_dict(actual_before)


def test_mission_authorization_binds_identity_generation_and_candidate(
    tmp_path,
) -> None:
    store = MissionStateStore(tmp_path)
    before = store.initialize(mission_state())
    candidate = replace(
        before,
        workflow_generation=1,
        operating_step=OperatingStep.ACT,
    )
    operation = "ADVANCE_MISSION"
    authorization = issue(store, before, candidate, operation)

    for changed in (
        replace(candidate, mission_id="OTHER-MISSION"),
        replace(candidate, workflow_generation=2),
        replace(candidate, operating_step=OperatingStep.VERIFY),
    ):
        with pytest.raises(PersistenceError, match="WRITE_NOT_AUTHORIZED"):
            store.save(changed, authorization=authorization, operation=operation)

    store.save(candidate, authorization=authorization, operation=operation)
    assert store.load().workflow_generation == 1
    generation_two = replace(candidate, workflow_generation=2)
    with pytest.raises(PersistenceError, match="WRITE_NOT_AUTHORIZED"):
        store.save(
            generation_two,
            authorization=authorization,
            operation=operation,
        )
    assert store.load().workflow_generation == 1


def test_cross_kind_authorization_is_refused(tmp_path) -> None:
    project_store = ProjectStateStore(tmp_path)
    project_before = project_store.initialize()
    project_after = project_candidate()
    mission_store = MissionStateStore(tmp_path)
    mission_before = mission_store.initialize(mission_state())
    mission_after = replace(mission_before, operating_step=OperatingStep.ACT)
    project_authorization = issue(
        project_store, project_before, project_after, "PROJECT_OPERATION"
    )

    with pytest.raises(PersistenceError, match="WRITE_NOT_AUTHORIZED"):
        mission_store.save(
            mission_after,
            authorization=project_authorization,
            operation="PROJECT_OPERATION",
        )

    mission_authorization = issue(
        mission_store, mission_before, mission_after, "MISSION_OPERATION"
    )
    with pytest.raises(PersistenceError, match="WRITE_NOT_AUTHORIZED"):
        project_store.save(
            project_after,
            authorization=mission_authorization,
            operation="MISSION_OPERATION",
        )


def test_direct_completed_mission_snapshot_is_refused(tmp_path) -> None:
    store = MissionStateStore(tmp_path)
    before = store.initialize(mission_state())
    completed = replace(
        before,
        status=MissionStatus.COMPLETED,
        role=MissionRole.CERTIFIER,
        operating_step=OperatingStep.REPORT,
        next_action="Report completion.",
    )

    with pytest.raises(PersistenceError) as captured:
        store.save(completed)

    assert captured.value.code == "WRITE_NOT_AUTHORIZED"
    assert store.load().status is MissionStatus.ACTIVE


def test_identical_save_is_explicit_noop_without_authorization(tmp_path) -> None:
    project_store = ProjectStateStore(tmp_path)
    project = project_store.initialize()
    project_bytes = project_store.state_path.read_bytes()
    mission_store = MissionStateStore(tmp_path)
    mission = mission_store.initialize(mission_state())
    mission_bytes = mission_store.mission_path.read_bytes()

    project_store.save(project)
    mission_store.save(mission)

    assert project_store.state_path.read_bytes() == project_bytes
    assert mission_store.mission_path.read_bytes() == mission_bytes


@pytest.mark.parametrize("status", (MissionStatus.COMPLETED, MissionStatus.CANCELLED))
def test_mission_initialize_refuses_terminal_state(tmp_path, status) -> None:
    store = MissionStateStore(tmp_path)

    with pytest.raises(PersistenceError) as captured:
        store.initialize(mission_state(status=status))

    assert captured.value.code == "INVALID_INITIAL_STATE"
    assert not store.mission_path.exists()


def test_mission_initialize_refuses_nonzero_generation(tmp_path) -> None:
    store = MissionStateStore(tmp_path)

    with pytest.raises(PersistenceError) as captured:
        store.initialize(mission_state(workflow_generation=1))

    assert captured.value.code == "INVALID_INITIAL_STATE"
    assert not store.mission_path.exists()


def test_write_authority_is_not_exported_by_public_packages() -> None:
    for public_module in (agentic_engineering_os, application, infrastructure):
        assert not hasattr(public_module, "WriteAuthorization")
        assert not hasattr(public_module, "issue_authoritative_write")
        assert not hasattr(public_module, "_issue_authoritative_write")
