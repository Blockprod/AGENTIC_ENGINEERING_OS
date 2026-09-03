"""Shell-free subprocess runner for authoritative verification commands."""

from __future__ import annotations

import math
import os
import subprocess
from pathlib import Path

from agentic_engineering_os.application.verification_coordinator import (
    VerificationProcessResult,
)

from .platform_environment import (
    RUNTIME_ENVIRONMENT_ALLOWLIST,
    build_bounded_environment,
)


class SubprocessVerificationCommandRunner:
    def __init__(self, *, timeout_seconds: float = 120.0) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = float(timeout_seconds)

    def run(self, argv: tuple[str, ...], cwd: Path) -> VerificationProcessResult:
        environment = build_bounded_environment(
            os.environ, RUNTIME_ENVIRONMENT_ALLOWLIST
        )
        try:
            process = subprocess.run(
                list(argv),
                shell=False,
                cwd=cwd,
                env=environment,
                capture_output=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except FileNotFoundError:
            return VerificationProcessResult(
                argv, cwd, False, None, b"", b"", "COMMAND_NOT_FOUND"
            )
        except subprocess.TimeoutExpired as error:
            return VerificationProcessResult(
                argv,
                cwd,
                True,
                None,
                error.stdout or b"",
                error.stderr or b"",
                "COMMAND_TIMEOUT",
            )
        except OSError:
            return VerificationProcessResult(
                argv, cwd, False, None, b"", b"", "COMMAND_START_FAILED"
            )
        return VerificationProcessResult(
            argv=argv,
            cwd=cwd,
            started=True,
            exit_code=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
        )
