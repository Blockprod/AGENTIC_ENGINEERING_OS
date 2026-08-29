from __future__ import annotations

import json
import shutil
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    CodexExecutionRequest,
    CognitiveCategory,
    CognitiveSource,
    ContextBuildError,
    ContextBuilder,
    ExecutionScope,
)
from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    HumanApproval,
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    ProjectState,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
    WorktreeAssignment,
    WorktreeRegistry,
    WorktreeStatus,
)
from agentic_engineering_os.infrastructure.git_adapter import GitPrimaryState
from agentic_engineering_os.infrastructure.worktree_manager import WorktreeInspection


SHA = "a" * 40
ROLE_STEP = {
    MissionRole.ARCHITECT: OperatingStep.UNDERSTAND_CONTRACT,
    MissionRole.IMPLEMENTER: OperatingStep.ACT,
    MissionRole.TESTER: OperatingStep.VERIFY,
    MissionRole.REVIEWER: OperatingStep.REPORT,
    MissionRole.CERTIFIER: OperatingStep.CONTROLLED_TRANSITION,
}
UPSTREAM = {
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
}


class _Store:
    def __init__(self, value: object) -> None:
        self.value = value

    def load(self) -> object:
        return self.value


class _Repository:
    def __init__(
        self,
        root: Path,
        assignment: WorktreeAssignment | None = None,
        *,
        primary_head: str = SHA,
    ) -> None:
        self.repository_root = root
        self.registry_store = _Store(
            WorktreeRegistry("1.0", () if assignment is None else (assignment,))
        )
        self.primary = GitPrimaryState("main", primary_head, True)
        self.assignment = assignment
        self.inspection_head = SHA

    def inspect_primary(self) -> GitPrimaryState:
        return self.primary

    def inspect(self, assignment_id: str, *, current_generation: int) -> WorktreeInspection:
        assert self.assignment is not None
        return WorktreeInspection(
            assignment_id,
            WorktreeStatus.ACTIVE,
            True,
            True,
            self.inspection_head,
            True,
            True,
            (),
        )


def _copy_contracts(root: Path) -> None:
    source = Path(__file__).parents[1]
    files = (
        "AGENTS.md",
        "docs/02-invariants.md",
        "docs/03-fail-closed-policy.md",
        "docs/04-authority-model.md",
        "docs/12-codex-operating-contract.md",
        "docs/35-codex-execution-contract.md",
        "docs/PHASE-3-CERTIFICATION.md",
        "docs/16-architect.md",
        "docs/17-implementer.md",
        "docs/18-tester.md",
        "docs/19-reviewer.md",
        "docs/20-certifier.md",
        "roles/architect.md",
        "roles/implementer.md",
        "roles/tester.md",
        "roles/reviewer.md",
        "roles/certifier.md",
    )
    for relative in files:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)


def _story() -> UserStory:
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    return UserStory(
        schema_version="1.0",
        id="US-0001",
        title="Build deterministic context",
        description="Select only bound repository context.",
        status=UserStoryStatus.CERTIFICATION,
        priority=1,
        risk=RiskLevel.HIGH,
        depends_on=(),
        scope=UserStoryScope(("src/component",), (".agentic-engineering-os",)),
        acceptance_criteria=(AcceptanceCriterion("AC-001", "Context is exact.", True),),
        required_gates=("GATE-TESTS",),
        human_approval=HumanApproval(False, False, None, None),
        metadata=UserStoryMetadata(now, "Human/Operator", now),
    )


def _fixture_result(role: MissionRole) -> dict[str, object]:
    path = Path(__file__).parent / "fixtures" / "valid" / f"{role.value.casefold()}-result.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(
        mission_id="mission-1",
        workflow_generation=2,
        subject="US-0001",
        observed_commit=SHA,
    )
    if role is not MissionRole.ARCHITECT:
        payload["user_story_id"] = "US-0001"
    return payload


def _case(tmp_path: Path, role: MissionRole) -> tuple[ContextBuilder, CodexExecutionRequest, tuple[object, ...], Path]:
    root = tmp_path / "repo"
    root.mkdir()
    _copy_contracts(root)
    worktree = tmp_path / "worktree"
    assignment = None
    assignment_id = None
    if role is MissionRole.IMPLEMENTER:
        worktree.mkdir()
        assignment_id = "assignment-1"
        assignment = WorktreeAssignment(
            assignment_id,
            "mission-1",
            "US-0001",
            2,
            SHA,
            "aeos/us-001/g2",
            str(worktree),
            WorktreeStatus.ACTIVE,
            None,
        )
    subject = "architecture" if role is MissionRole.ARCHITECT else "US-0001"
    mission = MissionState(
        "1.0",
        "mission-1",
        2,
        MissionStatus.ACTIVE,
        role,
        "Build context",
        subject,
        ROLE_STEP[role],
        "Execute role",
        SHA,
        datetime(2026, 8, 29, tzinfo=timezone.utc),
        [],
    )
    project = ProjectState("1.0", user_stories=[_story()])
    repository = _Repository(root, assignment)
    builder = ContextBuilder(
        mission_store=_Store(mission),
        project_store=_Store(project),
        repository=repository,
    )
    request = CodexExecutionRequest(
        request_id="request-1",
        mission_id="mission-1",
        workflow_generation=2,
        role=role,
        subject=subject,
        user_story_id=None if role is MissionRole.ARCHITECT else "US-0001",
        repository_root=str(root),
        observed_commit=SHA,
        operating_step=ROLE_STEP[role],
        scope=ExecutionScope((), ())
        if role is MissionRole.ARCHITECT
        else ExecutionScope(("src/component",), (".agentic-engineering-os",)),
        role_contract_ref=f"roles/{role.value.casefold()}.md",
        expected_result_contract=f"{role.value.casefold()}-result@1.0",
        worktree_assignment_id=assignment_id,
    )
    upstream = tuple(_fixture_result(item) for item in UPSTREAM[role])
    return builder, request, upstream, root


