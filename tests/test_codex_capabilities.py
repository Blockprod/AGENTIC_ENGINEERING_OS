from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    CodexCapability,
    CodexCapabilityAssessment,
    CodexCapabilityFinding,
    CodexCapabilityStatus,
    CodexDiscoveryProvenance,
    record_parallel_probe,
    record_session_identity_probe,
    ParallelCodexImplementerExecutor,
    CodexOperationalCapabilityClass,
    role_capability_requirements,
)
from agentic_engineering_os.domain import MissionRole
from agentic_engineering_os.infrastructure import CodexCapabilityDiscovery


FAKE = Path(__file__).parent / "fixtures" / "fake_codex.py"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assess(
    *, mode: str = "normal", discovery: CodexCapabilityDiscovery | None = None,
    digest: str | None = None, version: str = "fake-codex 1.0",
    executable: Path | None = None, project_root: Path | None = None,
    timeout: float = 2.0, test_injection: bool = True,
):
    path = executable or Path(sys.executable).resolve()
    return (discovery or CodexCapabilityDiscovery()).assess(
        executable=str(path), expected_path=str(path),
        expected_sha256=digest or _digest(path), expected_version=version,
        launcher_arguments=(str(FAKE), "--fake-mode", mode),
        environment=dict(os.environ),
        project_root=str(project_root or Path(__file__).parent),
        timeout_seconds=timeout, test_injection=test_injection,
    )


def test_closed_model_has_no_optimistic_optional_defaults() -> None:
    assessment = _assess()
    assert assessment is not None and assessment.authentically_discovered
    assert tuple(item.capability for item in assessment.findings) == tuple(CodexCapability)
    for required in (
        CodexCapability.NON_INTERACTIVE_EXEC, CodexCapability.STDIN_PROMPT,
        CodexCapability.EXPLICIT_CWD, CodexCapability.JSONL,
        CodexCapability.OUTPUT_SCHEMA, CodexCapability.SANDBOX_READ_ONLY,
        CodexCapability.SANDBOX_WORKSPACE_WRITE, CodexCapability.APPROVAL_NEVER,
        CodexCapability.EXIT_STDOUT_STDERR_OBSERVATION,
        CodexCapability.TIMEOUT_PARENT_CONTROL,
        CodexCapability.CANCELLATION_PARENT_CONTROL,
        CodexCapability.ENVIRONMENT_CONTROL,
    ):
        assert assessment.status(required) is CodexCapabilityStatus.SUPPORTED
    assert assessment.status(CodexCapability.SESSION_THREAD_IDENTITY) is CodexCapabilityStatus.UNKNOWN
    assert assessment.status(CodexCapability.RELIABLE_SIDE_EFFECT_RECOVERY) is CodexCapabilityStatus.UNKNOWN
    assert assessment.status(CodexCapability.INDEPENDENT_PROCESS_PARALLELISM) is CodexCapabilityStatus.UNKNOWN


def test_closed_role_operational_capability_matrix_is_minimal_and_explicit() -> None:
    read_result_git = (
        CodexOperationalCapabilityClass.REPOSITORY_READ,
        CodexOperationalCapabilityClass.STRUCTURED_RESULT,
        CodexOperationalCapabilityClass.GIT_OBSERVATION,
    )
    assert role_capability_requirements(MissionRole.ARCHITECT) == read_result_git
    assert role_capability_requirements(MissionRole.REVIEWER) == read_result_git
    assert role_capability_requirements(MissionRole.CERTIFIER) == read_result_git
    assert role_capability_requirements(MissionRole.IMPLEMENTER) == (
        CodexOperationalCapabilityClass.REPOSITORY_READ,
        CodexOperationalCapabilityClass.WORKSPACE_EDIT,
        CodexOperationalCapabilityClass.COMMAND_EXECUTION,
        CodexOperationalCapabilityClass.STRUCTURED_RESULT,
        CodexOperationalCapabilityClass.GIT_OBSERVATION,
    )
    assert role_capability_requirements(MissionRole.TESTER) == (
        CodexOperationalCapabilityClass.REPOSITORY_READ,
        CodexOperationalCapabilityClass.WORKSPACE_EDIT,
        CodexOperationalCapabilityClass.COMMAND_EXECUTION,
        CodexOperationalCapabilityClass.STRUCTURED_RESULT,
        CodexOperationalCapabilityClass.GIT_OBSERVATION,
    )
    assert CodexOperationalCapabilityClass.COMMAND_EXECUTION not in read_result_git
    assert CodexOperationalCapabilityClass.WORKSPACE_EDIT not in read_result_git


