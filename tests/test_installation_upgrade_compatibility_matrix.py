from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    ARTIFACT_CONTRACTS,
    PRODUCT_VERSION,
    ArtifactRequirement,
    ArtifactVersionObservation,
    CompatibilityArtifact,
    CompatibilityClassification,
    CompatibilityEvaluationContext,
    CompatibilityEvaluator,
    InstalledProduct,
)
from agentic_engineering_os.infrastructure import ProjectConfigurationValidator
from test_existing_repository_adoption import adopt, configuration, existing_repository, git
from test_repository_upgrade import (
    _AGENTS_V1,
    initialize_maintenance,
)


EXPECTED_WHEEL_SHA256 = "2940d2265f66189ffb277aa0d183e9d02263504dd72c57fb712802a07345dc48"


@pytest.fixture(scope="module")
def installed_product() -> tuple[Path, Path, dict[str, str]]:
    python_value = os.environ.get("P78_INSTALLED_PYTHON")
    wheel_value = os.environ.get("P78_WHEEL")
    if not python_value or not wheel_value:
        pytest.skip("P7.8 installed-candidate environment was not supplied")
    python = Path(python_value).resolve(strict=True)
    wheel = Path(wheel_value).resolve(strict=True)
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == EXPECTED_WHEEL_SHA256
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONNOUSERSITE"] = "1"
    probe = subprocess.run(
        [
            str(python),
            "-c",
            "import json,agentic_engineering_os as p;"
            "print(json.dumps({'version':p.__version__,'path':p.__file__},sort_keys=True))",
        ],
        cwd=wheel.parent,
        env=environment,
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    identity = json.loads(probe.stdout)
    assert identity["version"] == "0.1.0"
    assert "site-packages" in identity["path"].lower()
    return python, wheel, environment


def invoke(installed_product, root: Path, command: str, *arguments: str):
    python, _, environment = installed_product
    result = subprocess.run(
        [
            str(python), "-m", "agentic_engineering_os", command,
            "--repository", str(root), *arguments, "--json",
        ],
        cwd=root,
        env=environment,
        shell=False,
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout if result.stdout else result.stderr
    return result.returncode, json.loads(output), result


def snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def required_confirmations(payload: dict[str, object]) -> list[str]:
    return list(payload["result"]["required_human_confirmations"])


def confirmation_arguments(identifiers: list[str], producer: str) -> list[str]:
    result: list[str] = []
    for identifier in identifiers:
        result.extend(("--confirm", identifier))
    result.extend(("--confirmed-by", producer))
    return result


def write_configuration(root: Path, target: Path) -> None:
    target.write_text(
        ProjectConfigurationValidator().serialize(configuration()), encoding="utf-8"
    )


def current_repository(tmp_path: Path) -> Path:
    root = existing_repository(tmp_path)
    _, _, result = adopt(root)
    assert result.status.value == "ADOPTED"
    git(root, "add", ".")
    git(root, "commit", "-m", "adopt current Agentic OS")
    return root


def test_fresh_install_inspect_status_and_plan_are_read_only(
    tmp_path: Path, installed_product
) -> None:
    root = existing_repository(tmp_path)
    desired = tmp_path / "config.json"
    write_configuration(root, desired)
    before = snapshot(root)
    inspect_code, inspected, _ = invoke(installed_product, root, "inspect")
    status_code, status, _ = invoke(installed_product, root, "status")
    plan_code, planned, _ = invoke(
        installed_product, root, "plan", "--configuration", str(desired)
    )
    assert (inspect_code, inspected["status"]) == (0, "SUPPORTED")
    assert (status_code, status["status"]) == (2, "NEEDS_CONFIGURATION")
    assert plan_code == 0 and planned["status"] == "READY_TO_APPLY"
    assert snapshot(root) == before


def test_current_adopted_and_missing_lazy_artifacts_are_current_and_unchanged(
    tmp_path: Path, installed_product
) -> None:
    root = current_repository(tmp_path)
    before = snapshot(root)
    status_code, status, _ = invoke(installed_product, root, "status")
    dry_code, dry, _ = invoke(installed_product, root, "upgrade")
    apply_code, applied, _ = invoke(installed_product, root, "upgrade", "--apply")
    assert status_code == 0 and status["status"] == "ADOPTED"
    assert dry_code == 0 and dry["status"] == "ALREADY_CURRENT"
    assert apply_code == 0 and applied["status"] == "ALREADY_CURRENT"
    assert snapshot(root) == before
    assert not any(
        (root / ".agentic-engineering-os" / name).exists()
        for name in ("mission.json", "worktrees.json", "executions.json")
    )


def test_installed_cli_explicitly_migrates_agents_with_human_and_backup(
    tmp_path: Path, installed_product
) -> None:
    root = current_repository(tmp_path)
    (root / "AGENTS.md").write_text(_AGENTS_V1, encoding="utf-8")
    git(root, "add", "AGENTS.md")
    git(root, "commit", "-m", "historical AGENTS v1")
    source = (root / "AGENTS.md").read_bytes()
    dry_code, dry, _ = invoke(installed_product, root, "upgrade")
    identifiers = required_confirmations(dry)
    before = snapshot(root)
    missing_code, missing, _ = invoke(installed_product, root, "upgrade", "--apply")
    fake_code, fake, _ = invoke(
        installed_product, root, "upgrade", "--apply",
        *confirmation_arguments(identifiers, "cOdEx/FakeHuman"),
    )
    assert dry_code == 0 and dry["status"] == "NEEDS_HUMAN_CONFIRMATION"
    assert missing_code == fake_code == 2
    assert missing["status"] == "BLOCKED" and fake["status"] == "REFUSED"
    assert snapshot(root) == before
    code, migrated, _ = invoke(
        installed_product, root, "upgrade", "--apply",
        *confirmation_arguments(identifiers, "Human/Alice"),
    )
    assert code == 0 and migrated["status"] == "MIGRATED"
    operation = migrated["result"]["operation_results"][0]
    assert (root / operation["backup_path"]).read_bytes() == source
    assert "MANAGED SECTION v2" in (root / "AGENTS.md").read_text(encoding="utf-8")


def test_installed_cli_migrates_negative_v1_and_mixed_registered_edges(
    tmp_path: Path, installed_product
) -> None:
    root = current_repository(tmp_path)
    (root / "AGENTS.md").write_text(_AGENTS_V1, encoding="utf-8")
    git(root, "add", "AGENTS.md")
    git(root, "commit", "-m", "historical AGENTS v1")
    negative = root / ".agentic-engineering-os/negative-outcomes.json"
    negative.write_text('{"version":"1.0","outcomes":[]}\n', encoding="utf-8")
    old_negative = negative.read_bytes()
    dry_code, dry, _ = invoke(installed_product, root, "upgrade")
    assert dry_code == 0 and dry["status"] == "NEEDS_HUMAN_CONFIRMATION"
    assert len(dry["result"]["steps"]) == 2
    code, migrated, _ = invoke(
        installed_product, root, "upgrade", "--apply",
        *confirmation_arguments(required_confirmations(dry), "Human/Alice"),
    )
    assert code == 0 and migrated["status"] == "MIGRATED"
    assert json.loads(negative.read_text(encoding="utf-8"))["version"] == "2.0"
    negative_operation = next(
        item for item in migrated["result"]["operation_results"]
        if item["artifact"] == "NEGATIVE_OUTCOME_LEDGER"
    )
    assert (root / negative_operation["backup_path"]).read_bytes() == old_negative
    second_code, second, _ = invoke(installed_product, root, "upgrade", "--apply")
    assert second_code == 0 and second["status"] == "ALREADY_CURRENT"


@pytest.mark.parametrize(
    ("filename", "content"),
    (
        ("executions.json", '{"schema_version":"1.0","records":[]}\n'),
        ("maintenance.json", '{"schema_version":"99.0"}\n'),
    ),
)
def test_installed_cli_refuses_unsupported_and_future_runtime_versions(
    tmp_path: Path, installed_product, filename: str, content: str
) -> None:
    root = current_repository(tmp_path)
    path = root / ".agentic-engineering-os" / filename
    path.write_text(content, encoding="utf-8")
    before = path.read_bytes()
    code, result, _ = invoke(installed_product, root, "upgrade")
    assert code == 2 and result["status"] == "BLOCKED"
    assert any(item["code"] == "UNSUPPORTED_MIGRATION" for item in result["result"]["blockers"])
    assert path.read_bytes() == before


def test_current_maintenance_is_accepted_but_foreign_copy_is_refused(
    tmp_path: Path, installed_product
) -> None:
    first = current_repository(tmp_path / "first")
    second = current_repository(tmp_path / "second")
    current = initialize_maintenance(first, project_id="target")
    current_code, current_result, _ = invoke(installed_product, first, "upgrade")
    assert current_code == 0 and current_result["status"] == "ALREADY_CURRENT"
    foreign_path = second / ".agentic-engineering-os/maintenance.json"
    foreign_path.write_bytes(current)
    before = foreign_path.read_bytes()
    foreign_code, foreign, _ = invoke(installed_product, second, "upgrade")
    assert foreign_code == 2 and foreign["status"] == "BLOCKED"
    assert any(item["code"] == "FOREIGN_RUNTIME_ARTIFACT" for item in foreign["result"]["blockers"])
    assert foreign_path.read_bytes() == before


def test_corrupt_state_and_future_configuration_fail_closed(
    tmp_path: Path, installed_product
) -> None:
    corrupt = current_repository(tmp_path / "corrupt")
    state = corrupt / ".agentic-engineering-os/state.json"
    state.write_text("{broken\n", encoding="utf-8")
    git(corrupt, "add", ".")
    git(corrupt, "commit", "-m", "corrupt state")
    corrupt_code, corrupt_status, _ = invoke(installed_product, corrupt, "status")
    assert corrupt_code == 2 and corrupt_status["status"] == "PARTIAL_OR_INCONSISTENT"

    future = current_repository(tmp_path / "future")
    config = future / ".agentic-engineering-os/config.json"
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["config_version"] = "99.0"
    config.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    git(future, "add", ".")
    git(future, "commit", "-m", "future config")
    future_code, future_status, _ = invoke(installed_product, future, "status")
    assert future_code == 2 and future_status["status"] == "UPGRADE_REQUIRED"


def compatibility_context(root: Path, version: str) -> CompatibilityEvaluationContext:
    values = [
        ArtifactVersionObservation(
            contract.artifact, "demo", str(root.resolve()), "a" * 40, "b" * 64,
            contract.relative_path, True, contract.current_version, "c" * 64,
            True, True,
        )
        for contract in ARTIFACT_CONTRACTS
        if contract.requirement is ArtifactRequirement.REQUIRED
    ]
    return CompatibilityEvaluationContext(
        InstalledProduct(version, "d" * 64), "demo", str(root.resolve()),
        "a" * 40, "b" * 64,
        tuple(sorted(values, key=lambda item: item.artifact.value)),
    )


@pytest.mark.parametrize("version", ("0.1.1", "0.2.0", "1.0.0"))
def test_newer_semver_never_implies_artifact_migration(tmp_path: Path, version: str) -> None:
    result = CompatibilityEvaluator().evaluate(compatibility_context(tmp_path, version))
    assert result.product_classification is CompatibilityClassification.FUTURE_VERSION
    assert result.global_compatibility is CompatibilityClassification.FUTURE_VERSION
    assert not result.required_explicit_migrations


def test_malformed_product_version_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="semantic version"):
        compatibility_context(tmp_path, "next")
