from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentic_engineering_os.domain import (
    ArchetypeSupportLevel,
    CodexApprovalConstraint,
    CodexProjectConstraints,
    CodexSandboxConstraint,
    ExecutableDiscoveryMethod,
    MissionStateGitPolicy,
    ProjectConfiguration,
    ProjectPathPolicy,
    RepositoryArchetype,
    RepositoryRootPolicy,
    ToolchainAvailability,
    ToolchainDeclaration,
    ToolchainMachineFact,
    VerificationCommand,
    VerificationKind,
    WorkingDirectoryPolicy,
)
from agentic_engineering_os.infrastructure import (
    PlatformEnvironmentProbe,
    RepositoryArchetypeError,
    RepositoryArchetypeEvaluator,
    RepositoryArchetypeProfiler,
    RepositoryReconnaissance,
    RepositoryToolchainProbe,
)


def _git_path() -> str:
    path = shutil.which("git")
    assert path is not None
    return str(Path(path).resolve())


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        [_git_path(), "-C", str(root), *args],
        shell=False,
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repository(tmp_path: Path, files: dict[str, str], name: str = "repository") -> Path:
    root = tmp_path / name
    root.mkdir()
    for relative, content in files.items():
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "init", "-b", "arbitrary")
    _git(root, "config", "user.name", "P7.4 Test")
    _git(root, "config", "user.email", "p7.4@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "--allow-empty", "-m", "test: archetype fixture")
    return root


def _command(
    command_id: str,
    executable: str,
    *args: str,
    cwd: str = ".",
    kind: VerificationKind = VerificationKind.TEST,
    required: bool = True,
) -> VerificationCommand:
    return VerificationCommand(
        command_id,
        kind,
        executable,
        args,
        cwd,
        WorkingDirectoryPolicy.REPOSITORY_RELATIVE,
        required,
    )


def _configuration(
    *commands: VerificationCommand,
    project_id: str = "target",
    toolchains: tuple[str, ...] = ("python",),
    allowed_paths: tuple[str, ...] = ("src", "tests"),
) -> ProjectConfiguration:
    return ProjectConfiguration(
        config_version="1.0",
        project_id=project_id,
        repository_root_policy=RepositoryRootPolicy.CONFIG_PARENT_GIT_ROOT,
        toolchains=tuple(
            ToolchainDeclaration(identity, None)
            for identity in sorted(toolchains, key=str.casefold)
        ),
        verification_commands=tuple(
            sorted(commands, key=lambda item: item.command_id.casefold())
        ),
        path_policy=ProjectPathPolicy(
            tuple(sorted(allowed_paths, key=str.casefold)), (), ()
        ),
        context_sources=(),
        codex_constraints=CodexProjectConstraints(
            CodexSandboxConstraint.WORKSPACE_WRITE,
            CodexApprovalConstraint.NEVER,
            True,
            1,
        ),
        mission_state_git_policy=MissionStateGitPolicy.TRACKED,
    )


def _profile(root: Path, configuration: ProjectConfiguration):
    reconnaissance = RepositoryReconnaissance().inspect(root)
    return RepositoryArchetypeProfiler().build(reconnaissance, configuration)


def _platform(root: Path):
    return PlatformEnvironmentProbe().inspect(root, git_executable=_git_path())


def _fact(
    archetype: RepositoryArchetype,
    executable: str,
    path: Path | None = None,
    *,
    availability: ToolchainAvailability = ToolchainAvailability.AVAILABLE,
) -> ToolchainMachineFact:
    observed = Path(sys.executable) if path is None else path
    stat = observed.stat() if availability is ToolchainAvailability.AVAILABLE else None
    versions = {
        RepositoryArchetype.PYTHON: "Python 3.11.9",
        RepositoryArchetype.NODE: "10.0.0" if executable.startswith("npm") else "v20.0.0",
        RepositoryArchetype.RUST: "cargo 1.80.0",
    }
    return ToolchainMachineFact(
        archetype,
        executable,
        availability,
        str(observed) if availability is not ToolchainAvailability.UNAVAILABLE else None,
        versions[archetype] if availability is ToolchainAvailability.AVAILABLE else None,
        ExecutableDiscoveryMethod.EXPLICIT_PATH,
        stat.st_size if stat else None,
        stat.st_mtime_ns if stat else None,
        hashlib.sha256(observed.read_bytes()).hexdigest() if stat else None,
    )


def _evaluate(root: Path, configuration: ProjectConfiguration, facts):
    profile = _profile(root, configuration)
    assessment = RepositoryArchetypeEvaluator().evaluate(
        profile, configuration, _platform(root), tuple(facts)
    )
    return profile, assessment


def test_real_python_repository_with_explicit_command_is_execution_ready(
    tmp_path: Path,
) -> None:
    root = _repository(
        tmp_path,
        {
            "pyproject.toml": "[project]\nname='sample'\nversion='1.0'\n",
            "src/sample.py": "VALUE = 1\n",
            "tests/test_sample.py": "def test_sample(): assert True\n",
        },
    )
    configuration = _configuration(_command("tests", "python", "--version"))
    profile = _profile(root, configuration)
    facts = RepositoryToolchainProbe().observe(profile)

    assessment = RepositoryArchetypeEvaluator().evaluate(
        profile, configuration, _platform(root), facts
    )

    assert assessment.support_level is ArchetypeSupportLevel.EXECUTION_READY
    assert assessment.detected_archetypes == (RepositoryArchetype.PYTHON,)
    assert facts[0].availability is ToolchainAvailability.AVAILABLE
    assert facts[0].version and facts[0].version.startswith("Python ")


def test_python_missing_executable_remains_adoptable(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"pyproject.toml": "[project]\nname='p'\nversion='1'\n"})
    configuration = _configuration(_command("tests", "python", "--version"))
    profile = _profile(root, configuration)
    facts = RepositoryToolchainProbe(executable_locator=lambda name, path: None).observe(
        profile
    )
    assessment = RepositoryArchetypeEvaluator().evaluate(
        profile, configuration, _platform(root), facts
    )

    assert facts[0].availability is ToolchainAvailability.UNAVAILABLE
    assert assessment.support_level is ArchetypeSupportLevel.ADOPTABLE
    assert "TOOLCHAIN_UNAVAILABLE:tests" in assessment.blockers


