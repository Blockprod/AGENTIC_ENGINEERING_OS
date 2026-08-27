from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

import agentic_engineering_os.application.orchestrator as orchestrator_module
from agentic_engineering_os.application import Orchestrator
from agentic_engineering_os.domain import (
    HumanApproval,
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    ProjectState,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
    to_dict,
)
from agentic_engineering_os.infrastructure import (
    MissionStateStore,
    PersistenceError,
    ProjectStateStore,
)


COMMIT = "506485584359c295f58ded28403591959d275932"
DIVERGENT_COMMIT = "a" * 40
UPDATED_AT = datetime.fromisoformat("2026-08-28T14:00:00+02:00")


def mission_state(**overrides: object) -> MissionState:
    values: dict[str, object] = {
        "schema_version": "1.0",
        "mission_id": "P2.3",
        "status": MissionStatus.ACTIVE,
        "role": MissionRole.ORCHESTRATOR,
        "objective": "Coordinate the deterministic mission workflow.",
        "subject": "P2.3",
        "operating_step": OperatingStep.PREFLIGHT,
        "next_action": "Inspect the current mission context.",
        "observed_commit": COMMIT,
        "updated_at": datetime.fromisoformat("2026-08-28T13:00:00+02:00"),
    }
    values.update(overrides)
    return MissionState(**values)  # type: ignore[arg-type]


def user_story_requiring_human() -> UserStory:
    return UserStory(
        schema_version="1.0",
        id="US-HUMAN",
        title="Human decision",
        description="A User Story requiring explicit Human approval.",
        status=UserStoryStatus.REVIEW,
        priority=1,
        risk=RiskLevel.HIGH,
        depends_on=(),
        scope=UserStoryScope(allowed_paths=("src/",), forbidden_paths=()),
        acceptance_criteria=(),
        required_gates=(),
        human_approval=HumanApproval(
            required=True,
            approved=False,
            approved_by=None,
            approved_at=None,
        ),
        metadata=UserStoryMetadata(
            created_at=UPDATED_AT,
            created_by="human-operator",
            updated_at=UPDATED_AT,
        ),
    )


def initialized_orchestrator(
    tmp_path: Path, mission: MissionState | None = None
) -> tuple[Orchestrator, MissionStateStore, ProjectStateStore]:
    project_store = ProjectStateStore(tmp_path)
    project_store.initialize()
    mission_store = MissionStateStore(tmp_path)
    if mission is not None:
        mission_store.initialize(mission)
    orchestrator = Orchestrator(
        repository_root=tmp_path,
        mission_store=mission_store,
        project_state_store=project_store,
    )
    return orchestrator, mission_store, project_store


def test_same_commit_resumes_the_recorded_operating_step(tmp_path: Path) -> None:
    mission = mission_state(operating_step=OperatingStep.UNDERSTAND_CONTRACT)
    orchestrator, _, _ = initialized_orchestrator(tmp_path, mission)

    result = orchestrator.orchestrate(current_commit=COMMIT, updated_at=UPDATED_AT)

    assert result.success
    assert result.reason == "ROUTED"
    assert result.next_role is MissionRole.ARCHITECT
    assert result.handoff is not None
    assert result.handoff.operating_step is OperatingStep.UNDERSTAND_CONTRACT


def test_commit_divergence_forces_reconstruct_and_orchestrator(
    tmp_path: Path,
) -> None:
    mission = mission_state(
        role=MissionRole.IMPLEMENTER,
        operating_step=OperatingStep.ACT,
    )
    orchestrator, mission_store, _ = initialized_orchestrator(tmp_path, mission)

    result = orchestrator.orchestrate(
        current_commit=DIVERGENT_COMMIT,
        updated_at=UPDATED_AT,
    )

    assert result.success
    assert result.reason == "RECONSTRUCT_REQUIRED"
    assert result.next_role is MissionRole.ORCHESTRATOR
    assert result.handoff is not None
    assert result.handoff.operating_step is OperatingStep.RECONSTRUCT
    persisted = mission_store.load()
    assert persisted.role is MissionRole.ORCHESTRATOR
    assert persisted.operating_step is OperatingStep.RECONSTRUCT
    assert persisted.observed_commit == DIVERGENT_COMMIT


