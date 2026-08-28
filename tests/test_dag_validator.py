from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

import pytest

from agentic_engineering_os.application import DAGValidationError, DAGValidator
from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    DAGEdge,
    DAGNode,
    DAGSnapshot,
    HumanApproval,
    ProjectState,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
    to_dict,
)


NOW = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)


def story(
    identifier: str,
    *,
    depends_on: tuple[str, ...] = (),
    status: UserStoryStatus | str = UserStoryStatus.PLANNED,
    priority: int = 1,
    risk: RiskLevel | str = RiskLevel.LOW,
) -> UserStory:
    return UserStory(
        schema_version="1.0",
        id=identifier,
        title=f"Story {identifier}",
        description=f"Validate deterministic projection for {identifier}.",
        status=status,  # type: ignore[arg-type]
        priority=priority,
        risk=risk,  # type: ignore[arg-type]
        depends_on=depends_on,
        scope=UserStoryScope(allowed_paths=("src/",), forbidden_paths=()),
        acceptance_criteria=(
            AcceptanceCriterion("AC-001", "The projection is valid.", True),
        ),
        required_gates=(),
        human_approval=HumanApproval(False, False, None, None),
        metadata=UserStoryMetadata(NOW, "human-operator", NOW),
    )


def build(*stories: UserStory) -> DAGSnapshot:
    return DAGValidator().build(
        ProjectState(schema_version="1.0", user_stories=list(stories))
    )


def test_empty_project_produces_empty_immutable_snapshot() -> None:
    snapshot = build()

    assert snapshot == DAGSnapshot(nodes=(), edges=())
    with pytest.raises(FrozenInstanceError):
        snapshot.nodes = ()  # type: ignore[misc]


def test_single_node_is_projected_without_edges() -> None:
    snapshot = build(story("US-0001"))

    assert snapshot.nodes == (
        DAGNode("US-0001", UserStoryStatus.PLANNED, 1, RiskLevel.LOW, ()),
    )
    assert snapshot.edges == ()


def test_linear_chain_uses_dependency_to_dependent_direction() -> None:
    snapshot = build(
        story("US-0001", status=UserStoryStatus.CERTIFIED),
        story("US-0002", depends_on=("US-0001",)),
        story("US-0003", depends_on=("US-0002",)),
    )

    assert snapshot.edges == (
        DAGEdge("US-0001", "US-0002"),
        DAGEdge("US-0002", "US-0003"),
    )
    assert snapshot.nodes[0].status is UserStoryStatus.CERTIFIED


def test_diamond_has_every_declared_edge_exactly_once() -> None:
    snapshot = build(
        story("US-0004", depends_on=("US-0003", "US-0002")),
        story("US-0003", depends_on=("US-0001",)),
        story("US-0002", depends_on=("US-0001",)),
        story("US-0001"),
    )

    assert snapshot.edges == (
        DAGEdge("US-0001", "US-0002"),
        DAGEdge("US-0001", "US-0003"),
        DAGEdge("US-0002", "US-0004"),
        DAGEdge("US-0003", "US-0004"),
    )
    assert snapshot.nodes[-1].depends_on == ("US-0002", "US-0003")


def test_multiple_disconnected_components_and_isolated_node_are_valid() -> None:
    snapshot = build(
        story("US-0005"),
        story("US-0004", depends_on=("US-0003",)),
        story("US-0003"),
        story("US-0002", depends_on=("US-0001",)),
        story("US-0001"),
    )

    assert tuple(node.user_story_id for node in snapshot.nodes) == (
        "US-0001",
        "US-0002",
        "US-0003",
        "US-0004",
        "US-0005",
    )
    assert len(snapshot.edges) == 2


def test_terminal_and_non_terminal_statuses_do_not_change_graph_structure() -> None:
    snapshot = build(
        story("US-0001", status=UserStoryStatus.CERTIFIED),
        story(
            "US-0002",
            depends_on=("US-0001",),
            status=UserStoryStatus.CANCELLED,
        ),
        story("US-0003", status=UserStoryStatus.BLOCKED),
        story("US-0004", status=UserStoryStatus.REJECTED),
    )

    assert len(snapshot.nodes) == 4
    assert snapshot.edges == (DAGEdge("US-0001", "US-0002"),)