def test_toolchain_probe_runs_once_per_distinct_owned_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path, {"pyproject.toml": "[project]\nname='p'\nversion='1'\n"})
    configuration = _configuration(
        _command("lint", "python", "--version", kind=VerificationKind.LINT),
        _command("tests", "python", "--version"),
    )
    profile = _profile(root, configuration)
    calls: list[list[str]] = []

    def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert options["shell"] is False
        assert options["cwd"] == root
        environment = options["env"]
        assert isinstance(environment, dict)
        assert "P7_4_SECRET" not in environment
        return subprocess.CompletedProcess(command, 0, "Python 3.11.9\n", "")

    monkeypatch.setenv("P7_4_SECRET", "must-not-cross-boundary")
    monkeypatch.setattr(subprocess, "run", run)
    facts = RepositoryToolchainProbe(
        executable_locator=lambda name, path: sys.executable
    ).observe(profile)

    assert len(facts) == 1
    assert calls == [[sys.executable, "--version"]]


def test_toolchain_machine_facts_and_probe_timeout_are_fail_closed() -> None:
    with pytest.raises(ValueError, match="complete observation"):
        ToolchainMachineFact(
            RepositoryArchetype.PYTHON,
            "python",
            ToolchainAvailability.AVAILABLE,
            None,
            None,
            ExecutableDiscoveryMethod.UNAVAILABLE,
            None,
            None,
            None,
        )
    with pytest.raises(ValueError, match="timeout must be positive"):
        RepositoryToolchainProbe(timeout_seconds=float("nan"))


