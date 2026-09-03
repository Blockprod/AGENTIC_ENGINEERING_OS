"""Opt-in real Codex transport canary; excluded from standard execution."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.real_codex

from agentic_engineering_os.application import (
    CodexApprovalPolicy,
    CodexExecutionBinding,
    CodexSandboxMode,
    CompiledPrompt,
)
from agentic_engineering_os.domain import MissionRole
from agentic_engineering_os.infrastructure import (
    CodexRuntimeAdapter,
    CodexRuntimeConfiguration,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("AGENTIC_OS_RUN_CODEX_CANARY") != "1",
    reason="real Codex canary requires explicit opt-in",
)


def test_real_codex_exec_transport_canary(tmp_path: Path) -> None:
    executable_text = shutil.which("codex")
    assert executable_text is not None
    executable = Path(executable_text).resolve()
    version = subprocess.run(
        [str(executable), "--version"],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout.strip()
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-b", "main"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "P4.5 Canary"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "p4.5@example.invalid"], check=True)
    (root / "README.md").write_text("canary\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "test: canary"], check=True)
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip().casefold()
    prompt_text = "Return only P4_5_CANARY_OK. Do not use tools or modify files."
    prompt = CompiledPrompt(
        "canary-request",
        "a" * 64,
        "canary-mission",
        0,
        MissionRole.ARCHITECT,
        "canary",
        str(root.resolve()),
        None,
        commit,
        "architect-result@1.0",
        prompt_text,
        len(prompt_text),
        10,
        0,
    )
    binding = CodexExecutionBinding(
        prompt.request_id,
        prompt.context_fingerprint,
        prompt.mission_id,
        prompt.workflow_generation,
        prompt.role,
        prompt.subject,
        str(root.resolve()),
        commit,
        CodexSandboxMode.READ_ONLY,
        CodexApprovalPolicy.NEVER,
        120,
    )
    with executable.open("rb") as stream:
        executable_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    configuration = CodexRuntimeConfiguration(
        executable=str(executable),
        expected_executable_path=str(executable),
        expected_executable_version=version,
        expected_executable_sha256=executable_sha256,
    )

    observation = CodexRuntimeAdapter(configuration).execute(prompt, binding)

    assert observation.exit_code == 0
    assert observation.final_output == "P4_5_CANARY_OK"
    assert observation.git_after is not None and observation.git_after.clean is True
