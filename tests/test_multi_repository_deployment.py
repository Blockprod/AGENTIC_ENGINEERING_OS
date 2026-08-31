from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest


SOURCE_ROOT = Path(__file__).parents[1].resolve()
ENV_SECRET = "P511-ENV-SECRET-DO-NOT-LEAK"
FILE_SECRET = "P511-FILE-SECRET-DO-NOT-LEAK"
AGENTS_V1 = "\n".join(
    (
        "<!-- BEGIN AGENTIC_ENGINEERING_OS MANAGED SECTION v1 -->",
        "## Agentic Engineering OS",
        "",
        "Follow the installed AGENTIC_ENGINEERING_OS operating contract.",
        "Repository facts and Control Plane decisions prevail over agent declarations.",
        "<!-- END AGENTIC_ENGINEERING_OS MANAGED SECTION v1 -->",
        "",
    )
)


@dataclass(frozen=True, slots=True)
class InstalledProduct:
    base: Path
    repositories: Path
    wheel: Path
    environment: Path
    python: Path
    executable: Path
    child_environment: dict[str, str]
    import_origin: Path


@pytest.fixture(scope="module")
def product():
    base = Path(
        tempfile.mkdtemp(prefix=".p511-installed-product-", dir=SOURCE_ROOT.parent)
    ).resolve()
    assert not _within(SOURCE_ROOT, base)
    build_source = base / "wheel-source"
    build_source.mkdir()
    shutil.copy2(SOURCE_ROOT / "pyproject.toml", build_source / "pyproject.toml")
    shutil.copy2(SOURCE_ROOT / "README.md", build_source / "README.md")
    shutil.copytree(SOURCE_ROOT / "src", build_source / "src")
    wheelhouse = base / "wheelhouse"
    wheelhouse.mkdir()
    build = _run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(build_source),
            "--wheel-dir",
            str(wheelhouse),
        ],
        cwd=base,
    )
    assert build.returncode == 0, build.stderr
    product_wheels = tuple(wheelhouse.glob("agentic_engineering_os-*.whl"))
    assert len(product_wheels) == 1
    wheel = product_wheels[0]

    environment = base / "venv"
    created = _run([sys.executable, "-m", "venv", str(environment)], cwd=base)
    assert created.returncode == 0, created.stderr
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    executable = environment / (
        "Scripts/agentic-os.exe" if os.name == "nt" else "bin/agentic-os"
    )
    installed = _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "agentic-engineering-os==0.1.0",
        ],
        cwd=base,
    )
    assert installed.returncode == 0, installed.stderr
    shutil.rmtree(build_source)
    assert not build_source.exists()

    child_environment = os.environ.copy()
    child_environment.pop("PYTHONPATH", None)
    child_environment.pop("PYTHONHOME", None)
    child_environment["PYTHONNOUSERSITE"] = "1"
    child_environment["P511_ENV_SECRET"] = ENV_SECRET
    outside = base / "outside-cwd"
    outside.mkdir()
    origin_result = _run(
        [
            str(python),
            "-c",
            "import json, agentic_engineering_os as p; "
            "from agentic_engineering_os.resources.product import product_resource_path; "
            "print(json.dumps({'module': p.__file__, "
            "'schema': str(product_resource_path('schemas/user-story.schema.json')), "
            "'role': str(product_resource_path('roles/architect.md'))}, sort_keys=True))",
        ],
        cwd=outside,
        env=child_environment,
    )
    assert origin_result.returncode == 0, origin_result.stderr
    origin = json.loads(origin_result.stdout)
    import_origin = Path(origin["module"]).resolve()
    assert _within(environment, import_origin)
    assert not _within(SOURCE_ROOT, import_origin)
    assert Path(origin["schema"]).is_file()
    assert Path(origin["role"]).is_file()

    repositories = base / "repositories"
    repositories.mkdir()
    instance = InstalledProduct(
        base,
        repositories,
        wheel,
        environment,
        python,
        executable,
        child_environment,
        import_origin,
    )
    try:
        yield instance
    finally:
        _remove_tree(base)
        assert not base.exists()


