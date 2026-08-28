from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

import pytest

from agentic_engineering_os.application import (
    DAGValidator,
    ReadinessEngine,
    WavePlanner,
    WavePlanningError,
)
from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    DAGEdge,
    DAGNode,
    DAGSnapshot,
    DeferredReason,
    HumanApproval,
    NodeReadiness,
    ProjectState,
    ReadinessClassification,
    ReadinessSnapshot,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
    WavePlan,
    to_dict,
)


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def story(
    identifier: str,
    *,
    status: UserStoryStatus = UserStoryStatus.PLANNED,
    depends_on: tuple[str, ...] = (),
    priority: int = 1,
    risk: RiskLevel = RiskLevel.LOW,
    approval_required: bool = False,
    approval_applied: bool = False,
) -> UserStory:
    return UserStory(
        schema_version="1.0",
        id=identifier,
        title=f"Story {identifier}",
        description=f"Plan logical wave for {identifier}.",
        status=status,
        priority=priority,
        risk=risk,
        depends_on=depends_on,
        scope=UserStoryScope(allowed_paths=("src/",), forbidden_paths=()),
        acceptance_criteria=(
            AcceptanceCriterion("AC-001", "Wave placement is deterministic.", True),
        ),
        required_gates=(),
        human_approval=HumanApproval(
            required=approval_required,
            approved=approval_applied,
            approved_by="human-operator" if approval_applied else None,
            approved_at=NOW if approval_applied else None,
            evidence_ref="EV-HUMAN-001" if approval_applied else None,
        ),
        metadata=UserStoryMetadata(NOW, "human-operator", NOW),
    )


def project(*stories: UserStory) -> ProjectState:
    return ProjectState(schema_version="1.0", user_stories=list(stories))


def inputs(*stories: UserStory):
    state = project(*stories)
    dag = DAGValidator().build(state)
    readiness = ReadinessEngine().evaluate(dag, state)
    return state, dag, readiness


def plan(*stories: UserStory) -> WavePlan:
    state, dag, readiness = inputs(*stories)
    return WavePlanner().plan(dag, readiness, state)


def wave_ids(result: WavePlan) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(member.user_story_id for member in wave.members)
        for wave in result.waves
    )


def deferred_by_id(result: WavePlan, identifier: str):
    return next(item for item in result.deferred if item.user_story_id == identifier)


def test_empty_graph_produces_empty_immutable_plan() -> None:
    result = plan()

    assert result == WavePlan(waves=(), deferred=())
    with pytest.raises(FrozenInstanceError):
        result.waves = ()  # type: ignore[misc]


def test_one_ready_node_is_wave_zero() -> None:
    result = plan(story("US-0001"))

    assert wave_ids(result) == (("US-0001",),)
    assert result.waves[0].wave_index == 0
    assert result.deferred == ()


def test_certified_dependency_is_prior_satisfaction_not_planned_work() -> None:
    result = plan(
        story("US-0001", status=UserStoryStatus.CERTIFIED),
        story("US-0002", depends_on=("US-0001",)),
    )

    assert wave_ids(result) == (("US-0002",),)
    certified = deferred_by_id(result, "US-0001")
    assert certified.reason is DeferredReason.TERMINAL_SATISFIED


def test_linear_chain_forms_one_wave_per_dependency_level() -> None:
    result = plan(
        story("US-0001"),
        story("US-0002", depends_on=("US-0001",)),
        story("US-0003", depends_on=("US-0002",)),
    )

    assert wave_ids(result) == (("US-0001",), ("US-0002",), ("US-0003",))


def test_diamond_layering_is_deterministic() -> None:
    result = plan(
        story("US-0004", depends_on=("US-0003", "US-0002")),
        story("US-0002", depends_on=("US-0001",)),
        story("US-0001"),
        story("US-0003", depends_on=("US-0001",)),
    )

    assert wave_ids(result) == (
        ("US-0001",),
        ("US-0002", "US-0003"),
        ("US-0004",),
    )


