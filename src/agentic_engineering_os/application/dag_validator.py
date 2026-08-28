"""Deterministic, read-only projection of ProjectState dependency graphs."""

from __future__ import annotations

from agentic_engineering_os.domain import (
    DAGEdge,
    DAGNode,
    DAGSnapshot,
    ProjectState,
    RiskLevel,
    UserStory,
    UserStoryStatus,
    to_dict,
)

from .contract_validator import ContractValidator


class DAGValidationError(RuntimeError):
    """A ProjectState cannot be projected as a valid DAG."""

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


class DAGValidator:
    """Build one canonical immutable DAG snapshot without mutating its source."""

    def __init__(self, *, contract_validator: ContractValidator | None = None) -> None:
        self._contract_validator = contract_validator or ContractValidator()

    def build(self, project_state: ProjectState) -> DAGSnapshot:
        """Validate and project ``UserStory.depends_on`` exactly once per edge."""

        if not isinstance(project_state, ProjectState):
            raise DAGValidationError(
                "INVALID_PROJECT_STATE",
                "build requires an explicit ProjectState",
            )

        stories = tuple(project_state.user_stories)
        validated: list[UserStory] = []
        for index, story in enumerate(stories):
            validated.append(self._validate_user_story(story, index=index))

        try:
            project_result = self._contract_validator.validate(
                "project-state", to_dict(project_state)
            )
        except Exception as error:
            raise DAGValidationError(
                "INVALID_PROJECT_STATE",
                "ProjectState cannot be validated: "
                f"{type(error).__name__}: {error}",
            ) from error
        if not project_result.is_valid:
            codes = ", ".join(
                sorted({issue.code for issue in project_result.errors})
            )
            raise DAGValidationError(
                "INVALID_PROJECT_STATE",
                f"ProjectState violates its canonical contract ({codes})",
            )

        identifiers = [story.id for story in validated]
        duplicates = _duplicates(identifiers)
        if duplicates:
            raise DAGValidationError(
                "DUPLICATE_NODE",
                f"duplicate User Story IDs: {', '.join(duplicates)}",
                subjects=duplicates,
            )

        known_ids = frozenset(identifiers)
        missing = tuple(
            sorted(
                {
                    dependency
                    for story in validated
                    for dependency in story.depends_on
                    if dependency not in known_ids
                }
            )
        )
        if missing:
            raise DAGValidationError(
                "MISSING_DEPENDENCY",
                f"dependencies are absent from ProjectState: {', '.join(missing)}",
                subjects=missing,
            )

        nodes = tuple(
            DAGNode(
                user_story_id=story.id,
                status=UserStoryStatus(story.status),
                priority=story.priority,
                risk=RiskLevel(story.risk),
                depends_on=tuple(sorted(story.depends_on)),
            )
            for story in sorted(validated, key=lambda candidate: candidate.id)
        )
        edges = tuple(
            sorted(
                (
                    DAGEdge(
                        dependency_id=dependency,
                        dependent_id=story.id,
                    )
                    for story in validated
                    for dependency in story.depends_on
                ),
                key=lambda edge: (edge.dependency_id, edge.dependent_id),
            )
        )

        cycle = _find_cycle(nodes, edges)
        if cycle:
            rendered = " -> ".join((*cycle, cycle[0]))
            raise DAGValidationError(
                "CYCLE_DETECTED",
                f"dependency cycle detected: {rendered}",
                subjects=cycle,
            )

        snapshot = DAGSnapshot(nodes=nodes, edges=edges)
        serialized = to_dict(snapshot)
        result = self._contract_validator.validate("dag-snapshot", serialized)
        if not result.is_valid:
            raise DAGValidationError(
                "INVALID_DAG_SNAPSHOT",
                "canonical DAG projection violates its structural contract",
            )
        return snapshot

    def _validate_user_story(self, story: object, *, index: int) -> UserStory:
        if not isinstance(story, UserStory):
            raise DAGValidationError(
                "INVALID_USER_STORY",
                f"ProjectState user_stories[{index}] is not a UserStory",
                subjects=(f"index:{index}",),
            )
        if isinstance(story.depends_on, (list, tuple)) and story.id in story.depends_on:
            label = _story_label(story, index)
            raise DAGValidationError(
                "SELF_DEPENDENCY",
                f"User Story depends on itself: {label}",
                subjects=(label,),
            )
        try:
            serialized = to_dict(story)
            result = self._contract_validator.validate("user-story", serialized)
        except Exception as error:
            raise DAGValidationError(
                "INVALID_USER_STORY",
                f"User Story at index {index} cannot be validated: "
                f"{type(error).__name__}: {error}",
                subjects=(_story_label(story, index),),
            ) from error

        if not result.is_valid:
            label = _story_label(story, index)
            codes = ", ".join(sorted({issue.code for issue in result.errors}))
            raise DAGValidationError(
                "INVALID_USER_STORY",
                f"User Story violates its canonical contract: {label} ({codes})",
                subjects=(label,),
            )
        return story


def _duplicates(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return tuple(sorted(duplicates))


def _find_cycle(
    nodes: tuple[DAGNode, ...],
    edges: tuple[DAGEdge, ...],
) -> tuple[str, ...]:
    adjacency: dict[str, list[str]] = {node.user_story_id: [] for node in nodes}
    for edge in edges:
        adjacency[edge.dependency_id].append(edge.dependent_id)
    for dependents in adjacency.values():
        dependents.sort()

    colors = {identifier: 0 for identifier in adjacency}
    for identifier in sorted(adjacency):
        if colors[identifier] != 0:
            continue
        path: list[str] = [identifier]
        positions = {identifier: 0}
        colors[identifier] = 1
        frames: list[tuple[str, int]] = [(identifier, 0)]
        while frames:
            current, next_index = frames[-1]
            dependents = adjacency[current]
            if next_index == len(dependents):
                frames.pop()
                path.pop()
                positions.pop(current)
                colors[current] = 2
                continue
            dependent = dependents[next_index]
            frames[-1] = (current, next_index + 1)
            if colors[dependent] == 0:
                colors[dependent] = 1
                positions[dependent] = len(path)
                path.append(dependent)
                frames.append((dependent, 0))
            elif colors[dependent] == 1:
                return tuple(path[positions[dependent] :])
    return ()


def _story_label(story: UserStory, index: int) -> str:
    identifier = getattr(story, "id", None)
    return identifier if isinstance(identifier, str) and identifier else f"index:{index}"