def _run(
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        env=env,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _remove_tree(path: Path) -> None:
    def make_writable_and_retry(function, target, _error) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    shutil.rmtree(path, onerror=make_writable_and_retry)


def installed_cli_arguments(product: InstalledProduct) -> list[str]:
    return [str(product.python), "-m", "agentic_engineering_os"]


def git(root: Path, *arguments: str) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_DATE": "2026-08-31T12:00:00+02:00",
            "GIT_COMMITTER_DATE": "2026-08-31T12:00:00+02:00",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    result = _run(["git", "-C", str(root), *arguments], cwd=root, env=environment)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def repository(
    product: InstalledProduct,
    name: str,
    *,
    kind: str = "empty",
    agents: bytes | None = None,
    sensitive: bool = False,
    malicious_script: bool = False,
) -> Path:
    root = product.repositories / name
    assert not root.exists()
    root.mkdir(parents=True)
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "P5.11 Test")
    git(root, "config", "user.email", "p5.11@example.invalid")
    (root / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    if kind in {"python-minimal", "mixed"}:
        (root / "setup.cfg").write_text(
            f"[metadata]\nname = {name}\nversion = 0.1.0\n", encoding="utf-8"
        )
    if kind == "python-tests":
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )
        tests = root / "tests"
        tests.mkdir()
        (tests / "test_smoke.py").write_text(
            "def test_smoke():\n    assert True\n", encoding="utf-8"
        )
    if kind in {"node", "mixed"}:
        script = (
            f'"test":"echo {FILE_SECRET} && touch should-not-exist"'
            if malicious_script
            else '"build":"node --version"'
        )
        (root / "package.json").write_text(
            f'{{"name":"{name}","scripts":{{{script}}},"version":"1.0.0"}}\n',
            encoding="utf-8",
        )
        if malicious_script:
            (root / "package-lock.json").write_text(
                f'{{"lockfileVersion":3,"name":"{name}"}}\n', encoding="utf-8"
            )
    if kind == "rust":
        (root / "Cargo.toml").write_text(
            f'[package]\nname = "{name}"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )
    if agents is not None:
        (root / "AGENTS.md").write_bytes(agents)
    if sensitive:
        (root / ".env").write_text(
            f"PASSWORD={FILE_SECRET}\n", encoding="utf-8"
        )
    git(root, "add", ".")
    git(root, "commit", "-m", "target baseline")
    assert git(root, "status", "--porcelain") == ""
    return root


def configuration(
    product: InstalledProduct,
    name: str,
    toolchains: tuple[str, ...] = (),
) -> Path:
    path = product.base / "configurations" / f"{name}.json"
    path.parent.mkdir(exist_ok=True)
    payload = {
        "codex_constraints": {
            "approval_policy": "never",
            "maximum_parallel_executions": 1,
            "maximum_sandbox": "read-only",
            "require_clean_git": True,
        },
        "config_version": "1.0",
        "context_sources": [],
        "mission_state_git_policy": "TRACKED",
        "path_policy": {
            "allowed_paths": [],
            "forbidden_paths": [],
            "protected_paths": [],
        },
        "project_id": name,
        "repository_root_policy": "CONFIG_PARENT_GIT_ROOT",
        "toolchains": [
            {"identity": identity, "version_constraint": None}
            for identity in sorted(toolchains, key=str.casefold)
        ],
        "verification_commands": [],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def cli(
    product: InstalledProduct,
    repository_root: Path,
    command: str,
    *arguments: str,
) -> tuple[int, dict[str, object], subprocess.CompletedProcess[str]]:
    result = _run(
        [
            *installed_cli_arguments(product),
            command,
            "--repository",
            str(repository_root),
            *arguments,
            "--json",
        ],
        cwd=product.base / "outside-cwd",
        env=product.child_environment,
    )
    combined = result.stdout + result.stderr
    assert ENV_SECRET not in combined
    assert FILE_SECRET not in combined
    output = result.stdout.strip() or result.stderr.strip()
    assert output
    return result.returncode, json.loads(output), result


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def human_operation_ids(payload: dict[str, object]) -> tuple[str, ...]:
    result = payload["result"]
    assert isinstance(result, dict)
    plan = result["initialization_plan"]
    assert isinstance(plan, dict)
    operations = plan["operations"]
    assert isinstance(operations, list)
    return tuple(
        str(item["operation_id"])
        for item in operations
        if isinstance(item, dict) and item["human_confirmation_required"] is True
    )


def confirmation_arguments(identifiers: tuple[str, ...], producer: str) -> list[str]:
    arguments: list[str] = []
    for identifier in identifiers:
        arguments.extend(("--confirm", identifier))
    arguments.extend(("--confirmed-by", producer))
    return arguments


def adopt(
    product: InstalledProduct,
    root: Path,
    config: Path,
) -> dict[str, object]:
    code, payload, _ = cli(
        product,
        root,
        "init",
        "--configuration",
        str(config),
        "--apply",
    )
    assert code == 0, payload
    assert payload["status"] == "ADOPTED"
    return payload


def _within(parent: Path, child: Path) -> bool:
    try:
        return os.path.commonpath((str(parent), str(child))) == str(parent)
    except ValueError:
        return False


def test_wheel_venv_import_and_resources_are_checkout_independent(
    product: InstalledProduct,
) -> None:
    assert product.executable.is_file()
    assert product.import_origin.is_file()
    assert not (product.base / "wheel-source").exists()
    source_tokens = {
        str(SOURCE_ROOT).encode("utf-8").lower(),
        str(SOURCE_ROOT).replace("\\", "/").encode("utf-8").lower(),
    }
    with zipfile.ZipFile(product.wheel) as archive:
        for name in archive.namelist():
            content = archive.read(name).lower()
            assert all(token not in content for token in source_tokens)
    installed_package = product.import_origin.parent
    for path in installed_package.rglob("*"):
        if path.is_file() and path.stat().st_size <= 2_000_000:
            content = path.read_bytes().lower()
            assert all(token not in content for token in source_tokens)

    help_result = _run(
        [*installed_cli_arguments(product), "--help"],
        cwd=product.base / "outside-cwd",
        env=product.child_environment,
    )
    check = _run(
        [str(product.python), "-m", "pip", "check"],
        cwd=product.base / "outside-cwd",
        env=product.child_environment,
    )
    assert help_result.returncode == 0
    assert "inspect" in help_result.stdout and "upgrade" in help_result.stdout
    assert check.returncode == 0, check.stderr


@pytest.mark.parametrize(
    ("case", "kind", "expected_toolchains"),
    (
        ("a-python-minimal", "python-minimal", ("python",)),
        ("b-python-tests", "python-tests", ("python",)),
        ("c-node-minimal", "node", ("node",)),
        ("d-mixed", "mixed", ("node", "python")),
        ("m-rust-minimal", "rust", ("rust",)),
    ),
)
def test_toolchain_repository_matrix_is_inspected_and_adopted(
    product: InstalledProduct,
    case: str,
    kind: str,
    expected_toolchains: tuple[str, ...],
) -> None:
    root = repository(product, case, kind=kind)
    config = configuration(product, case, expected_toolchains)
    before = snapshot(root)
    inspect_code, inspected, _ = cli(product, root, "inspect")
    plan_code, planned, _ = cli(
        product, root, "plan", "--configuration", str(config)
    )
    dry_code, dry, _ = cli(
        product, root, "init", "--configuration", str(config)
    )

    observed = tuple(
        item["identity"] for item in inspected["result"]["toolchains"]
    )
    assert inspect_code == 0
    assert inspected["result"]["agentic_os"]["state"] == "UNINITIALIZED"
    assert observed == expected_toolchains
    assert planned["status"] == "READY_TO_APPLY" and plan_code == 0
    assert dry["status"] == "READY_TO_APPLY" and dry_code == 0
    assert snapshot(root) == before

    adopt(product, root, config)
    status_code, status, _ = cli(product, root, "status")
    assert status_code == 0 and status["status"] == "ADOPTED"
    assert (root / ".agentic-engineering-os/config.json").is_file()
    assert (root / ".agentic-engineering-os/state.json").is_file()
    assert "MANAGED SECTION v2" in (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "worktrees.json" in (root / ".gitignore").read_text(encoding="utf-8")
    assert not (root / ".agentic-engineering-os/mission.json").exists()
    for name in ("worktrees.json", "negative-outcomes.json", "executions.json"):
        assert not (root / ".agentic-engineering-os" / name).exists()


def test_clean_golden_path_is_idempotent_and_semantically_repeatable(
    product: InstalledProduct,
) -> None:
    outcomes: list[tuple[object, ...]] = []
    for suffix in ("one", "two"):
        name = f"golden-{suffix}"
        root = repository(product, name, kind="python-minimal")
        config = configuration(product, name, ("python",))
        plan_code, plan, _ = cli(
            product, root, "plan", "--configuration", str(config)
        )
        assert plan_code == 0
        operation_signature = tuple(
            (item["operation_type"], item["target_path"])
            for item in plan["result"]["initialization_plan"]["operations"]
        )
        adopt(product, root, config)
        before_second = snapshot(root)
        second_code, second, _ = cli(product, root, "init", "--apply")
        assert second_code == 0 and second["status"] == "ADOPTED"
        assert snapshot(root) == before_second
        outcomes.append((plan["status"], operation_signature, second["status"]))
    assert outcomes[0] == outcomes[1]


def test_user_agents_requires_exact_human_and_preserves_user_bytes(
    product: InstalledProduct,
) -> None:
    user_bytes = "# Team rules\r\n\r\nKeep café bytes.\r\n".encode("utf-8")
    root = repository(product, "e-user-agents", agents=user_bytes)
    config = configuration(product, "e-user-agents")
    plan_code, plan, _ = cli(
        product, root, "plan", "--configuration", str(config)
    )
    identifiers = human_operation_ids(plan)
    before = snapshot(root)
    missing_code, missing, _ = cli(
        product,
        root,
        "init",
        "--configuration",
        str(config),
        "--apply",
    )
    fake_code, fake, _ = cli(
        product,
        root,
        "init",
        "--configuration",
        str(config),
        "--apply",
        *confirmation_arguments(identifiers, "cOdEx/FakeHuman"),
    )
    assert plan_code == 0 and plan["status"] == "NEEDS_HUMAN_CONFIRMATION"
    assert identifiers
    assert missing_code == 2 and missing["status"] == "BLOCKED"
    assert fake_code == 2 and fake["status"] == "BLOCKED"
    assert snapshot(root) == before

    human_code, human, _ = cli(
        product,
        root,
        "init",
        "--configuration",
        str(config),
        "--apply",
        *confirmation_arguments(identifiers, "Human/Alice"),
    )
    assert human_code == 0 and human["status"] == "ADOPTED"
    integrated = (root / "AGENTS.md").read_bytes()
    assert integrated.startswith(user_bytes)
    assert b"MANAGED SECTION v2" in integrated
    before_second = snapshot(root)
    second_code, second, _ = cli(product, root, "init", "--apply")
    assert second_code == 0 and second["status"] == "ADOPTED"
    assert snapshot(root) == before_second


def test_dirty_detached_multiple_worktrees_and_sensitive_files_are_honest(
    product: InstalledProduct,
) -> None:
    dirty = repository(product, "h-dirty", kind="python-minimal")
    dirty_config = configuration(product, "h-dirty", ("python",))
    (dirty / "README.md").write_text("dirty\n", encoding="utf-8")
    dirty_code, dirty_plan, _ = cli(
        product, dirty, "plan", "--configuration", str(dirty_config)
    )
    assert dirty_code == 2 and dirty_plan["status"] == "BLOCKED"
    assert any(
        item["code"] == "DIRTY_REPOSITORY"
        for item in dirty_plan["result"]["findings"]
    )

    detached = repository(product, "i-detached")
    git(detached, "checkout", "--detach")
    detached_config = configuration(product, "i-detached")
    detached_code, detached_plan, _ = cli(
        product, detached, "plan", "--configuration", str(detached_config)
    )
    assert detached_code == 0 and detached_plan["status"] == "READY_TO_APPLY"
    assert detached_plan["result"]["repository_profile"]["git"]["detached"]["value"] is True

    multiple = repository(product, "j-multiple")
    sibling = product.repositories / "j-secondary-worktree"
    git(multiple, "worktree", "add", "-b", "secondary", str(sibling))
    try:
        multiple_config = configuration(product, "j-multiple")
        multiple_code, multiple_plan, _ = cli(
            product, multiple, "plan", "--configuration", str(multiple_config)
        )
        assert multiple_code == 0 and multiple_plan["status"] == "READY_TO_APPLY"
        assert len(multiple_plan["result"]["repository_profile"]["git"]["worktrees"]) == 2
    finally:
        git(multiple, "worktree", "remove", "--force", str(sibling))

    sensitive = repository(product, "l-sensitive", sensitive=True)
    sensitive_code, sensitive_result, sensitive_process = cli(
        product, sensitive, "inspect"
    )
    assert sensitive_code == 0
    assert any(
        item["relative_path"] == ".env"
        for item in sensitive_result["result"]["sensitive_paths"]
    )
    assert FILE_SECRET not in sensitive_process.stdout + sensitive_process.stderr


def test_partial_invalid_future_corrupt_and_tampered_repositories_block(
    product: InstalledProduct,
) -> None:
    partial = repository(product, "g-partial")
    partial_state = partial / ".agentic-engineering-os"
    partial_state.mkdir()
    (partial_state / "state.json").write_text("{}\n", encoding="utf-8")
    partial_code, partial_status, _ = cli(product, partial, "status")
    assert partial_code == 2
    assert partial_status["status"] == "PARTIAL_OR_INCONSISTENT"

    invalid = repository(product, "invalid-config")
    invalid_directory = invalid / ".agentic-engineering-os"
    invalid_directory.mkdir()
    (invalid_directory / "config.json").write_text("{broken\n", encoding="utf-8")
    git(invalid, "add", ".")
    git(invalid, "commit", "-m", "invalid config")
    invalid_code, invalid_status, _ = cli(product, invalid, "status")
    assert invalid_code == 2
    assert invalid_status["status"] == "PARTIAL_OR_INCONSISTENT"

    future = repository(product, "k-future-config")
    future_directory = future / ".agentic-engineering-os"
    future_directory.mkdir()
    future_payload = json.loads(
        configuration(product, "k-future-config").read_text(encoding="utf-8")
    )
    future_payload["config_version"] = "99.0"
    (future_directory / "config.json").write_text(
        json.dumps(future_payload, sort_keys=True) + "\n", encoding="utf-8"
    )
    git(future, "add", ".")
    git(future, "commit", "-m", "future config")
    future_code, future_status, _ = cli(product, future, "status")
    assert future_code == 2 and future_status["status"] == "UPGRADE_REQUIRED"

    corrupt = repository(product, "corrupt-state")
    corrupt_config = configuration(product, "corrupt-state")
    adopt(product, corrupt, corrupt_config)
    git(corrupt, "add", ".")
    git(corrupt, "commit", "-m", "adopted baseline")
    (corrupt / ".agentic-engineering-os/state.json").write_text(
        "{}\n", encoding="utf-8"
    )
    git(corrupt, "add", ".")
    git(corrupt, "commit", "-m", "corrupt state")
    corrupt_code, corrupt_status, _ = cli(product, corrupt, "status")
    assert corrupt_code == 2
    assert corrupt_status["status"] == "PARTIAL_OR_INCONSISTENT"

    tampered = repository(product, "tampered-agents")
    tampered_config = configuration(product, "tampered-agents")
    adopt(product, tampered, tampered_config)
    git(tampered, "add", ".")
    git(tampered, "commit", "-m", "adopted baseline")
    agents_path = tampered / "AGENTS.md"
    agents_path.write_text(
        agents_path.read_text(encoding="utf-8").replace(
            "Managed contract version: 2", "Managed contract version: tampered"
        ),
        encoding="utf-8",
    )
    git(tampered, "add", ".")
    git(tampered, "commit", "-m", "tamper AGENTS")
    tampered_code, tampered_status, _ = cli(product, tampered, "status")
    assert tampered_code == 2
    assert tampered_status["status"] == "PARTIAL_OR_INCONSISTENT"


def test_stale_plan_and_repository_mutation_are_refused_by_installed_service(
    product: InstalledProduct,
) -> None:
    root = repository(product, "stale-plan")
    config = configuration(product, "stale-plan")
    script = "\n".join(
        (
            "import json, sys",
            "from pathlib import Path",
            "from agentic_engineering_os.application import ExistingRepositoryAdoption",
            "from agentic_engineering_os.infrastructure import ProjectConfigurationValidator",
            "root, config = Path(sys.argv[1]), Path(sys.argv[2])",
            "desired = ProjectConfigurationValidator().parse(config.read_text(encoding='utf-8'))",
            "service = ExistingRepositoryAdoption()",
            "preparation = service.prepare_adoption(root, desired)",
            "(root / 'README.md').write_text('mutated after plan\\n', encoding='utf-8')",
            "result = service.apply_adoption(preparation)",
            "print(json.dumps({'status': result.status.value, 'findings': [item.code for item in result.findings]}))",
        )
    )
    result = _run(
        [str(product.python), "-c", script, str(root), str(config)],
        cwd=product.base / "outside-cwd",
        env=product.child_environment,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "BLOCKED"
    assert not (root / ".agentic-engineering-os/config.json").exists()


def test_supported_and_unsupported_upgrade_paths_are_explicit(
    product: InstalledProduct,
) -> None:
    supported = repository(product, "supported-upgrade")
    supported_config = configuration(product, "supported-upgrade")
    adopt(product, supported, supported_config)
    git(supported, "add", ".")
    git(supported, "commit", "-m", "adopted baseline")
    (supported / "AGENTS.md").write_text(AGENTS_V1, encoding="utf-8")
    git(supported, "add", "AGENTS.md")
    git(supported, "commit", "-m", "historical AGENTS v1")
    historical = (supported / "AGENTS.md").read_bytes()

    inspect_code, inspected, _ = cli(product, supported, "inspect")
    status_code, status, _ = cli(product, supported, "status")
    dry_code, dry, _ = cli(product, supported, "upgrade")
    required = tuple(dry["result"]["required_human_confirmations"])
    assert inspect_code == 0
    assert inspected["result"]["agentic_os"]["state"] == "UPGRADE_REQUIRED"
    assert status_code == 2 and status["status"] == "UPGRADE_REQUIRED"
    assert dry_code == 0 and dry["status"] == "NEEDS_HUMAN_CONFIRMATION"
    assert (supported / "AGENTS.md").read_bytes() == historical

    fake_code, fake, _ = cli(
        product,
        supported,
        "upgrade",
        "--apply",
        *confirmation_arguments(required, "Codex/FakeHuman"),
    )
    assert fake_code == 2 and fake["status"] == "REFUSED"
    assert (supported / "AGENTS.md").read_bytes() == historical
    human_code, human, _ = cli(
        product,
        supported,
        "upgrade",
        "--apply",
        *confirmation_arguments(required, "Human/Alice"),
    )
    assert human_code == 0 and human["status"] == "MIGRATED"
    backup = supported / human["result"]["operation_results"][0]["backup_path"]
    assert backup.read_bytes() == historical
    current_code, current, _ = cli(product, supported, "status")
    assert current_code == 0 and current["status"] == "ADOPTED"

    unsupported = repository(product, "unsupported-upgrade")
    unsupported_config = configuration(product, "unsupported-upgrade")
    adopt(product, unsupported, unsupported_config)
    git(unsupported, "add", ".")
    git(unsupported, "commit", "-m", "adopted baseline")
    (unsupported / ".agentic-engineering-os/executions.json").write_text(
        '{"schema_version":"1.0","records":[]}\n', encoding="utf-8"
    )
    unsupported_code, unsupported_plan, _ = cli(
        product, unsupported, "upgrade"
    )
    assert unsupported_code == 2 and unsupported_plan["status"] == "BLOCKED"
    assert any(
        item["code"] == "UNSUPPORTED_MIGRATION"
        for item in unsupported_plan["result"]["blockers"]
    )


def test_installed_product_refuses_cross_repository_authority(
    product: InstalledProduct,
) -> None:
    first = repository(product, "cross-first", agents=b"# First user rules\n")
    second = repository(product, "cross-second", agents=b"# Second user rules\n")
    migration_first = repository(
        product, "migration-first", agents=AGENTS_V1.encode("utf-8")
    )
    migration_second = repository(
        product, "migration-second", agents=AGENTS_V1.encode("utf-8")
    )
    first_config = configuration(product, "cross-first")
    second_config = configuration(product, "cross-second")
    script = "\n".join(
        (
            "import json, sys",
            "from dataclasses import replace",
            "from pathlib import Path",
            "from agentic_engineering_os.application import ExistingRepositoryAdoption, UpgradePlanner",
            "from agentic_engineering_os.domain import HumanOperationConfirmation",
            "from agentic_engineering_os.infrastructure import ProjectConfigurationValidator, RepositoryUpgradeService",
            "a, b, ca, cb, ma, mb = map(Path, sys.argv[1:7])",
            "validator = ProjectConfigurationValidator()",
            "service = ExistingRepositoryAdoption()",
            "pa = service.prepare_adoption(a, validator.parse(ca.read_text(encoding='utf-8')))",
            "pb = service.prepare_adoption(b, validator.parse(cb.read_text(encoding='utf-8')))",
            "opa = next(item for item in pa.initialization_plan.operations if item.human_confirmation_required)",
            "confirmation_a = HumanOperationConfirmation(pa.initialization_plan.input_fingerprint, opa.operation_id, opa.target_path, opa.expected_current_state, opa.expected_target_fingerprint, 'Human/Alice')",
            "wrong_confirmation = service.apply_adoption(pb, human_confirmations=(confirmation_a,))",
            "foreign_preparation = service.apply_adoption(replace(pa, repository_root=str(b)), human_confirmations=(confirmation_a,))",
            "upgrade_a = UpgradePlanner().plan(ma)",
            "foreign_upgrade = RepositoryUpgradeService().apply(replace(upgrade_a, repository_root=str(mb)))",
            "print(json.dumps({'confirmation': wrong_confirmation.status.value, 'preparation': foreign_preparation.status.value, 'migration': foreign_upgrade.status.value}))",
        )
    )
    result = _run(
        [
            str(product.python),
            "-c",
            script,
            str(first),
            str(second),
            str(first_config),
            str(second_config),
            str(migration_first),
            str(migration_second),
        ],
        cwd=product.base / "outside-cwd",
        env=product.child_environment,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "confirmation": "BLOCKED",
        "migration": "REFUSED",
        "preparation": "BLOCKED",
    }

    clean_first = repository(product, "state-first")
    clean_second = repository(product, "state-second")
    adopt(product, clean_first, configuration(product, "state-first"))
    adopt(product, clean_second, configuration(product, "state-second"))
    git(clean_first, "add", ".")
    git(clean_first, "commit", "-m", "adopt first")
    git(clean_second, "add", ".")
    git(clean_second, "commit", "-m", "adopt second")
    first_state = clean_first / ".agentic-engineering-os/state.json"
    second_state = clean_second / ".agentic-engineering-os/state.json"
    second_state.write_bytes(first_state.read_bytes())
    git(clean_second, "add", ".")
    git(clean_second, "commit", "-m", "swap foreign state")
    code, payload, _ = cli(product, clean_second, "status")
    assert code == 2
    assert payload["status"] == "PARTIAL_OR_INCONSISTENT"


def test_cli_security_attacks_fail_closed_without_project_execution(
    product: InstalledProduct,
) -> None:
    root = repository(
        product,
        "security-node",
        kind="node",
        malicious_script=True,
    )
    marker = root / "should-not-exist"
    inspect_code, inspected, process = cli(product, root, "inspect")
    assert inspect_code == 0
    assert not marker.exists()
    assert FILE_SECRET not in process.stdout + process.stderr
    assert inspected["result"]["candidate_commands"]

    malformed = product.base / "configurations/malformed.json"
    malformed.write_text(
        f'{{"password":"{FILE_SECRET}", broken\n', encoding="utf-8"
    )
    malformed_code, malformed_result, malformed_process = cli(
        product,
        root,
        "plan",
        "--configuration",
        str(malformed),
    )
    assert malformed_code == 2
    assert malformed_result["status"] == "BLOCKED"
    assert FILE_SECRET not in malformed_process.stdout + malformed_process.stderr
    assert not marker.exists()

    traversal = _run(
        [*installed_cli_arguments(product), "inspect", "--repository", "..", "--json"],
        cwd=product.base / "outside-cwd",
        env=product.child_environment,
    )
    assert traversal.returncode == 2
    assert json.loads(traversal.stderr)["result"]["code"] == "UNSAFE_REPOSITORY_PATH"
    unknown = _run(
        [*installed_cli_arguments(product), "unknown-command"],
        cwd=product.base / "outside-cwd",
        env=product.child_environment,
    )
    assert unknown.returncode == 2
    assert ENV_SECRET not in unknown.stdout + unknown.stderr

    linked = product.repositories / "linked-root"
    if os.name == "nt":
        link_result = _run(
            ["cmd", "/c", "mklink", "/J", str(linked), str(root)],
            cwd=product.repositories,
        )
        assert link_result.returncode == 0, link_result.stderr
    else:
        linked.symlink_to(root, target_is_directory=True)
    try:
        linked_code, linked_result, _ = cli(product, linked, "inspect")
        assert linked_code == 2
        assert linked_result["result"]["code"] == "UNSAFE_REPOSITORY_PATH"
        assert not marker.exists()
    finally:
        if os.name == "nt":
            os.rmdir(linked)
        else:
            linked.unlink()