def test_node_explicit_script_and_single_lockfile_can_be_ready_by_contract(
    tmp_path: Path,
) -> None:
    root = _repository(
        tmp_path,
        {
            "package.json": json.dumps({"scripts": {"test": "node --test"}}),
            "package-lock.json": "{}\n",
        },
    )
    configuration = _configuration(
        _command("tests", "npm", "run", "test"), toolchains=("node",)
    )
    profile, assessment = _evaluate(
        root, configuration, (_fact(RepositoryArchetype.NODE, "npm"),)
    )

    assert profile.components[0].declared_scripts == ("test",)
    assert profile.components[0].package_manager == "npm"
    assert profile.command_contracts[0].owner_archetype is RepositoryArchetype.NODE
    assert assessment.support_level is ArchetypeSupportLevel.EXECUTION_READY


def test_multiple_node_lockfiles_are_ambiguous(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        {
            "package.json": "{}\n",
            "package-lock.json": "{}\n",
            "yarn.lock": "",
        },
    )
    configuration = _configuration(toolchains=("node",), allowed_paths=())
    _, assessment = _evaluate(root, configuration, ())

    assert assessment.support_level is ArchetypeSupportLevel.AMBIGUOUS
    assert "MULTIPLE_NODE_LOCKFILES" in assessment.blockers


def test_rust_configured_command_can_be_ready_by_contract(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        {"Cargo.toml": "[package]\nname='sample'\nversion='0.1.0'\n"},
    )
    configuration = _configuration(
        _command("tests", "cargo", "test"), toolchains=("rust",)
    )
    _, assessment = _evaluate(
        root, configuration, (_fact(RepositoryArchetype.RUST, "cargo"),)
    )
    assert assessment.support_level is ArchetypeSupportLevel.EXECUTION_READY


def test_rust_workspace_is_observed_without_implying_a_command(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        {"Cargo.toml": "[workspace]\nmembers=[]\n"},
    )
    configuration = _configuration(toolchains=("rust",), allowed_paths=())
    profile, assessment = _evaluate(root, configuration, ())
    assert profile.components[0].workspace is True
    assert profile.components[0].package_manager == "cargo"
    assert assessment.support_level is ArchetypeSupportLevel.RECOGNIZED


def test_mixed_disjoint_components_keep_separate_owners_and_scopes(
    tmp_path: Path,
) -> None:
    root = _repository(
        tmp_path,
        {
            "backend/pyproject.toml": "[project]\nname='back'\nversion='1'\n",
            "frontend/package.json": "{}\n",
            "frontend/package-lock.json": "{}\n",
        },
    )
    configuration = _configuration(
        _command("node-tests", "node", "--version", cwd="frontend"),
        _command("python-tests", "python", "--version", cwd="backend"),
        toolchains=("node", "python"),
        allowed_paths=("backend", "frontend"),
    )
    profile, assessment = _evaluate(
        root,
        configuration,
        (
            _fact(RepositoryArchetype.NODE, "node"),
            _fact(RepositoryArchetype.PYTHON, "python"),
        ),
    )

    assert {item.root for item in profile.components} == {"backend", "frontend"}
    assert {item.owner_archetype for item in profile.command_contracts} == {
        RepositoryArchetype.NODE,
        RepositoryArchetype.PYTHON,
    }
    assert {item.source_scopes for item in profile.components} == {
        ("backend",),
        ("frontend",),
    }
    assert all("WINDOWS_V1_LOCAL" in item.required_capabilities for item in profile.components)
    assert assessment.support_level is ArchetypeSupportLevel.EXECUTION_READY


