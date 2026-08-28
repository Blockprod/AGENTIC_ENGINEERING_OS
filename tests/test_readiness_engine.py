from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

import pytest

from agentic_engineering_os.application import (
    DAGValidator,
    ReadinessEngine,
    ReadinessEvaluationError,
)
from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    DAGEdge,
    DAGNode,
    DAGSnapshot,
    HumanApproval,
    ProjectState,
    ReadinessClassification,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
    to_dict,
)


NOW = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)


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
        description=f"Determine readiness for {identifier}.",
        status=status,
        priority=priority,
        risk=risk,
        depends_on=depends_on,
        scope=UserStoryScope(allowed_paths=("src/",), forbidden_paths=()),
        acceptance_criteria=(
            AcceptanceCriterion("AC-001", "Readiness is deterministic.", True),
        ),
        required_gates=(),
        human_approval=HumanApproval(
            required=approval_required,
            approved=approval_applied,
            approved_by="human-operator" if approval_applied else None,
            approved_at=NOW if approval_applied else None,
            evidence_ref="EV-APPROVAL-001" if approval_applied else None,
        ),
        metadata=UserStoryMetadata(NOW, "human-operator", NOW),
    )


def state(*stories: UserStory) -> ProjectState:
    return ProjectState(schema_version="1.0", user_stories=list(stories))


def evaluate(*stories: UserStory):
    project_state = state(*stories)
    dag = DAGValidator().build(project_state)
    return ReadinessEngine().evaluate(dag, project_state)


def by_id(snapshot, identifier: str):
    return next(node for node in snapshot.nodes if node.user_story_id == identifier)


def test_empty_dag_produces_empty_immutable_snapshot() -> None:
    result = evaluate()

    assert result.nodes == ()
    assert result.ready_ids == ()
    with pytest.raises(FrozenInstanceError):
        result.nodes = ()  # type: ignore[misc]


def test_single_planned_root_is_ready() -> None:
    result = evaluate(story("US-0001"))

    node = result.nodes[0]
    assert node.classification is ReadinessClassification.READY
    assert node.satisfied_dependencies == ()
    assert node.unsatisfied_dependencies == ()
    assert result.ready_ids == ("US-0001",)


def test_certified_dependency_makes_planned_dependent_ready() -> None:
    result = evaluate(
        story("US-0001", status=UserStoryStatus.CERTIFIED),
        story("US-0002", depends_on=("US-0001",)),
    )

    dependent = by_id(result, "US-0002")
    assert dependent.classification is ReadinessClassification.READY
    assert dependent.satisfied_dependencies == ("US-0001",)
    assert dependent.unsatisfied_dependencies == ()


def test_all_multiple_dependencies_must_be_certified() -> None:
    ready = evaluate(
        story("US-0001", status=UserStoryStatus.CERTIFIED),
        story("US-0002", status=UserStoryStatus.CERTIFIED),
        story("US-0003", depends_on=("US-0002", "US-0001")),
    )
    waiting = evaluate(
        story("US-0001", status=UserStoryStatus.CERTIFIED),
        story("US-0002", status=UserStoryStatus.TESTING),
        story("US-0003", depends_on=("US-0002", "US-0001")),
    )

    assert by_id(ready, "US-0003").classification is ReadinessClassification.READY
    node = by_id(waiting, "US-0003")
    assert node.classification is ReadinessClassification.WAITING_DEPENDENCIES
    assert node.satisfied_dependencies == ("US-0001",)
    assert node.unsatisfied_dependencies == ("US-0002",)


def test_multiple_roots_and_disconnected_components_are_independent() -> None:
    result = evaluate(
        story("US-0005"),
        story("US-0004", depends_on=("US-0003",)),
        story("US-0003", status=UserStoryStatus.CERTIFIED),
        story("US-0002", depends_on=("US-0001",)),
        story("US-0001", status=UserStoryStatus.TESTING),
    )

    assert result.ready_ids == ("US-0004", "US-0005")
    assert result.waiting_ids == ("US-0002",)
    assert result.ineligible_ids == ("US-0001",)
    assert result.terminal_ids == ("US-0003",)


