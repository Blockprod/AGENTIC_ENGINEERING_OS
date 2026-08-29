"""Deterministic, repository-local context selection for Codex role execution."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, cast

from agentic_engineering_os.domain import (
    CertificationResult,
    MissionRole,
    MissionState,
    MissionStatus,
    OperatingStep,
    ProjectState,
    UserStory,
    WorktreeAssignment,
    WorktreeRegistry,
    WorktreeStatus,
    to_dict,
)
from .contract_validator import ContractValidator


_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
_ROLE_CONTRACTS = {
    MissionRole.ARCHITECT: ("roles/architect.md", "docs/16-architect.md"),
    MissionRole.IMPLEMENTER: ("roles/implementer.md", "docs/17-implementer.md"),
    MissionRole.TESTER: ("roles/tester.md", "docs/18-tester.md"),
    MissionRole.REVIEWER: ("roles/reviewer.md", "docs/19-reviewer.md"),
    MissionRole.CERTIFIER: ("roles/certifier.md", "docs/20-certifier.md"),
}
_EXPECTED_RESULTS = {
    role: f"{role.value.casefold()}-result@1.0" for role in _ROLE_CONTRACTS
}
_EXPECTED_STEPS = {
    MissionRole.ARCHITECT: OperatingStep.UNDERSTAND_CONTRACT,
    MissionRole.IMPLEMENTER: OperatingStep.ACT,
    MissionRole.TESTER: OperatingStep.VERIFY,
    MissionRole.REVIEWER: OperatingStep.REPORT,
    MissionRole.CERTIFIER: OperatingStep.CONTROLLED_TRANSITION,
}
_UPSTREAM_ROLES = {
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
_CONTRACT_NAME = {
    role: f"{role.value.casefold()}-result" for role in _ROLE_CONTRACTS
}
_COMMON_AUTHORITY = (
    "AGENTS.md",
    "docs/02-invariants.md",
    "docs/03-fail-closed-policy.md",
    "docs/04-authority-model.md",
    "docs/12-codex-operating-contract.md",
    "docs/35-codex-execution-contract.md",
    "docs/PHASE-3-CERTIFICATION.md",
)
_ALL_AUTHORITY_PATHS = frozenset(
    (*_COMMON_AUTHORITY, *(path for paths in _ROLE_CONTRACTS.values() for path in paths))
)


class ContextBuildError(RuntimeError):
    """Required context could not be proven current and safe."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class CognitiveCategory(str, Enum):
    ARCHITECTURE = "ARCHITECTURE"
    INVARIANT = "INVARIANT"
    HISTORICAL_FINDING = "HISTORICAL_FINDING"
    LESSON = "LESSON"
    EXAMPLE = "EXAMPLE"


@dataclass(frozen=True, slots=True)
class ExecutionScope:
    allowed_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CodexExecutionRequest:
    request_id: str
    mission_id: str
    workflow_generation: int
    role: MissionRole
    subject: str
    user_story_id: str | None
    repository_root: str
    observed_commit: str
    operating_step: OperatingStep
    scope: ExecutionScope
    role_contract_ref: str
    expected_result_contract: str
    worktree_assignment_id: str | None = None


@dataclass(frozen=True, slots=True)
class CognitiveSource:
    relative_path: str
    category: CognitiveCategory
    roles: tuple[MissionRole, ...]
    subjects: tuple[str, ...] = ()
    path_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AuthoritativeContextEntry:
    kind: str
    identity: str
    source: str
    fingerprint: str
    payload_json: str


@dataclass(frozen=True, slots=True)
class CognitiveContextEntry:
    category: CognitiveCategory
    relative_path: str
    fingerprint: str
    content: str


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    request_id: str
    role: MissionRole
    authoritative: tuple[AuthoritativeContextEntry, ...]
    cognitive: tuple[CognitiveContextEntry, ...]


class MissionStateReaderPort(Protocol):
    def load(self) -> MissionState: ...


class ProjectStateReaderPort(Protocol):
    def load(self) -> ProjectState: ...


