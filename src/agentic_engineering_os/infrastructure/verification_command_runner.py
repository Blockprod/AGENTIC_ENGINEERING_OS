"""Shell-free subprocess runner for authoritative verification commands."""

from __future__ import annotations

import math
import os
import subprocess
import threading
from pathlib import Path

from agentic_engineering_os.application.verification_coordinator import (
    VerificationProcessResult,
)

from .platform_environment import (
    RUNTIME_ENVIRONMENT_ALLOWLIST,
    build_bounded_environment,
)
from .codex_runtime_adapter import _terminate_process_tree


class SubprocessVerificationCommandRunner:
    def __init__(
        self, *, timeout_seconds: float = 120.0, max_output_bytes: int = 1_000_000
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = float(timeout_seconds)
        if (
            not isinstance(max_output_bytes, int)
            or isinstance(max_output_bytes, bool)
            or max_output_bytes <= 0
        ):
            raise ValueError("max_output_bytes must be positive")
        self._max_output_bytes = max_output_bytes

    def run(self, argv: tuple[str, ...], cwd: Path) -> VerificationProcessResult:
        environment = build_bounded_environment(
            os.environ, RUNTIME_ENVIRONMENT_ALLOWLIST
        )
        try:
            process = subprocess.Popen(
                list(argv),
                shell=False,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
                start_new_session=os.name != "nt",
            )
        except FileNotFoundError:
            return VerificationProcessResult(
                argv, cwd, False, None, b"", b"", "COMMAND_NOT_FOUND"
            )
        except OSError:
            return VerificationProcessResult(
                argv, cwd, False, None, b"", b"", "COMMAND_START_FAILED"
            )
        stdout = _BoundedByteCollector(process.stdout, self._max_output_bytes)
        stderr = _BoundedByteCollector(process.stderr, self._max_output_bytes)
        readers = (
            threading.Thread(target=stdout.drain, daemon=True),
            threading.Thread(target=stderr.drain, daemon=True),
        )
        for reader in readers:
            reader.start()
        failure = None
        exit_code = None
        try:
            exit_code = process.wait(timeout=self._timeout_seconds)
        except subprocess.TimeoutExpired:
            failure = "COMMAND_TIMEOUT"
            if not _terminate_process_tree(process):
                failure = "COMMAND_TREE_TERMINATION_UNCONFIRMED"
        finally:
            for reader in readers:
                reader.join(timeout=5.0)
        stdout_value, stdout_size, stdout_truncated = stdout.result()
        stderr_value, stderr_size, stderr_truncated = stderr.result()
        return VerificationProcessResult(
            argv=argv,
            cwd=cwd,
            started=True,
            exit_code=exit_code,
            stdout=stdout_value,
            stderr=stderr_value,
            failure_code=failure,
            stdout_size=stdout_size,
            stderr_size=stderr_size,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )


class _BoundedByteCollector:
    def __init__(self, stream, maximum: int) -> None:
        self._stream = stream
        self._maximum = maximum
        self._chunks: list[bytes] = []
        self._retained = 0
        self._observed = 0

    def drain(self) -> None:
        if self._stream is None:
            return
        try:
            while True:
                chunk = self._stream.read(64 * 1024)
                if not chunk:
                    return
                self._observed += len(chunk)
                remaining = self._maximum - self._retained
                if remaining > 0:
                    retained = chunk[:remaining]
                    self._chunks.append(retained)
                    self._retained += len(retained)
        except (OSError, ValueError):
            return
        finally:
            try:
                self._stream.close()
            except (OSError, ValueError):
                pass

    def result(self) -> tuple[bytes, int, bool]:
        return b"".join(self._chunks), self._observed, self._observed > self._maximum