@pytest.mark.parametrize(
    ("status", "classification"),
    [
        (UserStoryStatus.PROPOSED, ReadinessClassification.BLOCKED),
        (UserStoryStatus.PLANNED, ReadinessClassification.READY),
        (UserStoryStatus.BLOCKED, ReadinessClassification.BLOCKED),
        (UserStoryStatus.READY, ReadinessClassification.READY),
        (UserStoryStatus.IN_PROGRESS, ReadinessClassification.INELIGIBLE),
        (UserStoryStatus.IMPLEMENTED, ReadinessClassification.INELIGIBLE),
        (UserStoryStatus.TESTING, ReadinessClassification.INELIGIBLE),
        (UserStoryStatus.REVIEW, ReadinessClassification.INELIGIBLE),
        (UserStoryStatus.CERTIFICATION, ReadinessClassification.INELIGIBLE),
        (UserStoryStatus.CERTIFIED, ReadinessClassification.TERMINAL),
        (UserStoryStatus.REJECTED, ReadinessClassification.BLOCKED),
        (UserStoryStatus.REMEDIATION_REQUIRED, ReadinessClassification.BLOCKED),
        (UserStoryStatus.CANCELLED, ReadinessClassification.TERMINAL),
    ],
)
def test_own_state_policy_is_closed(
    status: UserStoryStatus,
    classification: ReadinessClassification,
) -> None:
    result = evaluate(story("US-0001", status=status))

    assert result.nodes[0].classification is classification


@pytest.mark.parametrize(
    "dependency_status",
    [status for status in UserStoryStatus if status is not UserStoryStatus.CERTIFIED],
)
def test_only_certified_satisfies_a_dependency(
    dependency_status: UserStoryStatus,
) -> None:
    result = evaluate(
        story("US-0001", status=dependency_status),
        story("US-0002", depends_on=("US-0001",)),
    )

    dependent = by_id(result, "US-0002")
    assert dependent.classification is ReadinessClassification.WAITING_DEPENDENCIES
    assert dependent.satisfied_dependencies == ()
    assert dependent.unsatisfied_dependencies == ("US-0001",)


def test_blocked_story_stays_blocked_with_certified_dependencies() -> None:
    result = evaluate(
        story("US-0001", status=UserStoryStatus.CERTIFIED),
        story(
            "US-0002",
            status=UserStoryStatus.BLOCKED,
            depends_on=("US-0001",),
        ),
    )

    assert by_id(result, "US-0002").classification is ReadinessClassification.BLOCKED


@pytest.mark.parametrize(
    "dependency_status",
    [
        UserStoryStatus.CANCELLED,
        UserStoryStatus.REJECTED,
        UserStoryStatus.BLOCKED,
    ],
)
def test_non_satisfying_terminal_or_blocked_dependency_never_readies_dependent(
    dependency_status: UserStoryStatus,
) -> None:
    result = evaluate(
        story("US-0001", status=dependency_status),
        story("US-0002", depends_on=("US-0001",)),
    )

    assert by_id(result, "US-0002").classification is (
        ReadinessClassification.WAITING_DEPENDENCIES
    )


def test_required_human_approval_must_be_authoritatively_applied() -> None:
    missing = evaluate(story("US-0001", approval_required=True))
    applied = evaluate(
        story("US-0001", approval_required=True, approval_applied=True)
    )

    assert missing.nodes[0].classification is ReadinessClassification.BLOCKED
    assert missing.nodes[0].reason == "REQUIRED_HUMAN_APPROVAL_NOT_APPLIED"
    assert applied.nodes[0].classification is ReadinessClassification.READY


@pytest.mark.parametrize("missing_field", ["approved_by", "approved_at", "evidence_ref"])
def test_incomplete_required_human_approval_fails_closed(missing_field: str) -> None:
    candidate = story("US-0001", approval_required=True, approval_applied=True)
    setattr(candidate.human_approval, missing_field, None)
    project_state = state(candidate)
    dag = DAGValidator().build(project_state)

    result = ReadinessEngine().evaluate(dag, project_state)

    assert result.nodes[0].classification is ReadinessClassification.BLOCKED


def test_reserved_codex_identity_cannot_satisfy_required_human_approval() -> None:
    candidate = story("US-0001", approval_required=True, approval_applied=True)
    candidate.human_approval.approved_by = "CoDeX/FakeHuman"
    project_state = state(candidate)
    dag = DAGValidator().build(project_state)

    result = ReadinessEngine().evaluate(dag, project_state)

    assert result.nodes[0].classification is ReadinessClassification.BLOCKED


def test_priority_and_risk_do_not_change_ready_classification() -> None:
    result = evaluate(
        story("US-0001", priority=99, risk=RiskLevel.CRITICAL),
        story("US-0002", priority=1, risk=RiskLevel.LOW),
    )

    assert result.ready_ids == ("US-0001", "US-0002")


