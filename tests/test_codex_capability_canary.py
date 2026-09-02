"""Opt-in P7.5 real sequential and two-process Codex capability probes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from agentic_engineering_os.application import (
    CodexApprovalPolicy,
    CodexCapability,
    CodexCapabilityStatus,
    CodexExecutionBinding,
    CodexSandboxMode,
    CompiledPrompt,
    record_parallel_probe,
    record_session_identity_probe,
)
from agentic_engineering_os.domain import MissionRole
from agentic_engineering_os.infrastructure import (
    CodexCapabilityDiscovery,
    CodexRuntimeAdapter,
    CodexRuntimeConfiguration,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("AGENTIC_OS_RUN_P7_5_CODEX_PROBES") != "1",
    reason="P7.5 real Codex probes require explicit opt-in",
)


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments], shell=False,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repository(tmp_path: Path, name: str) -> tuple[Path, str, Path]:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "P7.5 Capability Probe")
    _git(root, "config", "user.email", "p7.5@example.invalid")
    (root / "README.md").write_text("bounded capability probe\n", encoding="utf-8")
    schema = root / "result.schema.json"
    schema.write_text(
        json.dumps({
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object", "additionalProperties": False,
            "required": ["status"],
            "properties": {"status": {"type": "string"}},
        }), encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-m", "test: capability probe baseline")
    return root.resolve(), _git(root, "rev-parse", "HEAD").casefold(), schema.resolve()


def _runtime(root: Path, commit: str, schema: Path, request_id: str, marker: str):
    executable_text = shutil.which("codex")
    assert executable_text is not None
    executable = Path(executable_text).resolve(strict=True)
    version = subprocess.run(
        [str(executable), "--version"], shell=False, capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=True,
    ).stdout.strip()
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    prompt_text = (
        f'Return only the JSON object {{"status":"{marker}"}}. '
        "Do not use tools and do not modify files."
    )
    prompt = CompiledPrompt(
        request_id, "a" * 64, "P7.5", 5, MissionRole.ARCHITECT,
        marker, str(root), None, commit, "p7.5-probe@1.0", prompt_text,
        len(prompt_text), 10, 0,
    )
    binding = CodexExecutionBinding(
        prompt.request_id, prompt.context_fingerprint, prompt.mission_id,
        prompt.workflow_generation, prompt.role, prompt.subject, str(root),
        commit, CodexSandboxMode.READ_ONLY, CodexApprovalPolicy.NEVER, 180,
        output_schema_path=str(schema),
    )
    configuration = CodexRuntimeConfiguration(
        executable=str(executable), expected_executable_path=str(executable),
        expected_executable_version=version, expected_executable_sha256=digest,
    )
    return CodexRuntimeAdapter(configuration), prompt, binding, configuration


def test_real_sequential_codex_capability_canary(tmp_path: Path) -> None:
    root, commit, schema = _repository(tmp_path, "sequential")
    runtime, prompt, binding, _ = _runtime(root, commit, schema, "p7-5-seq", "P7_5_SEQUENTIAL_OK")
    observation = runtime.execute(prompt, binding)
    assert observation.exit_code == 0
    assert json.loads(observation.final_output or "null") == {"status": "P7_5_SEQUENTIAL_OK"}
    assert observation.thread_id
    assert observation.git_before == observation.git_after
    assert observation.git_after is not None and observation.git_after.clean is True
    configuration = _runtime(root, commit, schema, "assessment-only", "IGNORED")[3]
    assessment = CodexCapabilityDiscovery().assess(
        executable=configuration.executable,
        expected_path=configuration.expected_executable_path,
        expected_sha256=configuration.expected_executable_sha256,
        expected_version=configuration.expected_executable_version,
        environment=dict(os.environ), project_root=str(root),
    )
    assert assessment is not None
    observed = record_session_identity_probe(
        assessment, supported=observation.thread_id is not None,
        detail="real sequential JSONL thread.started event",
    )
    assert observed.status(CodexCapability.SESSION_THREAD_IDENTITY) is CodexCapabilityStatus.SUPPORTED


def test_two_real_codex_exec_processes_overlap_in_separate_repositories(tmp_path: Path) -> None:
    left = _repository(tmp_path, "left")
    right = _repository(tmp_path, "right")
    cases = (
        _runtime(*left, "p7-5-parallel-left", "P7_5_PARALLEL_LEFT"),
        _runtime(*right, "p7-5-parallel-right", "P7_5_PARALLEL_RIGHT"),
    )
    barrier = Barrier(2)
    def run(case):
        runtime, prompt, binding, _ = case
        barrier.wait(timeout=10)
        return runtime.execute(prompt, binding)
    with ThreadPoolExecutor(max_workers=2) as pool:
        observations = tuple(pool.map(run, cases))
    assert all(item.exit_code == 0 and item.process_id for item in observations)
    assert observations[0].process_id != observations[1].process_id
    assert all(item.git_before == item.git_after for item in observations)
    assert max(item.started_at for item in observations if item.started_at) < min(
        item.ended_at for item in observations
    )
    configuration = cases[0][3]
    assessment = CodexCapabilityDiscovery().assess(
        executable=configuration.executable,
        expected_path=configuration.expected_executable_path,
        expected_sha256=configuration.expected_executable_sha256,
        expected_version=configuration.expected_executable_version,
        environment=dict(os.environ), project_root=str(left[0]),
    )
    assert assessment is not None
    proven = record_parallel_probe(
        assessment, status=CodexCapabilityStatus.SUPPORTED,
        tested_concurrency=2, detail="two real independent overlapping codex exec processes",
    )
    assert proven.status(CodexCapability.INDEPENDENT_PROCESS_PARALLELISM) is CodexCapabilityStatus.SUPPORTED
    assert proven.tested_parallelism == 2