class RegistryReaderPort(Protocol):
    def load(self) -> WorktreeRegistry: ...


class PrimaryObservation(Protocol):
    branch_name: str
    head_commit: str
    clean: bool


class WorktreeObservation(Protocol):
    physical_exists: bool
    branch_matches: bool
    head_commit: str | None
    clean: bool | None
    resumable: bool
    reasons: tuple[str, ...]


class RepositoryContextPort(Protocol):
    @property
    def repository_root(self) -> Path: ...

    @property
    def registry_store(self) -> RegistryReaderPort: ...

    def inspect_primary(self) -> PrimaryObservation: ...

    def inspect(
        self, assignment_id: str, *, current_generation: int
    ) -> WorktreeObservation: ...


class ContextBuilder:
    """Build a reconstructible context without granting execution authority."""

    def __init__(
        self,
        *,
        mission_store: MissionStateReaderPort,
        project_store: ProjectStateReaderPort,
        repository: RepositoryContextPort,
        validator: ContractValidator | None = None,
    ) -> None:
        self._mission_store = mission_store
        self._project_store = project_store
        self._repository = repository
        self._validator = validator or ContractValidator()

    def build(
        self,
        request: CodexExecutionRequest,
        *,
        upstream_results: tuple[object, ...] = (),
        cognitive_sources: tuple[CognitiveSource, ...] = (),
    ) -> ExecutionContext:
        self._require_request(request)
        mission = self._mission_store.load()
        project = self._project_store.load()
        root = self._repository.repository_root.resolve(strict=True)
        primary = self._repository.inspect_primary()
        self._require_fresh_mission(request, mission)
        self._require_repository_binding(request, root, primary)
        story = self._select_story(request, project)
        assignment = self._select_worktree(request, mission, root)
        role_results = self._select_role_results(request, upstream_results)

        entries: list[AuthoritativeContextEntry] = []
        for relative_path in self._authority_paths(request.role):
            entries.append(self._authority_reference(root, relative_path))
        entries.append(
            _model_entry("MISSION_STATE", mission.mission_id, "mission-store", mission)
        )
        entries.append(
            _json_entry(
                "REPOSITORY",
                str(root),
                "git",
                {
                    "repository_root": str(root),
                    "branch_name": primary.branch_name,
                    "head_commit": primary.head_commit,
                    "clean": primary.clean,
                },
            )
        )
        if story is not None:
            entries.append(_model_entry("USER_STORY", story.id, "project-store", story))
            entries.extend(self._dependency_entries(story, project))
            entries.extend(self._control_entries(request.role, story, project))
        if assignment is not None:
            entries.append(
                _model_entry(
                    "WORKTREE_ASSIGNMENT",
                    assignment.assignment_id,
                    "worktree-registry",
                    assignment,
                )
            )
        for role, payload in role_results:
            entries.append(
                _json_entry(
                    "ROLE_RESULT",
                    role.value,
                    f"validated:{_CONTRACT_NAME[role]}",
                    payload,
                )
            )
        authoritative = tuple(
            sorted(entries, key=lambda item: (_authority_rank(item.kind), item.identity))
        )
        cognitive = self._select_cognitive(root, request, story, cognitive_sources)
        return ExecutionContext(request.request_id, request.role, authoritative, cognitive)

    @staticmethod
    def _require_request(request: CodexExecutionRequest) -> None:
        if not isinstance(request, CodexExecutionRequest):
            raise ContextBuildError("INVALID_REQUEST", "request must use the canonical type")
        text_values = (
            request.request_id,
            request.mission_id,
            request.subject,
            request.repository_root,
        )
        if not all(isinstance(value, str) and value.strip() for value in text_values):
            raise ContextBuildError("INVALID_REQUEST", "request identity fields must be non-empty")
        if request.role not in _ROLE_CONTRACTS:
            raise ContextBuildError("UNSUPPORTED_ROLE", "ORCHESTRATOR has no Codex role policy")
        if not isinstance(request.workflow_generation, int) or isinstance(
            request.workflow_generation, bool
        ) or request.workflow_generation < 0:
            raise ContextBuildError("INVALID_REQUEST", "generation must be non-negative")
        if not _SHA_PATTERN.fullmatch(request.observed_commit):
            raise ContextBuildError("INVALID_COMMIT", "observed commit must be a full lowercase SHA")
        if not isinstance(request.scope, ExecutionScope):
            raise ContextBuildError("INVALID_SCOPE", "scope must use ExecutionScope")
        if request.role_contract_ref != _ROLE_CONTRACTS[request.role][0]:
            raise ContextBuildError("ROLE_CONTRACT_MISMATCH", "role contract is not canonical")
        if request.expected_result_contract != _EXPECTED_RESULTS[request.role]:
            raise ContextBuildError("RESULT_CONTRACT_MISMATCH", "expected RoleResult is not canonical")
        if request.operating_step is not _EXPECTED_STEPS[request.role]:
            raise ContextBuildError("OPERATING_STEP_MISMATCH", "operating step is wrong for role")

    @staticmethod
    def _require_fresh_mission(request: CodexExecutionRequest, mission: MissionState) -> None:
        if mission.status is not MissionStatus.ACTIVE or mission.blockers:
            raise ContextBuildError("MISSION_NOT_EXECUTABLE", "mission is not active and unblocked")
        comparisons = (
            (mission.mission_id, request.mission_id, "MISSION_MISMATCH"),
            (mission.workflow_generation, request.workflow_generation, "GENERATION_MISMATCH"),
            (mission.role, request.role, "ROLE_MISMATCH"),
            (mission.subject, request.subject, "SUBJECT_MISMATCH"),
            (mission.operating_step, request.operating_step, "OPERATING_STEP_MISMATCH"),
            (mission.observed_commit, request.observed_commit, "COMMIT_MISMATCH"),
        )
        for actual, expected, code in comparisons:
            if actual != expected:
                raise ContextBuildError(code, "request differs from current MissionState")

    @staticmethod
    def _require_repository_binding(
        request: CodexExecutionRequest, root: Path, primary: PrimaryObservation
    ) -> None:
        try:
            requested_root = Path(request.repository_root).resolve(strict=True)
        except OSError as error:
            raise ContextBuildError("REPOSITORY_MISMATCH", "repository root is unavailable") from error
        if _path_key(requested_root) != _path_key(root):
            raise ContextBuildError("REPOSITORY_MISMATCH", "request targets another repository")
        if primary.head_commit != request.observed_commit:
            raise ContextBuildError("COMMIT_MISMATCH", "primary HEAD differs from request")
        if primary.clean is not True:
            raise ContextBuildError("DIRTY_REPOSITORY", "primary worktree must be clean")

    @staticmethod
    def _select_story(request: CodexExecutionRequest, project: ProjectState) -> UserStory | None:
        if request.role is MissionRole.ARCHITECT:
            if request.user_story_id is not None or request.worktree_assignment_id is not None:
                raise ContextBuildError("CROSS_STORY_CONTEXT", "Architect cannot bind a UserStory")
            if request.scope != ExecutionScope((), ()):
                raise ContextBuildError("INVALID_SCOPE", "Architect context has no write scope")
            return None
        if not request.user_story_id or request.subject != request.user_story_id:
            raise ContextBuildError("STORY_MISMATCH", "role requires its exact UserStory subject")
        matches = [story for story in project.user_stories if story.id == request.user_story_id]
        if len(matches) != 1:
            raise ContextBuildError("STORY_UNRESOLVED", "UserStory must resolve exactly once")
        story = matches[0]
        if story.human_approval is None:
            raise ContextBuildError("HUMAN_CONTEXT_MISSING", "Human Approval context is absent")
        expected_scope = ExecutionScope(story.scope.allowed_paths, story.scope.forbidden_paths)
        if request.scope != expected_scope:
            raise ContextBuildError("SCOPE_MISMATCH", "request scope differs from UserStory")
        return story

    def _select_worktree(
        self, request: CodexExecutionRequest, mission: MissionState, root: Path
    ) -> WorktreeAssignment | None:
        if request.role is not MissionRole.IMPLEMENTER:
            if request.worktree_assignment_id is not None:
                raise ContextBuildError("WORKTREE_NOT_ALLOWED", "role policy excludes a worktree")
            return None
        if not request.worktree_assignment_id:
            raise ContextBuildError("WORKTREE_REQUIRED", "Implementer requires an assignment")
        registry = self._repository.registry_store.load()
        matches = [
            item
            for item in registry.assignments
            if item.assignment_id == request.worktree_assignment_id
        ]
        if len(matches) != 1:
            raise ContextBuildError("WORKTREE_UNRESOLVED", "assignment must resolve exactly once")
        assignment = matches[0]
        expected = (
            assignment.mission_id == request.mission_id
            and assignment.user_story_id == request.user_story_id
            and assignment.workflow_generation == request.workflow_generation
            and assignment.baseline_commit == request.observed_commit
            and assignment.status is WorktreeStatus.ACTIVE
        )
        if not expected:
            raise ContextBuildError("WORKTREE_MISMATCH", "assignment binding is stale or divergent")
        path = _safe_existing_path(Path(assignment.worktree_path))
        if _path_key(path) == _path_key(root):
            raise ContextBuildError("WORKTREE_MISMATCH", "assignment must be isolated from primary")
        inspection = self._repository.inspect(
            assignment.assignment_id, current_generation=mission.workflow_generation
        )
        if (
            not inspection.resumable
            or inspection.head_commit != request.observed_commit
            or inspection.clean is not True
            or not inspection.physical_exists
            or not inspection.branch_matches
            or inspection.reasons
        ):
            raise ContextBuildError("WORKTREE_MISMATCH", "physical worktree is not exact and clean")
        return assignment

    def _select_role_results(
        self, request: CodexExecutionRequest, results: tuple[object, ...]
    ) -> tuple[tuple[MissionRole, dict[str, object]], ...]:
        if not isinstance(results, tuple):
            raise ContextBuildError("INVALID_UPSTREAM", "upstream results must be a tuple")
        selected: dict[MissionRole, dict[str, object]] = {}
        for result in results:
            if isinstance(result, Mapping):
                payload = dict(result)
            else:
                try:
                    payload = cast(dict[str, object], to_dict(result))
                except TypeError as error:
                    raise ContextBuildError("INVALID_UPSTREAM", "RoleResult is not structured") from error
            try:
                role = MissionRole(payload.get("role"))
            except (TypeError, ValueError) as error:
                raise ContextBuildError("INVALID_UPSTREAM", "RoleResult role is invalid") from error
            if role not in _CONTRACT_NAME or role in selected:
                raise ContextBuildError("INVALID_UPSTREAM", "RoleResult role is duplicate or unsupported")
            validation = self._validator.validate(_CONTRACT_NAME[role], payload)
            if not validation.is_valid:
                raise ContextBuildError("INVALID_UPSTREAM", "RoleResult contract validation failed")
            bindings: dict[str, object] = {
                "mission_id": request.mission_id,
                "workflow_generation": request.workflow_generation,
                "subject": request.subject,
                "observed_commit": request.observed_commit,
            }
            if role is not MissionRole.ARCHITECT:
                bindings["user_story_id"] = request.user_story_id
            if any(payload.get(key) != value for key, value in bindings.items()):
                raise ContextBuildError("STALE_ROLE_RESULT", "RoleResult binding is stale or cross-context")
            selected[role] = payload
        expected = set(_UPSTREAM_ROLES[request.role])
        if set(selected) != expected:
            raise ContextBuildError("UPSTREAM_SET_MISMATCH", "required upstream RoleResults are not exact")
        return tuple((role, selected[role]) for role in _UPSTREAM_ROLES[request.role])

    def _dependency_entries(
        self, story: UserStory, project: ProjectState
    ) -> tuple[AuthoritativeContextEntry, ...]:
        entries = []
        for dependency_id in sorted(story.depends_on):
            matches = [
                item
                for item in project.certifications
                if item.subject == dependency_id
                and item.result is CertificationResult.CERTIFIED
            ]
            if len(matches) != 1:
                raise ContextBuildError(
                    "DEPENDENCY_NOT_CERTIFIED", "each dependency needs one Certification"
                )
            entries.append(
                _model_entry("DEPENDENCY_CERTIFICATION", dependency_id, "project-store", matches[0])
            )
        return tuple(entries)

    @staticmethod
    def _control_entries(
        role: MissionRole, story: UserStory, project: ProjectState
    ) -> tuple[AuthoritativeContextEntry, ...]:
        if role is not MissionRole.CERTIFIER:
            return ()
        evidence = sorted(
            (item for item in project.evidence if item.subject == story.id),
            key=lambda item: item.evidence_id,
        )
        gates = sorted(
            (item for item in project.gates if item.subject == story.id),
            key=lambda item: item.gate_id,
        )
        return tuple(
            [
                _model_entry("EVIDENCE", item.evidence_id, "project-store", item)
                for item in evidence
            ]
            + [_model_entry("GATE", item.gate_id, "project-store", item) for item in gates]
        )

    @staticmethod
    def _authority_paths(role: MissionRole) -> tuple[str, ...]:
        role_contract, architecture_contract = _ROLE_CONTRACTS[role]
        return (*_COMMON_AUTHORITY, architecture_contract, role_contract)

    @staticmethod
    def _authority_reference(root: Path, relative_path: str) -> AuthoritativeContextEntry:
        content = _read_document(_safe_document(root, relative_path))
        return _json_entry(
            "CONTRACT_REFERENCE",
            relative_path,
            relative_path,
            {"relative_path": relative_path, "sha256": _fingerprint(content)},
        )

    def _select_cognitive(
        self,
        root: Path,
        request: CodexExecutionRequest,
        story: UserStory | None,
        sources: tuple[CognitiveSource, ...],
    ) -> tuple[CognitiveContextEntry, ...]:
        if not isinstance(sources, tuple):
            raise ContextBuildError("INVALID_COGNITIVE_SOURCE", "sources must be a tuple")
        selected: dict[str, CognitiveContextEntry] = {}
        authority = {path.casefold() for path in _ALL_AUTHORITY_PATHS}
        story_paths = story.scope.allowed_paths if story is not None else ()
        ordered_sources = sorted(
            sources,
            key=lambda item: (
                item.relative_path.casefold() if isinstance(item, CognitiveSource) else "",
                item.category.value if isinstance(item, CognitiveSource) and isinstance(item.category, CognitiveCategory) else "",
            ),
        )
        for source in ordered_sources:
            if (
                not isinstance(source, CognitiveSource)
                or not isinstance(source.category, CognitiveCategory)
                or not isinstance(source.roles, tuple)
                or not source.roles
                or any(role not in _ROLE_CONTRACTS for role in source.roles)
            ):
                raise ContextBuildError("INVALID_COGNITIVE_SOURCE", "source metadata is incomplete")
            if request.role not in source.roles:
                continue
            if source.subjects and request.subject not in source.subjects:
                continue
            if source.path_prefixes and not _paths_relevant(source.path_prefixes, story_paths):
                continue
            path = _safe_document(root, source.relative_path)
            relative = path.relative_to(root).as_posix()
            if relative.casefold() in authority:
                raise ContextBuildError("AUTHORITY_AS_COGNITIVE", "authority source cannot be recategorized")
            content = _read_document(path)
            if _contains_secret_material(content):
                raise ContextBuildError(
                    "SECRET_SOURCE_REJECTED", "document contains secret-like material"
                )
            selected.setdefault(
                relative.casefold(),
                CognitiveContextEntry(source.category, relative, _fingerprint(content), content),
            )
        return tuple(
            sorted(
                selected.values(),
                key=lambda item: (item.category.value, item.relative_path.casefold()),
            )
        )


