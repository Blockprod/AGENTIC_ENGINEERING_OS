"""Safe static discovery and identity-bound caching for the Codex CLI."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from agentic_engineering_os.application.codex_capabilities import (
    CodexCapability,
    CodexCapabilityAssessment,
    CodexCapabilityFinding,
    CodexCapabilityStatus,
    CodexDiscoveryProvenance,
    create_discovered_assessment,
)

from .platform_environment import (
    RUNTIME_ENVIRONMENT_ALLOWLIST,
    build_bounded_environment,
    discover_executable,
)


class CodexCapabilityDiscovery:
    """Recheck identity every time; reuse static help only for an unchanged binary."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, str, tuple[str, ...]], tuple[CodexCapabilityFinding, ...]] = {}

    def assess(
        self,
        *,
        executable: str,
        expected_path: str,
        expected_sha256: str,
        expected_version: str,
        launcher_arguments: tuple[str, ...] = (),
        environment: Mapping[str, str],
        project_root: str,
        timeout_seconds: float = 10.0,
        test_injection: bool = False,
        _observed_identity: tuple[str, str, str] | None = None,
    ) -> CodexCapabilityAssessment | None:
        fact = discover_executable(executable, environment, identity="codex")
        if fact.path is None:
            return None
        try:
            path = Path(fact.path).resolve(strict=True)
            expected = Path(expected_path).resolve(strict=True)
            root = Path(project_root).resolve(strict=True)
        except OSError:
            return None
        if not path.is_file() or _key(path) != _key(expected):
            return None
        if _contains(root, path) and not test_injection:
            return None
        child = build_bounded_environment(environment, RUNTIME_ENVIRONMENT_ALLOWLIST)
        if _observed_identity is None:
            digest = _sha256(path)
            version = _run((str(path), *launcher_arguments, "--version"), path.parent, child, timeout_seconds)
            if digest != expected_sha256 or version is None or version.strip() != expected_version:
                return None
            version = version.strip()
        else:
            observed_path, digest, version = _observed_identity
            if (
                _key(Path(observed_path)) != _key(path)
                or digest != expected_sha256
                or version != expected_version
            ):
                return None
        cache_arguments = (
            launcher_arguments[:1]
            if test_injection and launcher_arguments
            else launcher_arguments
        )
        key = (str(path), digest, version, cache_arguments)
        findings = self._cache.get(key)
        if findings is None:
            root_help = _run((str(path), *launcher_arguments, "--help"), path.parent, child, timeout_seconds)
            exec_help = _run((str(path), *launcher_arguments, "exec", "--help"), path.parent, child, timeout_seconds)
            resume_help = _run((str(path), *launcher_arguments, "exec", "resume", "--help"), path.parent, child, timeout_seconds)
            findings = _findings(root_help, exec_help, resume_help)
            self._cache[key] = findings
        provenance = (
            CodexDiscoveryProvenance.TEST_INJECTION_STATIC_HELP
            if test_injection
            else CodexDiscoveryProvenance.EXPLICIT_PATH_STATIC_HELP
            if Path(executable).is_absolute()
            else CodexDiscoveryProvenance.PATH_LOOKUP_STATIC_HELP
        )
        return create_discovered_assessment(
            executable_path=str(path), executable_sha256=digest,
            executable_version=version, discovery_provenance=provenance,
            platform=sys.platform, findings=findings,
        )


