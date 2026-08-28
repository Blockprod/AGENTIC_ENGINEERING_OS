from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

import pytest

from agentic_engineering_os.application import (
    DAGValidator,
    ExecutionConflictAnalyzer,
    ExecutionConflictError,
    ReadinessEngine,
    WavePlanner,
)
from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    ConflictAnalysis,
    ConflictClassification,
    ConflictReason,
    ExecutionWave,
    HumanApproval,
    ProjectState,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
    WaveMember,
    WavePlan,
    to_dict,
)


NOW = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)


def story(
    identifier: str,
    *,
    allowed: tuple[str, ...],
    forbidden: tuple[str, ...] = (),
    depends_on: tuple[str, ...] = (),
    priority: int = 1,
    risk: RiskLevel = RiskLevel.LOW,
) -> UserStory:
    return UserStory(
        schema_version="1.0",
        id=identifier,
        title=f"Story {identifier}",
        description=f"Analyze scope compatibility for {identifier}.",
        status=UserStoryStatus.PLANNED,
        priority=priority,
        risk=risk,
        depends_on=depends_on,
        scope=UserStoryScope(allowed_paths=allowed, forbidden_paths=forbidden),
        acceptance_criteria=(
            AcceptanceCriterion("AC-001", "Conflict is deterministic.", True),
        ),
        required_gates=(),
        human_approval=HumanApproval(False, False, None, None),
        metadata=UserStoryMetadata(NOW, "human-operator", NOW),
    )


def state(*stories: UserStory) -> ProjectState:
    return ProjectState(schema_version="1.0", user_stories=list(stories))


def prepare(*stories: UserStory):
    project_state = state(*stories)
    dag = DAGValidator().build(project_state)
    readiness = ReadinessEngine().evaluate(dag, project_state)
    wave_plan = WavePlanner().plan(dag, readiness, project_state)
    return project_state, wave_plan


def analyze(*stories: UserStory) -> ConflictAnalysis:
    project_state, wave_plan = prepare(*stories)
    return ExecutionConflictAnalyzer().analyze(wave_plan, project_state)


def pair(analysis: ConflictAnalysis, left: str, right: str):
    return next(
        item
        for item in analysis.pairs
        if item.left_user_story_id == left and item.right_user_story_id == right
    )


def test_empty_and_single_member_waves_have_no_pairs() -> None:
    assert analyze().pairs == ()
    assert analyze(story("US-0001", allowed=("src/a.py",))).pairs == ()


def test_exact_same_file_is_conflict() -> None:
    result = analyze(
        story("US-0001", allowed=("src/core.py",)),
        story("US-0002", allowed=("src/core.py",)),
    )

    collision = result.pairs[0]
    assert collision.classification is ConflictClassification.CONFLICT
    assert collision.reasons == (ConflictReason.PATH_OVERLAP,)
    assert collision.overlapping_paths == ("src/core.py",)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("src/auth/", "src/auth/models.py"),
        ("src/auth/models.py", "src/auth/"),
        ("src/", "src/auth/"),
        ("src/auth/", "src/"),
    ],
)
def test_ancestor_descendant_overlap_is_symmetric(left: str, right: str) -> None:
    result = analyze(
        story("US-0001", allowed=(left,)),
        story("US-0002", allowed=(right,)),
    )

    assert result.pairs[0].classification is ConflictClassification.CONFLICT
    assert result.pairs[0].overlapping_paths == (
        "src/auth/models.py" if "models.py" in left + right else "src/auth/",
    )


def test_disjoint_directories_are_safe() -> None:
    result = analyze(
        story("US-0001", allowed=("src/auth/",)),
        story("US-0002", allowed=("src/payments/",)),
    )

    assert result.safe_pairs == result.pairs
    assert result.pairs[0].reasons == ()
    assert result.pairs[0].overlapping_paths == ()


def test_file_and_same_named_directory_are_disjoint_by_explicit_convention() -> None:
    result = analyze(
        story("US-0001", allowed=("src/auth",)),
        story("US-0002", allowed=("src/auth/",)),
    )

    assert result.pairs[0].classification is ConflictClassification.SAFE


def test_casefold_policy_is_repository_deterministic() -> None:
    result = analyze(
        story("US-0001", allowed=("SRC/Auth/Models.py",)),
        story("US-0002", allowed=("src/auth/models.py",)),
    )

    assert result.pairs[0].classification is ConflictClassification.CONFLICT
    assert result.pairs[0].overlapping_paths == ("src/auth/models.py",)


