import inspect
import re
from datetime import datetime
from pathlib import Path

import pytest

import agentic_engineering_os.application as application_module
from agentic_engineering_os.application import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    StateTransitionService,
    TransitionContext,
    TransitionError,
)
from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    HumanApproval,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
    to_dict,
)


ROOT = Path(__file__).resolve().parents[1]
TIMESTAMP = datetime.fromisoformat("2026-08-27T10:00:00+02:00")
PROVEN = TransitionContext(preconditions_proven=True)


def make_user_story(
    status: UserStoryStatus = UserStoryStatus.PROPOSED,
    depends_on: tuple[str, ...] = (),
) -> UserStory:
    return UserStory(
        schema_version="1.0",
        id="US-0001",
        title="State transition engine",
        description="Apply only certified state transitions.",
        status=status,
        priority=1,
        risk=RiskLevel.LOW,
        depends_on=depends_on,
        scope=UserStoryScope(allowed_paths=("src/",), forbidden_paths=()),
        acceptance_criteria=(
            AcceptanceCriterion(
                id="AC-001",
                description="Only declared transitions are allowed.",
                mandatory=True,
            ),
        ),
        required_gates=("GATE-001",),
        human_approval=HumanApproval(
            required=False,
            approved=False,
            approved_by=None,
            approved_at=None,
        ),
        metadata=UserStoryMetadata(
            created_at=TIMESTAMP,
            created_by="human-operator",
            updated_at=TIMESTAMP,
        ),
    )


def normative_transitions() -> frozenset[tuple[UserStoryStatus, UserStoryStatus]]:
    document = (ROOT / "docs" / "05-state-machine.md").read_text(encoding="utf-8")
    pairs = re.findall(
        r"^\| `([A-Z_]+)` \| `([A-Z_]+)` \| Oui \|", document, re.MULTILINE
    )
    return frozenset(
        (UserStoryStatus(source), UserStoryStatus(target))
        for source, target in pairs
    )


def test_runtime_catalog_equals_the_28_normative_transitions() -> None:
    expected = normative_transitions()

    assert len(expected) == 28
    assert len(ALLOWED_TRANSITIONS) == 28
    assert ALLOWED_TRANSITIONS == expected


def test_all_28_normative_transitions_are_recognized() -> None:
    service = StateTransitionService()

    for source, target in ALLOWED_TRANSITIONS:
        result = service.evaluate(source, target, context=PROVEN)
        if (
            source is UserStoryStatus.CERTIFICATION
            and target is UserStoryStatus.CERTIFIED
        ):
            assert not result.allowed
            assert (
                result.refusals[0].code
                == "AUTHORITATIVE_PRECONDITION_REQUIRED"
            )
        else:
            assert result.allowed, (source, target, result.refusals)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (UserStoryStatus.PROPOSED, UserStoryStatus.PLANNED),
        (UserStoryStatus.READY, UserStoryStatus.IN_PROGRESS),
        (UserStoryStatus.IMPLEMENTED, UserStoryStatus.TESTING),
        (UserStoryStatus.REVIEW, UserStoryStatus.CERTIFICATION),
    ],
)
def test_representative_normal_transitions_pass(
    source: UserStoryStatus, target: UserStoryStatus
) -> None:
    result = StateTransitionService().evaluate(source, target, context=PROVEN)

    assert result.allowed
    assert result.source == source.value
    assert result.target == target.value
    assert result.refusals == ()


def test_direct_caller_boolean_cannot_authorize_certified_promotion() -> None:
    story = make_user_story(UserStoryStatus.CERTIFICATION)
    before = to_dict(story)

    result = StateTransitionService().apply(
        story,
        UserStoryStatus.CERTIFIED,
        context=TransitionContext(preconditions_proven=True),
    )

    assert not result.allowed
    assert result.refusals[0].code == "AUTHORITATIVE_PRECONDITION_REQUIRED"
    assert to_dict(story) == before


