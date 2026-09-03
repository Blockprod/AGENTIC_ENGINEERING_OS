from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agentic_engineering_os.domain import (
    CapabilityState,
    CaseSemantics,
    CaseSensitivityObservation,
    ExecutableDiscoveryMethod,
    FilesystemScope,
    PathSemantics,
    PlatformCertification,
    PlatformFamily,
    ProcessTerminationSemantics,
)
from agentic_engineering_os.infrastructure import (
    GitAdapter,
    GitOperationError,
    PlatformDiscoveryError,
    PlatformEnvironmentProbe,
    build_bounded_environment,
    discover_executable,
    windows_contract_path_key,
)


def _git_path() -> str:
    value = shutil.which("git")
    assert value is not None
    return str(Path(value).resolve())


def _environment(**changes: str) -> dict[str, str]:
    value = dict(os.environ)
    value.update(changes)
    return value


def _git(root: Path, executable: str, *arguments: str) -> str:
    result = subprocess.run(
        [executable, "-C", str(root), *arguments],
        shell=False,
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_windows_capabilities_are_deterministic_and_separate_facts(tmp_path: Path) -> None:
    root = tmp_path / "arbitrary root ünicode with spaces"
    root.mkdir()
    probe = PlatformEnvironmentProbe()

    first = probe.inspect(root, git_executable=_git_path(), codex_executable=sys.executable)
    second = probe.inspect(root, git_executable=_git_path(), codex_executable=sys.executable)

    assert first == second
    assert first.platform.family is PlatformFamily.WINDOWS
    assert first.platform.certification is PlatformCertification.WINDOWS_V1_TARGET
    assert first.platform.path_semantics is PathSemantics.WINDOWS_LOCAL
    assert (
        first.platform.process_termination
        is ProcessTerminationSemantics.WINDOWS_PROCESS_TREE_FORCE_KILL
    )
    assert first.platform.core_shell_required is False
    assert first.project.repository_root == str(root.resolve())
    assert first.project.filesystem_scope is FilesystemScope.LOCAL
    assert first.project.case_semantics is CaseSemantics.WINDOWS_CASEFOLD_POLICY
    assert first.machine.case_sensitivity is CaseSensitivityObservation.UNKNOWN
    assert first.machine.git.version is not None
    assert first.machine.git.version.startswith("git version ")
    assert first.machine.codex.discovery_method is ExecutableDiscoveryMethod.EXPLICIT_PATH
    assert first.machine.codex.version is None
    assert first.machine.codex.sha256 is None
    assert first.machine.python.discovery_method is ExecutableDiscoveryMethod.CURRENT_PROCESS
    first.require_windows_v1_local_safety(require_codex=True)
    with pytest.raises(FrozenInstanceError):
        first.platform.family = PlatformFamily.UNKNOWN


@pytest.mark.parametrize("platform_name", ["linux", "darwin", "other"])
def test_unsupported_platform_simulation_never_claims_certification(
    tmp_path: Path, platform_name: str
) -> None:
    capabilities = PlatformEnvironmentProbe(platform_name=platform_name).inspect(
        tmp_path,
        git_executable=_git_path(),
    )

    assert capabilities.platform.certification is PlatformCertification.NOT_CERTIFIED
    assert capabilities.project.filesystem_scope is FilesystemScope.UNKNOWN
    with pytest.raises(ValueError, match="UNSUPPORTED_PLATFORM"):
        capabilities.require_windows_v1_local_safety()


def test_unknown_reparse_semantics_fail_closed(tmp_path: Path) -> None:
    capabilities = PlatformEnvironmentProbe(
        reparse_detector=lambda path: None
    ).inspect(tmp_path, git_executable=_git_path())

    assert capabilities.project.reparse_point is CapabilityState.UNKNOWN
    with pytest.raises(ValueError, match="UNKNOWN_REPARSE_SEMANTICS"):
        capabilities.require_windows_v1_local_safety()


def test_observed_reparse_root_fails_closed(tmp_path: Path) -> None:
    capabilities = PlatformEnvironmentProbe(
        reparse_detector=lambda path: True
    ).inspect(tmp_path, git_executable=_git_path())

    assert capabilities.project.reparse_point is CapabilityState.SUPPORTED
    with pytest.raises(ValueError, match="UNKNOWN_REPARSE_SEMANTICS"):
        capabilities.require_windows_v1_local_safety()


def test_unavailable_temp_and_powershell_do_not_trigger_fallback(
    tmp_path: Path,
) -> None:
    unavailable = tmp_path / "missing-temp"
    environment = {"PATH": "", "PATHEXT": ".EXE", "TEMP": str(unavailable)}
    capabilities = PlatformEnvironmentProbe(
        environment=environment,
        executable_locator=lambda executable, path: None,
    ).inspect(tmp_path, git_executable=_git_path())

    assert capabilities.machine.temporary_root is None
    assert capabilities.machine.temporary_root_writable is CapabilityState.UNKNOWN
    assert capabilities.machine.powershell is CapabilityState.UNSUPPORTED
    with pytest.raises(ValueError, match="TEMPORARY_ROOT_UNAVAILABLE"):
        capabilities.require_windows_v1_local_safety()


def test_powershell_is_not_required_by_the_core_runtime(tmp_path: Path) -> None:
    capabilities = PlatformEnvironmentProbe(
        environment={"PATH": "", "PATHEXT": ".EXE", "TEMP": str(tmp_path)},
        executable_locator=lambda executable, path: None,
    ).inspect(tmp_path, git_executable=_git_path())

    assert capabilities.machine.powershell is CapabilityState.UNSUPPORTED
    capabilities.require_windows_v1_local_safety()


def test_environment_is_allowlisted_without_secret_leakage() -> None:
    environment = build_bounded_environment(
        {
            "Path": "C:\\tools",
            "TEMP": "C:\\temp",
            "API_TOKEN": "must-not-leak",
            "PASSWORD": "must-not-leak",
        },
        ("PATH", "TEMP"),
    )

    assert environment["PATH"] == "C:\\tools"
    assert environment["TEMP"] == "C:\\temp"
    assert "API_TOKEN" not in environment
    assert "PASSWORD" not in environment
    assert environment["GIT_TERMINAL_PROMPT"] == "0"


def test_windows_contract_path_key_handles_case_separator_and_nfc() -> None:
    composed = "Scope/Caf\N{LATIN SMALL LETTER E WITH ACUTE}.py"
    decomposed = "scope\\Cafe\N{COMBINING ACUTE ACCENT}.PY"

    assert windows_contract_path_key(composed) == windows_contract_path_key(decomposed)
    with pytest.raises(ValueError):
        windows_contract_path_key("bad\0path")


def test_discovery_distinguishes_explicit_path_path_lookup_and_unavailable() -> None:
    explicit = discover_executable(sys.executable, {}, identity="codex")
    looked_up = discover_executable(
        "codex",
        {"PATH": "bounded"},
        locator=lambda executable, path: sys.executable,
        identity="codex",
    )
    unavailable = discover_executable(
        "codex",
        {"PATH": "bounded"},
        locator=lambda executable, path: None,
        identity="codex",
    )

    assert explicit.discovery_method is ExecutableDiscoveryMethod.EXPLICIT_PATH
    assert looked_up.discovery_method is ExecutableDiscoveryMethod.PATH_LOOKUP
    assert unavailable.state is CapabilityState.UNSUPPORTED
    assert unavailable.discovery_method is ExecutableDiscoveryMethod.UNAVAILABLE


def test_git_adapter_uses_explicit_executable_without_shell(tmp_path: Path) -> None:
    root = tmp_path / "git root ünicode"
    root.mkdir()
    executable = _git_path()
    _git(root, executable, "init", "-b", "arbitrary")
    _git(root, executable, "config", "user.name", "P7.3 Test")
    _git(root, executable, "config", "user.email", "p7.3@example.invalid")
    (root / "README.md").write_text("platform boundary\n", encoding="utf-8")
    _git(root, executable, "add", "README.md")
    _git(root, executable, "commit", "-m", "test: platform boundary")

    adapter = GitAdapter(root, executable=executable)
    observed = adapter.observe_read_only()

    assert adapter.git_executable == executable
    assert observed.branch_name == "arbitrary"
    assert observed.clean is True
    with pytest.raises(GitOperationError, match="GIT_UNAVAILABLE"):
        GitAdapter(root, executable=str(tmp_path / "foreign-git.exe")).observe_read_only()


def test_git_adapter_uses_explicit_cwd_and_bounded_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured.update(options)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("P7_3_SECRET", "must-not-cross-boundary")
    monkeypatch.setattr(subprocess, "run", run)
    adapter = GitAdapter(tmp_path, executable="explicit-git")

    adapter._execute(tmp_path, (0,), "status")

    assert captured["command"] == ["explicit-git", "-C", str(tmp_path), "status"]
    assert captured["shell"] is False
    assert captured["cwd"] == tmp_path
    assert captured["timeout"] == 120.0
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert "P7_3_SECRET" not in environment
    assert environment["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True])
def test_git_adapter_requires_a_positive_finite_timeout(
    tmp_path: Path,
    value: float | int | bool,
) -> None:
    with pytest.raises(ValueError, match="positive number"):
        GitAdapter(tmp_path, timeout_seconds=value)


def test_git_adapter_timeout_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("git", 0.01)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(GitOperationError, match="GIT_TIMEOUT"):
        GitAdapter(tmp_path, timeout_seconds=0.01)._execute(tmp_path, (0,), "status")


def test_relative_project_binding_is_refused() -> None:
    with pytest.raises(PlatformDiscoveryError, match="INVALID_PROJECT_BINDING"):
        PlatformEnvironmentProbe().inspect("relative/repository")