def test_disconnected_components_share_logical_layers() -> None:
    result = plan(
        story("US-0004", depends_on=("US-0003",)),
        story("US-0003"),
        story("US-0002", depends_on=("US-0001",)),
        story("US-0001"),
    )

    assert wave_ids(result) == (
        ("US-0001", "US-0003"),
        ("US-0002", "US-0004"),
    )


def test_multiple_dependencies_require_all_prior_layers() -> None:
    result = plan(
        story("US-0001", status=UserStoryStatus.CERTIFIED),
        story("US-0002"),
        story("US-0003"),
        story("US-0004", depends_on=("US-0003", "US-0002", "US-0001")),
    )

    assert wave_ids(result) == (("US-0002", "US-0003"), ("US-0004",))


def test_partial_plan_keeps_blocked_branch_deferred() -> None:
    result = plan(
        story("US-0001"),
        story("US-0002"),
        story("US-0003", depends_on=("US-0001",)),
        story("US-0004", depends_on=("US-0002",)),
        story("US-0005", status=UserStoryStatus.BLOCKED),
        story("US-0006", depends_on=("US-0005",)),
    )

    assert wave_ids(result) == (
        ("US-0001", "US-0002"),
        ("US-0003", "US-0004"),
    )
    assert deferred_by_id(result, "US-0005").reason is DeferredReason.BLOCKED
    dependent = deferred_by_id(result, "US-0006")
    assert dependent.reason is DeferredReason.UNPLANNABLE_DEPENDENCY
    assert dependent.blocking_dependencies == ("US-0005",)


def test_priority_orders_members_without_changing_wave_membership() -> None:
    result = plan(
        story("US-0001", priority=5),
        story("US-0002", priority=1, risk=RiskLevel.CRITICAL),
        story("US-0003", priority=1, risk=RiskLevel.HIGH),
    )

    assert wave_ids(result) == (("US-0002", "US-0003", "US-0001"),)
    assert result.waves[0].members[0].risk is RiskLevel.CRITICAL


def test_priority_never_moves_dependent_into_same_or_earlier_wave() -> None:
    result = plan(
        story("US-0001", priority=99),
        story("US-0002", depends_on=("US-0001",), priority=1),
    )

    assert wave_ids(result) == (("US-0001",), ("US-0002",))


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (UserStoryStatus.PROPOSED, DeferredReason.BLOCKED),
        (UserStoryStatus.BLOCKED, DeferredReason.BLOCKED),
        (UserStoryStatus.REJECTED, DeferredReason.BLOCKED),
        (UserStoryStatus.REMEDIATION_REQUIRED, DeferredReason.BLOCKED),
        (UserStoryStatus.IN_PROGRESS, DeferredReason.INELIGIBLE),
        (UserStoryStatus.IMPLEMENTED, DeferredReason.INELIGIBLE),
        (UserStoryStatus.TESTING, DeferredReason.INELIGIBLE),
        (UserStoryStatus.REVIEW, DeferredReason.INELIGIBLE),
        (UserStoryStatus.CERTIFICATION, DeferredReason.INELIGIBLE),
        (UserStoryStatus.CERTIFIED, DeferredReason.TERMINAL_SATISFIED),
        (UserStoryStatus.CANCELLED, DeferredReason.TERMINAL_UNSATISFIED),
    ],
)
def test_non_plannable_own_states_are_deferred(
    status: UserStoryStatus,
    reason: DeferredReason,
) -> None:
    result = plan(story("US-0001", status=status))

    assert result.waves == ()
    assert result.deferred[0].reason is reason


def test_transitive_deferral_does_not_cross_blocked_node() -> None:
    result = plan(
        story("US-0001", status=UserStoryStatus.BLOCKED),
        story("US-0002", depends_on=("US-0001",)),
        story("US-0003", depends_on=("US-0002",)),
    )

    assert result.waves == ()
    assert deferred_by_id(result, "US-0001").reason is DeferredReason.BLOCKED
    assert deferred_by_id(result, "US-0002").blocking_dependencies == ("US-0001",)
    assert deferred_by_id(result, "US-0003").blocking_dependencies == ("US-0002",)