@pytest.mark.parametrize(
    ("step", "expected_role"),
    [
        (OperatingStep.UNDERSTAND_CONTRACT, MissionRole.ARCHITECT),
        (OperatingStep.ACT, MissionRole.IMPLEMENTER),
        (OperatingStep.VERIFY, MissionRole.TESTER),
        (OperatingStep.REPORT, MissionRole.REVIEWER),
        (OperatingStep.CONTROLLED_TRANSITION, MissionRole.CERTIFIER),
    ],
)
def test_routes_each_specialized_role_without_executing_it(
    tmp_path: Path,
    step: OperatingStep,
    expected_role: MissionRole,
) -> None:
    orchestrator, _, _ = initialized_orchestrator(
        tmp_path, mission_state(operating_step=step)
    )

    result = orchestrator.orchestrate(current_commit=COMMIT, updated_at=UPDATED_AT)

    assert result.success
    assert result.next_role is expected_role
    assert result.handoff is not None
    assert result.handoff.to_role is expected_role
    assert result.handoff.operating_step is step


def test_handoff_contains_only_complete_context_not_authority(tmp_path: Path) -> None:
    mission = mission_state(
        role=MissionRole.TESTER,
        operating_step=OperatingStep.REPORT,
        blockers=["Non-blocking context"],
    )
    orchestrator, _, _ = initialized_orchestrator(tmp_path, mission)

    result = orchestrator.orchestrate(current_commit=COMMIT, updated_at=UPDATED_AT)

    handoff = result.handoff
    assert handoff is not None
    assert handoff.from_role is MissionRole.TESTER
    assert handoff.to_role is MissionRole.REVIEWER
    assert handoff.mission_id == mission.mission_id
    assert handoff.subject == mission.subject
    assert handoff.objective == mission.objective
    assert handoff.observed_commit == COMMIT
    assert handoff.operating_step is OperatingStep.REPORT
    assert handoff.blockers == ("Non-blocking context",)
    assert "Control Plane authority" in handoff.instructions
    assert "CERTIFIED" not in handoff.instructions


def test_success_persists_candidate_and_reload_is_identical(tmp_path: Path) -> None:
    mission = mission_state(operating_step=OperatingStep.VERIFY)
    orchestrator, mission_store, project_store = initialized_orchestrator(
        tmp_path, mission
    )
    project_before = project_store.state_path.read_bytes()

    result = orchestrator.orchestrate(current_commit=COMMIT, updated_at=UPDATED_AT)

    assert result.success
    assert result.updated_mission_state is not None
    reloaded = mission_store.load()
    assert to_dict(reloaded) == to_dict(result.updated_mission_state)
    assert reloaded.role is MissionRole.TESTER
    assert reloaded.updated_at == UPDATED_AT
    assert result.handoff is not None
    assert reloaded.next_action == result.handoff.instructions
    assert project_store.state_path.read_bytes() == project_before


def test_absent_mission_fails_without_fallback(tmp_path: Path) -> None:
    orchestrator, mission_store, _ = initialized_orchestrator(tmp_path)

    result = orchestrator.orchestrate(current_commit=COMMIT, updated_at=UPDATED_AT)

    assert not result.success
    assert result.reason == "MISSION_STATE_UNAVAILABLE:MISSION_ABSENT"
    assert result.handoff is None
    assert not mission_store.mission_path.exists()


def test_corrupt_mission_fails_without_fallback(tmp_path: Path) -> None:
    orchestrator, mission_store, _ = initialized_orchestrator(tmp_path)
    mission_store.mission_path.write_text("{", encoding="utf-8")

    result = orchestrator.orchestrate(current_commit=COMMIT, updated_at=UPDATED_AT)

    assert not result.success
    assert result.reason == "MISSION_STATE_UNAVAILABLE:INVALID_JSON"
    assert mission_store.mission_path.read_text(encoding="utf-8") == "{"


