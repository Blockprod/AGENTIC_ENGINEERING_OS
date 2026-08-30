"""Offline fake for the minimal codex exec transport used by P4.5 tests."""

from __future__ import annotations

import json
import os
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

    if "exec" not in arguments or not arguments or arguments[-1] != "-":
        print("invalid invocation", file=sys.stderr)
        return 64
    if "-C" not in arguments:
        print("missing cwd", file=sys.stderr)
        return 64
    cwd = Path(arguments[arguments.index("-C") + 1])
    os.chdir(cwd)
    prompt = sys.stdin.read()

    emit({"type": "thread.started", "thread_id": "11111111-2222-3333-4444-555555555555"})
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
                    "item": {"type": "command_execution", "status": "failed"},
                }
            )
        emit(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": payload},
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
    if mode == "tool-failure":
        emit(
            {
                "type": "item.completed",
                "item": {
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
