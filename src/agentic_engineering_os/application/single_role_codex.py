"""One bounded Codex role execution composed from the certified P4 pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Protocol

from agentic_engineering_os.domain import (
    MissionRole,
    ProjectState,
    UserStory,
    WorktreeAssignment,
    WorktreeStatus,
)
from .architect import ArchitectInput, ArchitectResult
from .certifier import CertifierInput
from .codex_output_schema import codex_output_schema_path
from .codex_runtime import (
    CodexApprovalPolicy,
    CodexExecutionBinding,
    CodexSandboxMode,
)
from .context_builder import (
    CodexExecutionRequest,
    CognitiveSource,
    ContextBuilder,
    ExecutionScope,
    RepositoryContextPort,
    _relevant_control_items,
)
from .execution_recovery import RestartSafeCodexExecutionService
from .execution_state import (
    CodexExecutionStatus,
    ExecutionExecutableIdentity,
    RestartDisposition,
)
from .implementer import ImplementerInput, ImplementerResult
from .orchestrator import RoleHandoff
from .prompt_compiler import PromptCompiler
from .result_intake import ResultIntakeOutcome, ResultIntakeValidationContext, RoleResult
from .reviewer import ReviewerInput, ReviewerResult
from .tester import TesterInput, TesterResult


_ROLE_CONTRACT = {
    role: f"roles/{role.value.casefold()}.md"
    for role in (
        MissionRole.ARCHITECT,
        MissionRole.IMPLEMENTER,
        MissionRole.TESTER,
        MissionRole.REVIEWER,
        MissionRole.CERTIFIER,
    )
}
_RESULT_CONTRACT = {
    role: f"{role.value.casefold()}-result@1.0" for role in _ROLE_CONTRACT
}
_MUTATING_ROLES = frozenset({MissionRole.IMPLEMENTER, MissionRole.TESTER})


class _Reader(Protocol):
    def load(self) -> object: ...


@dataclass(frozen=True, slots=True)
class SingleRoleArtifacts:
    architect_result: ArchitectResult | None = None
    implementer_result: ImplementerResult | None = None
    tester_result: TesterResult | None = None
    reviewer_result: ReviewerResult | None = None
    cognitive_sources: tuple[CognitiveSource, ...] = ()


@dataclass(frozen=True, slots=True)
class SingleRoleExecutionOutcome:
    request_id: str
    execution_id: str
    role: MissionRole
    status: CodexExecutionStatus
    validated: bool
    validated_result: RoleResult | None
    intake_replayed: bool
    completed_reused: bool
    blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.validated != (self.validated_result is not None):
            raise ValueError("validated must match presence of a canonical RoleResult")


class SingleRoleExecutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class SingleRoleCodexExecutor:
    """Compose P4.2→P4.7 once; never advance authoritative workflow state."""

    def __init__(
        self,
        *,
        mission_store: _Reader,
        project_store: _Reader,
        repository: RepositoryContextPort,
        execution_service: RestartSafeCodexExecutionService,
        executable_identity: ExecutionExecutableIdentity,
        prompt_compiler: PromptCompiler | None = None,
        timeout_seconds: float = 900.0,
    ) -> None:
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        self._project_store = project_store
        self._repository = repository
        self._builder = ContextBuilder(
            mission_store=mission_store,
            project_store=project_store,
            repository=repository,
        )
        self._compiler = prompt_compiler or PromptCompiler()
        self._executions = execution_service
        self._executable = executable_identity
        self._timeout_seconds = float(timeout_seconds)

    def execute(
        self,
        handoff: RoleHandoff,
        *,
        request_id: str,
        artifacts: SingleRoleArtifacts = SingleRoleArtifacts(),
        cancellation: Event | None = None,
    ) -> SingleRoleExecutionOutcome:
        if not isinstance(handoff, RoleHandoff):
            raise SingleRoleExecutionError("INVALID_HANDOFF", "canonical RoleHandoff is required")
        if handoff.from_role is not MissionRole.ORCHESTRATOR:
            raise SingleRoleExecutionError(
                "INVALID_HANDOFF", "RoleHandoff must originate from ORCHESTRATOR"
            )
        if handoff.to_role not in _ROLE_CONTRACT:
            raise SingleRoleExecutionError("UNSUPPORTED_ROLE", "only five closed Codex roles are supported")
        if not isinstance(request_id, str) or not request_id.strip():
            raise SingleRoleExecutionError("INVALID_REQUEST_ID", "request identity must be explicit")
        if not isinstance(artifacts, SingleRoleArtifacts):
            raise SingleRoleExecutionError("INVALID_ARTIFACTS", "role artifacts must use the closed contract")

        project = self._project_store.load()
        if not isinstance(project, ProjectState):
            raise SingleRoleExecutionError("INVALID_PROJECT_STATE", "ProjectState is unavailable or invalid")
        story = _select_story(project, handoff)
        role_input, upstream = _role_input(handoff, story, project, artifacts)
        assignment = _assignment(self._repository, handoff)
        request = _request(
            request_id,
            handoff,
            story,
            assignment,
            self._repository.repository_root,
        )
        context = self._builder.build(
            request,
            upstream_results=upstream,
            cognitive_sources=artifacts.cognitive_sources,
        )
        compiled = self._compiler.compile(context)
        cwd = compiled.worktree_path or compiled.repository_root
        schema = codex_output_schema_path(handoff.to_role)
        validation_context = ResultIntakeValidationContext(role_input, str(schema.resolve(strict=False)))
        binding = CodexExecutionBinding(
            request_id=compiled.request_id,
            context_fingerprint=compiled.context_fingerprint,
            mission_id=compiled.mission_id,
            workflow_generation=compiled.workflow_generation,
            role=compiled.role,
            subject=compiled.subject,
            cwd=cwd,
            expected_commit=compiled.observed_commit,
            sandbox=(
                CodexSandboxMode.WORKSPACE_WRITE
                if compiled.role in _MUTATING_ROLES
                else CodexSandboxMode.READ_ONLY
            ),
            approval_policy=CodexApprovalPolicy.NEVER,
            timeout_seconds=self._timeout_seconds,
            output_schema_path=str(schema.resolve(strict=False)),
        )
        record = self._executions.plan(compiled, binding, self._executable)
        inspection = self._executions.inspect_restart(
            record.execution_id,
            compiled,
            binding,
            self._executable,
            validation_context=validation_context,
        )
        if inspection.disposition is RestartDisposition.SAFE_NOT_STARTED:
            self._executions.execute(
                record.execution_id,
                compiled,
                binding,
                cancellation=cancellation,
            )
            inspection = self._executions.inspect_restart(
                record.execution_id,
                compiled,
                binding,
                self._executable,
                validation_context=validation_context,
            )
        if inspection.disposition is RestartDisposition.INTAKE_REPLAY_AVAILABLE:
            intake = self._executions.replay_intake(
                record.execution_id, compiled, validation_context
            )
            return _from_intake(record.execution_id, request_id, compiled.role, intake, replayed=True)
        if inspection.disposition is RestartDisposition.VALIDATED_NO_RERUN:
            intake = self._executions.revalidate_completed(
                record.execution_id, compiled, validation_context
            )
            return _from_intake(record.execution_id, request_id, compiled.role, intake, reused=True)
        return SingleRoleExecutionOutcome(
            request_id=request_id,
            execution_id=record.execution_id,
            role=compiled.role,
            status=inspection.status,
            validated=False,
            validated_result=None,
            intake_replayed=False,
            completed_reused=False,
            blockers=inspection.reasons or (inspection.disposition.value,),
        )


def _select_story(project: ProjectState, handoff: RoleHandoff) -> UserStory | None:
    if handoff.to_role is MissionRole.ARCHITECT:
        return None
    matches = [story for story in project.user_stories if story.id == handoff.subject]
    if len(matches) != 1:
        raise SingleRoleExecutionError("STORY_UNRESOLVED", "handoff subject must resolve exactly one UserStory")
    return matches[0]


def _role_input(
    handoff: RoleHandoff,
    story: UserStory | None,
    project: ProjectState,
    artifacts: SingleRoleArtifacts,
) -> tuple[object, tuple[object, ...]]:
    role = handoff.to_role
    supplied = {
        MissionRole.ARCHITECT: artifacts.architect_result,
        MissionRole.IMPLEMENTER: artifacts.implementer_result,
        MissionRole.TESTER: artifacts.tester_result,
        MissionRole.REVIEWER: artifacts.reviewer_result,
    }
    expected_upstream = {
        MissionRole.ARCHITECT: (),
        MissionRole.IMPLEMENTER: (),
        MissionRole.TESTER: (MissionRole.IMPLEMENTER,),
        MissionRole.REVIEWER: (MissionRole.IMPLEMENTER, MissionRole.TESTER),
        MissionRole.CERTIFIER: (
            MissionRole.ARCHITECT,
            MissionRole.IMPLEMENTER,
            MissionRole.TESTER,
            MissionRole.REVIEWER,
        ),
    }[role]
    if any((value is not None) != (item in expected_upstream) for item, value in supplied.items()):
        raise SingleRoleExecutionError("UPSTREAM_SET_MISMATCH", "role artifacts are not the exact required set")
    upstream = tuple(supplied[item] for item in expected_upstream)
    if role is MissionRole.ARCHITECT:
        return ArchitectInput.from_handoff(handoff), upstream
    assert story is not None
    if role is MissionRole.IMPLEMENTER:
        return ImplementerInput.from_handoff(handoff, story), upstream
    if role is MissionRole.TESTER:
        assert artifacts.implementer_result is not None
        return TesterInput.from_handoff(handoff, story, artifacts.implementer_result), upstream
    if role is MissionRole.REVIEWER:
        assert artifacts.implementer_result is not None and artifacts.tester_result is not None
        return ReviewerInput.from_handoff(
            handoff, story, artifacts.implementer_result, artifacts.tester_result
        ), upstream
    assert role is MissionRole.CERTIFIER
    evidence, gates = _relevant_control_items(project, story)
    return (
        CertifierInput.from_handoff(
            handoff,
            story,
            artifacts.architect_result,
            artifacts.implementer_result,
            artifacts.tester_result,
            artifacts.reviewer_result,
            evidence,
            gates,
        ),
        upstream,
    )


def _assignment(repository: RepositoryContextPort, handoff: RoleHandoff) -> WorktreeAssignment | None:
    if handoff.to_role is not MissionRole.IMPLEMENTER:
        return None
    registry = repository.registry_store.load()
    matches = [
        item
        for item in registry.assignments
        if item.mission_id == handoff.mission_id
        and item.user_story_id == handoff.subject
        and item.workflow_generation == handoff.workflow_generation
        and item.baseline_commit == handoff.observed_commit.casefold()
        and item.status is WorktreeStatus.ACTIVE
    ]
    if len(matches) != 1:
        raise SingleRoleExecutionError("WORKTREE_UNRESOLVED", "exactly one ACTIVE bound assignment is required")
    return matches[0]


def _request(
    request_id: str,
    handoff: RoleHandoff,
    story: UserStory | None,
    assignment: WorktreeAssignment | None,
    repository_root: Path,
) -> CodexExecutionRequest:
    scope = (
        ExecutionScope((), ())
        if story is None
        else ExecutionScope(story.scope.allowed_paths, story.scope.forbidden_paths)
    )
    requirements = [handoff.instructions]
    if story is not None:
        requirements.extend(
            f"Acceptance Criterion {item.id}: {item.description}"
            for item in story.acceptance_criteria
        )
        requirements.extend(f"Required Gate: {item}" for item in story.required_gates)
    return CodexExecutionRequest(
        request_id=request_id,
        mission_id=handoff.mission_id,
        workflow_generation=handoff.workflow_generation,
        role=handoff.to_role,
        subject=handoff.subject,
        user_story_id=story.id if story is not None else None,
        repository_root=str(repository_root.resolve(strict=True)),
        observed_commit=handoff.observed_commit.casefold(),
        operating_step=handoff.operating_step,
        scope=scope,
        task=f"{handoff.objective}\n{handoff.instructions}",
        verification_requirements=tuple(dict.fromkeys(requirements)),
        role_contract_ref=_ROLE_CONTRACT[handoff.to_role],
        expected_result_contract=_RESULT_CONTRACT[handoff.to_role],
        worktree_assignment_id=(assignment.assignment_id if assignment is not None else None),
    )


def _from_intake(
    execution_id: str,
    request_id: str,
    role: MissionRole,
    intake: ResultIntakeOutcome,
    *,
    replayed: bool = False,
    reused: bool = False,
) -> SingleRoleExecutionOutcome:
    if intake.accepted and intake.validated_result is not None:
        return SingleRoleExecutionOutcome(
            request_id,
            execution_id,
            role,
            CodexExecutionStatus.VALIDATED,
            True,
            intake.validated_result,
            replayed,
            reused,
            (),
        )
    return SingleRoleExecutionOutcome(
        request_id,
        execution_id,
        role,
        CodexExecutionStatus.FAILED,
        False,
        None,
        replayed,
        reused,
        tuple(f"{item.code.value}: {item.message}" for item in intake.refusal_reasons),
    )