@pytest.mark.parametrize(
    ("context", "expected_code"),
    [
        (None, "CONTEXT_REQUIRED"),
        (
            TransitionContext(preconditions_proven=False),
            "PRECONDITIONS_NOT_PROVEN",
        ),
        (
            TransitionContext(
                preconditions_proven=True,
                dependency_statuses={"fabricated": UserStoryStatus.CERTIFIED},
                target_commit="a" * 40,
            ),
            "AUTHORITATIVE_PRECONDITION_REQUIRED",
        ),
    ],
)
def test_direct_context_without_authoritative_proof_cannot_promote(
    context: TransitionContext | None,
    expected_code: str,
) -> None:
    story = make_user_story(UserStoryStatus.CERTIFICATION)
    before = to_dict(story)

    result = StateTransitionService().apply(
        story,
        UserStoryStatus.CERTIFIED,
        context=context,
    )

    assert not result.allowed
    assert result.refusals[0].code == expected_code
    assert to_dict(story) == before


def test_public_transition_api_exposes_no_authorization_constructor() -> None:
    assert "authorization" not in inspect.signature(
        StateTransitionService.apply
    ).parameters
    assert "authorization" not in inspect.signature(
        StateTransitionService.evaluate
    ).parameters
    assert not any("Authorization" in name for name in application_module.__all__)


def test_arbitrary_object_cannot_fabricate_trusted_authorization() -> None:
    story = make_user_story(UserStoryStatus.CERTIFICATION)
    before = to_dict(story)

    result = StateTransitionService()._apply_authorized(
        story,
        UserStoryStatus.CERTIFIED,
        context=TransitionContext(
            preconditions_proven=True,
            target_commit="a" * 40,
        ),
        authorization=object(),
    )

    assert not result.allowed
    assert result.refusals[0].code == "AUTHORITATIVE_PRECONDITION_REQUIRED"
    assert to_dict(story) == before


def test_transition_to_ready_accepts_only_certified_dependencies() -> None:
    story = make_user_story(UserStoryStatus.PLANNED, ("US-0002", "US-0003"))
    context = TransitionContext(
        preconditions_proven=True,
        dependency_statuses={
            "US-0002": UserStoryStatus.CERTIFIED,
            "US-0003": UserStoryStatus.CERTIFIED,
        },
    )

    result = StateTransitionService().apply(
        story, UserStoryStatus.READY, context=context
    )

    assert result.allowed
    assert story.status is UserStoryStatus.READY


def test_remediation_path_is_allowed_without_shortcuts() -> None:
    story = make_user_story(UserStoryStatus.REJECTED)
    service = StateTransitionService()

    first = service.apply(story, UserStoryStatus.REMEDIATION_REQUIRED, context=PROVEN)
    second = service.apply(story, UserStoryStatus.READY, context=PROVEN)

    assert first.allowed
    assert second.allowed
    assert story.status is UserStoryStatus.READY


def test_apply_changes_only_status() -> None:
    story = make_user_story()
    before = to_dict(story)

    result = StateTransitionService().apply(
        story, UserStoryStatus.PLANNED, context=PROVEN
    )
    after = to_dict(story)

    assert result.allowed
    assert after["status"] == "PLANNED"
    assert {key: value for key, value in after.items() if key != "status"} == {
        key: value for key, value in before.items() if key != "status"
    }


def test_undeclared_transition_is_refused() -> None:
    result = StateTransitionService().evaluate(
        UserStoryStatus.PROPOSED, UserStoryStatus.CERTIFIED, context=PROVEN
    )

    assert not result.allowed
    assert result.refusals[0].code == "TRANSITION_NOT_ALLOWED"


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES, key=lambda item: item.value))
def test_terminal_states_have_no_outgoing_transition(
    terminal: UserStoryStatus,
) -> None:
    service = StateTransitionService()

    for target in UserStoryStatus:
        result = service.evaluate(terminal, target, context=PROVEN)
        assert not result.allowed
        assert result.refusals[0].code == "TERMINAL_SOURCE_STATE"

    assert not any(source is terminal for source, _ in ALLOWED_TRANSITIONS)


@pytest.mark.parametrize(
    "target", [UserStoryStatus.CERTIFIED, UserStoryStatus.IN_PROGRESS]
)
def test_rejected_shortcuts_are_refused(target: UserStoryStatus) -> None:
    result = StateTransitionService().evaluate(
        UserStoryStatus.REJECTED, target, context=PROVEN
    )

    assert not result.allowed
    assert result.refusals[0].code == "TRANSITION_NOT_ALLOWED"


