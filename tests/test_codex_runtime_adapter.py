from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    CodexApprovalPolicy,
    CodexExecutionBinding,
    CodexSandboxMode,
    CompiledPrompt,
)
from agentic_engineering_os.domain import MissionRole
from agentic_engineering_os.infrastructure import (
    CodexCapabilityDiscovery,
    CodexRuntimeAdapter,
    CodexRuntimeConfiguration,
)


FAKE = Path(__file__).parent / "fixtures" / "fake_codex.py"


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "P4.5 Test Operator")
    git(root, "config", "user.email", "p4.5@example.invalid")
    (root / "README.md").write_text("fake runtime repository\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "test: baseline")
    return root.resolve(), git(root, "rev-parse", "HEAD").casefold()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compiled(root: Path, commit: str, *, prompt: str = "bounded prompt") -> CompiledPrompt:
    return CompiledPrompt(
        request_id="request-p4-5",
        context_fingerprint="a" * 64,
        mission_id="mission-p4-5",
        workflow_generation=4,
        role=MissionRole.IMPLEMENTER,
        subject="US-P4-5",
        repository_root=str(root),
        worktree_path=None,
        observed_commit=commit,
        expected_result_contract="implementer-result@1.0",
        prompt_text=prompt,
        character_count=len(prompt),
        section_count=10,
        cognitive_item_count=0,
    )


def binding(prompt: CompiledPrompt, root: Path, **changes: object) -> CodexExecutionBinding:
    value = CodexExecutionBinding(
        request_id=prompt.request_id,
        context_fingerprint=prompt.context_fingerprint,
        mission_id=prompt.mission_id,
        workflow_generation=prompt.workflow_generation,
        role=prompt.role,
        subject=prompt.subject,
        cwd=str(root),
        expected_commit=prompt.observed_commit,
        sandbox=CodexSandboxMode.READ_ONLY,
        approval_policy=CodexApprovalPolicy.NEVER,
        timeout_seconds=5.0,
    )
    return replace(value, **changes)


def configuration(mode: str = "normal", *, maximum: int = 1_000_000) -> CodexRuntimeConfiguration:
    executable = Path(sys.executable).resolve()
    return CodexRuntimeConfiguration(
        executable=str(executable),
        expected_executable_path=str(executable),
        expected_executable_version="fake-codex 1.0",
        expected_executable_sha256=digest(executable),
        launcher_arguments=(str(FAKE), "--fake-mode", mode),
        max_output_characters=maximum,
        test_executable_injection=True,
    )


def adapter(mode: str = "normal", **kwargs: object) -> CodexRuntimeAdapter:
    discovery = (
        CodexCapabilityDiscovery()
        if mode in {
            "malformed-help", "help-fail", "help-timeout",
            "missing-output-schema", "missing-workspace-sandbox",
        }
        else None
    )
    return CodexRuntimeAdapter(
        configuration(mode, **kwargs), capability_discovery=discovery
    )


def test_required_capability_unknown_blocks_before_codex_spawn(tmp_path: Path) -> None:
    root, commit = repository(tmp_path)
    prompt = compiled(root, commit)
    observation = adapter("malformed-help").execute(prompt, binding(prompt, root))
    assert observation.process_id is None
    assert "REQUIRED_CAPABILITY_NOT_SUPPORTED:NON_INTERACTIVE_EXEC" in observation.issues


def test_optional_unknown_capabilities_do_not_block_sequential_execution(tmp_path: Path) -> None:
    root, commit = repository(tmp_path)
    prompt = compiled(root, commit)
    observation = adapter().execute(prompt, binding(prompt, root))
    assert observation.exit_code == 0
    assert observation.process_id is not None


def test_output_schema_capability_mismatch_blocks_before_spawn(tmp_path: Path) -> None:
    root, commit = repository(tmp_path)
    schema = root / "result.schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "test: add schema")
    commit = git(root, "rev-parse", "HEAD").casefold()
    prompt = compiled(root, commit)
    observation = adapter("missing-output-schema").execute(
        prompt, binding(prompt, root, output_schema_path=str(schema.resolve()))
    )
    assert observation.process_id is None
    assert observation.issues == ("REQUIRED_CAPABILITY_NOT_SUPPORTED:OUTPUT_SCHEMA",)


def test_workspace_write_sandbox_mismatch_blocks_before_spawn(tmp_path: Path) -> None:
    root, commit = repository(tmp_path)
    prompt = compiled(root, commit)
    observation = adapter("missing-workspace-sandbox").execute(
        prompt, binding(prompt, root, sandbox=CodexSandboxMode.WORKSPACE_WRITE)
    )
    assert observation.process_id is None
    assert observation.issues == (
        "REQUIRED_CAPABILITY_NOT_SUPPORTED:SANDBOX_WORKSPACE_WRITE",
    )


def decoded_final(observation) -> dict[str, object]:
    assert observation.final_output is not None
    return json.loads(observation.final_output)


def test_exec_uses_stdin_explicit_cwd_jsonl_and_closed_process_policy(tmp_path: Path) -> None:
    root, commit = repository(tmp_path)
    prompt = compiled(root, commit, prompt="literal prompt; $(touch injected) & echo unsafe")

    observation = adapter().execute(prompt, binding(prompt, root))
    final = decoded_final(observation)

    assert observation.exit_code == 0
    assert observation.thread_id == "11111111-2222-3333-4444-555555555555"
    assert observation.invalid_jsonl_lines == ()
    assert final["prompt"] == prompt.prompt_text
    assert Path(str(final["cwd"])) == root
    assert "exec" in final["args"] and final["args"][-1] == "-"
    assert final["args"].index("-a") < final["args"].index("exec")
    assert "-C" in final["args"] and "--json" in final["args"]
    assert "read-only" in final["args"] and "never" in final["args"]
    assert not (root / "injected").exists()
    assert observation.git_before == observation.git_after
    assert observation.git_after is not None and observation.git_after.clean is True
    assert observation.issues == ()
    assert [event.line_number for event in observation.events] == [1, 2, 3]


def test_models_are_immutable_and_exit_zero_is_not_a_role_verdict(tmp_path: Path) -> None:
    root, commit = repository(tmp_path)
    prompt = compiled(root, commit)
    observation = adapter().execute(prompt, binding(prompt, root))

    with pytest.raises(FrozenInstanceError):
        observation.exit_code = 99
    assert not hasattr(observation, "verdict")
    assert not hasattr(observation, "certification")
    assert observation.exit_code == 0


def test_output_schema_is_restricted_to_cwd_and_transported(tmp_path: Path) -> None:
    root, commit = repository(tmp_path)
    schema = root / "role-result.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    git(root, "add", schema.name)
    git(root, "commit", "-m", "test: schema")
    commit = git(root, "rev-parse", "HEAD").casefold()
    prompt = compiled(root, commit)

    observation = adapter().execute(
        prompt,
        binding(prompt, root, output_schema_path=str(schema)),
    )
    final = decoded_final(observation)

    index = final["args"].index("--output-schema")
    assert Path(final["args"][index + 1]) == schema

    outside = tmp_path / "outside.json"
    outside.write_text('{"type":"object"}', encoding="utf-8")
    refused = adapter().execute(
        prompt,
        binding(prompt, root, output_schema_path=str(outside)),
    )
    assert refused.process_id is None
    assert refused.issues == ("INVALID_OUTPUT_SCHEMA",)


def test_compiled_prompt_binding_mismatch_and_cwd_escape_fail_before_spawn(
    tmp_path: Path,
) -> None:
    root, commit = repository(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    prompt = compiled(root, commit)

    stale = adapter().execute(
        prompt,
        binding(prompt, root, workflow_generation=99),
    )
    escaped = adapter().execute(
        prompt, binding(prompt, str(root / ".." / "other"))
    )

    assert stale.process_id is None
    assert stale.issues == ("COMPILED_PROMPT_BINDING_MISMATCH",)
    assert escaped.process_id is None
    assert escaped.issues == ("BINDING_CWD_MISMATCH",)


def test_compiled_worktree_path_is_the_only_accepted_execution_root(tmp_path: Path) -> None:
    root, commit = repository(tmp_path)
    worktree = tmp_path / "worktree"
    git(root, "worktree", "add", "-b", "test/runtime", str(worktree), commit)
    prompt = replace(compiled(root, commit), worktree_path=str(worktree.resolve()))

    accepted = adapter().execute(prompt, binding(prompt, worktree.resolve()))
    refused = adapter().execute(prompt, binding(prompt, root))

    assert accepted.process_id is not None
    assert accepted.git_after is not None and accepted.git_after.clean is True
    assert refused.process_id is None
    assert refused.issues == ("BINDING_CWD_MISMATCH",)


def test_executable_absence_substitution_digest_and_version_fail_closed(
    tmp_path: Path,
) -> None:
    root, commit = repository(tmp_path)
    prompt = compiled(root, commit)
    expected = binding(prompt, root)
    executable = Path(sys.executable).resolve()

    missing_configuration = replace(
        configuration(),
        executable=str(tmp_path / "missing-codex.exe"),
        expected_executable_path=str(tmp_path / "missing-codex.exe"),
    )
    missing = CodexRuntimeAdapter(missing_configuration).execute(prompt, expected)
    substituted = CodexRuntimeAdapter(
        replace(configuration(), expected_executable_path=str(FAKE.resolve()))
    ).execute(prompt, expected)
    changed = CodexRuntimeAdapter(
        replace(configuration(), expected_executable_sha256="0" * 64)
    ).execute(prompt, expected)
    bad_version = adapter("bad-version").execute(prompt, expected)
    no_version = adapter("version-fail").execute(prompt, expected)

    assert missing.issues == ("EXECUTABLE_UNAVAILABLE",)
    assert substituted.issues == ("EXECUTABLE_PATH_MISMATCH",)
    assert changed.issues == ("EXECUTABLE_DIGEST_MISMATCH",)
    assert bad_version.issues == ("EXECUTABLE_VERSION_MISMATCH",)
    assert no_version.issues == ("EXECUTABLE_VERSION_UNOBSERVABLE",)
    assert all(item.process_id is None for item in (missing, substituted, changed, bad_version, no_version))
    assert executable.exists()


def test_environment_is_allowlisted_and_secret_is_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, commit = repository(tmp_path)
    prompt = compiled(root, commit)
    monkeypatch.setenv("P4_5_SECRET", "must-not-cross-boundary")

    observation = adapter().execute(prompt, binding(prompt, root))

    assert decoded_final(observation)["secret_present"] is False
    assert "must-not-cross-boundary" not in observation.stdout
    with pytest.raises(ValueError, match="unsafe name"):
        replace(configuration(), environment_allowlist=("PATH", "P4_5_SECRET"))


def test_malformed_jsonl_preserves_every_line_and_missing_final(tmp_path: Path) -> None:
    root, commit = repository(tmp_path)
    prompt = compiled(root, commit)

    observation = adapter("malformed").execute(prompt, binding(prompt, root))

    assert len(observation.events) == 1
    assert [item.line_number for item in observation.invalid_jsonl_lines] == [2, 3]
    assert "MALFORMED_JSONL" in observation.issues
    assert "MISSING_FINAL_OUTPUT" in observation.issues
    assert observation.exit_code == 0


def test_zero_exit_with_stderr_and_tool_failure_remains_factual(tmp_path: Path) -> None:
    root, commit = repository(tmp_path)
    prompt = compiled(root, commit)
    warning = adapter("zero-stderr").execute(prompt, binding(prompt, root))
    tool = adapter("tool-failure").execute(prompt, binding(prompt, root))

    assert warning.exit_code == 0 and "warning from fake" in warning.stderr
    assert warning.issues == ("STDERR_OBSERVED",)
    assert tool.exit_code == 0 and tool.tool_failure_observed
    assert "TOOL_FAILURE_OBSERVED" in tool.issues
    assert tool.final_output is not None


def test_nonzero_and_process_failure_after_side_effect_preserve_git_state(
    tmp_path: Path,
) -> None:
    root, commit = repository(tmp_path)
    prompt = compiled(root, commit)
    nonzero = adapter("nonzero").execute(prompt, binding(prompt, root))
    failed = adapter("process-failure-after-change").execute(prompt, binding(prompt, root))

    assert nonzero.exit_code == 7
    assert "PROCESS_EXIT_NON_ZERO" in nonzero.issues
    assert failed.exit_code == 9
    assert failed.git_after is not None and failed.git_after.clean is False
    assert failed.git_after.changed_paths == ("failed.txt",)
    assert "GIT_CHANGED_AFTER_PROCESS_FAILURE" in failed.issues
    assert "GIT_CHANGED_WITHOUT_FINAL_OUTPUT" in failed.issues
    assert (root / "failed.txt").exists()


def test_timeout_keeps_partial_side_effect_observable_without_retry(tmp_path: Path) -> None:
    root, commit = repository(tmp_path)
    prompt = compiled(root, commit)

    observation = adapter("timeout-side-effect").execute(
        prompt,
        binding(prompt, root, timeout_seconds=0.25),
    )

    assert observation.timed_out
    assert observation.exit_code is not None and observation.exit_code != 0
    assert observation.git_after is not None and observation.git_after.clean is False
    assert observation.git_after.changed_paths == ("partial.txt",)
    assert "PROCESS_TIMED_OUT" in observation.issues
    assert "GIT_CHANGED_WITHOUT_FINAL_OUTPUT" in observation.issues
    assert (root / "partial.txt").exists()


def test_explicit_cancellation_interrupts_process_and_observes_repository(
    tmp_path: Path,
) -> None:
    root, commit = repository(tmp_path)
    prompt = compiled(root, commit)
    cancellation = threading.Event()
    timer = threading.Timer(2.0, cancellation.set)
    timer.start()
    try:
        observation = adapter("sleep").execute(
            prompt,
            binding(prompt, root, timeout_seconds=8),
            cancellation=cancellation,
        )
    finally:
        timer.cancel()

    assert observation.interrupted
    assert not observation.timed_out
    assert "PROCESS_INTERRUPTED" in observation.issues
    assert observation.git_after is not None and observation.git_after.clean is True


def test_git_drift_before_launch_blocks_and_after_launch_is_observed(tmp_path: Path) -> None:
    root, commit = repository(tmp_path)
    prompt = compiled(root, commit)
    (root / "dirty-before.txt").write_text("dirty\n", encoding="utf-8")
    before = adapter().execute(prompt, binding(prompt, root))
    (root / "dirty-before.txt").unlink()
    after = adapter("drift-after").execute(prompt, binding(prompt, root))

    assert before.process_id is None
    assert before.git_before is not None and before.git_before.clean is False
    assert before.git_before.changed_paths == ("dirty-before.txt",)
    assert before.issues == ("GIT_NOT_CLEAN_BEFORE",)
    assert after.process_id is not None
    assert after.git_after is not None and after.git_after.clean is False
    assert after.git_after.changed_paths == ("drift.txt",)
    assert "GIT_STATE_CHANGED" in after.issues


def test_clean_head_drift_before_launch_is_refused(tmp_path: Path) -> None:
    root, commit = repository(tmp_path)
    prompt = compiled(root, commit)
    (root / "new-head.txt").write_text("new head\n", encoding="utf-8")
    git(root, "add", "new-head.txt")
    git(root, "commit", "-m", "test: drift head")

    observation = adapter().execute(prompt, binding(prompt, root))

    assert observation.process_id is None
    assert observation.git_before is not None
    assert observation.git_before.clean is True
    assert observation.git_before.head_commit != commit
    assert observation.issues == ("GIT_HEAD_MISMATCH_BEFORE",)


def test_git_observation_failure_after_execution_is_unknown_not_clean(
    tmp_path: Path,
) -> None:
    root, commit = repository(tmp_path)
    prompt = compiled(root, commit)

    observation = adapter("git-observation-failure").execute(prompt, binding(prompt, root))

    assert observation.git_after is not None
    assert observation.git_after.head_commit is None
    assert observation.git_after.clean is None
    assert observation.git_after.error is not None
    assert "GIT_OBSERVATION_AFTER_FAILED" in observation.issues


def test_large_stderr_is_bounded_and_marked(tmp_path: Path) -> None:
    root, commit = repository(tmp_path)
    prompt = compiled(root, commit)

    observation = adapter("huge-stderr", maximum=1024).execute(
        prompt, binding(prompt, root)
    )

    assert len(observation.stderr) == 1024
    assert observation.stderr_truncated
    assert "STDERR_TRUNCATED" in observation.issues


def test_spawn_failure_is_returned_as_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, commit = repository(tmp_path)
    prompt = compiled(root, commit)

    real_popen = subprocess.Popen

    def fail(*args: object, **kwargs: object):
        command = args[0]
        if isinstance(command, list) and "exec" in command:
            raise OSError("controlled spawn failure")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fail)
    observation = adapter().execute(prompt, binding(prompt, root))

    assert observation.process_id is None
    assert observation.started_at is not None
    assert observation.issues == ("PROCESS_SPAWN_FAILED:OSError",)
