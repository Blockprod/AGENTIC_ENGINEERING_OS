"""Windows-first platform and machine discovery without granting authority."""

from __future__ import annotations

import ntpath
import os
import platform as stdlib_platform
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path, PureWindowsPath

from agentic_engineering_os.domain.platform import (
    CapabilityState,
    CaseSemantics,
    CaseSensitivityObservation,
    ExecutableDiscoveryMethod,
    ExecutableFact,
    FilesystemScope,
    MachineFacts,
    PathSemantics,
    PlatformCapabilities,
    PlatformCertification,
    PlatformFacts,
    PlatformFamily,
    ProcessTerminationSemantics,
    ProjectPlatformBinding,
)


RUNTIME_ENVIRONMENT_ALLOWLIST = (
    "APPDATA",
    "CODEX_HOME",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "USERPROFILE",
    "WINDIR",
)


class PlatformDiscoveryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class PlatformEnvironmentProbe:
    """Observe bounded facts for one project binding; never persist them."""

    def __init__(
        self,
        *,
        platform_name: str | None = None,
        environment: Mapping[str, str] | None = None,
        executable_locator: Callable[[str, str | None], str | None] | None = None,
        reparse_detector: Callable[[Path], bool | None] | None = None,
    ) -> None:
        self._platform_name = sys.platform if platform_name is None else platform_name
        self._environment = dict(os.environ if environment is None else environment)
        self._locator = executable_locator or (
            lambda executable, path: shutil.which(executable, path=path)
        )
        self._reparse_detector = reparse_detector or _is_reparse_point

    def inspect(
        self,
        repository_root: Path | str,
        *,
        git_executable: str = "git",
        codex_executable: str | None = None,
    ) -> PlatformCapabilities:
        requested = Path(repository_root)
        if not requested.is_absolute():
            raise PlatformDiscoveryError(
                "INVALID_PROJECT_BINDING", "repository root must be absolute"
            )
        try:
            root = requested.resolve(strict=True)
        except OSError as error:
            raise PlatformDiscoveryError(
                "INVALID_PROJECT_BINDING", "repository root cannot be resolved"
            ) from error
        if not root.is_dir():
            raise PlatformDiscoveryError(
                "INVALID_PROJECT_BINDING", "repository root must be a directory"
            )

        family = _platform_family(self._platform_name)
        reparse = self._reparse_detector(root)
        filesystem_scope = _filesystem_scope(root, family)
        platform_facts = _platform_facts(family, self._environment)
        temporary_root, temporary_state = _temporary_root(self._environment)
        path_value = environment_value(self._environment, "PATH")
        git = discover_executable(
            git_executable,
            self._environment,
            locator=self._locator,
            identity="git",
        )
        git = _with_version(git, root, self._environment)
        codex = (
            discover_executable(
                codex_executable,
                self._environment,
                locator=self._locator,
                identity="codex",
            )
            if codex_executable is not None
            else ExecutableFact(
                "codex",
                CapabilityState.UNKNOWN,
                None,
                None,
                None,
                ExecutableDiscoveryMethod.NOT_REQUESTED,
            )
        )
        python_path = Path(sys.executable).resolve(strict=True)
        python = ExecutableFact(
            "python",
            CapabilityState.SUPPORTED,
            str(python_path),
            stdlib_platform.python_version(),
            None,
            ExecutableDiscoveryMethod.CURRENT_PROCESS,
        )
        powershell = CapabilityState.SUPPORTED if (
            self._locator("pwsh", path_value)
            or self._locator("powershell", path_value)
        ) else CapabilityState.UNSUPPORTED
        machine = MachineFacts(
            temporary_root=str(temporary_root) if temporary_root is not None else None,
            temporary_root_writable=temporary_state,
            symlink_semantics=CapabilityState.UNKNOWN,
            junction_semantics=CapabilityState.UNKNOWN,
            case_sensitivity=CaseSensitivityObservation.UNKNOWN,
            powershell=powershell,
            git=git,
            codex=codex,
            python=python,
        )
        project = ProjectPlatformBinding(
            repository_root=str(root),
            filesystem_scope=filesystem_scope,
            case_semantics=(
                CaseSemantics.WINDOWS_CASEFOLD_POLICY
                if family is PlatformFamily.WINDOWS
                else CaseSemantics.UNKNOWN
            ),
            reparse_point=(
                CapabilityState.UNKNOWN
                if reparse is None
                else CapabilityState.SUPPORTED
                if reparse
                else CapabilityState.UNSUPPORTED
            ),
        )
        return PlatformCapabilities(platform_facts, machine, project)