@pytest.mark.parametrize(
    "dependency_status",
    [
        UserStoryStatus.IMPLEMENTED,
        UserStoryStatus.TESTING,
        UserStoryStatus.REVIEW,
        UserStoryStatus.CERTIFICATION,
        UserStoryStatus.REJECTED,
        UserStoryStatus.BLOCKED,
        UserStoryStatus.CANCELLED,
    ],
)
def test_non_certified_dependency_blocks_ready(
    dependency_status: UserStoryStatus,
) -> None:
    result = StateTransitionService().evaluate(
        UserStoryStatus.PLANNED,
        UserStoryStatus.READY,
        required_dependencies=("US-0002",),
        context=TransitionContext(
            preconditions_proven=True,
            dependency_statuses={"US-0002": dependency_status},
        ),
    )

    assert not result.allowed
    assert result.refusals[0].code == "DEPENDENCY_NOT_CERTIFIED"


def test_missing_dependency_context_blocks_ready() -> None:
    result = StateTransitionService().evaluate(
        UserStoryStatus.PLANNED,
        UserStoryStatus.READY,
        required_dependencies=("US-0002",),
        context=PROVEN,
    )

    assert not result.allowed
    assert result.refusals[0].code == "DEPENDENCY_CONTEXT_REQUIRED"


def test_dependency_absent_from_context_blocks_ready() -> None:
    result = StateTransitionService().evaluate(
        UserStoryStatus.PLANNED,
        UserStoryStatus.READY,
        required_dependencies=("US-0002",),
        context=TransitionContext(preconditions_proven=True, dependency_statuses={}),
    )

    assert not result.allowed
    assert result.refusals[0].code == "DEPENDENCY_MISSING"


def test_dependency_with_unknown_state_blocks_ready() -> None:
    result = StateTransitionService().evaluate(
        UserStoryStatus.PLANNED,
        UserStoryStatus.READY,
        required_dependencies=("US-0002",),
        context=TransitionContext(
            preconditions_proven=True,
            dependency_statuses={"US-0002": "MAGIC"},
        ),
    )

    assert not result.allowed
    assert result.refusals[0].code == "DEPENDENCY_STATE_UNKNOWN"


@pytest.mark.parametrize(
    ("source", "target", "code"),
    [
        ("MAGIC", UserStoryStatus.PLANNED, "UNKNOWN_SOURCE_STATE"),
        (UserStoryStatus.PROPOSED, "MAGIC", "UNKNOWN_TARGET_STATE"),
    ],
)
def test_unknown_states_are_refused(
    source: UserStoryStatus | str,
    target: UserStoryStatus | str,
    code: str,
) -> None:
    result = StateTransitionService().evaluate(source, target, context=PROVEN)

    assert not result.allowed
    assert result.refusals[0].code == code


def test_absent_context_is_refused() -> None:
    result = StateTransitionService().evaluate(
        UserStoryStatus.PROPOSED, UserStoryStatus.PLANNED
    )

    assert not result.allowed
    assert result.refusals[0].code == "CONTEXT_REQUIRED"


@pytest.mark.parametrize("proven", [None, False])
def test_unproven_mandatory_precondition_is_refused(proven: bool | None) -> None:
    result = StateTransitionService().evaluate(
        UserStoryStatus.PROPOSED,
        UserStoryStatus.PLANNED,
        context=TransitionContext(preconditions_proven=proven),
    )

    assert not result.allowed
    assert result.refusals[0].code == "PRECONDITIONS_NOT_PROVEN"


def test_refusal_does_not_mutate_user_story() -> None:
    story = make_user_story(UserStoryStatus.REJECTED)
    before = to_dict(story)

    result = StateTransitionService().apply(
        story, UserStoryStatus.CERTIFIED, context=PROVEN
    )

    assert not result.allowed
    assert to_dict(story) == before


def test_unexpected_error_does_not_mutate_user_story(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    story = make_user_story()
    before = to_dict(story)

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("unexpected evaluation failure")

    monkeypatch.setattr(StateTransitionService, "evaluate", explode)

    with pytest.raises(TransitionError, match="application could not be completed"):
        StateTransitionService().apply(
            story, UserStoryStatus.PLANNED, context=PROVEN
        )

    assert to_dict(story) == before