def test_three_node_matrix_is_complete_and_canonical() -> None:
    result = analyze(
        story("US-0003", allowed=("src/c/",)),
        story("US-0001", allowed=("src/a/",)),
        story("US-0002", allowed=("src/a/file.py",)),
    )

    assert tuple(
        (item.left_user_story_id, item.right_user_story_id)
        for item in result.pairs
    ) == (
        ("US-0001", "US-0002"),
        ("US-0001", "US-0003"),
        ("US-0002", "US-0003"),
    )
    assert pair(result, "US-0001", "US-0002").classification is (
        ConflictClassification.CONFLICT
    )
    assert pair(result, "US-0001", "US-0003").classification is (
        ConflictClassification.SAFE
    )
    assert pair(result, "US-0002", "US-0003").classification is (
        ConflictClassification.SAFE
    )


def test_multiple_waves_analyze_only_same_wave_pairs() -> None:
    result = analyze(
        story("US-0001", allowed=("src/a/",)),
        story("US-0002", allowed=("src/b/",)),
        story("US-0003", allowed=("src/c/",), depends_on=("US-0001",)),
        story("US-0004", allowed=("src/d/",), depends_on=("US-0002",)),
    )

    assert tuple(
        (item.wave_index, item.left_user_story_id, item.right_user_story_id)
        for item in result.pairs
    ) == (
        (0, "US-0001", "US-0002"),
        (1, "US-0003", "US-0004"),
    )


def test_empty_scope_is_unknown_never_safe() -> None:
    result = analyze(
        story("US-0001", allowed=()),
        story("US-0002", allowed=("src/a.py",)),
    )

    unknown = result.pairs[0]
    assert unknown.classification is ConflictClassification.UNKNOWN
    assert unknown.reasons == (ConflictReason.SCOPE_UNSPECIFIED,)


def test_canonical_duplicate_scope_is_unknown_when_no_overlap_is_proven() -> None:
    result = analyze(
        story("US-0001", allowed=("src/A.py", "src/a.py")),
        story("US-0002", allowed=("src/b.py",)),
    )

    assert result.pairs[0].classification is ConflictClassification.UNKNOWN
    assert result.pairs[0].reasons == (ConflictReason.SCOPE_AMBIGUOUS,)


def test_fully_forbidden_allowed_region_is_ambiguous_not_safe() -> None:
    result = analyze(
        story("US-0001", allowed=("src/a.py",), forbidden=("src/a.py",)),
        story("US-0002", allowed=("src/a.py",)),
    )

    assert result.pairs[0].classification is ConflictClassification.UNKNOWN
    assert result.pairs[0].reasons == (ConflictReason.SCOPE_AMBIGUOUS,)
    assert result.pairs[0].overlapping_paths == ()


def test_shared_forbidden_path_does_not_create_conflict() -> None:
    result = analyze(
        story("US-0001", allowed=("src/a.py",), forbidden=("secrets/",)),
        story("US-0002", allowed=("src/b.py",), forbidden=("secrets/",)),
    )

    assert result.pairs[0].classification is ConflictClassification.SAFE


def test_forbidden_subtree_is_removed_from_possible_overlap() -> None:
    result = analyze(
        story("US-0001", allowed=("src/",), forbidden=("src/auth/",)),
        story("US-0002", allowed=("src/auth/",)),
    )

    assert result.pairs[0].classification is ConflictClassification.SAFE
    assert result.pairs[0].overlapping_paths == ()


@pytest.mark.parametrize(
    "invalid_path",
    [
        " src/a.py",
        "../src/a.py",
        "C:/repo/src/a.py",
        "/src/a.py",
        "src\\a.py",
        "src//a.py",
    ],
)
def test_invalid_absolute_traversal_or_ambiguous_path_is_refused(
    invalid_path: str,
) -> None:
    project_state, wave_plan = prepare(
        story("US-0001", allowed=(invalid_path,)),
        story("US-0002", allowed=("src/b.py",)),
    )
    before = (deepcopy(to_dict(project_state)), deepcopy(to_dict(wave_plan)))

    with pytest.raises(ExecutionConflictError) as captured:
        ExecutionConflictAnalyzer().analyze(wave_plan, project_state)

    assert captured.value.code == "INVALID_SCOPE"
    assert (to_dict(project_state), to_dict(wave_plan)) == before


def test_priority_and_risk_never_create_conflict() -> None:
    result = analyze(
        story("US-0001", allowed=("src/a.py",), priority=1, risk=RiskLevel.CRITICAL),
        story("US-0002", allowed=("src/b.py",), priority=99, risk=RiskLevel.CRITICAL),
    )

    assert result.pairs[0].classification is ConflictClassification.SAFE


