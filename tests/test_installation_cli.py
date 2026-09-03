from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from agentic_engineering_os import cli
from agentic_engineering_os.application import ContractValidator
from agentic_engineering_os.domain import AGENTS_MANAGED_SECTION
from agentic_engineering_os.infrastructure import ProjectConfigurationValidator
from agentic_engineering_os.resources.product import (
    product_resource_path,
    product_resource_text,
)
from test_existing_repository_adoption import configuration, existing_repository, git
from test_repository_upgrade import _AGENTS_V1


def invoke(capsys, *arguments: str):
    code = cli.main([*arguments, "--json"])
    captured = capsys.readouterr()
    output = captured.out if captured.out else captured.err
    return code, json.loads(output)


def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "desired-config.json"
    path.write_text(
        ProjectConfigurationValidator().serialize(configuration()), encoding="utf-8"
    )
    return path


def visible_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def required_ids(payload: dict[str, object]) -> list[str]:
    result = payload["result"]
    assert isinstance(result, dict)
    initialization = result.get("initialization_plan")
    if isinstance(initialization, dict):
        operations = initialization["operations"]
        assert isinstance(operations, list)
        return [
            str(item["operation_id"])
            for item in operations
            if isinstance(item, dict) and item["human_confirmation_required"] is True
        ]
    values = result["required_human_confirmations"]
    assert isinstance(values, list)
    return [str(item) for item in values]


def confirmation_arguments(identifiers: list[str], producer: str) -> list[str]:
    arguments: list[str] = []
    for identifier in identifiers:
        arguments.extend(("--confirm", identifier))
    arguments.extend(("--confirmed-by", producer))
    return arguments


def test_help_and_package_resources_are_available() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "agentic_engineering_os", "--help"],
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "inspect" in result.stdout
    assert "upgrade" in result.stdout
    assert ContractValidator().validate("user-story", {}).errors
    assert product_resource_path("schemas/architect-result.schema.json").is_file()
    assert product_resource_path("schemas/architect-result.codex.schema.json").is_file()
    assert "Architect" in product_resource_text("roles/architect.md")
    with pytest.raises(Exception):
        product_resource_text("../README.md")


