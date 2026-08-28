"""Deterministic same-Wave conflict analysis from canonical User Story scopes."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from agentic_engineering_os.domain import (
    ConflictAnalysis,
    ConflictClassification,
    ConflictReason,
    ExecutionConflict,
    ProjectState,
    UserStory,
    UserStoryScope,
    WavePlan,
    to_dict,
)

from .contract_validator import ContractValidator
from .dag_validator import DAGValidationError, DAGValidator
from .implementer import ImplementerInputError, _normalize_path
from .readiness_engine import ReadinessEngine, ReadinessEvaluationError
from .wave_planner import WavePlanner, WavePlanningError


class ExecutionConflictError(RuntimeError):
    """Conflict compatibility cannot be determined reliably."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        subjects: tuple[str, ...] = (),
    ) -> None:
        self.code = code
        self.message = message
        self.subjects = subjects
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class _CanonicalScope:
    allowed: tuple[str, ...]
    forbidden: tuple[str, ...]
    unspecified: bool
    ambiguous: bool


class ExecutionConflictAnalyzer:
    """Analyze pairwise scope compatibility without changing logical Waves."""

    def __init__(
        self,
        *,
        dag_validator: DAGValidator | None = None,
        readiness_engine: ReadinessEngine | None = None,
        wave_planner: WavePlanner | None = None,
        contract_validator: ContractValidator | None = None,
    ) -> None:
        self._dag_validator = dag_validator or DAGValidator()
        self._readiness_engine = readiness_engine or ReadinessEngine()
        self._wave_planner = wave_planner or WavePlanner()
        self._contract_validator = contract_validator or ContractValidator()

    def analyze(
        self,
        wave_plan: WavePlan,
        project_state: ProjectState,
    ) -> ConflictAnalysis:
        """Return canonical pairwise results for members of each logical Wave."""

        canonical_plan = self._assert_consistent(wave_plan, project_state)
        stories = {story.id: story for story in project_state.user_stories}
        scopes = {
            identifier: _canonical_scope(stories[identifier])
            for wave in canonical_plan.waves
            for identifier in (member.user_story_id for member in wave.members)
        }
        pairs: list[ExecutionConflict] = []
        for wave in canonical_plan.waves:
            identifiers = sorted(member.user_story_id for member in wave.members)
            for left_id, right_id in combinations(identifiers, 2):
                pairs.append(
                    _analyze_pair(
                        wave.wave_index,
                        left_id,
                        right_id,
                        scopes[left_id],
                        scopes[right_id],
                    )
                )
        result = ConflictAnalysis(pairs=tuple(pairs))
        self._validate_result(result)
        return result

    def _assert_consistent(
        self,
        wave_plan: WavePlan,
        project_state: ProjectState,
    ) -> WavePlan:
        if not isinstance(wave_plan, WavePlan):
            raise ExecutionConflictError(
                "WAVE_STATE_MISMATCH", "an explicit WavePlan is required"
            )
        if not isinstance(project_state, ProjectState):
            raise ExecutionConflictError(
                "WAVE_STATE_MISMATCH", "an explicit ProjectState is required"
            )
        try:
            dag = self._dag_validator.build(project_state)
            readiness = self._readiness_engine.evaluate(dag, project_state)
            canonical = self._wave_planner.plan(dag, readiness, project_state)
        except (DAGValidationError, ReadinessEvaluationError, WavePlanningError) as error:
            code = getattr(error, "code", type(error).__name__)
            subjects = getattr(error, "subjects", ())
            raise ExecutionConflictError(
                "WAVE_STATE_MISMATCH",
                f"canonical WavePlan reconstruction failed: {code}",
                subjects=subjects,
            ) from error
        except Exception as error:
            raise ExecutionConflictError(
                "WAVE_STATE_MISMATCH",
                "canonical WavePlan reconstruction could not complete: "
                f"{type(error).__name__}: {error}",
            ) from error
        if wave_plan != canonical:
            raise ExecutionConflictError(
                "WAVE_STATE_MISMATCH",
                "WavePlan is not canonical for the supplied ProjectState",
            )
        return canonical

    def _validate_result(self, result: ConflictAnalysis) -> None:
        try:
            validation = self._contract_validator.validate(
                "conflict-analysis", to_dict(result)
            )
        except Exception as error:
            raise ExecutionConflictError(
                "INVALID_PAIR",
                "ConflictAnalysis validation could not complete: "
                f"{type(error).__name__}: {error}",
            ) from error
        if not validation.is_valid:
            raise ExecutionConflictError(
                "INVALID_PAIR",
                "ConflictAnalysis violates its structural contract",
            )


