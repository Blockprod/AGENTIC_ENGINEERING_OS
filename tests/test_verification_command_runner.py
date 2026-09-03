import subprocess
from io import BytesIO
from pathlib import Path

import pytest

from agentic_engineering_os.infrastructure.verification_command_runner import (
    SubprocessVerificationCommandRunner,
)


@pytest.mark.parametrize("exit_code", [0, 9])
def test_runner_executes_exact_argv_without_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
) -> None:
    calls: list[tuple[object, dict[str, object]]] = []

    class FakeProcess:
        def __init__(self):
            self.stdout = BytesIO(b"out")
            self.stderr = BytesIO(b"err")
            self.returncode = exit_code

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

    def fake_popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    argv = ("python", "-m", "pytest")

    result = SubprocessVerificationCommandRunner().run(argv, tmp_path)

    assert result.argv == argv
    assert result.exit_code == exit_code
    assert result.started
    assert calls[0][0] == list(argv)
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["cwd"] == tmp_path


def test_runner_reports_missing_executable_without_evidence_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "Popen", unavailable)

    result = SubprocessVerificationCommandRunner().run(("missing",), tmp_path)

    assert not result.started
    assert result.exit_code is None
    assert result.failure_code == "COMMAND_NOT_FOUND"