def test_input_order_does_not_change_snapshot_or_source() -> None:
    first = story("US-0001")
    second = story("US-0002", depends_on=("US-0001",))
    third = story("US-0003", depends_on=("US-0002", "US-0001"))
    source = ProjectState(
        schema_version="1.0",
        user_stories=[third, first, second],
    )
    before = deepcopy(to_dict(source))

    projected = DAGValidator().build(source)
    reordered = build(second, third, first)

    assert projected == reordered
    assert to_dict(source) == before
    assert [item.id for item in source.user_stories] == [
        "US-0003",
        "US-0001",
        "US-0002",
    ]
    assert third.depends_on == ("US-0002", "US-0001")
    third.status = UserStoryStatus.CANCELLED
    assert projected.nodes[-1].status is UserStoryStatus.PLANNED


@pytest.mark.parametrize(
    ("candidate", "code"),
    [
        ((story("US-0001", depends_on=("US-9999",)),), "MISSING_DEPENDENCY"),
        ((story("US-0001", depends_on=("US-0001",)),), "SELF_DEPENDENCY"),
        (
            (
                story("US-0001", depends_on=("US-0002", "US-0002")),
                story("US-0002"),
            ),
            "INVALID_USER_STORY",
        ),
        ((story("US-0001", priority=0),), "INVALID_USER_STORY"),
        ((story("US-0001", status="MAGIC"),), "INVALID_USER_STORY"),
    ],
)
def test_invalid_graph_inputs_fail_closed_without_source_mutation(
    candidate: tuple[UserStory, ...], code: str
) -> None:
    state = ProjectState(schema_version="1.0", user_stories=list(candidate))
    before = deepcopy(to_dict(state))

    with pytest.raises(DAGValidationError) as captured:
        DAGValidator().build(state)

    assert captured.value.code == code
    assert to_dict(state) == before


def test_duplicate_node_is_refused_for_directly_constructed_project_state() -> None:
    state = ProjectState(
        schema_version="1.0",
        user_stories=[story("US-0001"), story("US-0001")],
    )

    with pytest.raises(DAGValidationError) as captured:
        DAGValidator().build(state)

    assert captured.value.code == "DUPLICATE_NODE"
    assert captured.value.subjects == ("US-0001",)


@pytest.mark.parametrize(
    "stories",
    [
        (
            story("US-0001", depends_on=("US-0002",)),
            story("US-0002", depends_on=("US-0001",)),
        ),
        (
            story("US-0001", depends_on=("US-0003",)),
            story("US-0002", depends_on=("US-0001",)),
            story("US-0003", depends_on=("US-0002",)),
        ),
        (
            story("US-0001"),
            story("US-0002", depends_on=("US-0003",)),
            story("US-0003", depends_on=("US-0002",)),
            story("US-0004"),
        ),
    ],
    ids=("two-node", "three-node", "disconnected-component"),
)
def test_cycles_are_refused_deterministically(stories: tuple[UserStory, ...]) -> None:
    with pytest.raises(DAGValidationError) as first:
        build(*stories)
    with pytest.raises(DAGValidationError) as second:
        build(*reversed(stories))

    assert first.value.code == "CYCLE_DETECTED"
    assert first.value.subjects == second.value.subjects
    assert first.value.message == second.value.message


def test_non_user_story_and_non_project_state_are_refused() -> None:
    invalid = ProjectState(schema_version="1.0")
    invalid.user_stories.append(object())  # type: ignore[arg-type]

    with pytest.raises(DAGValidationError, match="INVALID_USER_STORY"):
        DAGValidator().build(invalid)
    with pytest.raises(DAGValidationError, match="INVALID_PROJECT_STATE"):
        DAGValidator().build(object())  # type: ignore[arg-type]


def test_invalid_project_state_contract_is_refused() -> None:
    state = ProjectState(schema_version="2.0", user_stories=[story("US-0001")])

    with pytest.raises(DAGValidationError) as captured:
        DAGValidator().build(state)

    assert captured.value.code == "INVALID_PROJECT_STATE"


@pytest.mark.parametrize("size", (1, 2, 10, 100, 1500))
def test_generated_acyclic_chains_have_complete_canonical_edges(size: int) -> None:
    stories = [
        story(
            f"US-{index:04d}",
            depends_on=(() if index == 1 else (f"US-{index - 1:04d}",)),
        )
        for index in range(1, size + 1)
    ]

    snapshot = build(*reversed(stories))

    declared = {
        (dependency, item.id)
        for item in stories
        for dependency in item.depends_on
    }
    projected = {(edge.dependency_id, edge.dependent_id) for edge in snapshot.edges}
    assert projected == declared
    assert snapshot.edges == tuple(
        sorted(snapshot.edges, key=lambda edge: (edge.dependency_id, edge.dependent_id))
    )
    assert len(snapshot.edges) == max(0, size - 1)