def test_module_and_console_entrypoints_share_cli_semantics(tmp_path: Path) -> None:
    root = existing_repository(tmp_path)
    before = visible_files(root)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    module = subprocess.run(
        [
            sys.executable,
            "-m",
            "agentic_engineering_os",
            "inspect",
            "--repository",
            str(root),
            "--json",
        ],
        env=environment,
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    executable = Path(sys.executable).with_name(
        "agentic-os.exe" if os.name == "nt" else "agentic-os"
    )
    if not executable.is_file():
        return
    try:
        console = subprocess.run(
            [str(executable), "inspect", "--repository", str(root), "--json"],
            env=environment,
            shell=False,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        if getattr(error, "winerror", None) == 4551:
            pytest.skip("host policy blocks the optional console-script shim")
        raise
    assert module.returncode == console.returncode == 0
    assert json.loads(module.stdout) == json.loads(console.stdout)
    assert module.stderr == console.stderr == ""
    assert visible_files(root) == before


def test_inspect_status_and_plan_are_read_only(tmp_path: Path, capsys) -> None:
    root = existing_repository(tmp_path)
    desired = config_file(tmp_path)
    before = visible_files(root)

    inspect_code, inspected = invoke(
        capsys, "inspect", "--repository", str(root)
    )
    status_code, status = invoke(capsys, "status", "--repository", str(root))
    plan_code, plan = invoke(
        capsys,
        "plan",
        "--repository",
        str(root),
        "--configuration",
        str(desired),
    )

    assert inspect_code == cli.EXIT_SUCCESS
    assert inspected["status"] == "SUPPORTED"
    assert status_code == cli.EXIT_BLOCKED
    assert status["status"] == "NEEDS_CONFIGURATION"
    assert plan_code == cli.EXIT_SUCCESS
    assert plan["status"] == "READY_TO_APPLY"
    assert plan["result"]["initialization_plan"]["operations"]
    assert visible_files(root) == before


def test_init_requires_exact_human_and_is_idempotent(tmp_path: Path, capsys) -> None:
    user_agents = "# User rules\n\nKeep this text.\n"
    root = existing_repository(tmp_path, agents=user_agents)
    desired = config_file(tmp_path)
    common = (
        "init",
        "--repository",
        str(root),
        "--configuration",
        str(desired),
    )
    plan_code, plan = invoke(capsys, *common)
    identifiers = required_ids(plan)
    before = visible_files(root)

    missing_code, missing = invoke(capsys, *common, "--apply")
    fake_code, fake = invoke(
        capsys,
        *common,
        "--apply",
        *confirmation_arguments(identifiers, "cOdEx/FakeHuman"),
    )

    assert plan_code == cli.EXIT_SUCCESS
    assert plan["status"] == "NEEDS_HUMAN_CONFIRMATION"
    assert identifiers
    assert missing_code == cli.EXIT_BLOCKED
    assert missing["status"] == "BLOCKED"
    assert fake_code == cli.EXIT_BLOCKED
    assert fake["status"] == "BLOCKED"
    assert visible_files(root) == before

    applied_code, applied = invoke(
        capsys,
        *common,
        "--apply",
        *confirmation_arguments(identifiers, "Human/Alice"),
    )
    assert applied_code == cli.EXIT_SUCCESS
    assert applied["status"] == "ADOPTED"
    assert (root / "AGENTS.md").read_text(encoding="utf-8").startswith(user_agents)

    status_code, status = invoke(capsys, "status", "--repository", str(root))
    second_code, second = invoke(capsys, "init", "--repository", str(root), "--apply")
    assert status_code == cli.EXIT_SUCCESS
    assert status["status"] == "ADOPTED"
    assert second_code == cli.EXIT_SUCCESS
    assert second["status"] == "ADOPTED"


def test_missing_config_partial_state_and_traversal_block(tmp_path: Path, capsys) -> None:
    root = existing_repository(tmp_path)
    code, payload = invoke(capsys, "init", "--repository", str(root), "--apply")
    assert code == cli.EXIT_BLOCKED
    assert payload["status"] == "NEEDS_CONFIGURATION"

    partial = existing_repository(tmp_path / "partial")
    runtime = partial / ".agentic-engineering-os"
    runtime.mkdir()
    (runtime / "state.json").write_text("{}\n", encoding="utf-8")
    partial_code, partial_payload = invoke(
        capsys, "status", "--repository", str(partial)
    )
    assert partial_code == cli.EXIT_BLOCKED
    assert partial_payload["status"] == "PARTIAL_OR_INCONSISTENT"

    monkeypatch_cwd = tmp_path / "cwd"
    monkeypatch_cwd.mkdir()
    previous = Path.cwd()
    try:
        os.chdir(monkeypatch_cwd)
        traversal_code, traversal = invoke(
            capsys, "inspect", "--repository", ".."
        )
    finally:
        os.chdir(previous)
    assert traversal_code == cli.EXIT_BLOCKED
    assert traversal["result"]["code"] == "UNSAFE_REPOSITORY_PATH"


def test_upgrade_is_explicit_and_human_authority_is_preserved(
    tmp_path: Path, capsys
) -> None:
    root = existing_repository(tmp_path, agents=_AGENTS_V1)
    source = (root / "AGENTS.md").read_bytes()

    inspected_code, _ = invoke(capsys, "inspect", "--repository", str(root))
    plan_code, plan = invoke(capsys, "upgrade", "--repository", str(root))
    identifiers = required_ids(plan)
    assert inspected_code == cli.EXIT_SUCCESS
    assert plan_code == cli.EXIT_SUCCESS
    assert plan["status"] == "NEEDS_HUMAN_CONFIRMATION"
    assert (root / "AGENTS.md").read_bytes() == source

    fake_code, fake = invoke(
        capsys,
        "upgrade",
        "--repository",
        str(root),
        "--apply",
        *confirmation_arguments(identifiers, "CODEX/FakeHuman"),
    )
    assert fake_code == cli.EXIT_BLOCKED
    assert fake["status"] == "REFUSED"
    assert (root / "AGENTS.md").read_bytes() == source

    migrated_code, migrated = invoke(
        capsys,
        "upgrade",
        "--repository",
        str(root),
        "--apply",
        *confirmation_arguments(identifiers, "Human/Alice"),
    )
    assert migrated_code == cli.EXIT_SUCCESS
    assert migrated["status"] == "MIGRATED"
    migrated_text = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert AGENTS_MANAGED_SECTION.rstrip() in migrated_text


def test_unknown_upgrade_and_yes_flag_fail_closed(tmp_path: Path, capsys) -> None:
    root = existing_repository(tmp_path)
    runtime = root / ".agentic-engineering-os"
    runtime.mkdir()
    (runtime / "executions.json").write_text(
        '{"schema_version":"99.0","records":[]}\n', encoding="utf-8"
    )
    code, payload = invoke(capsys, "upgrade", "--repository", str(root))
    assert code == cli.EXIT_BLOCKED
    assert payload["status"] == "BLOCKED"

    with pytest.raises(SystemExit) as rejected:
        cli.main(["init", "--repository", str(root), "--yes"])
    assert rejected.value.code == 2


def test_stale_init_is_refused_and_command_like_identity_remains_inert(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = existing_repository(tmp_path)
    desired = config_file(tmp_path)
    original = cli.ExistingRepositoryAdoption.apply_adoption

    def stale_apply(service, preparation, *, human_confirmations=()):
        (root / "README.md").write_text("changed after planning\n", encoding="utf-8")
        return original(
            service,
            preparation,
            human_confirmations=human_confirmations,
        )

    monkeypatch.setattr(
        cli.ExistingRepositoryAdoption, "apply_adoption", stale_apply
    )
    code, payload = invoke(
        capsys,
        "init",
        "--repository",
        str(root),
        "--configuration",
        str(desired),
        "--apply",
    )
    assert code == cli.EXIT_BLOCKED
    assert payload["status"] == "BLOCKED"
    assert not (root / ".agentic-engineering-os/config.json").exists()

    protected = tmp_path / "must-not-exist"
    current = existing_repository(tmp_path / "identity", agents="# User rules\n")
    plan_code, plan = invoke(
        capsys,
        "init",
        "--repository",
        str(current),
        "--configuration",
        str(desired),
    )
    identifiers = required_ids(plan)
    injected_code, injected = invoke(
        capsys,
        "init",
        "--repository",
        str(current),
        "--configuration",
        str(desired),
        "--apply",
        *confirmation_arguments(
            identifiers, f"Human/Alice;touch-{protected.name}"
        ),
    )
    assert plan_code == cli.EXIT_SUCCESS
    assert injected_code == cli.EXIT_SUCCESS
    assert injected["status"] == "ADOPTED"
    assert not protected.exists()


def test_symlink_repository_is_refused(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = existing_repository(tmp_path)
    link = tmp_path / "repository-link"
    original = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda candidate: candidate == link or original(candidate),
    )
    code, payload = invoke(capsys, "inspect", "--repository", str(link))
    assert code == cli.EXIT_BLOCKED
    assert payload["result"]["code"] == "UNSAFE_REPOSITORY_PATH"


def test_wheel_installs_and_runs_outside_source_checkout(tmp_path: Path) -> None:
    source_root = Path(__file__).parents[1]
    build_source = tmp_path / "wheel-source"
    build_source.mkdir()
    shutil.copy2(source_root / "pyproject.toml", build_source / "pyproject.toml")
    shutil.copy2(source_root / "README.md", build_source / "README.md")
    shutil.copytree(source_root / "src", build_source / "src")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(build_source),
            "--no-deps",
            "--wheel-dir",
            str(wheelhouse),
        ],
        cwd=tmp_path,
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    wheels = tuple(wheelhouse.glob("agentic_engineering_os-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        packaged = set(archive.namelist())
    for directory, pattern in (("schemas", "*.json"), ("roles", "*.md")):
        for source in (source_root / directory).glob(pattern):
            expected = (
                f"agentic_engineering_os/resources/{directory}/{source.name}"
            )
            assert expected in packaged
    assert (
        "agentic_engineering_os/resources/docs/12-codex-operating-contract.md"
        in packaged
    )

    environment = tmp_path / "installed"
    created = subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(environment)],
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    executable = environment / ("Scripts/agentic-os.exe" if os.name == "nt" else "bin/agentic-os")
    installed = subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheels[0])],
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr

    outside = tmp_path / "outside"
    outside.mkdir()
    target = existing_repository(outside)
    child_environment = os.environ.copy()
    child_environment.pop("PYTHONPATH", None)
    portable = [str(python), "-m", "agentic_engineering_os"]
    help_result = subprocess.run(
        [*portable, "--help"],
        cwd=outside,
        env=child_environment,
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    resource_result = subprocess.run(
        [
            str(python),
            "-c",
            "from agentic_engineering_os.application import ContractValidator; "
            "from agentic_engineering_os.resources.product import product_resource_path; "
            "assert ContractValidator().validate('user-story', {}).errors; "
            "print(product_resource_path('roles/architect.md'))",
        ],
        cwd=outside,
        env=child_environment,
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    inspection = subprocess.run(
        [*portable, "inspect", "--repository", str(target), "--json"],
        cwd=outside,
        env=child_environment,
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert executable.is_file()
    assert resource_result.returncode == 0, resource_result.stderr
    assert str(source_root) not in resource_result.stdout
    assert inspection.returncode == 0, inspection.stderr
    assert json.loads(inspection.stdout)["status"] == "SUPPORTED"