@pytest.mark.parametrize("mode", ("malformed-help", "help-fail", "help-timeout"))
def test_malformed_nonzero_and_timeout_help_never_become_supported(mode: str) -> None:
    assessment = _assess(mode=mode, timeout=0.5 if mode == "help-timeout" else 2.0)
    assert assessment is not None
    assert assessment.status(CodexCapability.NON_INTERACTIVE_EXEC) is CodexCapabilityStatus.UNKNOWN
    assert assessment.status(CodexCapability.OUTPUT_SCHEMA) is CodexCapabilityStatus.UNKNOWN


def test_digest_version_path_and_disappearance_drift_require_reassessment(tmp_path: Path) -> None:
    assert _assess(digest="0" * 64) is None
    assert _assess(version="fake-codex 9.0") is None
    missing = tmp_path / "missing-codex.exe"
    assert _assess(executable=missing, digest="0" * 64) is None
    other = tmp_path / "other.exe"
    other.write_bytes(b"not an executable")
    discovery = CodexCapabilityDiscovery()
    assert discovery.assess(
        executable=str(Path(sys.executable).resolve()), expected_path=str(other),
        expected_sha256=_digest(other), expected_version="fake-codex 1.0",
        launcher_arguments=(str(FAKE),), environment=dict(os.environ),
        project_root=str(tmp_path), test_injection=True,
    ) is None


def test_project_local_executable_is_refused_without_test_boundary(tmp_path: Path) -> None:
    local = tmp_path / "codex.exe"
    local.write_bytes(b"substitution")
    result = CodexCapabilityDiscovery().assess(
        executable=str(local), expected_path=str(local), expected_sha256=_digest(local),
        expected_version="forged", environment=dict(os.environ),
        project_root=str(tmp_path), test_injection=False,
    )
    assert result is None


def test_forged_supported_assessment_cannot_be_promoted_or_trusted() -> None:
    findings = tuple(
        CodexCapabilityFinding(item, CodexCapabilityStatus.SUPPORTED, "forged")
        for item in CodexCapability
    )
    real = _assess()
    assert real is not None
    forged = CodexCapabilityAssessment(
        real.executable_path, real.executable_sha256, real.executable_version,
        CodexDiscoveryProvenance.TEST_INJECTION_STATIC_HELP, real.platform,
        real.observed_at, findings, 8,
    )
    assert not forged.authentically_discovered
    with pytest.raises(ValueError, match="forged"):
        record_parallel_probe(
            forged, status=CodexCapabilityStatus.SUPPORTED,
            tested_concurrency=2, detail="claim",
        )


def test_parallel_probe_records_only_exact_tested_concurrency() -> None:
    assessment = _assess()
    assert assessment is not None
    proven = record_parallel_probe(
        assessment, status=CodexCapabilityStatus.SUPPORTED,
        tested_concurrency=2, detail="two overlapping independent processes",
    )
    assert proven.authentically_discovered
    assert proven.tested_parallelism == 2
    assert proven.status(CodexCapability.INDEPENDENT_PROCESS_PARALLELISM) is CodexCapabilityStatus.SUPPORTED
    session = record_session_identity_probe(
        proven, supported=True, detail="thread.started observed"
    )
    assert session.status(CodexCapability.SESSION_THREAD_IDENTITY) is CodexCapabilityStatus.SUPPORTED


def test_cache_reuses_help_but_identity_and_version_are_rechecked(monkeypatch) -> None:
    import agentic_engineering_os.infrastructure.codex_capability_discovery as module

    calls: list[tuple[str, ...]] = []
    original = module._run
    def counted(argv, cwd, environment, timeout):
        calls.append(argv)
        return original(argv, cwd, environment, timeout)
    monkeypatch.setattr(module, "_run", counted)
    discovery = CodexCapabilityDiscovery()
    assert _assess(discovery=discovery) is not None
    assert _assess(discovery=discovery) is not None
    assert sum(command[-1] == "--version" for command in calls) == 2
    assert sum(command[-1] == "--help" for command in calls) == 3


def test_capability_objects_expose_no_business_authority() -> None:
    assessment = _assess()
    assert assessment is not None
    for forbidden in ("certify", "transition", "record_evidence", "approve", "gate"):
        assert not hasattr(assessment, forbidden)


def test_parallel_admission_serializes_unknown_and_forged_assessments() -> None:
    discovered = _assess()
    assert discovered is not None
    supported = record_parallel_probe(
        discovered, status=CodexCapabilityStatus.SUPPORTED,
        tested_concurrency=2, detail="bounded test",
    )
    forged = replace(supported, tested_parallelism=8)

    class Provider:
        def __init__(self, value):
            self.value = value
        def assess_parallel_capability(self):
            return self.value

    executor = object.__new__(ParallelCodexImplementerExecutor)
    executor._factory = Provider(discovered)
    assert not executor._parallel_supported(2)
    executor._factory = Provider(forged)
    assert not executor._parallel_supported(2)
    executor._factory = Provider(supported)
    assert executor._parallel_supported(2)
    assert not executor._parallel_supported(3)