def discover_executable(
    requested: str,
    environment: Mapping[str, str],
    *,
    locator: Callable[[str, str | None], str | None] | None = None,
    identity: str | None = None,
) -> ExecutableFact:
    """Resolve one executable by explicit path or bounded PATH lookup."""

    name = identity or requested
    if not isinstance(requested, str) or not requested.strip() or "\0" in requested:
        return ExecutableFact(
            name or "executable",
            CapabilityState.UNSUPPORTED,
            None,
            None,
            None,
            ExecutableDiscoveryMethod.UNAVAILABLE,
        )
    candidate = Path(requested)
    if candidate.is_absolute() or candidate.parent != Path("."):
        method = ExecutableDiscoveryMethod.EXPLICIT_PATH
    else:
        method = ExecutableDiscoveryMethod.PATH_LOOKUP
        finder = locator or (lambda value, path: shutil.which(value, path=path))
        located = finder(requested, environment_value(environment, "PATH"))
        if located is None:
            return ExecutableFact(
                name,
                CapabilityState.UNSUPPORTED,
                None,
                None,
                None,
                ExecutableDiscoveryMethod.UNAVAILABLE,
            )
        candidate = Path(located)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return ExecutableFact(
            name,
            CapabilityState.UNSUPPORTED,
            None,
            None,
            None,
            ExecutableDiscoveryMethod.UNAVAILABLE,
        )
    if not resolved.is_file():
        return ExecutableFact(
            name,
            CapabilityState.UNSUPPORTED,
            None,
            None,
            None,
            ExecutableDiscoveryMethod.UNAVAILABLE,
        )
    return ExecutableFact(
        name,
        CapabilityState.SUPPORTED,
        str(resolved),
        None,
        None,
        method,
    )


def build_bounded_environment(
    parent: Mapping[str, str], allowlist: tuple[str, ...]
) -> dict[str, str]:
    environment: dict[str, str] = {}
    for name in allowlist:
        value = environment_value(parent, name)
        if value is not None:
            environment[name] = value
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["NO_COLOR"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def environment_value(environment: Mapping[str, str], name: str) -> str | None:
    requested = name.casefold()
    for key, value in environment.items():
        if key.casefold() == requested:
            return value
    return None


def windows_contract_path_key(value: str) -> str:
    """Canonical comparison key for repository-relative Windows contract paths."""

    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("path must be non-empty text without NUL")
    normalized = unicodedata.normalize("NFC", value.replace("/", "\\"))
    return ntpath.normcase(normalized).casefold()


def _platform_family(value: str) -> PlatformFamily:
    normalized = value.casefold()
    if normalized.startswith("win"):
        return PlatformFamily.WINDOWS
    if normalized.startswith("linux"):
        return PlatformFamily.LINUX
    if normalized.startswith("darwin"):
        return PlatformFamily.MACOS
    return PlatformFamily.UNKNOWN


def _platform_facts(
    family: PlatformFamily, environment: Mapping[str, str]
) -> PlatformFacts:
    if family is PlatformFamily.WINDOWS:
        suffix_text = environment_value(environment, "PATHEXT") or ""
        suffixes = tuple(
            dict.fromkeys(
                item.strip().casefold()
                for item in suffix_text.split(";")
                if item.strip()
            )
        )
        return PlatformFacts(
            family,
            PlatformCertification.WINDOWS_V1_TARGET,
            PathSemantics.WINDOWS_LOCAL,
            suffixes,
            ProcessTerminationSemantics.DIRECT_CHILD_FORCE_KILL,
            False,
        )
    return PlatformFacts(
        family,
        PlatformCertification.NOT_CERTIFIED,
        (
            PathSemantics.POSIX_UNCERTIFIED
            if family in {PlatformFamily.LINUX, PlatformFamily.MACOS}
            else PathSemantics.UNKNOWN
        ),
        (),
        (
            ProcessTerminationSemantics.POSIX_UNCERTIFIED
            if family in {PlatformFamily.LINUX, PlatformFamily.MACOS}
            else ProcessTerminationSemantics.UNKNOWN
        ),
        False,
    )


def _filesystem_scope(root: Path, family: PlatformFamily) -> FilesystemScope:
    if family is not PlatformFamily.WINDOWS:
        return FilesystemScope.UNKNOWN
    windows = PureWindowsPath(str(root))
    if str(windows).startswith("\\\\"):
        return FilesystemScope.NETWORK_OR_UNC
    return FilesystemScope.LOCAL


def _temporary_root(
    environment: Mapping[str, str],
) -> tuple[Path | None, CapabilityState]:
    candidate_text = environment_value(environment, "TEMP") or environment_value(
        environment, "TMP"
    )
    if not candidate_text:
        return None, CapabilityState.UNKNOWN
    try:
        candidate = Path(candidate_text).resolve(strict=True)
        if not candidate.is_dir() or _is_reparse_point(candidate) is not False:
            return candidate, CapabilityState.UNKNOWN
        with tempfile.TemporaryFile(dir=candidate):
            pass
    except OSError:
        return None, CapabilityState.UNKNOWN
    return candidate, CapabilityState.SUPPORTED


def _with_version(
    fact: ExecutableFact,
    cwd: Path,
    environment: Mapping[str, str],
) -> ExecutableFact:
    if fact.state is not CapabilityState.SUPPORTED or fact.path is None:
        return fact
    try:
        result = subprocess.run(
            [fact.path, "--version"],
            shell=False,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=build_bounded_environment(environment, RUNTIME_ENVIRONMENT_ALLOWLIST),
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ExecutableFact(
            fact.identity,
            CapabilityState.UNKNOWN,
            fact.path,
            None,
            None,
            fact.discovery_method,
        )
    version = result.stdout.strip() if result.returncode == 0 else None
    return ExecutableFact(
        fact.identity,
        CapabilityState.SUPPORTED if version else CapabilityState.UNKNOWN,
        fact.path,
        version,
        None,
        fact.discovery_method,
    )


def _is_reparse_point(path: Path) -> bool | None:
    try:
        if path.is_symlink():
            return True
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", None)
    except OSError:
        return None
    if attributes is None:
        return False
    reparse_flag = getattr(os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)