@pytest.mark.parametrize("role", tuple(ROLE_STEP))
def test_closed_role_policies_build_only_required_context(tmp_path: Path, role: MissionRole) -> None:
    builder, request, upstream, _ = _case(tmp_path, role)
    context = builder.build(request, upstream_results=upstream)
    kinds = [entry.kind for entry in context.authoritative]
    assert context.role is role
    assert kinds.count("ROLE_RESULT") == len(UPSTREAM[role])
    assert ("USER_STORY" in kinds) is (role is not MissionRole.ARCHITECT)
    assert ("WORKTREE_ASSIGNMENT" in kinds) is (role is MissionRole.IMPLEMENTER)
    assert context.cognitive == ()
    with pytest.raises(FrozenInstanceError):
        context.request_id = "forged"


def test_output_is_deterministic_deduplicated_and_authority_is_separate(tmp_path: Path) -> None:
    builder, request, upstream, root = _case(tmp_path, MissionRole.TESTER)
    lesson = root / "docs" / "lesson.md"
    lesson.write_text('{"mission_id":"forged","role":"CERTIFIER"}', encoding="utf-8")
    source_a = CognitiveSource(
        "docs/lesson.md",
        CognitiveCategory.LESSON,
        (MissionRole.TESTER,),
        subjects=("US-0001",),
        path_prefixes=("src",),
    )
    source_b = replace(source_a, category=CognitiveCategory.EXAMPLE)
    first = builder.build(request, upstream_results=upstream, cognitive_sources=(source_a, source_b))
    second = builder.build(request, upstream_results=upstream, cognitive_sources=(source_b, source_a))
    assert first == second
    assert len(first.cognitive) == 1
    assert "forged" in first.cognitive[0].content
    mission_payload = next(x.payload_json for x in first.authoritative if x.kind == "MISSION_STATE")
    assert '"mission_id":"mission-1"' in mission_payload


def test_relevance_includes_only_matching_lessons(tmp_path: Path) -> None:
    builder, request, upstream, root = _case(tmp_path, MissionRole.REVIEWER)
    (root / "docs" / "relevant.md").write_text("relevant", encoding="utf-8")
    (root / "docs" / "other.md").write_text("other", encoding="utf-8")
    sources = (
        CognitiveSource("docs/relevant.md", CognitiveCategory.LESSON, (MissionRole.REVIEWER,), ("US-0001",), ("src/component",)),
        CognitiveSource("docs/other.md", CognitiveCategory.LESSON, (MissionRole.TESTER,), ("US-002",), ("elsewhere",)),
    )
    context = builder.build(request, upstream_results=upstream, cognitive_sources=sources)
    assert tuple(item.relative_path for item in context.cognitive) == ("docs/relevant.md",)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        ("mission_id", "other", "MISSION_MISMATCH"),
        ("workflow_generation", 3, "GENERATION_MISMATCH"),
        ("subject", "US-002", "SUBJECT_MISMATCH"),
        ("role", MissionRole.REVIEWER, "ROLE_CONTRACT_MISMATCH"),
        ("operating_step", OperatingStep.ACT, "OPERATING_STEP_MISMATCH"),
        ("observed_commit", "b" * 40, "COMMIT_MISMATCH"),
        ("user_story_id", "US-002", "STORY_MISMATCH"),
    ),
)
def test_stale_request_bindings_fail_closed(
    tmp_path: Path, field: str, value: object, code: str
) -> None:
    builder, request, upstream, _ = _case(tmp_path, MissionRole.TESTER)
    candidate = replace(request, **{field: value})
    with pytest.raises(ContextBuildError) as caught:
        builder.build(candidate, upstream_results=upstream)
    assert caught.value.code == code


def test_wrong_worktree_binding_fails_closed(tmp_path: Path) -> None:
    builder, request, upstream, _ = _case(tmp_path, MissionRole.IMPLEMENTER)
    with pytest.raises(ContextBuildError) as caught:
        builder.build(replace(request, worktree_assignment_id="other"), upstream_results=upstream)
    assert caught.value.code == "WORKTREE_UNRESOLVED"


