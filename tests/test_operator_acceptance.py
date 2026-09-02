from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_engineering_os import cli
from agentic_engineering_os.application import ExistingRepositoryAdoption
from agentic_engineering_os.infrastructure import ProjectConfigurationValidator
from test_existing_repository_adoption import (
    adopt,
    configuration,
    existing_repository,
    git,
)


def invoke(capsys, *arguments: str, json_output: bool = True):
    suffix = ("--json",) if json_output else ()
    code = cli.main([*arguments, *suffix])
    captured = capsys.readouterr()
    text = captured.out if captured.out else captured.err
    return code, json.loads(text), text


def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "desired.json"
    path.write_text(
        ProjectConfigurationValidator().serialize(configuration()), encoding="utf-8"
    )
    return path


def test_help_makes_canonical_entrypoint_and_command_modes_discoverable() -> None:
    help_result = subprocess.run(
        [sys.executable, "-m", "agentic_engineering_os", "--help"],
        shell=False, capture_output=True, text=True, check=False,
    )
    assert help_result.returncode == 0
    assert "Canonical entrypoint: python -m agentic_engineering_os" in help_result.stdout
    assert "optional 'agentic-os' shim" in help_result.stdout
    for command in ("inspect", "status", "plan", "health", "metrics", "incidents", "diagnose"):
        assert f"{command}" in help_result.stdout
        result = subprocess.run(
            [sys.executable, "-m", "agentic_engineering_os", command, "--help"],
            shell=False, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0
        assert "read-only" in result.stdout.lower() or command in {"health", "metrics", "incidents", "diagnose"}
    for command in ("init", "upgrade"):
        result = subprocess.run(
            [sys.executable, "-m", "agentic_engineering_os", command, "--help"],
            shell=False, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0
        assert "dry-run" in result.stdout.lower() and "MUTATING" in result.stdout


def test_status_exposes_deterministic_next_action_and_read_only_mode(
    tmp_path: Path, capsys
) -> None:
    root = existing_repository(tmp_path)
    code, payload, _ = invoke(capsys, "status", "--repository", str(root))
    assert code == 2 and payload["status"] == "NEEDS_CONFIGURATION"
    assert payload["mode"] == "READ_ONLY"
    assert "ProjectConfiguration" in payload["next_action"]
    assert payload["authority_notice"] == "OPERATOR_GUIDANCE_ONLY_NOT_AUTHORIZATION"
    assert payload["confirmations"] == []


def test_human_confirmation_projection_is_exact_and_actionable(
    tmp_path: Path, capsys
) -> None:
    root = existing_repository(tmp_path, agents="# User rules\n")
    desired = config_file(tmp_path)
    code, payload, _ = invoke(
        capsys, "plan", "--repository", str(root),
        "--configuration", str(desired),
    )
    assert code == 0 and payload["status"] == "NEEDS_HUMAN_CONFIRMATION"
    confirmations = payload["confirmations"]
    assert confirmations
    assert len({item["id"] for item in confirmations}) == len(confirmations)
    for item in confirmations:
        assert item["operation"] and item["target"]
        assert item["reason"] and item["consequence"]
        assert item["id"] in item["usage"] and "Human/<identity>" in item["usage"]
    assert "apply explicitly" in payload["next_action"]


def test_init_and_upgrade_modes_distinguish_dry_run_from_apply(
    tmp_path: Path, capsys
) -> None:
    root = existing_repository(tmp_path)
    desired = config_file(tmp_path)
    dry_code, dry, _ = invoke(
        capsys, "init", "--repository", str(root),
        "--configuration", str(desired),
    )
    assert dry_code == 0 and dry["mode"] == "DRY_RUN"
    assert "--apply" in dry["next_action"]
    apply_code, applied, _ = invoke(
        capsys, "init", "--repository", str(root),
        "--configuration", str(desired), "--apply",
    )
    assert apply_code == 0 and applied["mode"] == "APPLY_RESULT"
    assert applied["status"] == "ADOPTED"
    git(root, "add", ".")
    git(root, "commit", "-m", "adopt")
    upgrade_code, upgrade, _ = invoke(
        capsys, "upgrade", "--repository", str(root)
    )
    assert upgrade_code == 0 and upgrade["mode"] == "DRY_RUN"
    assert upgrade["status"] == "ALREADY_CURRENT"


def test_compact_and_human_output_retain_identical_semantics(
    tmp_path: Path, capsys
) -> None:
    root = existing_repository(tmp_path)
    _, compact, compact_text = invoke(
        capsys, "status", "--repository", str(root)
    )
    _, human, human_text = invoke(
        capsys, "status", "--repository", str(root), json_output=False
    )
    assert compact == human
    assert "\n" not in compact_text.strip()
    assert "\n" in human_text.strip()
    for key in ("status", "result", "next_action", "confirmations", "mode"):
        assert key in compact


@pytest.mark.parametrize(
    ("status", "fragment"),
    (
        ("PARTIAL_OR_INCONSISTENT", "Do not apply automatically"),
        ("UPGRADE_REQUIRED", "without --apply"),
        ("BLOCKED", "blockers"),
        ("UNKNOWN", "diagnose"),
        ("FROZEN", "Do not start new work"),
        ("RECOVERY_REQUIRED", "operator recovery"),
        ("ATTENTION_REQUIRED", "incidents"),
    ),
)
def test_major_failure_and_recovery_states_have_safe_next_actions(status, fragment) -> None:
    action = cli._next_action("status", status, [])
    assert fragment in action
    assert "automatically recover" not in action.lower()


def test_expected_failure_has_no_traceback_or_secret_and_rejects_yes(
    tmp_path: Path, capsys
) -> None:
    root = existing_repository(tmp_path)
    secret = "P79-SECRET-MUST-NOT-ECHO"
    invalid = tmp_path / "invalid.json"
    invalid.write_text(f'{{"password":"{secret}",broken\n', encoding="utf-8")
    code, payload, text = invoke(
        capsys, "plan", "--repository", str(root),
        "--configuration", str(invalid),
    )
    assert code == 2 and payload["status"] == "BLOCKED"
    assert payload["mode"] == "READ_ONLY"
    assert payload["next_action"]
    assert "Traceback" not in text and secret not in text
    with pytest.raises(SystemExit) as rejected:
        cli.main(["init", "--repository", str(root), "--yes"])
    assert rejected.value.code == 2


def test_fake_codex_human_is_refused_without_bypass_guidance(
    tmp_path: Path, capsys
) -> None:
    root = existing_repository(tmp_path, agents="# Existing rules\n")
    desired = config_file(tmp_path)
    _, planned, _ = invoke(
        capsys, "init", "--repository", str(root),
        "--configuration", str(desired),
    )
    arguments: list[str] = []
    for item in planned["confirmations"]:
        arguments.extend(("--confirm", item["id"]))
    code, refused, text = invoke(
        capsys, "init", "--repository", str(root),
        "--configuration", str(desired), "--apply", *arguments,
        "--confirmed-by", "CoDeX/FakeHuman",
    )
    assert code == 2 and refused["status"] == "BLOCKED"
    assert refused["mode"] == "APPLY_ATTEMPT"
    assert "--yes" not in refused["next_action"]
    assert "Traceback" not in text


def test_authority_language_remains_exact(tmp_path: Path, capsys) -> None:
    root = existing_repository(tmp_path)
    _, payload, text = invoke(capsys, "status", "--repository", str(root))
    assert payload["authority_notice"] == "OPERATOR_GUIDANCE_ONLY_NOT_AUTHORIZATION"
    assert "CERTIFIED" not in text and "AUTHORIZED" not in text
    assert cli._next_action("health", "HEALTHY", []).endswith(
        "HEALTHY is not Certification."
    )


@pytest.fixture(scope="module")
def installed_python() -> tuple[Path, dict[str, str]]:
    value = os.environ.get("P79_INSTALLED_PYTHON")
    if not value:
        pytest.skip("P7.9 installed-product environment was not supplied")
    python = Path(value).resolve(strict=True)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONNOUSERSITE"] = "1"
    return python, environment


def installed_invoke(installed_python, root: Path, command: str, *arguments: str):
    python, environment = installed_python
    result = subprocess.run(
        [str(python), "-m", "agentic_engineering_os", command,
         "--repository", str(root), *arguments, "--json"],
        cwd=root, env=environment, shell=False,
        capture_output=True, text=True, check=False,
    )
    text = result.stdout if result.stdout else result.stderr
    return result.returncode, json.loads(text), text


def test_installed_first_run_existing_and_diagnostic_journeys(
    tmp_path: Path, installed_python
) -> None:
    root = existing_repository(tmp_path)
    desired = config_file(tmp_path)
    before = (root / "README.md").read_bytes()
    assert installed_invoke(installed_python, root, "inspect")[0] == 0
    status_code, status, _ = installed_invoke(installed_python, root, "status")
    assert status_code == 2 and status["status"] == "NEEDS_CONFIGURATION"
    dry_code, dry, _ = installed_invoke(
        installed_python, root, "init", "--configuration", str(desired)
    )
    assert dry_code == 0 and dry["mode"] == "DRY_RUN"
    apply_code, applied, _ = installed_invoke(
        installed_python, root, "init", "--configuration", str(desired), "--apply"
    )
    assert apply_code == 0 and applied["status"] == "ADOPTED"
    assert (root / "README.md").read_bytes() == before
    git(root, "add", ".")
    git(root, "commit", "-m", "adopted")
    adopted_code, adopted, _ = installed_invoke(installed_python, root, "status")
    assert adopted_code == 0 and adopted["status"] == "ADOPTED"
    diagnose_code, diagnose, text = installed_invoke(installed_python, root, "diagnose")
    assert diagnose_code == 2
    assert diagnose["status"] == "ATTENTION_REQUIRED"
    assert diagnose["next_action"] and "Traceback" not in text