def test_project_state_order_does_not_change_readiness_or_mutate_inputs() -> None:
    first = story("US-0001", status=UserStoryStatus.CERTIFIED)
    second = story("US-0002", depends_on=("US-0001",))
    third = story("US-0003")
    project_state = state(third, second, first)
    dag = DAGValidator().build(project_state)
    state_before = deepcopy(to_dict(project_state))
    dag_before = deepcopy(to_dict(dag))

    actual = ReadinessEngine().evaluate(dag, project_state)
    other_state = state(first, third, second)
    expected = ReadinessEngine().evaluate(
        DAGValidator().build(other_state), other_state
    )

    assert actual == expected
    assert to_dict(project_state) == state_before
    assert to_dict(dag) == dag_before
    assert tuple(node.user_story_id for node in actual.nodes) == (
        "US-0001",
        "US-0002",
        "US-0003",
    )


@pytest.mark.parametrize("field", ["status", "depends_on", "priority", "risk"])
def test_contractual_node_divergence_is_refused(field: str) -> None:
    candidate = story("US-0001")
    project_state = state(candidate)
    dag = DAGValidator().build(project_state)
    replacements = {
        "status": UserStoryStatus.READY,
        "depends_on": ("US-9999",),
        "priority": 2,
        "risk": RiskLevel.HIGH,
    }
    divergent_node = replace(dag.nodes[0], **{field: replacements[field]})
    divergent = replace(dag, nodes=(divergent_node,))
    before = deepcopy(to_dict(project_state))

    with pytest.raises(ReadinessEvaluationError) as captured:
        ReadinessEngine().evaluate(divergent, project_state)

    assert captured.value.code == "DAG_STATE_MISMATCH"
    assert to_dict(project_state) == before


def test_dag_node_absent_from_project_state_is_refused() -> None:
    project_state = state(story("US-0001"))
    canonical = DAGValidator().build(project_state)
    extra = DAGNode(
        "US-0002", UserStoryStatus.PLANNED, 1, RiskLevel.LOW, ()
    )
    divergent = replace(canonical, nodes=(*canonical.nodes, extra))

    with pytest.raises(ReadinessEvaluationError) as captured:
        ReadinessEngine().evaluate(divergent, project_state)

    assert captured.value.code == "MISSING_USER_STORY"
    assert captured.value.subjects == ("US-0002",)


def test_project_state_story_absent_from_dag_is_refused() -> None:
    project_state = state(story("US-0001"))

    with pytest.raises(ReadinessEvaluationError) as captured:
        ReadinessEngine().evaluate(DAGSnapshot(nodes=(), edges=()), project_state)

    assert captured.value.code == "DAG_STATE_MISMATCH"


@pytest.mark.parametrize("replacement_edges", [(), (DAGEdge("US-0002", "US-0001"),)])
def test_missing_or_invented_edge_is_refused(
    replacement_edges: tuple[DAGEdge, ...],
) -> None:
    project_state = state(
        story("US-0001", status=UserStoryStatus.CERTIFIED),
        story("US-0002", depends_on=("US-0001",)),
    )
    canonical = DAGValidator().build(project_state)
    divergent = replace(canonical, edges=replacement_edges)

    with pytest.raises(ReadinessEvaluationError) as captured:
        ReadinessEngine().evaluate(divergent, project_state)

    assert captured.value.code == "DAG_STATE_MISMATCH"


def test_structurally_invalid_dag_is_refused() -> None:
    project_state = state(story("US-0001"))
    canonical = DAGValidator().build(project_state)
    invalid = replace(canonical, nodes=(canonical.nodes[0], canonical.nodes[0]))

    with pytest.raises(ReadinessEvaluationError) as captured:
        ReadinessEngine().evaluate(invalid, project_state)

    assert captured.value.code == "INVALID_DAG"


def test_invalid_project_graph_is_refused_without_mutation() -> None:
    first = story("US-0001", depends_on=("US-0002",))
    second = story("US-0002", depends_on=("US-0001",))
    project_state = state(first, second)
    direct_dag = DAGSnapshot(
        nodes=(
            DAGNode("US-0001", first.status, 1, RiskLevel.LOW, first.depends_on),
            DAGNode("US-0002", second.status, 1, RiskLevel.LOW, second.depends_on),
        ),
        edges=(DAGEdge("US-0001", "US-0002"), DAGEdge("US-0002", "US-0001")),
    )
    before = deepcopy(to_dict(project_state))

    with pytest.raises(ReadinessEvaluationError) as captured:
        ReadinessEngine().evaluate(direct_dag, project_state)

    assert captured.value.code == "INVALID_DAG"
    assert to_dict(project_state) == before


def test_result_contains_no_wave_parallel_or_execution_assignment() -> None:
    serialized = to_dict(evaluate(story("US-0001")))
    rendered = repr(serialized).casefold()

    assert "wave" not in rendered
    assert "parallel" not in rendered
    assert "worktree" not in rendered
    assert "execution_batch" not in rendered