def test_invalid_project_state_blocks_without_mutating_mission(tmp_path: Path) -> None:
    mission = mission_state()
    orchestrator, mission_store, project_store = initialized_orchestrator(
        tmp_path, mission
    )
    mission_before = mission_store.mission_path.read_bytes()
    project_store.state_path.write_text("{", encoding="utf-8")

    result = orchestrator.orchestrate(current_commit=COMMIT, updated_at=UPDATED_AT)

    assert not result.success
    assert result.reason == "PROJECT_STATE_UNAVAILABLE:INVALID_JSON"
    assert mission_store.mission_path.read_bytes() == mission_before


@pytest.mark.parametrize(
    ("blockers", "expected_reason"),
    [
        (["Verification environment unavailable"], "MISSION_BLOCKED"),
        (["HUMAN_REQUIRED: approve destructive scope"], "HUMAN_REQUIRED"),
        (["human_required"], "HUMAN_REQUIRED"),
    ],
)
def test_blocked_mission_never_routes_around_blockers(
    tmp_path: Path,
    blockers: list[str],
    expected_reason: str,
) -> None:
    mission = mission_state(status=MissionStatus.BLOCKED, blockers=blockers)
    orchestrator, mission_store, _ = initialized_orchestrator(tmp_path, mission)
    before = mission_store.mission_path.read_bytes()

    result = orchestrator.orchestrate(current_commit=COMMIT, updated_at=UPDATED_AT)

    assert not result.success
    assert result.reason == expected_reason
    assert result.next_role is None
    assert result.handoff is None
    assert result.blockers == tuple(blockers)
    assert mission_store.mission_path.read_bytes() == before


def test_project_state_human_requirement_cannot_be_auto_approved(
    tmp_path: Path,
) -> None:
    mission = mission_state(subject="US-HUMAN")
    mission_store = InMemoryMissionStore(mission)
    project_state = ProjectState(
        schema_version="1.0", user_stories=[user_story_requiring_human()]
    )
    orchestrator = in_memory_orchestrator(
        tmp_path, mission_store, project_state=project_state
    )

    result = orchestrator.orchestrate(current_commit=COMMIT, updated_at=UPDATED_AT)

    assert not result.success
    assert result.reason == "HUMAN_REQUIRED"
    assert result.next_role is None
    assert result.handoff is None
    assert mission_store.save_attempts == []


def test_human_marker_blocks_even_if_status_is_active(tmp_path: Path) -> None:
    mission = mission_state(blockers=["HUMAN_REQUIRED: choose deployment scope"])
    store = InMemoryMissionStore(mission)
    orchestrator = in_memory_orchestrator(tmp_path, store)

    result = orchestrator.orchestrate(current_commit=COMMIT, updated_at=UPDATED_AT)

    assert not result.success
    assert result.reason == "HUMAN_REQUIRED"
    assert result.handoff is None
    assert store.save_attempts == []


class InMemoryMissionStore:
    def __init__(self, mission: object, *, fail_save: bool = False) -> None:
        self.mission = mission
        self.fail_save = fail_save
        self.save_attempts: list[MissionState] = []

    def load(self) -> MissionState:
        return self.mission  # type: ignore[return-value]

    def save(self, state: MissionState) -> Path:
        self.save_attempts.append(state)
        if self.fail_save:
            raise PersistenceError("WRITE_FAILED", "simulated failure")
        self.mission = state
        return Path("mission.json")


class InMemoryProjectStore:
    def __init__(self, state: object) -> None:
        self.state = state

    def load(self) -> ProjectState:
        return self.state  # type: ignore[return-value]