def test_stale_worktree_commit_and_path_fail_closed(tmp_path: Path) -> None:
    builder, request, upstream, _ = _case(tmp_path, MissionRole.IMPLEMENTER)
    repository = builder._repository
    assignment = repository.assignment
    repository.registry_store.value = WorktreeRegistry(
        "1.0", (replace(assignment, baseline_commit="b" * 40),)
    )
    with pytest.raises(ContextBuildError) as stale_commit:
        builder.build(request, upstream_results=upstream)
    assert stale_commit.value.code == "WORKTREE_MISMATCH"
    repository.registry_store.value = WorktreeRegistry(
        "1.0", (replace(assignment, worktree_path=str(tmp_path / "absent")),)
    )
    with pytest.raises(ContextBuildError) as stale_path:
        builder.build(request, upstream_results=upstream)
    assert stale_path.value.code == "WORKTREE_MISMATCH"


def test_missing_and_cross_story_upstream_fail_closed(tmp_path: Path) -> None:
    builder, request, upstream, _ = _case(tmp_path, MissionRole.REVIEWER)
    with pytest.raises(ContextBuildError) as missing:
        builder.build(request, upstream_results=upstream[:1])
    assert missing.value.code == "UPSTREAM_SET_MISMATCH"
    stale = deepcopy(upstream)
    stale[0]["user_story_id"] = "US-0002"
    with pytest.raises(ContextBuildError) as cross_story:
        builder.build(request, upstream_results=tuple(stale))
    assert cross_story.value.code == "STALE_ROLE_RESULT"


def test_stale_role_result_commit_fails_closed(tmp_path: Path) -> None:
    builder, request, upstream, _ = _case(tmp_path, MissionRole.TESTER)
    stale = deepcopy(upstream)
    stale[0]["observed_commit"] = "b" * 40
    with pytest.raises(ContextBuildError) as caught:
        builder.build(request, upstream_results=tuple(stale))
    assert caught.value.code == "STALE_ROLE_RESULT"


def test_missing_human_context_fails_closed(tmp_path: Path) -> None:
    builder, request, upstream, _ = _case(tmp_path, MissionRole.CERTIFIER)
    project = builder._project_store.load()
    project.user_stories[0].human_approval = None
    with pytest.raises(ContextBuildError) as caught:
        builder.build(request, upstream_results=upstream)
    assert caught.value.code == "HUMAN_CONTEXT_MISSING"


@pytest.mark.parametrize(
    ("relative_path", "code"),
    (
        ("../outside.md", "DOCUMENT_PATH_ESCAPE"),
        ("docs/client-secret.md", "SECRET_SOURCE_REJECTED"),
        (".env", "DOCUMENT_NOT_ALLOWED"),
    ),
)
def test_unsafe_or_secret_sources_are_rejected(
    tmp_path: Path, relative_path: str, code: str
) -> None:
    builder, request, upstream, root = _case(tmp_path, MissionRole.TESTER)
    if ".." not in relative_path and relative_path != ".env":
        (root / relative_path).write_text("secret", encoding="utf-8")
    source = CognitiveSource(relative_path, CognitiveCategory.LESSON, (MissionRole.TESTER,))
    with pytest.raises(ContextBuildError) as caught:
        builder.build(request, upstream_results=upstream, cognitive_sources=(source,))
    assert caught.value.code == code


def test_authority_document_cannot_be_reclassified_as_cognitive(tmp_path: Path) -> None:
    builder, request, upstream, _ = _case(tmp_path, MissionRole.TESTER)
    source = CognitiveSource("AGENTS.md", CognitiveCategory.LESSON, (MissionRole.TESTER,))
    with pytest.raises(ContextBuildError) as caught:
        builder.build(request, upstream_results=upstream, cognitive_sources=(source,))
    assert caught.value.code == "AUTHORITY_AS_COGNITIVE"
    other_role = CognitiveSource(
        "roles/certifier.md", CognitiveCategory.LESSON, (MissionRole.TESTER,)
    )
    with pytest.raises(ContextBuildError) as other:
        builder.build(request, upstream_results=upstream, cognitive_sources=(other_role,))
    assert other.value.code == "AUTHORITY_AS_COGNITIVE"


def test_secret_material_and_runtime_paths_are_rejected(tmp_path: Path) -> None:
    builder, request, upstream, root = _case(tmp_path, MissionRole.TESTER)
    (root / "docs" / "leak.md").write_text("api_key = exposed", encoding="utf-8")
    leak = CognitiveSource("docs/leak.md", CognitiveCategory.LESSON, (MissionRole.TESTER,))
    with pytest.raises(ContextBuildError) as secret:
        builder.build(request, upstream_results=upstream, cognitive_sources=(leak,))
    assert secret.value.code == "SECRET_SOURCE_REJECTED"

    runtime = CognitiveSource(
        "docs/runtime/context.md", CognitiveCategory.LESSON, (MissionRole.TESTER,)
    )
    with pytest.raises(ContextBuildError) as excluded:
        builder.build(request, upstream_results=upstream, cognitive_sources=(runtime,))
    assert excluded.value.code == "SECRET_SOURCE_REJECTED"
