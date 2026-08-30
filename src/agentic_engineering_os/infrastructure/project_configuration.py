"""Strict loading and validation of repository-local project configuration."""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from agentic_engineering_os.domain import (
    CodexApprovalConstraint,
    CodexProjectConstraints,
    CodexSandboxConstraint,
    MissionStateGitPolicy,
    ProjectConfiguration,
    ProjectPathPolicy,
    RepositoryRootPolicy,
    ToolchainDeclaration,
    VerificationCommand,
    VerificationKind,
    WorkingDirectoryPolicy,
    to_dict,
)
from agentic_engineering_os.resources.project_configuration import (
    ProductResourceError,
    project_configuration_schema_text,
)


CONFIG_DIRECTORY = ".agentic-engineering-os"
CONFIG_FILENAME = "config.json"
CONFIG_VERSION = "1.0"

_IDENTITY = re.compile(r"^[^\s/\\:]+$")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[/\\]")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|password|passwd|secret|credential)"
    r"\s*[:=]\s*\S+"
)
_SECRET_TOKEN = re.compile(
    r"(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-\S+|"
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----)"
)
_URI_CREDENTIAL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/@\s:]+:[^/@\s]+@")
_SHELL_EXECUTABLES = frozenset(
    {
        "bash",
        "cmd",
        "cmd.exe",
        "command.com",
        "dash",
        "fish",
        "ksh",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "sh",
        "zsh",
    }
)
_SHELL_CONTROL_ARGUMENTS = frozenset({"&&", "||", ";", "|", ">", ">>", "<"})
_RESERVED_WRITE_ROOTS = frozenset({".git", ".agentic-engineering-os"})
_SECRET_PATH_COMPONENTS = frozenset({".env", "secrets", "credentials"})