def in_memory_orchestrator(
    tmp_path: Path,
    mission_store: InMemoryMissionStore,
    project_state: object | None = None,
) -> Orchestrator:
    return Orchestrator(
        repository_root=tmp_path,
        mission_store=mission_store,
        project_state_store=InMemoryProjectStore(
            ProjectState(schema_version="1.0")
            if project_state is None
            else project_state
        ),
    )


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("role", "UNKNOWN", "UNKNOWN_MISSION_ROLE"),
        ("operating_step", "UNKNOWN", "UNKNOWN_OPERATING_STEP"),
        ("status", "UNKNOWN", "UNKNOWN_MISSION_STATUS"),
    ],
)
def test_unknown_mission_values_fail_closed(
    tmp_path: Path, field: str, value: str, reason: str
) -> None:
    mission = mission_state()
    setattr(mission, field, value)
    store = InMemoryMissionStore(mission)
    orchestrator = in_memory_orchestrator(tmp_path, store)

    result = orchestrator.orchestrate(current_commit=COMMIT, updated_at=UPDATED_AT)

    assert not result.success
    assert result.reason == reason
    assert store.save_attempts == []


def test_invalid_loaded_mission_is_not_repaired(tmp_path: Path) -> None:
    mission = mission_state(observed_commit="not-a-commit")
    store = InMemoryMissionStore(mission)
    orchestrator = in_memory_orchestrator(tmp_path, store)

    result = orchestrator.orchestrate(current_commit=COMMIT, updated_at=UPDATED_AT)

    assert not result.success
    assert result.reason == "INVALID_OBSERVED_COMMIT"
    assert result.updated_mission_state is None
    assert store.save_attempts == []


def test_non_project_state_is_refused(tmp_path: Path) -> None:
    store = InMemoryMissionStore(mission_state())
    orchestrator = in_memory_orchestrator(tmp_path, store, project_state=object())

    result = orchestrator.orchestrate(current_commit=COMMIT, updated_at=UPDATED_AT)

    assert not result.success
    assert result.reason == "INVALID_PROJECT_STATE"
    assert store.save_attempts == []


def test_ambiguous_routing_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mission = mission_state(operating_step=OperatingStep.VERIFY)
    store = InMemoryMissionStore(mission)
    policy = (
        *orchestrator_module._ROUTING_POLICY,
        (OperatingStep.VERIFY, MissionRole.REVIEWER),
    )
    monkeypatch.setattr(orchestrator_module, "_ROUTING_POLICY", policy)
    orchestrator = in_memory_orchestrator(tmp_path, store)

    result = orchestrator.orchestrate(current_commit=COMMIT, updated_at=UPDATED_AT)

    assert not result.success
    assert result.reason == "AMBIGUOUS_ROUTING"
    assert store.save_attempts == []


def test_persistence_failure_leaves_loaded_mission_unmodified(tmp_path: Path) -> None:
    mission = mission_state(
        role=MissionRole.IMPLEMENTER,
        operating_step=OperatingStep.VERIFY,
        blockers=["Context to preserve"],
    )
    before = to_dict(mission)
    store = InMemoryMissionStore(mission, fail_save=True)
    orchestrator = in_memory_orchestrator(tmp_path, store)

    result = orchestrator.orchestrate(current_commit=COMMIT, updated_at=UPDATED_AT)

    assert not result.success
    assert result.reason == "MISSION_PERSISTENCE_UNAVAILABLE:WRITE_FAILED"
    assert result.handoff is None
    assert to_dict(mission) == before
    assert len(store.save_attempts) == 1
    assert store.save_attempts[0] is not mission
    assert store.save_attempts[0].blockers is not mission.blockers


def test_same_inputs_produce_same_routing(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first, _, _ = initialized_orchestrator(
        first_root, mission_state(operating_step=OperatingStep.RECORD_EVIDENCE)
    )
    second, _, _ = initialized_orchestrator(
        second_root, mission_state(operating_step=OperatingStep.RECORD_EVIDENCE)
    )

    first_result = first.orchestrate(current_commit=COMMIT, updated_at=UPDATED_AT)
    second_result = second.orchestrate(current_commit=COMMIT, updated_at=UPDATED_AT)

    assert first_result == second_result