def _canonical_scope(story: UserStory) -> _CanonicalScope:
    try:
        allowed = tuple(
            _normalize_path(path, allow_directory=True)
            for path in story.scope.allowed_paths
        )
        forbidden = tuple(
            _normalize_path(path, allow_directory=True)
            for path in story.scope.forbidden_paths
        )
    except ImplementerInputError as error:
        raise ExecutionConflictError(
            "INVALID_SCOPE",
            f"User Story {story.id} has an invalid repository path: {error}",
            subjects=(story.id,),
        ) from error

    allowed_unique = tuple(sorted(set(allowed)))
    forbidden_unique = tuple(sorted(set(forbidden)))
    duplicate_canonical_paths = (
        len(allowed_unique) != len(allowed)
        or len(forbidden_unique) != len(forbidden)
    )
    fully_forbidden = any(
        _fully_excluded(allowed_path, forbidden_path)
        for allowed_path in allowed_unique
        for forbidden_path in forbidden_unique
    )
    return _CanonicalScope(
        allowed=allowed_unique,
        forbidden=forbidden_unique,
        unspecified=not allowed_unique,
        ambiguous=duplicate_canonical_paths or fully_forbidden,
    )


def _analyze_pair(
    wave_index: int,
    left_id: str,
    right_id: str,
    left: _CanonicalScope,
    right: _CanonicalScope,
) -> ExecutionConflict:
    overlaps = tuple(
        sorted(
            {
                overlap
                for left_path in left.allowed
                for right_path in right.allowed
                if (overlap := _overlap_region(left_path, right_path)) is not None
                and not any(
                    _fully_excluded(overlap, forbidden)
                    for forbidden in (*left.forbidden, *right.forbidden)
                )
            }
        )
    )
    reasons: set[ConflictReason] = set()
    if overlaps:
        classification = ConflictClassification.CONFLICT
        reasons.add(ConflictReason.PATH_OVERLAP)
    elif left.unspecified or right.unspecified:
        classification = ConflictClassification.UNKNOWN
        reasons.add(ConflictReason.SCOPE_UNSPECIFIED)
    elif left.ambiguous or right.ambiguous:
        classification = ConflictClassification.UNKNOWN
        reasons.add(ConflictReason.SCOPE_AMBIGUOUS)
    else:
        classification = ConflictClassification.SAFE

    if (
        left.ambiguous or right.ambiguous
    ) and classification is not ConflictClassification.SAFE:
        reasons.add(ConflictReason.SCOPE_AMBIGUOUS)
    return ExecutionConflict(
        wave_index=wave_index,
        left_user_story_id=left_id,
        right_user_story_id=right_id,
        classification=classification,
        reasons=tuple(sorted(reasons, key=lambda reason: reason.value)),
        overlapping_paths=overlaps,
    )


def _overlap_region(left: str, right: str) -> str | None:
    left_directory = left.endswith("/")
    right_directory = right.endswith("/")
    if not left_directory and not right_directory:
        return left if left == right else None
    if left_directory and right_directory:
        if left.startswith(right):
            return left
        if right.startswith(left):
            return right
        return None
    directory, file_path = (left, right) if left_directory else (right, left)
    return file_path if file_path.startswith(directory) else None


def _fully_excluded(region: str, forbidden: str) -> bool:
    forbidden_directory = forbidden.endswith("/")
    if forbidden_directory:
        return region.startswith(forbidden)
    return not region.endswith("/") and region == forbidden