class ProjectConfigurationError(RuntimeError):
    """Configuration could not be read or proven valid."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class _DuplicateJsonKeyError(ValueError):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    candidate: dict[str, Any] = {}
    for key, value in pairs:
        if key in candidate:
            raise _DuplicateJsonKeyError(key)
        candidate[key] = value
    return candidate


class ProjectConfigurationValidator:
    """Validate the closed P5.2 schema and deterministic local semantics."""

    def __init__(self) -> None:
        try:
            schema = json.loads(
                project_configuration_schema_text(),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
            Draft202012Validator.check_schema(schema)
            self._validator = Draft202012Validator(schema)
        except (ProductResourceError, json.JSONDecodeError, SchemaError) as error:
            raise ProjectConfigurationError(
                "VALIDATION_UNAVAILABLE",
                "installed project configuration schema is unavailable or invalid",
            ) from error
        except _DuplicateJsonKeyError as error:
            raise ProjectConfigurationError(
                "VALIDATION_UNAVAILABLE",
                f"installed schema contains duplicate JSON key: {error.key}",
            ) from error

    def parse(self, text: str) -> ProjectConfiguration:
        """Parse strict JSON text and return its canonical immutable model."""

        if not isinstance(text, str):
            raise ProjectConfigurationError("INVALID_JSON", "configuration must be UTF-8 text")
        try:
            candidate = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
        except _DuplicateJsonKeyError as error:
            raise ProjectConfigurationError(
                "DUPLICATE_JSON_KEY", f"duplicate JSON key: {error.key}"
            ) from error
        except json.JSONDecodeError as error:
            raise ProjectConfigurationError(
                "INVALID_JSON",
                f"configuration is not valid JSON at line {error.lineno}, column {error.colno}",
            ) from error
        return self.validate(candidate)

    def validate(self, candidate: object) -> ProjectConfiguration:
        """Validate already parsed JSON-compatible data without adding defaults."""

        if isinstance(candidate, Mapping):
            version = candidate.get("config_version")
            if version is not None and version != CONFIG_VERSION:
                raise ProjectConfigurationError(
                    "UNKNOWN_CONFIG_VERSION",
                    f"unsupported config_version: {version!r}",
                )
        errors = sorted(
            self._validator.iter_errors(candidate),
            key=lambda item: (
                tuple(f"{type(part).__name__}:{part}" for part in item.absolute_path),
                item.message,
            ),
        )
        if errors:
            details = "; ".join(
                f"{'.'.join(map(str, error.absolute_path)) or '<root>'}: {error.message}"
                for error in errors
            )
            raise ProjectConfigurationError(
                "INVALID_SCHEMA", f"configuration violates its schema: {details}"
            )
        data = cast(Mapping[str, object], candidate)
        _validate_semantics(data)
        try:
            return _hydrate(data)
        except (KeyError, TypeError, ValueError) as error:
            raise ProjectConfigurationError(
                "INVALID_DOMAIN_DATA",
                f"configuration cannot be hydrated: {type(error).__name__}: {error}",
            ) from error

    def serialize(self, configuration: ProjectConfiguration) -> str:
        """Return canonical UTF-8-compatible JSON after complete validation."""

        if not isinstance(configuration, ProjectConfiguration):
            raise ProjectConfigurationError(
                "INVALID_DOMAIN_DATA", "canonical ProjectConfiguration is required"
            )
        try:
            candidate = cast(dict[str, object], to_dict(configuration))
        except (TypeError, ValueError) as error:
            raise ProjectConfigurationError(
                "INVALID_DOMAIN_DATA", "configuration cannot be serialized"
            ) from error
        self.validate(candidate)
        return json.dumps(
            candidate,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
        ) + "\n"


class ProjectConfigurationLoader:
    """Read the canonical config path; never create, repair, or migrate it."""

    def __init__(
        self,
        repository_root: Path | str,
        *,
        validator: ProjectConfigurationValidator | None = None,
    ) -> None:
        root = Path(repository_root)
        try:
            self._root = root.resolve(strict=True)
        except OSError as error:
            raise ProjectConfigurationError(
                "INVALID_REPOSITORY_ROOT", f"repository root cannot be resolved: {root}"
            ) from error
        if not self._root.is_dir():
            raise ProjectConfigurationError(
                "INVALID_REPOSITORY_ROOT", f"repository root is not a directory: {self._root}"
            )
        self._directory = self._root / CONFIG_DIRECTORY
        self._path = self._directory / CONFIG_FILENAME
        self._validator = validator or ProjectConfigurationValidator()

    @property
    def config_path(self) -> Path:
        return self._path

    def load(self) -> ProjectConfiguration:
        self._assert_safe_path()
        if not self._path.exists():
            raise ProjectConfigurationError(
                "CONFIG_ABSENT", f"project configuration is absent: {self._path}"
            )
        if not self._path.is_file():
            raise ProjectConfigurationError(
                "READ_FAILED", f"configuration path is not a file: {self._path}"
            )
        try:
            text = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ProjectConfigurationError(
                "READ_FAILED", f"configuration cannot be read: {self._path}"
            ) from error
        return self._validator.parse(text)

    def _assert_safe_path(self) -> None:
        if self._directory.is_symlink():
            raise ProjectConfigurationError(
                "UNSAFE_PATH", f"configuration directory cannot be a symlink: {self._directory}"
            )
        if self._path.is_symlink():
            raise ProjectConfigurationError(
                "UNSAFE_PATH", f"configuration file cannot be a symlink: {self._path}"
            )
        try:
            parent = self._directory.parent.resolve(strict=True)
        except OSError as error:
            raise ProjectConfigurationError(
                "UNSAFE_PATH", "configuration parent cannot be resolved"
            ) from error
        if _path_key(parent) != _path_key(self._root):
            raise ProjectConfigurationError(
                "UNSAFE_PATH", "configuration path escapes repository root"
            )


def _validate_semantics(data: Mapping[str, object]) -> None:
    _reject_secret_values(data)
    _validate_identity(cast(str, data["project_id"]), "project_id")

    toolchains = cast(Sequence[Mapping[str, object]], data["toolchains"])
    toolchain_keys: list[str] = []
    for index, item in enumerate(toolchains):
        identity = cast(str, item["identity"])
        _validate_identity(identity, f"toolchains.{index}.identity")
        version = item["version_constraint"]
        if isinstance(version, str):
            if version != version.strip():
                _invalid("INVALID_TOOLCHAIN", "version_constraint must be trimmed")
            if (
                "/" in version
                or "\\" in version
                or _WINDOWS_ABSOLUTE.search(version)
                or "file:" in version.casefold()
            ):
                _invalid(
                    "INVALID_TOOLCHAIN",
                    "version_constraint cannot contain a machine-specific path",
                )
        toolchain_keys.append(_identity_key(identity))
    _require_unique_and_sorted(toolchain_keys, "toolchain identities")

    commands = cast(Sequence[Mapping[str, object]], data["verification_commands"])
    command_keys: list[str] = []
    for index, item in enumerate(commands):
        command_id = cast(str, item["command_id"])
        _validate_identity(command_id, f"verification_commands.{index}.command_id")
        command_keys.append(_identity_key(command_id))
        _validate_executable(cast(str, item["executable"]))
        for argument in cast(Sequence[str], item["args"]):
            _validate_argument(argument)
        cwd = cast(str, item["cwd"])
        _normalize_relative_path(
            cwd,
            allow_root=True,
            field=f"verification_commands.{index}.cwd",
        )
        _reject_reserved_or_secret_write_path(cwd, "verification command cwd")
    _require_unique_and_sorted(command_keys, "verification command ids")

    policy = cast(Mapping[str, object], data["path_policy"])
    normalized: dict[str, tuple[str, ...]] = {}
    for field in ("allowed_paths", "protected_paths", "forbidden_paths"):
        values = cast(Sequence[str], policy[field])
        keys = tuple(
            _normalize_relative_path(value, allow_root=False, field=f"path_policy.{field}")
            for value in values
        )
        _require_unique_and_sorted(list(keys), f"{field} normalized paths")
        normalized[field] = keys
        if field != "forbidden_paths":
            for value in values:
                _reject_reserved_or_secret_write_path(value, field)
    _validate_path_policy(normalized)

    context_keys: list[str] = []
    for source in cast(Sequence[str], data["context_sources"]):
        key = _normalize_relative_path(source, allow_root=False, field="context_sources")
        _reject_reserved_or_secret_write_path(source, "context_sources")
        if PurePosixPath(source).suffix.casefold() != ".md":
            _invalid("INVALID_CONTEXT_SOURCE", "context sources must be Markdown files")
        context_keys.append(key)
    _require_unique_and_sorted(context_keys, "context source paths")


def _validate_identity(value: str, field: str) -> None:
    if value != value.strip() or unicodedata.normalize("NFC", value) != value:
        _invalid("INVALID_IDENTITY", f"{field} must be trimmed NFC text")
    if not _IDENTITY.fullmatch(value) or any(unicodedata.category(char) == "Cc" for char in value):
        _invalid("INVALID_IDENTITY", f"{field} contains unsafe characters")


def _validate_executable(value: str) -> None:
    if (
        value != value.strip()
        or not value
        or any(char.isspace() for char in value)
        or any(char in value for char in ("/", "\\", ":", "\0"))
        or value.casefold() in _SHELL_EXECUTABLES
    ):
        _invalid(
            "INVALID_COMMAND",
            "command executable must be a bare non-shell program name",
        )


def _validate_argument(value: str) -> None:
    if "\0" in value or "\r" in value or "\n" in value:
        _invalid("INVALID_COMMAND", "command arguments cannot contain control separators")
    if value in _SHELL_CONTROL_ARGUMENTS:
        _invalid("INVALID_COMMAND", "shell control arguments are not supported")
    path_candidate = value.split("=", 1)[1] if "=" in value else value
    if _WINDOWS_ABSOLUTE.match(path_candidate) or path_candidate.startswith(("/", "file:/")):
        _invalid("ABSOLUTE_PATH", "command arguments cannot embed absolute paths")
    if ("/" in value or "\\" in value) and ".." in value.replace("\\", "/").split("/"):
        _invalid("PATH_TRAVERSAL", "command arguments cannot traverse outside the repository")


def _normalize_relative_path(value: str, *, allow_root: bool, field: str) -> str:
    if value != value.strip() or unicodedata.normalize("NFC", value) != value:
        _invalid("INVALID_PATH", f"{field} path must be trimmed NFC text")
    if value == ".":
        if allow_root:
            return "."
        _invalid("INVALID_PATH", f"{field} cannot name the repository root")
    if (
        not value
        or "\0" in value
        or "\\" in value
        or value.startswith("/")
        or _WINDOWS_ABSOLUTE.match(value)
    ):
        _invalid("ABSOLUTE_PATH", f"{field} must be a portable repository-relative path")
    parts = value.split("/")
    if any(part == ".." for part in parts):
        _invalid("PATH_TRAVERSAL", f"{field} cannot traverse outside the repository")
    if any(part in {"", "."} for part in parts) or str(PurePosixPath(value)) != value:
        _invalid("INVALID_PATH", f"{field} must use canonical POSIX path syntax")
    return "/".join(unicodedata.normalize("NFC", part).casefold() for part in parts)


def _reject_reserved_or_secret_write_path(value: str, field: str) -> None:
    parts = tuple(part.casefold() for part in value.split("/"))
    if parts[0] in _RESERVED_WRITE_ROOTS or any(
        part in _SECRET_PATH_COMPONENTS
        or part.startswith(".env.")
        or "secret" in part
        or "credential" in part
        or part.endswith((".pem", ".key", ".p12", ".pfx"))
        for part in parts
    ):
        _invalid(
            "RESERVED_PATH",
            f"{field} cannot grant access to runtime, Git, or secret-like areas",
        )


def _validate_path_policy(paths: Mapping[str, tuple[str, ...]]) -> None:
    allowed = paths["allowed_paths"]
    protected = paths["protected_paths"]
    forbidden = paths["forbidden_paths"]
    if set(allowed) & set(protected) or set(allowed) & set(forbidden) or set(protected) & set(forbidden):
        _invalid("CONFLICTING_PATHS", "the same normalized path cannot have multiple policies")
    for candidate in (*allowed, *protected):
        if any(_is_same_or_ancestor(blocked, candidate) for blocked in forbidden):
            _invalid(
                "CONFLICTING_PATHS",
                "a forbidden path cannot contain an allowed or protected path",
            )


def _is_same_or_ancestor(left: str, right: str) -> bool:
    return left == "." or left == right or right.startswith(f"{left}/")


def _require_unique_and_sorted(keys: list[str], label: str) -> None:
    if len(keys) != len(set(keys)):
        _invalid("DUPLICATE_NORMALIZED_VALUE", f"duplicate {label}")
    if keys != sorted(keys):
        _invalid("NON_CANONICAL_ORDER", f"{label} must use canonical lexical order")


def _reject_secret_values(value: object, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, str):
        if (
            _SECRET_ASSIGNMENT.search(value)
            or _SECRET_TOKEN.search(value)
            or _URI_CREDENTIAL.search(value)
        ):
            location = ".".join(path) or "<root>"
            _invalid("SECRET_VALUE", f"secret-like value is forbidden at {location}")
        return
    if isinstance(value, Mapping):
        for key in sorted(value):
            _reject_secret_values(value[key], (*path, str(key)))
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        for index, item in enumerate(value):
            _reject_secret_values(item, (*path, str(index)))


def _hydrate(data: Mapping[str, object]) -> ProjectConfiguration:
    toolchains = cast(Sequence[Mapping[str, object]], data["toolchains"])
    commands = cast(Sequence[Mapping[str, object]], data["verification_commands"])
    path_policy = cast(Mapping[str, object], data["path_policy"])
    codex = cast(Mapping[str, object], data["codex_constraints"])
    return ProjectConfiguration(
        config_version=cast(str, data["config_version"]),
        project_id=cast(str, data["project_id"]),
        repository_root_policy=RepositoryRootPolicy(
            cast(str, data["repository_root_policy"])
        ),
        toolchains=tuple(
            ToolchainDeclaration(
                identity=cast(str, item["identity"]),
                version_constraint=cast(str | None, item["version_constraint"]),
            )
            for item in toolchains
        ),
        verification_commands=tuple(
            VerificationCommand(
                command_id=cast(str, item["command_id"]),
                kind=VerificationKind(cast(str, item["kind"])),
                executable=cast(str, item["executable"]),
                args=tuple(cast(Sequence[str], item["args"])),
                cwd=cast(str, item["cwd"]),
                cwd_policy=WorkingDirectoryPolicy(cast(str, item["cwd_policy"])),
                required=cast(bool, item["required"]),
            )
            for item in commands
        ),
        path_policy=ProjectPathPolicy(
            allowed_paths=tuple(cast(Sequence[str], path_policy["allowed_paths"])),
            protected_paths=tuple(cast(Sequence[str], path_policy["protected_paths"])),
            forbidden_paths=tuple(cast(Sequence[str], path_policy["forbidden_paths"])),
        ),
        context_sources=tuple(cast(Sequence[str], data["context_sources"])),
        codex_constraints=CodexProjectConstraints(
            maximum_sandbox=CodexSandboxConstraint(cast(str, codex["maximum_sandbox"])),
            approval_policy=CodexApprovalConstraint(cast(str, codex["approval_policy"])),
            require_clean_git=cast(bool, codex["require_clean_git"]),
            maximum_parallel_executions=cast(int, codex["maximum_parallel_executions"]),
        ),
        mission_state_git_policy=MissionStateGitPolicy(
            cast(str, data["mission_state_git_policy"])
        ),
    )


def _identity_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _path_key(value: Path) -> str:
    return os.path.normcase(str(value.resolve(strict=False)))


def _invalid(code: str, message: str) -> NoReturn:
    raise ProjectConfigurationError(code, message)