@pytest.mark.parametrize(
    "blocking_status",
    [
        UserStoryStatus.IN_PROGRESS,
        UserStoryStatus.IMPLEMENTED,
        UserStoryStatus.TESTING,
        UserStoryStatus.REVIEW,
        UserStoryStatus.CERTIFICATION,
        UserStoryStatus.CANCELLED,
        UserStoryStatus.REJECTED,
    ],
)
def test_dependent_of_non_certified_unplannable_branch_is_deferred(
    blocking_status: UserStoryStatus,
) -> None:
    result = plan(
        story("US-0001", status=blocking_status),
        story("US-0002", depends_on=("US-0001",)),
    )

    dependent = deferred_by_id(result, "US-0002")
    assert dependent.reason is DeferredReason.UNPLANNABLE_DEPENDENCY
    assert dependent.blocking_dependencies == ("US-0001",)


def test_required_human_approval_is_consumed_only_through_readiness() -> None:
    blocked = plan(story("US-0001", approval_required=True))
    approved = plan(
        story("US-0001", approval_required=True, approval_applied=True)
    )

    assert blocked.waves == ()
    assert deferred_by_id(blocked, "US-0001").reason is DeferredReason.BLOCKED
    assert wave_ids(approved) == (("US-0001",),)


def test_project_state_order_does_not_change_plan_or_inputs() -> None:
    first = story("US-0001")
    second = story("US-0002", depends_on=("US-0001",))
    third = story("US-0003")
    state, dag, readiness = inputs(third, second, first)
    before = (deepcopy(to_dict(state)), deepcopy(to_dict(dag)), deepcopy(to_dict(readiness)))

    actual = WavePlanner().plan(dag, readiness, state)
    other_state, other_dag, other_readiness = inputs(first, third, second)
    expected = WavePlanner().plan(other_dag, other_readiness, other_state)

    assert actual == expected
    assert (to_dict(state), to_dict(dag), to_dict(readiness)) == before


def test_prospective_simulation_never_changes_statuses() -> None:
    first = story("US-0001", status=UserStoryStatus.READY)
    second = story("US-0002", depends_on=("US-0001",))
    state, dag, readiness = inputs(first, second)

    result = WavePlanner().plan(dag, readiness, state)

    assert wave_ids(result) == (("US-0001",), ("US-0002",))
    assert first.status is UserStoryStatus.READY
    assert second.status is UserStoryStatus.PLANNED


def test_recomputation_discards_old_projection_after_authoritative_change() -> None:
    initial = plan(
        story("US-0001", status=UserStoryStatus.READY),
        story("US-0002", depends_on=("US-0001",)),
    )
    changed = plan(
        story("US-0001", status=UserStoryStatus.BLOCKED),
        story("US-0002", depends_on=("US-0001",)),
    )

    assert wave_ids(initial) == (("US-0001",), ("US-0002",))
    assert changed.waves == ()
    assert tuple(item.user_story_id for item in changed.deferred) == (
        "US-0001",
        "US-0002",
    )


@pytest.mark.parametrize("field", ["status", "depends_on", "priority", "risk"])
def test_dag_state_mismatch_is_refused_without_mutation(field: str) -> None:
    state, dag, readiness = inputs(story("US-0001"))
    replacements = {
        "status": UserStoryStatus.READY,
        "depends_on": ("US-9999",),
        "priority": 2,
        "risk": RiskLevel.HIGH,
    }
    bad_node = replace(dag.nodes[0], **{field: replacements[field]})
    divergent = replace(dag, nodes=(bad_node,))
    before = (
        deepcopy(to_dict(state)),
        deepcopy(to_dict(divergent)),
        deepcopy(to_dict(readiness)),
    )

    with pytest.raises(WavePlanningError) as captured:
        WavePlanner().plan(divergent, readiness, state)

    assert captured.value.code == "DAG_STATE_MISMATCH"
    assert (to_dict(state), to_dict(divergent), to_dict(readiness)) == before


