"""Deterministic enforcement of the certified User Story state machine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from agentic_engineering_os.domain import UserStory, UserStoryStatus


Transition = tuple[UserStoryStatus, UserStoryStatus]

ALLOWED_TRANSITIONS: frozenset[Transition] = frozenset(
    {
        (UserStoryStatus.PROPOSED, UserStoryStatus.PLANNED),
        (UserStoryStatus.PROPOSED, UserStoryStatus.CANCELLED),
        (UserStoryStatus.PLANNED, UserStoryStatus.READY),
        (UserStoryStatus.PLANNED, UserStoryStatus.BLOCKED),
        (UserStoryStatus.PLANNED, UserStoryStatus.CANCELLED),
        (UserStoryStatus.BLOCKED, UserStoryStatus.READY),
        (UserStoryStatus.BLOCKED, UserStoryStatus.CANCELLED),
        (UserStoryStatus.READY, UserStoryStatus.IN_PROGRESS),
        (UserStoryStatus.READY, UserStoryStatus.BLOCKED),
        (UserStoryStatus.READY, UserStoryStatus.CANCELLED),
        (UserStoryStatus.IN_PROGRESS, UserStoryStatus.IMPLEMENTED),
        (UserStoryStatus.IN_PROGRESS, UserStoryStatus.BLOCKED),
        (UserStoryStatus.IN_PROGRESS, UserStoryStatus.CANCELLED),
        (UserStoryStatus.IMPLEMENTED, UserStoryStatus.TESTING),
        (UserStoryStatus.IMPLEMENTED, UserStoryStatus.CANCELLED),
        (UserStoryStatus.TESTING, UserStoryStatus.REVIEW),
        (UserStoryStatus.TESTING, UserStoryStatus.REJECTED),
        (UserStoryStatus.TESTING, UserStoryStatus.CANCELLED),
        (UserStoryStatus.REVIEW, UserStoryStatus.CERTIFICATION),
        (UserStoryStatus.REVIEW, UserStoryStatus.REJECTED),
        (UserStoryStatus.REVIEW, UserStoryStatus.CANCELLED),
        (UserStoryStatus.CERTIFICATION, UserStoryStatus.CERTIFIED),
        (UserStoryStatus.CERTIFICATION, UserStoryStatus.REJECTED),
        (UserStoryStatus.CERTIFICATION, UserStoryStatus.CANCELLED),
        (UserStoryStatus.REJECTED, UserStoryStatus.REMEDIATION_REQUIRED),
        (UserStoryStatus.REJECTED, UserStoryStatus.CANCELLED),
        (UserStoryStatus.REMEDIATION_REQUIRED, UserStoryStatus.READY),
        (UserStoryStatus.REMEDIATION_REQUIRED, UserStoryStatus.CANCELLED),
    }
)

TERMINAL_STATES: frozenset[UserStoryStatus] = frozenset(
    {UserStoryStatus.CERTIFIED, UserStoryStatus.CANCELLED}
)

_CERTIFIED_PROMOTION = (
    UserStoryStatus.CERTIFICATION,
    UserStoryStatus.CERTIFIED,
)


def _certified_authorization_boundary():
    @dataclass(frozen=True, slots=True)
    class CertifiedTransitionAuthorization:
        subject: str
        target_commit: str
        certification_id: str

    def issue(*, subject: str, target_commit: str, certification_id: str) -> object:
        return CertifiedTransitionAuthorization(
            subject=subject,
            target_commit=target_commit,
            certification_id=certification_id,
        )

    def matches(candidate: object, *, subject: str, target_commit: str) -> bool:
        return (
            isinstance(candidate, CertifiedTransitionAuthorization)
            and candidate.subject == subject
            and candidate.target_commit == target_commit
            and bool(candidate.certification_id)
        )

    return issue, matches


(
    _issue_certified_transition_authorization,
    _matches_certified_transition_authorization,
) = _certified_authorization_boundary()


@dataclass(frozen=True, slots=True)
class TransitionContext:
    """Only facts currently available to the transition decision."""

    preconditions_proven: bool | None = None
    dependency_statuses: Mapping[str, UserStoryStatus | str] | None = None
    target_commit: str | None = None


@dataclass(frozen=True, slots=True)
class TransitionRefusal:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class TransitionResult:
    source: str
    target: str
    refusals: tuple[TransitionRefusal, ...] = ()

    @property
    def allowed(self) -> bool:
        return not self.refusals


class TransitionError(RuntimeError):
    """A technical error prevented a reliable transition decision."""


class StateTransitionService:
    """Evaluate and apply only transitions proven by explicit context."""

    def evaluate(
        self,
        source: UserStoryStatus | str,
        target: UserStoryStatus | str,
        *,
        required_dependencies: tuple[str, ...] = (),
        context: TransitionContext | None = None,
    ) -> TransitionResult:
        try:
            return self._evaluate(
                source,
                target,
                required_dependencies,
                context,
                subject=None,
                authorization=None,
            )
        except Exception as error:
            raise TransitionError(
                f"transition evaluation could not be completed: "
                f"{type(error).__name__}: {error}"
            ) from error

    def apply(
        self,
        user_story: UserStory,
        target: UserStoryStatus | str,
        *,
        context: TransitionContext | None = None,
    ) -> TransitionResult:
        """Apply an allowed transition by changing only ``status``."""

        return self._apply(
            user_story,
            target,
            context=context,
            authorization=None,
        )

    def _apply_authorized(
        self,
        user_story: UserStory,
        target: UserStoryStatus | str,
        *,
        context: TransitionContext,
        authorization: object,
    ) -> TransitionResult:
        """Apply using an internal capability resolved from authoritative state."""

        return self._apply(
            user_story,
            target,
            context=context,
            authorization=authorization,
        )

    def _apply(
        self,
        user_story: UserStory,
        target: UserStoryStatus | str,
        *,
        context: TransitionContext | None,
        authorization: object | None,
    ) -> TransitionResult:
        try:
            if authorization is None:
                result = self.evaluate(
                    user_story.status,
                    target,
                    required_dependencies=user_story.depends_on,
                    context=context,
                )
            else:
                result = self._evaluate(
                    user_story.status,
                    target,
                    user_story.depends_on,
                    context,
                    subject=user_story.id,
                    authorization=authorization,
                )
            if result.allowed:
                user_story.status = UserStoryStatus(result.target)
            return result
        except TransitionError:
            raise
        except Exception as error:
            raise TransitionError(
                f"transition application could not be completed: "
                f"{type(error).__name__}: {error}"
            ) from error

    @staticmethod
    def _evaluate(
        source: UserStoryStatus | str,
        target: UserStoryStatus | str,
        required_dependencies: tuple[str, ...],
        context: TransitionContext | None,
        *,
        subject: str | None,
        authorization: object | None,
    ) -> TransitionResult:
        source_state = _known_state(source)
        target_state = _known_state(target)
        source_label = _state_label(source)
        target_label = _state_label(target)

        if source_state is None:
            return _refused(
                source_label,
                target_label,
                "UNKNOWN_SOURCE_STATE",
                f"unknown source state: {source_label}",
            )
        if target_state is None:
            return _refused(
                source_label,
                target_label,
                "UNKNOWN_TARGET_STATE",
                f"unknown target state: {target_label}",
            )
        if source_state in TERMINAL_STATES:
            return _refused(
                source_label,
                target_label,
                "TERMINAL_SOURCE_STATE",
                f"terminal state {source_label} has no outgoing transitions",
            )
        if (source_state, target_state) not in ALLOWED_TRANSITIONS:
            return _refused(
                source_label,
                target_label,
                "TRANSITION_NOT_ALLOWED",
                f"transition {source_label} -> {target_label} is not declared",
            )
        if context is None:
            return _refused(
                source_label,
                target_label,
                "CONTEXT_REQUIRED",
                "transition preconditions require explicit context",
            )
        if context.preconditions_proven is not True:
            return _refused(
                source_label,
                target_label,
                "PRECONDITIONS_NOT_PROVEN",
                "mandatory transition preconditions are not proven",
            )
        if (source_state, target_state) == _CERTIFIED_PROMOTION and not (
            subject is not None
            and isinstance(context.target_commit, str)
            and _matches_certified_transition_authorization(
                authorization,
                subject=subject,
                target_commit=context.target_commit,
            )
        ):
            return _refused(
                source_label,
                target_label,
                "AUTHORITATIVE_PRECONDITION_REQUIRED",
                "promotion to CERTIFIED requires trusted authorization",
            )

        dependency_refusals = _dependency_refusals(
            target_state, required_dependencies, context.dependency_statuses
        )
        return TransitionResult(
            source=source_label,
            target=target_label,
            refusals=dependency_refusals,
        )


def _known_state(value: UserStoryStatus | str) -> UserStoryStatus | None:
    try:
        return UserStoryStatus(value)
    except (TypeError, ValueError):
        return None


def _state_label(value: object) -> str:
    if isinstance(value, UserStoryStatus):
        return value.value
    return str(value)


def _refused(
    source: str, target: str, code: str, message: str
) -> TransitionResult:
    return TransitionResult(
        source=source,
        target=target,
        refusals=(TransitionRefusal(code=code, message=message),),
    )


def _dependency_refusals(
    target: UserStoryStatus,
    required_dependencies: tuple[str, ...],
    dependency_statuses: Mapping[str, UserStoryStatus | str] | None,
) -> tuple[TransitionRefusal, ...]:
    if target is not UserStoryStatus.READY or not required_dependencies:
        return ()
    if dependency_statuses is None:
        return (
            TransitionRefusal(
                code="DEPENDENCY_CONTEXT_REQUIRED",
                message="dependency states are required for a transition to READY",
            ),
        )

    refusals: list[TransitionRefusal] = []
    for dependency_id in dict.fromkeys(required_dependencies):
        if dependency_id not in dependency_statuses:
            refusals.append(
                TransitionRefusal(
                    code="DEPENDENCY_MISSING",
                    message=f"dependency is absent from context: {dependency_id}",
                )
            )
            continue
        dependency_state = _known_state(dependency_statuses[dependency_id])
        if dependency_state is None:
            refusals.append(
                TransitionRefusal(
                    code="DEPENDENCY_STATE_UNKNOWN",
                    message=f"dependency has an unknown state: {dependency_id}",
                )
            )
        elif dependency_state is not UserStoryStatus.CERTIFIED:
            refusals.append(
                TransitionRefusal(
                    code="DEPENDENCY_NOT_CERTIFIED",
                    message=(
                        f"dependency {dependency_id} is {dependency_state.value}, "
                        "not CERTIFIED"
                    ),
                )
            )
    return tuple(refusals)