def test_mixed_overlapping_components_are_ambiguous(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        {
            "pyproject.toml": "[project]\nname='mixed'\nversion='1'\n",
            "package.json": "{}\n",
        },
    )
    configuration = _configuration(toolchains=("node", "python"), allowed_paths=())
    _, assessment = _evaluate(root, configuration, ())
    assert assessment.support_level is ArchetypeSupportLevel.AMBIGUOUS
    assert "OVERLAPPING_COMPONENT_SCOPES" in assessment.blockers


def test_one_unavailable_mixed_toolchain_prevents_readiness(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        {
            "backend/pyproject.toml": "[project]\nname='back'\nversion='1'\n",
            "frontend/package.json": "{}\n",
        },
    )
    configuration = _configuration(
        _command("node-tests", "node", "--version", cwd="frontend"),
        _command("python-tests", "python", "--version", cwd="backend"),
        toolchains=("node", "python"),
        allowed_paths=("backend", "frontend"),
    )
    _, assessment = _evaluate(
        root, configuration, (_fact(RepositoryArchetype.PYTHON, "python"),)
    )
    assert assessment.support_level is ArchetypeSupportLevel.ADOPTABLE
    assert "MISSING_MACHINE_FACT:node-tests" in assessment.blockers


def test_explicit_configuration_can_select_only_one_disjoint_component(
    tmp_path: Path,
) -> None:
    root = _repository(
        tmp_path,
        {
            "backend/pyproject.toml": "[project]\nname='back'\nversion='1'\n",
            "frontend/package.json": "{}\n",
        },
    )
    configuration = _configuration(
        _command("python-tests", "python", "--version", cwd="backend"),
        toolchains=("python",),
        allowed_paths=("backend",),
    )
    profile, assessment = _evaluate(
        root, configuration, (_fact(RepositoryArchetype.PYTHON, "python"),)
    )
    assert {item.archetype for item in profile.components} == {
        RepositoryArchetype.PYTHON,
        RepositoryArchetype.NODE,
    }
    assert assessment.support_level is ArchetypeSupportLevel.EXECUTION_READY


def test_unknown_archetype_and_manifest_without_command_never_become_ready(
    tmp_path: Path,
) -> None:
    unknown = _repository(tmp_path, {"go.mod": "module example.invalid/demo\n"}, "unknown")
    python = _repository(
        tmp_path,
        {"pyproject.toml": "[project]\nname='known'\nversion='1'\n"},
        "python",
    )
    empty = _configuration(allowed_paths=())

    _, unknown_assessment = _evaluate(unknown, empty, ())
    _, python_assessment = _evaluate(python, empty, ())

    assert unknown_assessment.support_level is ArchetypeSupportLevel.UNSUPPORTED
    assert python_assessment.support_level is ArchetypeSupportLevel.RECOGNIZED


def test_discovered_node_candidate_never_becomes_command_authority(
    tmp_path: Path,
) -> None:
    root = _repository(
        tmp_path,
        {
            "package.json": json.dumps({"scripts": {"test": "node --test"}}),
            "package-lock.json": "{}\n",
        },
    )
    repository_profile = RepositoryReconnaissance().inspect(root)
    assert repository_profile.candidate_commands
    configuration = _configuration(toolchains=("node",), allowed_paths=())
    profile = RepositoryArchetypeProfiler().build(repository_profile, configuration)
    assessment = RepositoryArchetypeEvaluator().evaluate(
        profile, configuration, _platform(root), ()
    )

    assert profile.command_contracts == ()
    assert assessment.support_level is ArchetypeSupportLevel.RECOGNIZED


def test_configured_command_without_matching_toolchain_is_not_ready(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path, {"pyproject.toml": "[project]\nname='p'\nversion='1'\n"})
    configuration = _configuration(
        _command("tests", "cargo", "test"), toolchains=("python",)
    )
    _, assessment = _evaluate(root, configuration, ())
    assert assessment.support_level is ArchetypeSupportLevel.ADOPTABLE
    assert "COMMAND_TOOLCHAIN_MISMATCH" in assessment.blockers