def _findings(root: str | None, execute: str | None, resume: str | None) -> tuple[CodexCapabilityFinding, ...]:
    unknown = (
        root is None
        or execute is None
        or "Usage: codex" not in root
        or "Usage: codex exec" not in execute
    )
    def observed(capability: CodexCapability, condition: bool, detail: str) -> CodexCapabilityFinding:
        status = CodexCapabilityStatus.UNKNOWN if unknown else (
            CodexCapabilityStatus.SUPPORTED if condition else CodexCapabilityStatus.UNSUPPORTED
        )
        return CodexCapabilityFinding(capability, status, detail)
    root_text = root or ""
    exec_text = execute or ""
    combined = root_text + "\n" + exec_text
    values: dict[CodexCapability, CodexCapabilityFinding] = {
        CodexCapability.NON_INTERACTIVE_EXEC: observed(CodexCapability.NON_INTERACTIVE_EXEC, "non-interactively" in exec_text and "codex exec" in exec_text, "exec help"),
        CodexCapability.STDIN_PROMPT: observed(CodexCapability.STDIN_PROMPT, "read from stdin" in exec_text and "-" in exec_text, "exec help"),
        CodexCapability.EXPLICIT_CWD: observed(CodexCapability.EXPLICIT_CWD, "--cd <DIR>" in combined or "-C, --cd" in combined, "CLI help"),
        CodexCapability.JSONL: observed(CodexCapability.JSONL, "--json" in exec_text and "JSONL" in exec_text, "exec help"),
        CodexCapability.OUTPUT_SCHEMA: observed(CodexCapability.OUTPUT_SCHEMA, "--output-schema <FILE>" in exec_text, "exec help"),
        CodexCapability.SANDBOX_READ_ONLY: observed(CodexCapability.SANDBOX_READ_ONLY, "read-only" in combined, "CLI help"),
        CodexCapability.SANDBOX_WORKSPACE_WRITE: observed(CodexCapability.SANDBOX_WORKSPACE_WRITE, "workspace-write" in combined, "CLI help"),
        CodexCapability.APPROVAL_NEVER: observed(CodexCapability.APPROVAL_NEVER, "--ask-for-approval" in combined and "never" in combined, "CLI help"),
        CodexCapability.EXIT_STDOUT_STDERR_OBSERVATION: CodexCapabilityFinding(CodexCapability.EXIT_STDOUT_STDERR_OBSERVATION, CodexCapabilityStatus.SUPPORTED, "parent subprocess boundary"),
        CodexCapability.TIMEOUT_PARENT_CONTROL: CodexCapabilityFinding(CodexCapability.TIMEOUT_PARENT_CONTROL, CodexCapabilityStatus.SUPPORTED, "parent subprocess boundary"),
        CodexCapability.CANCELLATION_PARENT_CONTROL: CodexCapabilityFinding(CodexCapability.CANCELLATION_PARENT_CONTROL, CodexCapabilityStatus.SUPPORTED, "parent subprocess boundary"),
        CodexCapability.SESSION_THREAD_IDENTITY: CodexCapabilityFinding(CodexCapability.SESSION_THREAD_IDENTITY, CodexCapabilityStatus.UNKNOWN, "requires active JSONL observation"),
        CodexCapability.RESUME_INTERFACE_PRESENT: CodexCapabilityFinding(CodexCapability.RESUME_INTERFACE_PRESENT, CodexCapabilityStatus.SUPPORTED if resume and "codex exec resume" in resume else CodexCapabilityStatus.UNKNOWN if resume is None else CodexCapabilityStatus.UNSUPPORTED, "resume help"),
        CodexCapability.RELIABLE_SIDE_EFFECT_RECOVERY: CodexCapabilityFinding(CodexCapability.RELIABLE_SIDE_EFFECT_RECOVERY, CodexCapabilityStatus.UNKNOWN, "not safely probed"),
        CodexCapability.ENVIRONMENT_CONTROL: CodexCapabilityFinding(CodexCapability.ENVIRONMENT_CONTROL, CodexCapabilityStatus.SUPPORTED, "bounded parent environment"),
        CodexCapability.INDEPENDENT_PROCESS_PARALLELISM: CodexCapabilityFinding(CodexCapability.INDEPENDENT_PROCESS_PARALLELISM, CodexCapabilityStatus.UNKNOWN, "requires bounded active probe"),
    }
    return tuple(values[item] for item in CodexCapability)


def _run(argv: tuple[str, ...], cwd: Path, environment: Mapping[str, str], timeout: float) -> str | None:
    try:
        result = subprocess.run(list(argv), shell=False, cwd=cwd, env=dict(environment), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = result.stdout.strip()
    return output if result.returncode == 0 and output and len(output) <= 200_000 else None


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _key(path: Path) -> str:
    return os.path.normcase(str(path)).casefold()


def _contains(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
