"""Offline fake for the minimal codex exec transport used by P4.5 tests."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)


def main() -> int:
    arguments = sys.argv[1:]
    mode = "normal"
    result_file = None
    parallel_barrier = None
    parallel_size = None
    delay_seconds = 0.0
    if "--fake-mode" in arguments:
        index = arguments.index("--fake-mode")
        mode = arguments[index + 1]
        del arguments[index : index + 2]
    if "--fake-result-file" in arguments:
        index = arguments.index("--fake-result-file")
        result_file = Path(arguments[index + 1])
        del arguments[index : index + 2]
    if "--fake-parallel-barrier" in arguments:
        index = arguments.index("--fake-parallel-barrier")
        parallel_barrier = Path(arguments[index + 1])
        del arguments[index : index + 2]
    if "--fake-parallel-size" in arguments:
        index = arguments.index("--fake-parallel-size")
        parallel_size = int(arguments[index + 1])
        del arguments[index : index + 2]
    if "--fake-delay" in arguments:
        index = arguments.index("--fake-delay")
        delay_seconds = float(arguments[index + 1])
        del arguments[index : index + 2]
    if "--version" in arguments:
        if mode == "version-fail":
            return 3
        print("fake-codex 2.0" if mode == "bad-version" else "fake-codex 1.0")
        return 0
    if "--help" in arguments:
        if mode == "help-timeout":
            time.sleep(30)
        if mode == "help-fail":
            return 6
        if mode == "malformed-help":
            print("unparseable")
            return 0
        if "resume" in arguments:
            print("Usage: codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]")
        elif "exec" in arguments:
            help_text = "Run Codex non-interactively\nUsage: codex exec [OPTIONS] [PROMPT]\nInitial instructions read from stdin with -\n-C, --cd <DIR>\n--json JSONL\n--output-schema <FILE>\n--sandbox <SANDBOX_MODE> read-only workspace-write\n--ask-for-approval never"
            if mode == "missing-output-schema":
                help_text = help_text.replace("--output-schema <FILE>\n", "")
            if mode == "missing-workspace-sandbox":
                help_text = help_text.replace(" workspace-write", "")
            print(help_text)
        else:
            help_text = "Usage: codex [OPTIONS] <COMMAND>\n-C, --cd <DIR>\n--sandbox read-only workspace-write\n--ask-for-approval never"
            if mode == "missing-workspace-sandbox":
                help_text = help_text.replace(" workspace-write", "")
            print(help_text)
        return 0

    if "exec" not in arguments or not arguments or arguments[-1] != "-":
        print("invalid invocation", file=sys.stderr)
        return 64
    if "-C" not in arguments:
        print("missing cwd", file=sys.stderr)
        return 64
    cwd = Path(arguments[arguments.index("-C") + 1])
    os.chdir(cwd)
    prompt = sys.stdin.read()

    if (
        prompt.startswith("Read the repository instruction file")
        or prompt.startswith("Use the command execution tool")
        or prompt.startswith("Use the workspace editing tool")
    ):
        emit({"type": "thread.started", "thread_id": "11111111-2222-3333-4444-555555555555"})
        emit({"type": "turn.started"})
        if prompt.startswith("Read the repository instruction file"):
            match = re.search(r"repository marker when explicitly requested: ([0-9a-f]{64})", Path("AGENTS.md").read_text(encoding="utf-8"))
            if match is None:
                return 68
            observed = match.group(1)
        elif mode == "tool-failure" and prompt.startswith("Use the command execution tool"):
            emit({
                "type": "item.completed",
                "item": {"id": "probe-command", "type": "command_execution", "status": "failed", "command": "blocked"},
            })
            observed = "blocked"
        elif prompt.startswith("Use the command execution tool"):
            match = re.search(r"python -c .*?([0-9a-f]{64})", prompt)
            if match is None:
                return 68
            observed = match.group(1)
            command = (
                f'powershell -Command "python -c print(\'{observed}\')"'
                if mode == "probe-shell-wrapper"
                else f'python -c "print(\'{observed}\')"'
            )
            emit({
                "type": "item.completed",
                "item": {"id": "probe-command", "type": "command_execution", "status": "completed", "exit_code": 0, "command": command},
            })
        else:
            match = re.search(r"with exactly `([0-9a-f]{64})`", prompt)
            if match is None:
                return 68
            observed = match.group(1)
            target = (
                Path("wrong-target.txt")
                if mode == "probe-wrong-edit-target"
                else Path("operational-edit-proof.txt")
            )
            target.write_text(observed + "\n", encoding="utf-8")
            if mode == "probe-extra-edit-file":
                Path("extra.txt").write_text("unexpected\n", encoding="utf-8")
            emit({
                "type": "item.completed",
                "item": {"id": "probe-command", "type": "command_execution", "status": "completed", "exit_code": 0},
            })
        terminal_text = (
            "not-json"
            if mode == "probe-invalid-edit-terminal"
            and prompt.startswith("Use the workspace editing tool")
            else json.dumps({"observed": observed}, sort_keys=True)
        )
        emit({
            "type": "item.completed",
            "item": {
                "id": "probe-result",
                "type": "agent_message",
                "text": terminal_text,
            },
        })
        emit({"type": "turn.completed"})
        return 0

    emit({"type": "thread.started", "thread_id": "11111111-2222-3333-4444-555555555555"})
    emit({"type": "turn.started"})
    if mode in {
        "role-result",
        "role-result-side-effect",
        "role-result-tool-failure",
        "role-result-forbidden-side-effect",
        "role-result-invalid-side-effect",
        "role-result-parallel",
    }:
        if result_file is None or not result_file.is_file():
            print("fake RoleResult file is absent", file=sys.stderr)
            return 65
        payload = result_file.read_text(encoding="utf-8")
        if mode == "role-result-parallel":
            if parallel_barrier is None or parallel_size is None or parallel_size < 1:
                print("fake parallel barrier is invalid", file=sys.stderr)
                return 66
            parallel_barrier.mkdir(parents=True, exist_ok=True)
            marker = parallel_barrier / f"{os.getpid()}.started"
            marker.write_text(str(Path.cwd()), encoding="utf-8")
            deadline = time.monotonic() + 10.0
            while len(tuple(parallel_barrier.glob("*.started"))) < parallel_size:
                if time.monotonic() >= deadline:
                    print("fake parallel barrier timed out", file=sys.stderr)
                    return 67
                time.sleep(0.02)
            time.sleep(delay_seconds)
        if mode in {"role-result-side-effect", "role-result-parallel"}:
            value = json.loads(payload)
            paths = value.get("files_changed", value.get("test_files_changed", []))
            for relative in paths:
                target = Path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("fake Codex side effect\n", encoding="utf-8")
        if mode == "role-result-forbidden-side-effect":
            Path("forbidden.txt").write_text("forbidden side effect\n", encoding="utf-8")
        if mode == "role-result-invalid-side-effect":
            target = Path("src/feature.py")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("invalid result side effect\n", encoding="utf-8")
        if mode == "role-result-tool-failure":
            emit(
                {
                    "type": "item.completed",
                    "item": {"id": "role-tool", "type": "command_execution", "status": "failed"},
                }
            )
        emit(
            {
                "type": "item.completed",
                "item": {"id": "role-result", "type": "agent_message", "text": payload},
            }
        )
        emit({"type": "turn.completed"})
        return 0
    if mode == "malformed":
        print("not-json", flush=True)
        print('{"type":"duplicate","type":"forged"}', flush=True)
        return 0
    if mode == "missing-final":
        return 0
    if mode == "timeout-side-effect":
        Path("partial.txt").write_text("partial\n", encoding="utf-8")
        time.sleep(30)
        return 0
    if mode == "sleep":
        time.sleep(30)
        return 0
    if mode == "process-failure-after-change":
        Path("failed.txt").write_text("failed\n", encoding="utf-8")
        print("fake process failed", file=sys.stderr, flush=True)
        return 9
    if mode == "git-observation-failure":
        Path(".git").rename(".git-hidden")
    if mode == "drift-after":
        Path("drift.txt").write_text("drift\n", encoding="utf-8")
    if mode in {"tool-failure", "execution-tool-failure"}:
        emit(
            {
                "type": "item.completed",
                "item": {
                    "id": "tool-failure",
                    "type": "command_execution",
                    "status": "failed",
                    "message": "sandbox blocked command",
                },
            }
        )
    if mode == "huge-stderr":
        print("E" * 20_000, file=sys.stderr, flush=True)
    if mode == "zero-stderr":
        print("warning from fake", file=sys.stderr, flush=True)
    if mode == "nonzero":
        print("non-zero from fake", file=sys.stderr, flush=True)
        return 7

    emit(
        {
            "type": "item.completed",
            "item": {
                "id": "final-result",
                "type": "agent_message",
                "text": json.dumps(
                    {
                        "args": arguments,
                        "cwd": str(Path.cwd()),
                        "prompt": prompt,
                        "secret_present": "P4_5_SECRET" in os.environ,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
        }
    )
    emit({"type": "turn.completed"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
