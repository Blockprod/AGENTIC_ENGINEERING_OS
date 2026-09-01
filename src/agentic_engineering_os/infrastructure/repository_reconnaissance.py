"""Bounded, deterministic, read-only reconnaissance of a target repository."""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import tomllib
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from agentic_engineering_os.domain import (
    AgenticOsInitializationState,
    AgenticOsStateObservation,
    CandidateCommandObservation,
    DocumentStatus,
    GitRepositoryObservation,
    GitWorktreeObservation,
    ManifestObservation,
    ManagedSectionObservation,
    ManagedSectionStatus,
    ObservationClassification,
    ObservedValue,
    PathObservation,
    ReconnaissanceIssue,
    RepositoryProfile,
    RepositorySupportStatus,
    RuntimeFileObservation,
    SymlinkObservation,
    ToolchainObservation,
    VerificationKind,
    GITIGNORE_MANAGED_SECTION,
    GITIGNORE_SECTION_END,
    GITIGNORE_SECTION_START,
    MAINTENANCE_SCHEMA_VERSION,
)

from .agents_integration import AgentsIntegrationService
from .git_adapter import GitAdapter, GitOperationError, GitReadOnlyState
from .project_configuration import (
    CONFIG_DIRECTORY,
    CONFIG_FILENAME,
    CONFIG_VERSION,
    ProjectConfigurationError,
    ProjectConfigurationLoader,
    ProjectConfigurationValidator,
)


_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        ".agentic-engineering-os",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "node_modules",
        "target",
        "vendor",
    }
)
_SENSITIVE_NAMES = frozenset({".env", "credentials", "secrets"})
_SENSITIVE_SUFFIXES = (".key", ".pem", ".p12", ".pfx")
_KNOWN_GITIGNORE_RULES = frozenset(
    {
        ".agentic-engineering-os/",
        ".agentic-engineering-os/worktrees.json",
        ".agentic-engineering-os/.worktrees.*.tmp",
        ".agentic-engineering-os/negative-outcomes.json",
        ".agentic-engineering-os/.negative-outcomes.*.tmp",
        ".agentic-engineering-os/executions.json",
        ".agentic-engineering-os/.executions.*.tmp",
        ".agentic-engineering-os/maintenance.json",
        ".agentic-engineering-os/.maintenance.*.tmp",
        ".agentic-engineering-os/.maintenance.lock",
        ".agentic-engineering-os/operational-events/",
    }
)
_REQUIRED_GITIGNORE_RULES = frozenset(
    {
        ".agentic-engineering-os/worktrees.json",
        ".agentic-engineering-os/.worktrees.*.tmp",
        ".agentic-engineering-os/negative-outcomes.json",
        ".agentic-engineering-os/.negative-outcomes.*.tmp",
        ".agentic-engineering-os/executions.json",
        ".agentic-engineering-os/.executions.*.tmp",
        ".agentic-engineering-os/maintenance.json",
        ".agentic-engineering-os/.maintenance.*.tmp",
        ".agentic-engineering-os/.maintenance.lock",
        ".agentic-engineering-os/operational-events/",
    }
)
_RUNTIME_FORMATS = {
    "state.json": ("schema_version", "1.0"),
    "mission.json": ("schema_version", "1.0"),
    "worktrees.json": ("schema_version", "1.0"),
    "negative-outcomes.json": ("version", "2.0"),
    "executions.json": ("schema_version", "1.1"),
    "maintenance.json": ("schema_version", MAINTENANCE_SCHEMA_VERSION),
}
_PACKAGE_SCRIPT_KINDS = {
    "build": VerificationKind.BUILD,
    "lint": VerificationKind.LINT,
    "test": VerificationKind.TEST,
    "type-check": VerificationKind.TYPECHECK,
    "typecheck": VerificationKind.TYPECHECK,
}
_SECRET_VALUE = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|password|passwd|secret|credential)"
    r"\s*[:=]\s*\S+|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|"
    r"[A-Za-z][A-Za-z0-9+.-]*://[^/@\s:]+:[^/@\s]+@"
)


