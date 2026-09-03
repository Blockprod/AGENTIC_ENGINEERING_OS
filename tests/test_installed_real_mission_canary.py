"""Protected end-to-end mission canary executed through the installed wheel."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


pytestmark = [
    pytest.mark.real_codex,
    pytest.mark.clean_room,
    pytest.mark.skipif(
        os.environ.get("AGENTIC_OS_RUN_INSTALLED_MISSION_CANARY") != "1",
        reason="installed real mission canary requires explicit opt-in",
    ),
]


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _invoke(
    python: Path,
    environment: dict[str, str],
    root: Path,
    *arguments: str,
    timeout: int = 120,
) -> tuple[int, dict[str, object], str]:
    result = subprocess.run(
        [
            str(python),
            "-m",
            "agentic_engineering_os",
            *arguments,
            "--repository",
            str(root),
            "--json",
        ],
        cwd=root,
        env=environment,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )
    output = result.stdout if result.stdout else result.stderr
    assert "Traceback" not in output
    return result.returncode, json.loads(output), output


def test_installed_wheel_completes_real_codex_mission(tmp_path: Path) -> None:
    installed = os.environ.get("P79_INSTALLED_PYTHON")
    if not installed:
        pytest.skip("installed wheel interpreter was not supplied")
    python = Path(installed).resolve(strict=True)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONNOUSERSITE"] = "1"

    root = tmp_path / "external-python-repository"
    package = root / "src" / "canary_project"
    package.mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "README.md").write_text("# External mission canary\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\n"
        'name = "canary-project"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.11"\n\n'
        "[tool.pytest.ini_options]\n"
        'pythonpath = ["src"]\n'
        'testpaths = ["tests"]\n',
        encoding="utf-8",
    )
    (package / "__init__.py").write_text("\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Installed Mission Canary")
    _git(root, "config", "user.email", "canary@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "test: external baseline")

    desired = tmp_path / "configuration.json"
    desired.write_text(
        json.dumps(
            {
                "codex_constraints": {
                    "approval_policy": "never",
                    "maximum_parallel_executions": 1,
                    "maximum_sandbox": "workspace-write",
                    "require_clean_git": True,
                },
                "config_version": "1.0",
                "context_sources": ["README.md"],
                "gate_policies": [
                    {
                        "aggregation": "ALL_REQUIRED_PASS",
                        "policy_id": "tests",
                        "repository_dependent": True,
                        "required": True,
                        "verification_command_ids": ["tests"],
                    }
                ],
                "mission_state_git_policy": "IGNORED",
                "path_policy": {
                    "allowed_paths": ["src/canary_project", "tests"],
                    "forbidden_paths": [],
                    "protected_paths": ["pyproject.toml"],
                },
                "project_id": "installed-mission-canary",
                "repository_root_policy": "CONFIG_PARENT_GIT_ROOT",
                "toolchains": [
                    {"identity": "python", "version_constraint": ">=3.11"}
                ],
                "verification_commands": [
                    {
                        "args": ["-m", "pytest", "-q"],
                        "command_id": "tests",
                        "cwd": ".",
                        "cwd_policy": "REPOSITORY_RELATIVE",
                        "executable": "python",
                        "kind": "TEST",
                        "required": True,
                    }
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    code, payload, output = _invoke(
        python,
        environment,
        root,
        "init",
        "--configuration",
        str(desired),
        "--apply",
    )
    assert code == 0, output
    assert payload["status"] == "ADOPTED"
    _git(root, "add", ".")
    _git(root, "commit", "-m", "chore: adopt agentic engineering os")
    code, payload, output = _invoke(
        python,
        environment,
        root,
        "init",
        "--configuration",
        str(desired),
        "--apply",
        "--confirmed-by",
        "Human/InstalledMissionCanary",
    )
    assert code == 0, output
    assert payload["status"] == "ADOPTED"

    objective = (
        "Plan exactly one user story. Add src/canary_project/arithmetic.py with a "
        "typed add(left: int, right: int) -> int function returning the sum, and add "
        "tests/test_arithmetic.py proving positive, negative, and zero cases. Do not "
        "change pyproject.toml or any file outside src/canary_project and tests."
    )
    code, mission, output = _invoke(
        python,
        environment,
        root,
        "mission",
        "run",
        "--objective",
        objective,
        "--scope",
        "src/canary_project",
        "--scope",
        "tests",
        "--verification-command",
        "tests",
        timeout=3600,
    )
    assert code == 0, output
    assert mission["status"] == "COMPLETED", output
    assert mission["completed_story_ids"]
    assert not mission["current_story_ids"]
    assert not mission["blockers"]
    mission_id = str(mission["mission_id"])

    status_code, status, status_output = _invoke(
        python,
        environment,
        root,
        "mission",
        "status",
        "--mission-id",
        mission_id,
    )
    assert status_code == 0, status_output
    assert status["status"] == "COMPLETED"
    assert status["repository_head"] == _git(root, "rev-parse", "HEAD").casefold()
    assert _git(root, "status", "--porcelain=v1") == ""
    assert (root / "src" / "canary_project" / "arithmetic.py").is_file()
    assert (root / "tests" / "test_arithmetic.py").is_file()