def test_configured_command_without_detected_archetype_is_unsupported(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path, {"go.mod": "module example.invalid/demo\n"})
    configuration = _configuration(_command("tests", "python", "--version"))
    _, assessment = _evaluate(root, configuration, ())
    assert assessment.support_level is ArchetypeSupportLevel.UNSUPPORTED
    assert "CONFIGURED_COMMAND_WITHOUT_DETECTED_TOOLCHAIN" in assessment.blockers


def test_ambiguous_command_ownership_is_fail_closed(tmp_path: Path) -> None:
    root = _repository(
        tmp_path,
        {
            "pyproject.toml": "[project]\nname='mixed'\nversion='1'\n",
            "package.json": "{}\n",
        },
    )
    configuration = _configuration(
        _command("verify", "tool", "check"),
        toolchains=("node", "python"),
        allowed_paths=(),
    )
    _, assessment = _evaluate(root, configuration, ())
    assert assessment.support_level is ArchetypeSupportLevel.AMBIGUOUS
    assert "AMBIGUOUS_COMMAND_OWNERSHIP" in assessment.blockers


@pytest.mark.parametrize("cwd", ["../foreign", "C:/foreign"])
def test_foreign_or_traversing_command_cwd_is_refused(
    tmp_path: Path, cwd: str
) -> None:
    root = _repository(tmp_path, {"pyproject.toml": "[project]\nname='p'\nversion='1'\n"})
    configuration = _configuration(_command("tests", "python", "--version", cwd=cwd))
    with pytest.raises(RepositoryArchetypeError, match="INVALID_PROJECT_CONFIGURATION"):
        _profile(root, configuration)


def test_shell_metacharacter_is_not_execution_ready(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"pyproject.toml": "[project]\nname='p'\nversion='1'\n"})
    configuration = _configuration(_command("tests", "python", "safe;unsafe"))
    _, assessment = _evaluate(
        root, configuration, (_fact(RepositoryArchetype.PYTHON, "python"),)
    )
    assert assessment.support_level is ArchetypeSupportLevel.ADOPTABLE
    assert "SHELL_METACHARACTER:tests" in assessment.blockers


def test_executable_substitution_is_unknown_not_available(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"package.json": "{}\n"})
    configuration = _configuration(
        _command("tests", "node", "--version"), toolchains=("node",)
    )
    profile = _profile(root, configuration)
    facts = RepositoryToolchainProbe(
        executable_locator=lambda name, path: sys.executable
    ).observe(profile)
    assessment = RepositoryArchetypeEvaluator().evaluate(
        profile, configuration, _platform(root), facts
    )
    assert facts[0].availability is ToolchainAvailability.UNKNOWN
    assert assessment.support_level is ArchetypeSupportLevel.ADOPTABLE


def test_stale_machine_fact_is_refused(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"pyproject.toml": "[project]\nname='p'\nversion='1'\n"})
    observed = tmp_path / "observed-tool.exe"
    observed.write_bytes(b"first")
    fact = _fact(RepositoryArchetype.PYTHON, "python", observed)
    observed.write_bytes(b"other")
    assert fact.observed_mtime_ns is not None
    os.utime(observed, ns=(fact.observed_mtime_ns, fact.observed_mtime_ns))
    configuration = _configuration(_command("tests", "python", "--version"))
    _, assessment = _evaluate(root, configuration, (fact,))
    assert assessment.support_level is ArchetypeSupportLevel.ADOPTABLE
    assert "STALE_MACHINE_FACT:tests" in assessment.blockers


def test_profile_from_repository_a_cannot_be_reused_on_b(tmp_path: Path) -> None:
    files = {"pyproject.toml": "[project]\nname='p'\nversion='1'\n"}
    first = _repository(tmp_path, files, "first")
    second = _repository(tmp_path, files, "second")
    configuration = _configuration(_command("tests", "python", "--version"))
    profile = _profile(first, configuration)
    assessment = RepositoryArchetypeEvaluator().evaluate(
        profile,
        configuration,
        _platform(second),
        (_fact(RepositoryArchetype.PYTHON, "python"),),
    )
    assert assessment.support_level is ArchetypeSupportLevel.ADOPTABLE
    assert "CROSS_REPOSITORY_PROFILE" in assessment.blockers