def _safe_document(root: Path, relative_path: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ContextBuildError("INVALID_DOCUMENT_PATH", "document path must be explicit")
    candidate_text = relative_path.replace("\\", "/")
    candidate = Path(candidate_text)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ContextBuildError("DOCUMENT_PATH_ESCAPE", "document path must stay repository-local")
    parts = tuple(part.casefold() for part in candidate.parts)
    if not (
        candidate_text in {"AGENTS.md", "README.md"}
        or (parts and parts[0] in {"docs", "roles"})
    ):
        raise ContextBuildError("DOCUMENT_NOT_ALLOWED", "only explicit repository documentation is allowed")
    if candidate.suffix.casefold() != ".md" or any(_secret_component(part) for part in parts):
        raise ContextBuildError("SECRET_SOURCE_REJECTED", "secret-like sources are excluded")
    try:
        cursor = root
        for part in candidate.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ContextBuildError("DOCUMENT_SYMLINK_REJECTED", "document symlinks are excluded")
        resolved = (root / candidate).resolve(strict=True)
    except OSError as error:
        raise ContextBuildError("DOCUMENT_UNAVAILABLE", "required document is unavailable") from error
    if not resolved.is_file() or not _contains(root, resolved):
        raise ContextBuildError("DOCUMENT_PATH_ESCAPE", "document is outside repository")
    return resolved


def _safe_existing_path(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ContextBuildError("WORKTREE_MISMATCH", "worktree path is unavailable") from error
    if not resolved.is_dir():
        raise ContextBuildError("WORKTREE_MISMATCH", "worktree path is invalid")
    return resolved


def _read_document(path: Path) -> str:
    try:
        if path.stat().st_size > 256_000:
            raise ContextBuildError("DOCUMENT_TOO_LARGE", "document exceeds the context limit")
        return path.read_text(encoding="utf-8")
    except UnicodeError as error:
        raise ContextBuildError("DOCUMENT_ENCODING", "document must be UTF-8") from error
    except OSError as error:
        raise ContextBuildError("DOCUMENT_UNAVAILABLE", "document cannot be read") from error


def _secret_component(component: str) -> bool:
    return (
        component == ".env"
        or component.startswith(".env.")
        or component
        in {
            ".git",
            ".venv",
            ".agentic-engineering-os",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "__pycache__",
            "cache",
            "caches",
            "runtime",
        }
        or any(token in component for token in ("secret", "credential", "id_rsa"))
        or component.endswith((".pem", ".key", ".p12", ".pfx"))
    )


def _contains_secret_material(content: str) -> bool:
    if re.search(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", content):
        return True
    return bool(
        re.search(
            r"(?im)^\s*(?:api[_-]?key|password|access[_-]?token)\s*[:=]\s*\S+",
            content,
        )
    )


def _paths_relevant(prefixes: tuple[str, ...], story_paths: tuple[str, ...]) -> bool:
    left_paths = tuple(_scope_path(item) for item in prefixes)
    right_paths = tuple(_scope_path(item) for item in story_paths)
    return any(
        left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")
        for left in left_paths
        for right in right_paths
    )


def _scope_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or Path(value).is_absolute()
        or ".." in Path(value).parts
    ):
        raise ContextBuildError("INVALID_RELEVANCE_METADATA", "path prefix is not repository-local")
    return value.replace("\\", "/").strip("/").casefold()


def _model_entry(kind: str, identity: str, source: str, model: object) -> AuthoritativeContextEntry:
    return _json_entry(kind, identity, source, cast(dict[str, object], to_dict(model)))


def _json_entry(
    kind: str, identity: str, source: str, payload: Mapping[str, object]
) -> AuthoritativeContextEntry:
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return AuthoritativeContextEntry(kind, identity, source, _fingerprint(payload_json), payload_json)


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _authority_rank(kind: str) -> int:
    return {
        "CONTRACT_REFERENCE": 0,
        "MISSION_STATE": 1,
        "REPOSITORY": 2,
        "USER_STORY": 3,
        "WORKTREE_ASSIGNMENT": 4,
        "DEPENDENCY_CERTIFICATION": 5,
        "ROLE_RESULT": 6,
        "EVIDENCE": 7,
        "GATE": 8,
    }[kind]


def _path_key(path: Path) -> str:
    return str(path).replace("\\", "/").casefold()


def _contains(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