class RepositoryReconnaissanceError(RuntimeError):
    """The requested filesystem boundary cannot be inspected safely."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class _DuplicateJsonKeyError(ValueError):
    pass


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    candidate: dict[str, Any] = {}
    for key, value in pairs:
        if key in candidate:
            raise _DuplicateJsonKeyError(key)
        candidate[key] = value
    return candidate


class RepositoryReconnaissance:
    """Inspect bounded repository facts without writing or granting authority."""

    def __init__(
        self,
        *,
        max_configuration_bytes: int = 256_000,
        max_top_level_entries: int = 512,
        max_context_entries: int = 512,
        max_context_sources: int = 128,
        max_context_depth: int = 2,
        git_adapter_factory: Callable[[Path], GitAdapter] = GitAdapter,
    ) -> None:
        limits = (
            max_configuration_bytes,
            max_top_level_entries,
            max_context_entries,
            max_context_sources,
            max_context_depth,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in limits
        ):
            raise ValueError("reconnaissance limits must be positive integers")
        self._max_configuration_bytes = max_configuration_bytes
        self._max_top_level_entries = max_top_level_entries
        self._max_context_entries = max_context_entries
        self._max_context_sources = max_context_sources
        self._max_context_depth = max_context_depth
        self._git_adapter_factory = git_adapter_factory
        self._agents_integration = AgentsIntegrationService()

    def inspect(self, repository_root: Path | str) -> RepositoryProfile:
        """Return one reconstructible profile; never create or repair files."""

        requested = Path(repository_root)
        if requested.is_symlink():
            raise RepositoryReconnaissanceError(
                "UNSAFE_REPOSITORY_ROOT", "repository root cannot be a symlink"
            )
        try:
            root = requested.resolve(strict=True)
        except OSError as error:
            raise RepositoryReconnaissanceError(
                "INVALID_REPOSITORY_ROOT", "repository root cannot be resolved"
            ) from error
        if not root.is_dir():
            raise RepositoryReconnaissanceError(
                "INVALID_REPOSITORY_ROOT", "repository root must be a directory"
            )

        issues: list[ReconnaissanceIssue] = []
        git, support = self._observe_git(root, issues)
        top_level, sensitive, symlinks, top_complete = self._observe_top_level(
            root, issues
        )
        manifests, manifest_data = self._observe_manifests(root, top_level, issues)
        toolchains = self._infer_toolchains(top_level)
        commands = self._discover_commands(top_level, manifest_data, issues)
        context_sources, context_complete, context_symlinks = self._context_sources(
            root, top_level, issues
        )
        symlinks = tuple(
            sorted(
                {*symlinks, *context_symlinks},
                key=lambda item: _sort_key(item.relative_path),
            )
        )
        agentic_os = self._observe_agentic_os(root, issues)
        codex = ObservedValue(
            ObservationClassification.UNKNOWN,
            None,
            "machine-boundary",
            "Codex availability is not inspected by repository-only reconnaissance",
        )
        return RepositoryProfile(
            requested_root=str(root),
            support_status=support,
            git=git,
            top_level_entries=top_level,
            manifests=manifests,
            toolchains=toolchains,
            candidate_commands=commands,
            context_sources=context_sources,
            sensitive_paths=sensitive,
            symlinks=symlinks,
            agentic_os=agentic_os,
            codex_availability=codex,
            scan_complete=top_complete and context_complete,
            issues=tuple(
                sorted(
                    issues,
                    key=lambda item: (item.code, item.source, item.detail),
                )
            ),
        )

    def _observe_git(
        self, root: Path, issues: list[ReconnaissanceIssue]
    ) -> tuple[GitRepositoryObservation, RepositorySupportStatus]:
        try:
            state = self._git_adapter_factory(root).observe_read_only()
        except GitOperationError as error:
            classification = (
                ObservationClassification.FACT
                if error.code == "NOT_GIT_REPOSITORY"
                else ObservationClassification.UNKNOWN
            )
            is_repository = error.code != "NOT_GIT_REPOSITORY"
            issues.append(
                ReconnaissanceIssue(
                    error.code,
                    classification,
                    "git",
                    error.message,
                )
            )
            unknown = lambda detail: ObservedValue(  # noqa: E731
                ObservationClassification.UNKNOWN, None, "git", detail
            )
            return (
                GitRepositoryObservation(
                    is_repository=ObservedValue(
                        classification,
                        is_repository if classification is ObservationClassification.FACT else None,
                        "git rev-parse --show-toplevel",
                        error.message,
                    ),
                    top_level=unknown("Git top-level is unavailable"),
                    branch=unknown("Git branch is unavailable"),
                    detached=unknown("Git detached state is unavailable"),
                    head_commit=unknown("Git HEAD is unavailable"),
                    clean=unknown("Git cleanliness is unavailable"),
                    worktrees=(),
                    errors=(error.code,),
                ),
                (
                    RepositorySupportStatus.BLOCKED
                    if error.code == "NOT_GIT_REPOSITORY"
                    else RepositorySupportStatus.UNKNOWN
                ),
            )

        same_root = _path_key(state.top_level) == _path_key(root)
        if not same_root:
            issues.append(
                ReconnaissanceIssue(
                    "REPOSITORY_ROOT_MISMATCH",
                    ObservationClassification.FACT,
                    "git rev-parse --show-toplevel",
                    "requested root is inside a different Git top-level",
                )
            )
        git = _git_profile(state)
        return git, (
            RepositorySupportStatus.SUPPORTED
            if same_root
            else RepositorySupportStatus.BLOCKED
        )

    def _observe_top_level(
        self, root: Path, issues: list[ReconnaissanceIssue]
    ) -> tuple[
        tuple[PathObservation, ...],
        tuple[PathObservation, ...],
        tuple[SymlinkObservation, ...],
        bool,
    ]:
        entries: list[os.DirEntry[str]] = []
        try:
            with os.scandir(root) as stream:
                for entry in stream:
                    entries.append(entry)
                    if len(entries) > self._max_top_level_entries:
                        issues.append(
                            ReconnaissanceIssue(
                                "TOP_LEVEL_LIMIT_EXCEEDED",
                                ObservationClassification.UNKNOWN,
                                "filesystem:lstat",
                                f"top-level entry limit {self._max_top_level_entries} exceeded",
                            )
                        )
                        return (), (), (), False
        except OSError as error:
            issues.append(
                ReconnaissanceIssue(
                    "FILESYSTEM_OBSERVATION_FAILED",
                    ObservationClassification.UNKNOWN,
                    "filesystem:lstat",
                    type(error).__name__,
                )
            )
            return (), (), (), False

        visible: list[PathObservation] = []
        sensitive: list[PathObservation] = []
        symlinks: list[SymlinkObservation] = []
        for entry in sorted(entries, key=lambda item: _sort_key(item.name)):
            relative = _relative_name(entry.name)
            entry_path = root / relative
            if entry_path.is_symlink():
                symlinks.append(_symlink_observation(root, relative))
                continue
            try:
                kind = "DIRECTORY" if entry.is_dir(follow_symlinks=False) else "FILE"
            except OSError as error:
                issues.append(
                    ReconnaissanceIssue(
                        "FILESYSTEM_ENTRY_UNREADABLE",
                        ObservationClassification.UNKNOWN,
                        relative,
                        type(error).__name__,
                    )
                )
                continue
            observation = PathObservation(
                relative,
                kind,
                ObservationClassification.FACT,
                "filesystem:lstat",
            )
            if _is_sensitive_name(relative) or relative == ".git":
                sensitive.append(observation)
            elif relative not in _IGNORED_DIRECTORIES:
                visible.append(observation)
        return tuple(visible), tuple(sensitive), tuple(symlinks), True

    def _observe_manifests(
        self,
        root: Path,
        top_level: tuple[PathObservation, ...],
        issues: list[ReconnaissanceIssue],
    ) -> tuple[tuple[ManifestObservation, ...], dict[str, object]]:
        names = {item.relative_path for item in top_level if item.kind == "FILE"}
        specifications = {
            "Cargo.toml": "TOML",
            "package.json": "JSON",
            "pyproject.toml": "TOML",
            "setup.cfg": "INI",
            "tox.ini": "INI",
        }
        observations: list[ManifestObservation] = []
        parsed: dict[str, object] = {}
        for relative, format_name in sorted(specifications.items(), key=lambda item: _sort_key(item[0])):
            if relative not in names:
                continue
            status, data, detail = self._parse_manifest(root, relative, format_name)
            observations.append(
                ManifestObservation(
                    relative,
                    format_name,
                    status,
                    (
                        ObservationClassification.FACT
                        if status is DocumentStatus.VALID
                        else ObservationClassification.UNKNOWN
                    ),
                    detail,
                )
            )
            if status is DocumentStatus.VALID:
                parsed[relative] = data
            else:
                issues.append(
                    ReconnaissanceIssue(
                        "MANIFEST_NOT_USABLE",
                        ObservationClassification.UNKNOWN,
                        relative,
                        detail,
                    )
                )
        return tuple(observations), parsed

    def _parse_manifest(
        self, root: Path, relative: str, format_name: str
    ) -> tuple[DocumentStatus, object, str]:
        status, text = self._read_bounded_file(root, relative)
        if status is not DocumentStatus.VALID or text is None:
            return status, None, f"{relative} is {status.value}"
        try:
            if format_name == "JSON":
                data = json.loads(
                    text,
                    object_pairs_hook=_strict_json_object,
                    parse_constant=lambda value: (_raise_invalid_constant(value)),
                )
            elif format_name == "TOML":
                data = tomllib.loads(text)
            else:
                parser = configparser.ConfigParser(interpolation=None, strict=True)
                parser.read_string(text)
                data = tuple(sorted(parser.sections(), key=_sort_key))
        except (_DuplicateJsonKeyError, json.JSONDecodeError, tomllib.TOMLDecodeError, configparser.Error, ValueError) as error:
            return DocumentStatus.INVALID, None, f"strict {format_name} parse failed: {type(error).__name__}"
        return DocumentStatus.VALID, data, f"strict {format_name} parse succeeded"

    def _infer_toolchains(
        self, top_level: tuple[PathObservation, ...]
    ) -> tuple[ToolchainObservation, ...]:
        files = {item.relative_path for item in top_level if item.kind == "FILE"}
        python_markers = sorted(
            (
                name
                for name in files
                if name in {"pyproject.toml", "setup.cfg", "tox.ini", "noxfile.py"}
                or (name.startswith("requirements") and name.endswith(".txt"))
            ),
            key=_sort_key,
        )
        evidence = {
            "node": tuple(name for name in ("package.json",) if name in files),
            "python": tuple(python_markers),
            "rust": tuple(name for name in ("Cargo.toml",) if name in files),
        }
        return tuple(
            ToolchainObservation(
                identity,
                ObservationClassification.INFERENCE,
                paths,
                "toolchain candidate inferred only from explicit root markers",
            )
            for identity, paths in sorted(evidence.items())
            if paths
        )

    def _discover_commands(
        self,
        top_level: tuple[PathObservation, ...],
        manifests: Mapping[str, object],
        issues: list[ReconnaissanceIssue],
    ) -> tuple[CandidateCommandObservation, ...]:
        package = manifests.get("package.json")
        if not isinstance(package, Mapping):
            return ()
        scripts = package.get("scripts")
        if scripts is None:
            return ()
        if not isinstance(scripts, Mapping) or any(not isinstance(key, str) for key in scripts):
            issues.append(
                ReconnaissanceIssue(
                    "INVALID_COMMAND_DECLARATION",
                    ObservationClassification.UNKNOWN,
                    "package.json:scripts",
                    "scripts must be an object with string keys",
                )
            )
            return ()
        runner, runner_issue = _package_runner(top_level)
        if runner is None:
            issues.append(
                ReconnaissanceIssue(
                    runner_issue,
                    ObservationClassification.UNKNOWN,
                    "package.json",
                    (
                        "multiple package-manager lockfiles are present"
                        if runner_issue == "AMBIGUOUS_PACKAGE_MANAGER"
                        else "no package-manager lockfile proves an invocation"
                    ),
                )
            )
            return ()
        commands: list[CandidateCommandObservation] = []
        for name, kind in sorted(_PACKAGE_SCRIPT_KINDS.items()):
            declared = scripts.get(name)
            if declared is None:
                continue
            if not isinstance(declared, str) or not declared.strip():
                issues.append(
                    ReconnaissanceIssue(
                        "INVALID_COMMAND_DECLARATION",
                        ObservationClassification.UNKNOWN,
                        f"package.json:scripts.{name}",
                        "script body is not a non-empty string",
                    )
                )
                continue
            if _SECRET_VALUE.search(declared):
                issues.append(
                    ReconnaissanceIssue(
                        "SECRET_LIKE_COMMAND_REDACTED",
                        ObservationClassification.UNKNOWN,
                        f"package.json:scripts.{name}",
                        "script exists but its content is not retained",
                    )
                )
                continue
            commands.append(
                CandidateCommandObservation(
                    command_id=name,
                    kind=kind,
                    executable=runner,
                    args=("run", name),
                    classification=ObservationClassification.INFERENCE,
                    source=f"package.json:scripts.{name}",
                    detail="candidate invocation derived from an explicit package script; not executed or authoritative",
                )
            )
        return tuple(commands)

    def _context_sources(
        self,
        root: Path,
        top_level: tuple[PathObservation, ...],
        issues: list[ReconnaissanceIssue],
    ) -> tuple[tuple[PathObservation, ...], bool, tuple[SymlinkObservation, ...]]:
        sources: list[PathObservation] = []
        symlinks: list[SymlinkObservation] = []
        for item in top_level:
            name = item.relative_path
            if item.kind == "FILE" and _is_root_context_name(name):
                if _safe_size(root / name, self._max_configuration_bytes):
                    sources.append(
                        PathObservation(name, "CONTEXT_SOURCE", ObservationClassification.FACT, "filesystem:lstat")
                    )
                else:
                    issues.append(
                        ReconnaissanceIssue(
                            "CONTEXT_SOURCE_TOO_LARGE",
                            ObservationClassification.UNKNOWN,
                            name,
                            "candidate context source exceeds size limit",
                        )
                    )
        docs = root / "docs"
        if docs.is_symlink():
            return (
                tuple(sorted(sources, key=lambda item: _sort_key(item.relative_path))),
                True,
                (_symlink_observation(root, "docs"),),
            )
        if not docs.exists() or not docs.is_dir():
            return tuple(sorted(sources, key=lambda item: _sort_key(item.relative_path))), True, ()
        complete = True
        observed_entries = 0
        pending: list[tuple[Path, int]] = [(docs, 1)]
        while pending:
            directory, depth = pending.pop(0)
            try:
                children = sorted(directory.iterdir(), key=lambda path: _sort_key(path.name))
            except OSError as error:
                issues.append(
                    ReconnaissanceIssue(
                        "CONTEXT_SCAN_FAILED",
                        ObservationClassification.UNKNOWN,
                        _relative(root, directory),
                        type(error).__name__,
                    )
                )
                complete = False
                continue
            for child in children:
                observed_entries += 1
                if observed_entries > self._max_context_entries:
                    issues.append(
                        ReconnaissanceIssue(
                            "CONTEXT_ENTRY_LIMIT_EXCEEDED",
                            ObservationClassification.UNKNOWN,
                            "docs",
                            f"context entry limit {self._max_context_entries} exceeded",
                        )
                    )
                    complete = False
                    pending.clear()
                    break
                relative = _relative(root, child)
                if child.is_symlink():
                    symlinks.append(_symlink_observation(root, relative))
                    continue
                if child.is_dir() and depth < self._max_context_depth and child.name not in _IGNORED_DIRECTORIES:
                    pending.append((child, depth + 1))
                    continue
                if not child.is_file() or child.suffix.casefold() != ".md" or _is_sensitive_name(relative):
                    continue
                if len(sources) >= self._max_context_sources:
                    issues.append(
                        ReconnaissanceIssue(
                            "CONTEXT_SOURCE_LIMIT_EXCEEDED",
                            ObservationClassification.UNKNOWN,
                            "docs",
                            f"context source limit {self._max_context_sources} exceeded",
                        )
                    )
                    complete = False
                    pending.clear()
                    break
                if not _safe_size(child, self._max_configuration_bytes):
                    issues.append(
                        ReconnaissanceIssue(
                            "CONTEXT_SOURCE_TOO_LARGE",
                            ObservationClassification.UNKNOWN,
                            relative,
                            "candidate context source exceeds size limit",
                        )
                    )
                    continue
                sources.append(
                    PathObservation(relative, "CONTEXT_SOURCE", ObservationClassification.FACT, "filesystem:lstat")
                )
        return (
            tuple(sorted(sources, key=lambda item: _sort_key(item.relative_path))),
            complete,
            tuple(sorted(symlinks, key=lambda item: _sort_key(item.relative_path))),
        )

    def _observe_agentic_os(
        self,
        root: Path,
        issues: list[ReconnaissanceIssue],
    ) -> AgenticOsStateObservation:
        config_status, config_version, config_fingerprint = self._config_status(root)
        agents_reference = self._agents_reference(root)
        gitignore_rules = self._gitignore_rules(root)
        agents_managed = self._agents_managed_section(root)
        gitignore_managed = self._managed_section(
            root,
            ".gitignore",
            GITIGNORE_SECTION_START,
            GITIGNORE_SECTION_END,
            GITIGNORE_MANAGED_SECTION,
        )
        runtime_files = tuple(
            self._runtime_file(root, filename)
            for filename in sorted(_RUNTIME_FORMATS, key=_sort_key)
        )
        runtime_bad = any(
            item.status
            in {
                DocumentStatus.INVALID,
                DocumentStatus.TOO_LARGE,
                DocumentStatus.UNSAFE,
            }
            for item in runtime_files
        )
        runtime_upgrade = any(
            item.status is DocumentStatus.UNKNOWN_VERSION for item in runtime_files
        )
        integration_complete = (
            agents_managed.status is ManagedSectionStatus.CURRENT
            and gitignore_managed.status is ManagedSectionStatus.CURRENT
            and agents_reference.value is True
            and _REQUIRED_GITIGNORE_RULES.issubset(gitignore_rules)
        )
        agentic_dir = root / CONFIG_DIRECTORY
        has_footprint = (
            agentic_dir.exists()
            or bool(gitignore_rules)
            or agents_reference.value is True
        )
        if (
            config_status is DocumentStatus.UNKNOWN_VERSION
            or runtime_upgrade
            or agents_managed.status is ManagedSectionStatus.UPGRADE_REQUIRED
        ):
            state = AgenticOsInitializationState.UPGRADE_REQUIRED
            detail = "an observed config, runtime, or AGENTS contract version is incompatible"
        elif (
            config_status is DocumentStatus.VALID
            and not runtime_bad
            and integration_complete
        ):
            state = AgenticOsInitializationState.INITIALIZED
            detail = "P5.2 config and minimum integration facts are present; runtime readiness is not implied"
        elif config_status is DocumentStatus.ABSENT and not has_footprint:
            state = AgenticOsInitializationState.UNINITIALIZED
            detail = "no Agentic OS footprint was observed"
        else:
            state = AgenticOsInitializationState.PARTIAL_OR_INCONSISTENT
            detail = "Agentic OS footprint is partial, invalid, unsafe, or contradictory"
        if config_status not in {DocumentStatus.ABSENT, DocumentStatus.VALID}:
            issues.append(
                ReconnaissanceIssue(
                    "PROJECT_CONFIGURATION_NOT_USABLE",
                    ObservationClassification.UNKNOWN,
                    f"{CONFIG_DIRECTORY}/{CONFIG_FILENAME}",
                    config_status.value,
                )
            )
        return AgenticOsStateObservation(
            state=state,
            classification=ObservationClassification.INFERENCE,
            config_status=config_status,
            config_version=config_version,
            agents_reference=agents_reference,
            gitignore_rules=gitignore_rules,
            agents_managed_section=agents_managed,
            gitignore_managed_section=gitignore_managed,
            config_semantic_fingerprint=config_fingerprint,
            runtime_files=runtime_files,
            detail=detail,
        )

    def _config_status(
        self, root: Path
    ) -> tuple[DocumentStatus, str | None, str | None]:
        path = root / CONFIG_DIRECTORY / CONFIG_FILENAME
        if path.is_symlink() or path.parent.is_symlink():
            return DocumentStatus.UNSAFE, None, None
        if not path.exists():
            return DocumentStatus.ABSENT, None, None
        if not path.is_file() or not _safe_size(path, self._max_configuration_bytes):
            return DocumentStatus.TOO_LARGE, None, None
        try:
            config = ProjectConfigurationLoader(root).load()
            canonical = ProjectConfigurationValidator().serialize(config)
            return (
                DocumentStatus.VALID,
                config.config_version,
                _sha256_text(canonical),
            )
        except ProjectConfigurationError as error:
            if error.code == "UNKNOWN_CONFIG_VERSION":
                return (
                    DocumentStatus.UNKNOWN_VERSION,
                    _json_schema_version(path, "config_version"),
                    None,
                )
            if error.code == "UNSAFE_PATH":
                return DocumentStatus.UNSAFE, None, None
            return (
                DocumentStatus.INVALID,
                _json_schema_version(path, "config_version"),
                None,
            )

    def _managed_section(
        self,
        root: Path,
        relative: str,
        start_marker: str,
        end_marker: str,
        canonical_section: str,
    ) -> ManagedSectionObservation:
        status, content = self._read_bounded_bytes(root, relative)
        if status is DocumentStatus.ABSENT:
            return ManagedSectionObservation(
                relative,
                ManagedSectionStatus.FILE_ABSENT,
                None,
                "filesystem:lstat",
                "target file is absent",
            )
        if status is DocumentStatus.UNSAFE:
            return ManagedSectionObservation(
                relative,
                ManagedSectionStatus.UNSAFE,
                None,
                "filesystem:lstat",
                "target file is a symlink or otherwise unsafe",
            )
        if status is not DocumentStatus.VALID or content is None:
            return ManagedSectionObservation(
                relative,
                ManagedSectionStatus.UNKNOWN,
                None,
                relative,
                "target file is not readable bounded UTF-8 text",
            )
        try:
            text = content.decode("utf-8")
        except UnicodeError:
            return ManagedSectionObservation(
                relative,
                ManagedSectionStatus.UNKNOWN,
                None,
                relative,
                "target file is not readable bounded UTF-8 text",
            )
        normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")
        section_status = _managed_section_status(
            normalized_text, start_marker, end_marker, canonical_section
        )
        return ManagedSectionObservation(
            relative,
            section_status,
            hashlib.sha256(content).hexdigest(),
            relative,
            "managed markers and canonical section were inspected without retaining user content",
        )

    def _agents_managed_section(self, root: Path) -> ManagedSectionObservation:
        status, content = self._read_bounded_bytes(root, "AGENTS.md")
        if status is DocumentStatus.ABSENT:
            inspection = self._agents_integration.inspect(None)
        elif status is DocumentStatus.UNSAFE:
            return ManagedSectionObservation(
                "AGENTS.md",
                ManagedSectionStatus.UNSAFE,
                None,
                "filesystem:lstat",
                "target file is a symlink or otherwise unsafe",
            )
        elif status is not DocumentStatus.VALID or content is None:
            return ManagedSectionObservation(
                "AGENTS.md",
                ManagedSectionStatus.UNKNOWN,
                None,
                "AGENTS.md",
                "target file is not readable bounded UTF-8 text",
            )
        else:
            inspection = self._agents_integration.inspect(content)
        return ManagedSectionObservation(
            "AGENTS.md",
            inspection.status,
            inspection.content_fingerprint,
            "AgentsIntegrationService",
            (
                "AGENTS.md managed contract was inspected exactly"
                if inspection.managed_version is None
                else f"AGENTS.md managed contract version {inspection.managed_version} was inspected"
            ),
        )

    def _agents_reference(self, root: Path) -> ObservedValue:
        path = root / "AGENTS.md"
        if path.is_symlink():
            return ObservedValue(
                ObservationClassification.UNKNOWN,
                None,
                "AGENTS.md",
                "AGENTS.md is a symlink and was not read",
            )
        if not path.exists():
            return ObservedValue(
                ObservationClassification.FACT,
                False,
                "filesystem:lstat",
                "AGENTS.md is absent",
            )
        if not path.is_file() or not _safe_size(path, self._max_configuration_bytes):
            return ObservedValue(
                ObservationClassification.UNKNOWN,
                None,
                "AGENTS.md",
                "AGENTS.md is not a bounded regular file",
            )
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return ObservedValue(
                ObservationClassification.UNKNOWN,
                None,
                "AGENTS.md",
                "AGENTS.md could not be read as UTF-8",
            )
        present = "agentic_engineering_os" in content.casefold() or "agentic-engineering-os" in content.casefold()
        return ObservedValue(
            ObservationClassification.FACT,
            present,
            "AGENTS.md",
            "literal Agentic OS reference presence; managed-section compliance is not inferred",
        )

    def _gitignore_rules(self, root: Path) -> tuple[str, ...]:
        status, text = self._read_bounded_file(root, ".gitignore")
        if status is not DocumentStatus.VALID or text is None:
            return ()
        lines = {
            line.strip().replace("\\", "/")
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        return tuple(sorted(lines & _KNOWN_GITIGNORE_RULES, key=_sort_key))

    def _runtime_file(self, root: Path, filename: str) -> RuntimeFileObservation:
        relative = f"{CONFIG_DIRECTORY}/{filename}"
        status, text = self._read_bounded_file(root, relative)
        if status is DocumentStatus.ABSENT:
            return RuntimeFileObservation(
                relative, status, None, ObservationClassification.FACT, "file is absent"
            )
        if status is not DocumentStatus.VALID or text is None:
            return RuntimeFileObservation(
                relative, status, None, ObservationClassification.UNKNOWN, "file was not read"
            )
        try:
            data = json.loads(
                text,
                object_pairs_hook=_strict_json_object,
                parse_constant=lambda value: (_raise_invalid_constant(value)),
            )
        except (_DuplicateJsonKeyError, json.JSONDecodeError, ValueError):
            return RuntimeFileObservation(
                relative,
                DocumentStatus.INVALID,
                None,
                ObservationClassification.UNKNOWN,
                "runtime document is not strict JSON",
            )
        version_field, expected = _RUNTIME_FORMATS[filename]
        version = data.get(version_field) if isinstance(data, Mapping) else None
        if not isinstance(version, str):
            return RuntimeFileObservation(
                relative,
                DocumentStatus.INVALID,
                None,
                ObservationClassification.UNKNOWN,
                f"runtime document has no string {version_field}",
            )
        compatible = version == expected
        return RuntimeFileObservation(
            relative,
            DocumentStatus.VERSION_OBSERVED if compatible else DocumentStatus.UNKNOWN_VERSION,
            version,
            ObservationClassification.FACT,
            "schema version is compatible" if compatible else "schema version is not supported by this product",
        )

    def _read_bounded_file(
        self, root: Path, relative: str
    ) -> tuple[DocumentStatus, str | None]:
        path = root / Path(relative)
        if _has_symlink_component(root, path):
            return DocumentStatus.UNSAFE, None
        if not path.exists():
            return DocumentStatus.ABSENT, None
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return DocumentStatus.UNSAFE, None
        if not resolved.is_file() or not _contains(root, resolved):
            return DocumentStatus.UNSAFE, None
        if not _safe_size(resolved, self._max_configuration_bytes):
            return DocumentStatus.TOO_LARGE, None
        try:
            return DocumentStatus.VALID, resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return DocumentStatus.INVALID, None

    def _read_bounded_bytes(
        self, root: Path, relative: str
    ) -> tuple[DocumentStatus, bytes | None]:
        path = root / Path(relative)
        if _has_symlink_component(root, path):
            return DocumentStatus.UNSAFE, None
        if not path.exists():
            return DocumentStatus.ABSENT, None
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return DocumentStatus.UNSAFE, None
        if not resolved.is_file() or not _contains(root, resolved):
            return DocumentStatus.UNSAFE, None
        if not _safe_size(resolved, self._max_configuration_bytes):
            return DocumentStatus.TOO_LARGE, None
        try:
            return DocumentStatus.VALID, resolved.read_bytes()
        except OSError:
            return DocumentStatus.INVALID, None


def _git_profile(state: GitReadOnlyState) -> GitRepositoryObservation:
    fact = ObservationClassification.FACT
    worktrees = tuple(
        GitWorktreeObservation(str(item.path), item.head_commit, item.branch_name)
        for item in sorted(state.worktrees, key=lambda item: _path_key(item.path))
    )
    return GitRepositoryObservation(
        is_repository=ObservedValue(fact, True, "git rev-parse --show-toplevel", "Git repository observed"),
        top_level=ObservedValue(fact, str(state.top_level), "git rev-parse --show-toplevel", "canonical Git top-level"),
        branch=ObservedValue(fact, state.branch_name, "git symbolic-ref --short HEAD", "branch is absent only for detached HEAD"),
        detached=ObservedValue(fact, state.detached, "git symbolic-ref --short HEAD", "detached state observed"),
        head_commit=ObservedValue(fact, state.head_commit, "git rev-parse HEAD", "full commit SHA"),
        clean=ObservedValue(fact, state.clean, "git status --porcelain=v1", "GIT_OPTIONAL_LOCKS=0"),
        worktrees=worktrees,
        errors=(),
    )


def _package_runner(
    top_level: tuple[PathObservation, ...]
) -> tuple[str | None, str]:
    files = {item.relative_path for item in top_level if item.kind == "FILE"}
    managers = []
    if "package-lock.json" in files:
        managers.append("npm")
    if "pnpm-lock.yaml" in files:
        managers.append("pnpm")
    if "yarn.lock" in files:
        managers.append("yarn")
    if len(managers) == 1:
        return managers[0], ""
    return (
        None,
        "AMBIGUOUS_PACKAGE_MANAGER" if managers else "PACKAGE_MANAGER_UNKNOWN",
    )


def _symlink_observation(root: Path, relative: str) -> SymlinkObservation:
    path = root / Path(relative)
    try:
        resolved = path.resolve(strict=True)
        scope = "INSIDE_REPOSITORY" if _contains(root, resolved) else "OUTSIDE_REPOSITORY"
        classification = ObservationClassification.FACT
    except OSError:
        scope = "UNKNOWN"
        classification = ObservationClassification.UNKNOWN
    return SymlinkObservation(relative, scope, classification, "filesystem:resolve")


def _is_root_context_name(name: str) -> bool:
    folded = name.casefold()
    return folded in {"agents.md", "readme.md", "contributing.md"} or (
        folded.startswith("readme.") and folded.endswith(".md")
    )


def _managed_section_status(
    text: str,
    start_marker: str,
    end_marker: str,
    canonical_section: str,
) -> ManagedSectionStatus:
    start_count = text.count(start_marker)
    end_count = text.count(end_marker)
    if start_count == 0 and end_count == 0:
        return ManagedSectionStatus.SECTION_ABSENT
    if start_count != 1 or end_count != 1:
        return ManagedSectionStatus.AMBIGUOUS
    start = text.index(start_marker)
    end = text.index(end_marker)
    end_after = end + len(end_marker)
    if (
        start >= end
        or (start > 0 and text[start - 1] != "\n")
        or (start + len(start_marker) < len(text) and text[start + len(start_marker)] != "\n")
        or (end > 0 and text[end - 1] != "\n")
        or (end_after < len(text) and text[end_after] != "\n")
    ):
        return ManagedSectionStatus.AMBIGUOUS
    observed = text[start:end_after]
    return (
        ManagedSectionStatus.CURRENT
        if observed == canonical_section.rstrip("\n")
        else ManagedSectionStatus.TAMPERED
    )


def _is_sensitive_name(relative: str) -> bool:
    parts = tuple(part.casefold() for part in relative.replace("\\", "/").split("/"))
    return any(
        part in _SENSITIVE_NAMES
        or part.startswith(".env.")
        or "secret" in part
        or "credential" in part
        or part.endswith(_SENSITIVE_SUFFIXES)
        for part in parts
    )


def _json_schema_version(path: Path, field: str) -> str | None:
    try:
        data = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=lambda value: (_raise_invalid_constant(value)),
        )
    except (OSError, UnicodeError, _DuplicateJsonKeyError, json.JSONDecodeError, ValueError):
        return None
    value = data.get(field) if isinstance(data, Mapping) else None
    return value if isinstance(value, str) else None


def _safe_size(path: Path, maximum: int) -> bool:
    try:
        return path.is_file() and path.stat().st_size <= maximum
    except OSError:
        return False


def _has_symlink_component(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return True
    return False


def _contains(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _relative_name(name: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise RepositoryReconnaissanceError(
            "INVALID_FILESYSTEM_ENTRY", "top-level entry name is unsafe"
        )
    return name


def _sort_key(value: str) -> tuple[str, str]:
    normalized = unicodedata.normalize("NFC", value)
    return normalized.casefold(), normalized


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _raise_invalid_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
