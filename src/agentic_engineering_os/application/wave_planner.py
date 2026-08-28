"""Deterministic prospective layering of validated readiness candidates."""

from __future__ import annotations

from agentic_engineering_os.domain import (
    DAGNode,
    DAGSnapshot,
    DeferredNode,
    DeferredReason,
    ExecutionWave,
    NodeReadiness,
    ProjectState,
    ReadinessClassification,
    ReadinessSnapshot,
    UserStoryStatus,
    WaveMember,
    WavePlan,
    to_dict,
)

from .contract_validator import ContractValidator
from .readiness_engine import ReadinessEngine, ReadinessEvaluationError


class WavePlanningError(RuntimeError):
    """A logical WavePlan cannot be derived reliably from supplied inputs."""

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


_PLANNABLE = frozenset(
    {
        ReadinessClassification.READY,
        ReadinessClassification.WAITING_DEPENDENCIES,
    }
)


class WavePlanner:
    """Build zero-based logical DAG layers without execution authority."""

    def __init__(
        self,
        *,
        readiness_engine: ReadinessEngine | None = None,
        contract_validator: ContractValidator | None = None,
    ) -> None:
        self._readiness_engine = readiness_engine or ReadinessEngine()
        self._contract_validator = contract_validator or ContractValidator()

    def plan(
        self,
        dag_snapshot: DAGSnapshot,
        readiness_snapshot: ReadinessSnapshot,
        project_state: ProjectState,
    ) -> WavePlan:
        """Return prospective layers, assuming only prior Waves will succeed."""

        canonical_readiness = self._assert_consistent(
            dag_snapshot, readiness_snapshot, project_state
        )
        nodes = {node.user_story_id: node for node in dag_snapshot.nodes}
        readiness = {
            node.user_story_id: node for node in canonical_readiness.nodes
        }
        prospectively_satisfied = {
            node.user_story_id
            for node in dag_snapshot.nodes
            if node.status is UserStoryStatus.CERTIFIED
        }
        remaining = {
            identifier
            for identifier, decision in readiness.items()
            if decision.classification in _PLANNABLE
        }
        dependents: dict[str, list[str]] = {
            identifier: [] for identifier in nodes
        }
        for edge in dag_snapshot.edges:
            dependents[edge.dependency_id].append(edge.dependent_id)
        unresolved = {
            identifier: sum(
                dependency not in prospectively_satisfied
                for dependency in nodes[identifier].depends_on
            )
            for identifier in remaining
        }
        frontier = {
            identifier for identifier in remaining if unresolved[identifier] == 0
        }
        waves: list[ExecutionWave] = []
        planned: set[str] = set()

        while frontier:
            ordered = sorted(
                frontier,
                key=lambda identifier: (
                    nodes[identifier].priority,
                    identifier,
                ),
            )
            waves.append(
                ExecutionWave(
                    wave_index=len(waves),
                    members=tuple(
                        WaveMember(
                            user_story_id=identifier,
                            priority=nodes[identifier].priority,
                            risk=nodes[identifier].risk,
                        )
                        for identifier in ordered
                    ),
                )
            )
            layer = set(frontier)
            remaining -= layer
            planned |= layer
            prospectively_satisfied |= layer
            next_frontier: set[str] = set()
            for identifier in sorted(layer):
                for dependent in dependents[identifier]:
                    if dependent not in remaining:
                        continue
                    unresolved[dependent] -= 1
                    if unresolved[dependent] == 0:
                        next_frontier.add(dependent)
            frontier = next_frontier

        deferred = tuple(
            self._deferred_node(
                identifier,
                nodes=nodes,
                readiness=readiness,
                prospectively_satisfied=prospectively_satisfied,
            )
            for identifier in sorted(set(nodes) - planned)
        )
        result = WavePlan(waves=tuple(waves), deferred=deferred)
        self._validate_result(result)
        return result

    def _assert_consistent(
        self,
        dag_snapshot: DAGSnapshot,
        readiness_snapshot: ReadinessSnapshot,
        project_state: ProjectState,
    ) -> ReadinessSnapshot:
        if not isinstance(dag_snapshot, DAGSnapshot):
            raise WavePlanningError("INVALID_DAG", "an explicit DAGSnapshot is required")
        if not isinstance(readiness_snapshot, ReadinessSnapshot):
            raise WavePlanningError(
                "READINESS_MISMATCH", "an explicit ReadinessSnapshot is required"
            )
        if not isinstance(project_state, ProjectState):
            raise WavePlanningError(
                "DAG_STATE_MISMATCH", "an explicit ProjectState is required"
            )
        try:
            canonical = self._readiness_engine.evaluate(dag_snapshot, project_state)
        except ReadinessEvaluationError as error:
            code = (
                "INVALID_DAG"
                if error.code == "INVALID_DAG"
                else "DAG_STATE_MISMATCH"
            )
            raise WavePlanningError(
                code,
                f"DAG/ProjectState consistency failed: {error.code}",
                subjects=error.subjects,
            ) from error
        except Exception as error:
            raise WavePlanningError(
                "INVALID_DAG",
                "readiness reconstruction could not complete: "
                f"{type(error).__name__}: {error}",
            ) from error
        if readiness_snapshot != canonical:
            expected_ids = {node.user_story_id for node in canonical.nodes}
            actual_ids = {node.user_story_id for node in readiness_snapshot.nodes}
            raise WavePlanningError(
                "READINESS_MISMATCH",
                "ReadinessSnapshot is not canonical for the supplied DAG and state",
                subjects=tuple(sorted(expected_ids ^ actual_ids)),
            )
        return canonical

    @staticmethod
    def _deferred_node(
        identifier: str,
        *,
        nodes: dict[str, DAGNode],
        readiness: dict[str, NodeReadiness],
        prospectively_satisfied: set[str],
    ) -> DeferredNode:
        node = nodes[identifier]
        decision = readiness[identifier]
        status = node.status
        classification = decision.classification
        if status is UserStoryStatus.CERTIFIED:
            reason = DeferredReason.TERMINAL_SATISFIED
            blockers: tuple[str, ...] = ()
        elif status is UserStoryStatus.CANCELLED:
            reason = DeferredReason.TERMINAL_UNSATISFIED
            blockers = ()
        elif classification is ReadinessClassification.BLOCKED:
            reason = DeferredReason.BLOCKED
            blockers = ()
        elif classification is ReadinessClassification.INELIGIBLE:
            reason = DeferredReason.INELIGIBLE
            blockers = ()
        elif classification in _PLANNABLE:
            reason = DeferredReason.UNPLANNABLE_DEPENDENCY
            blockers = tuple(
                dependency
                for dependency in node.depends_on
                if dependency not in prospectively_satisfied
            )
            if not blockers:
                raise WavePlanningError(
                    "UNPLANNABLE_GRAPH",
                    f"plannable node was not layered: {identifier}",
                    subjects=(identifier,),
                )
        else:
            raise WavePlanningError(
                "UNPLANNABLE_GRAPH",
                f"unsupported readiness classification: {identifier}",
                subjects=(identifier,),
            )
        return DeferredNode(identifier, reason, blockers)

    def _validate_result(self, result: WavePlan) -> None:
        try:
            validation = self._contract_validator.validate("wave-plan", to_dict(result))
        except Exception as error:
            raise WavePlanningError(
                "UNPLANNABLE_GRAPH",
                f"WavePlan validation could not complete: {type(error).__name__}: {error}",
            ) from error
        if not validation.is_valid:
            raise WavePlanningError(
                "UNPLANNABLE_GRAPH",
                "WavePlan violates its structural contract",
            )