def test_optional_missing_toolchain_does_not_block_required_python(
    tmp_path: Path,
) -> None:
    root = _repository(
        tmp_path,
        {
            "backend/pyproject.toml": "[project]\nname='back'\nversion='1'\n",
            "frontend/package.json": "{}\n",
        },
    )
    configuration = _configuration(
        _command("node-lint", "node", "--check", cwd="frontend", required=False),
        _command("python-tests", "python", "--version", cwd="backend"),
        toolchains=("node", "python"),
        allowed_paths=("backend", "frontend"),
    )
    _, assessment = _evaluate(
        root, configuration, (_fact(RepositoryArchetype.PYTHON, "python"),)
    )
    assert assessment.support_level is ArchetypeSupportLevel.EXECUTION_READY


def test_profile_and_assessment_are_bound_to_configuration_identity(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path, {"pyproject.toml": "[project]\nname='p'\nversion='1'\n"})
    first = _configuration(_command("tests", "python", "--version"), project_id="a")
    second = replace(first, project_id="b")
    profile = _profile(root, first)
    assessment = RepositoryArchetypeEvaluator().evaluate(
        profile, second, _platform(root), (_fact(RepositoryArchetype.PYTHON, "python"),)
    )
    assert assessment.support_level is ArchetypeSupportLevel.ADOPTABLE
    assert "PROJECT_CONFIGURATION_MISMATCH" in assessment.blockers


def test_same_project_id_with_changed_toolchain_contract_is_refused(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path, {"pyproject.toml": "[project]\nname='p'\nversion='1'\n"})
    first = _configuration(_command("tests", "python", "--version"))
    second = replace(
        first,
        toolchains=(ToolchainDeclaration("node", None),),
    )
    profile = _profile(root, first)
    assessment = RepositoryArchetypeEvaluator().evaluate(
        profile, second, _platform(root), (_fact(RepositoryArchetype.PYTHON, "python"),)
    )
    assert assessment.support_level is ArchetypeSupportLevel.ADOPTABLE
    assert "PROFILE_CONFIGURATION_MISMATCH" in assessment.blockers


def test_forged_command_ownership_is_refused(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"pyproject.toml": "[project]\nname='p'\nversion='1'\n"})
    configuration = _configuration(_command("tests", "python", "--version"))
    profile = _profile(root, configuration)
    forged_contract = replace(
        profile.command_contracts[0],
        owner_archetype=RepositoryArchetype.NODE,
        owner_component_id="node:.",
    )
    forged_profile = replace(profile, command_contracts=(forged_contract,))
    assessment = RepositoryArchetypeEvaluator().evaluate(
        forged_profile,
        configuration,
        _platform(root),
        (_fact(RepositoryArchetype.NODE, "python"),),
    )
    assert assessment.support_level is ArchetypeSupportLevel.AMBIGUOUS
    assert "PROFILE_COMMAND_MISMATCH" in assessment.blockers


def test_unimplemented_version_constraint_is_fail_closed(tmp_path: Path) -> None:
    root = _repository(tmp_path, {"pyproject.toml": "[project]\nname='p'\nversion='1'\n"})
    configuration = _configuration(_command("tests", "python", "--version"))
    configuration = replace(
        configuration,
        toolchains=(ToolchainDeclaration("python", ">=3.11"),),
    )
    _, assessment = _evaluate(
        root, configuration, (_fact(RepositoryArchetype.PYTHON, "python"),)
    )
    assert assessment.support_level is ArchetypeSupportLevel.ADOPTABLE
    assert "VERSION_CONSTRAINT_NOT_EVALUATED:PYTHON" in assessment.blockers