def test_forged_wave_plan_is_refused() -> None:
    project_state, wave_plan = prepare(
        story("US-0001", allowed=("src/a.py",)),
        story("US-0002", allowed=("src/b.py",)),
    )
    forged_member = replace(wave_plan.waves[0].members[0], priority=99)
    forged_wave = replace(
        wave_plan.waves[0],
        members=(forged_member, *wave_plan.waves[0].members[1:]),
    )

    with pytest.raises(ExecutionConflictError) as captured:
        ExecutionConflictAnalyzer().analyze(
            replace(wave_plan, waves=(forged_wave,)), project_state
        )

    assert captured.value.code == "WAVE_STATE_MISMATCH"


def test_repeated_member_and_pair_are_refused_by_wave_consistency() -> None:
    project_state, wave_plan = prepare(
        story("US-0001", allowed=("src/a.py",)),
        story("US-0002", allowed=("src/b.py",)),
    )
    member = wave_plan.waves[0].members[0]
    duplicate = replace(
        wave_plan,
        waves=(replace(wave_plan.waves[0], members=(member, member)),),
    )

    with pytest.raises(ExecutionConflictError, match="WAVE_STATE_MISMATCH"):
        ExecutionConflictAnalyzer().analyze(duplicate, project_state)


def test_story_absent_from_state_is_refused() -> None:
    expanded_state, expanded_plan = prepare(
        story("US-0001", allowed=("src/a.py",)),
        story("US-0002", allowed=("src/b.py",)),
    )
    reduced_state = state(story("US-0001", allowed=("src/a.py",)))

    with pytest.raises(ExecutionConflictError) as captured:
        ExecutionConflictAnalyzer().analyze(expanded_plan, reduced_state)

    assert captured.value.code == "WAVE_STATE_MISMATCH"
    assert len(expanded_state.user_stories) == 2


def test_direct_dependency_hidden_in_same_wave_is_refused() -> None:
    project_state, canonical = prepare(
        story("US-0001", allowed=("src/a.py",)),
        story("US-0002", allowed=("src/b.py",), depends_on=("US-0001",)),
    )
    members = tuple(member for wave in canonical.waves for member in wave.members)
    forged = WavePlan(
        waves=(ExecutionWave(wave_index=0, members=members),),
        deferred=canonical.deferred,
    )

    with pytest.raises(ExecutionConflictError) as captured:
        ExecutionConflictAnalyzer().analyze(forged, project_state)

    assert captured.value.code == "WAVE_STATE_MISMATCH"


def test_analysis_does_not_change_wave_or_create_safe_groups() -> None:
    project_state, wave_plan = prepare(
        story("US-0001", allowed=("src/a/",)),
        story("US-0002", allowed=("src/a/file.py",)),
        story("US-0003", allowed=("src/c/",)),
    )
    before = (deepcopy(to_dict(project_state)), deepcopy(to_dict(wave_plan)))

    result = ExecutionConflictAnalyzer().analyze(wave_plan, project_state)

    assert (to_dict(project_state), to_dict(wave_plan)) == before
    assert len(wave_plan.waves) == 1
    rendered = repr(to_dict(result)).casefold()
    assert "group" not in rendered
    assert "worktree" not in rendered
    assert "authorization" not in rendered


def test_recomputation_reflects_authoritative_scope_change() -> None:
    initial = analyze(
        story("US-0001", allowed=("src/a/",)),
        story("US-0002", allowed=("src/b/",)),
    )
    changed = analyze(
        story("US-0001", allowed=("src/a/",)),
        story("US-0002", allowed=("src/a/sub/",)),
    )

    assert initial.pairs[0].classification is ConflictClassification.SAFE
    assert changed.pairs[0].classification is ConflictClassification.CONFLICT


def test_pair_order_and_analysis_are_deterministic_from_state_order() -> None:
    first = (
        story("US-0003", allowed=("src/c.py",)),
        story("US-0001", allowed=("src/a.py",)),
        story("US-0002", allowed=("src/b.py",)),
    )
    second = (first[1], first[2], first[0])

    assert analyze(*first) == analyze(*second)


def test_pair_model_is_immutable_and_derived_properties_are_not_sources() -> None:
    result = analyze(
        story("US-0001", allowed=("src/a.py",)),
        story("US-0002", allowed=("src/b.py",)),
    )

    assert result.safe_pairs == result.pairs
    assert result.conflicting_pairs == ()
    assert result.unknown_pairs == ()
    with pytest.raises(FrozenInstanceError):
        result.pairs = ()  # type: ignore[misc]
