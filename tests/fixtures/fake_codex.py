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
    if "--fake-mode" in arguments:
        index = arguments.index("--fake-mode")
        mode = arguments[index + 1]
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