def test_project_state_node_missing_from_dag_is_refused() -> None:
    _, first_dag, first_readiness = inputs(story("US-0001"))
    expanded_state = project(story("US-0001"), story("US-0002"))
    before = deepcopy(to_dict(expanded_state))

    with pytest.raises(WavePlanningError) as captured:
        WavePlanner().plan(first_dag, first_readiness, expanded_state)

    assert captured.value.code == "DAG_STATE_MISMATCH"
    assert to_dict(expanded_state) == before


def test_forged_ready_classification_is_refused() -> None:
    state, dag, readiness = inputs(story("US-0001", status=UserStoryStatus.BLOCKED))
    forged = replace(
        readiness,
        nodes=(
            replace(
                readiness.nodes[0],
                classification=ReadinessClassification.READY,
                reason="LOGICALLY_ELIGIBLE",
            ),
        ),
    )

    with pytest.raises(WavePlanningError) as captured:
        WavePlanner().plan(dag, forged, state)

    assert captured.value.code == "READINESS_MISMATCH"


def test_missing_readiness_node_is_refused() -> None:
    state, dag, _ = inputs(story("US-0001"))

    with pytest.raises(WavePlanningError) as captured:
        WavePlanner().plan(dag, ReadinessSnapshot(nodes=()), state)

    assert captured.value.code == "READINESS_MISMATCH"
    assert captured.value.subjects == ("US-0001",)


@pytest.mark.parametrize(
    "field",
    ["satisfied_dependencies", "unsatisfied_dependencies", "reason"],
)
def test_forged_readiness_details_are_refused(field: str) -> None:
    state, dag, readiness = inputs(story("US-0001"))
    replacements = {
        "satisfied_dependencies": ("US-9999",),
        "unsatisfied_dependencies": ("US-9999",),
        "reason": "FORGED",
    }
    forged_node = replace(readiness.nodes[0], **{field: replacements[field]})
    forged = replace(readiness, nodes=(forged_node,))

    with pytest.raises(WavePlanningError) as captured:
        WavePlanner().plan(dag, forged, state)

    assert captured.value.code == "READINESS_MISMATCH"


def test_direct_invalid_dag_is_refused() -> None:
    first = story("US-0001", depends_on=("US-0002",))
    second = story("US-0002", depends_on=("US-0001",))
    state = project(first, second)
    dag = DAGSnapshot(
        nodes=(
            DAGNode("US-0001", first.status, 1, RiskLevel.LOW, first.depends_on),
            DAGNode("US-0002", second.status, 1, RiskLevel.LOW, second.depends_on),
        ),
        edges=(DAGEdge("US-0001", "US-0002"), DAGEdge("US-0002", "US-0001")),
    )
    readiness = ReadinessSnapshot(
        nodes=(
            NodeReadiness(
                "US-0001",
                ReadinessClassification.WAITING_DEPENDENCIES,
                (),
                ("US-0002",),
                "DEPENDENCIES_NOT_CERTIFIED",
            ),
            NodeReadiness(
                "US-0002",
                ReadinessClassification.WAITING_DEPENDENCIES,
                (),
                ("US-0001",),
                "DEPENDENCIES_NOT_CERTIFIED",
            ),
        )
    )

    with pytest.raises(WavePlanningError) as captured:
        WavePlanner().plan(dag, readiness, state)

    assert captured.value.code == "INVALID_DAG"


def test_wave_plan_exposes_no_execution_authority_or_conflict_group() -> None:
    rendered = repr(to_dict(plan(story("US-0001")))).casefold()

    for forbidden in (
        "worktree",
        "scheduler",
        "parallel_group",
        "execution_batch",
        "certification",
        "transition",
    ):
        assert forbidden not in rendered
