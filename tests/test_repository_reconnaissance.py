import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from agentic_engineering_os.domain import (
    AGENTS_MANAGED_SECTION,
    GITIGNORE_MANAGED_SECTION,
    AgenticOsInitializationState,
    DocumentStatus,
    ManagedSectionStatus,
    ObservationClassification,
    RepositorySupportStatus,
    VerificationKind,
)
from agentic_engineering_os.infrastructure import (
    ProjectConfigurationValidator,
    RepositoryReconnaissance,
    RepositoryReconnaissanceError,
)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def repository(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "P5.3 Test")
    git(root, "config", "user.email", "p5.3@example.invalid")
    content = {"README.md": "# Target\n", **(files or {})}
    for relative, text in content.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    return root


def valid_configuration() -> dict[str, object]:
    return {
        "config_version": "1.0",
        "project_id": "target",
        "repository_root_policy": "CONFIG_PARENT_GIT_ROOT",
        "toolchains": [],
        "verification_commands": [],
        "path_policy": {
            "allowed_paths": [],
            "protected_paths": [],
            "forbidden_paths": [],
        },
        "context_sources": [],
        "codex_constraints": {
            "maximum_sandbox": "read-only",
            "approval_policy": "never",
            "require_clean_git": True,
            "maximum_parallel_executions": 1,
        },
        "mission_state_git_policy": "TRACKED",
    }


def write_configuration(root: Path, candidate: object) -> Path:
    directory = root / ".agentic-engineering-os"
    directory.mkdir(exist_ok=True)
    path = directory / "config.json"
    if isinstance(candidate, str):
        path.write_text(candidate, encoding="utf-8")
    else:
        path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
    return path


def user_file_snapshot(root: Path) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        (path.relative_to(root).as_posix(), path.read_bytes())
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold())
        if path.is_file() and ".git" not in path.relative_to(root).parts
    )


def test_python_project_is_inferred_from_explicit_markers(tmp_path: Path) -> None:
    root = repository(
        tmp_path,
        {
            "pyproject.toml": "[project]\nname = 'demo'\nversion = '0.1.0'\n",
            "requirements-dev.txt": "pytest\n",
        },
    )

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.support_status is RepositorySupportStatus.SUPPORTED
    assert [(item.identity, item.classification) for item in profile.toolchains] == [
        ("python", ObservationClassification.INFERENCE)
    ]
    assert profile.toolchains[0].evidence_paths == (
        "pyproject.toml",
        "requirements-dev.txt",
    )
    assert profile.manifests[0].status is DocumentStatus.VALID


def test_node_scripts_produce_observed_candidates_without_execution(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    package = {
        "scripts": {
            "build": "node build.js",
            "test": f"node -e write-file {marker}",
            "lint": "eslint .",
            "typecheck": "tsc --noEmit",
        }
    }
    root = repository(
        tmp_path,
        {
            "package.json": json.dumps(package),
            "package-lock.json": "{}",
        },
    )

    profile = RepositoryReconnaissance().inspect(root)

    assert [item.identity for item in profile.toolchains] == ["node"]
    assert [item.command_id for item in profile.candidate_commands] == [
        "build",
        "lint",
        "test",
        "typecheck",
    ]
    assert all(item.executable == "npm" for item in profile.candidate_commands)
    assert all(item.classification is ObservationClassification.INFERENCE for item in profile.candidate_commands)
    assert not marker.exists()
    assert str(marker) not in repr(profile)


def test_mixed_python_node_reports_both_without_primary_language(tmp_path: Path) -> None:
    root = repository(
        tmp_path,
        {
            "package.json": '{"scripts":{"test":"vitest"}}',
            "package-lock.json": "{}",
            "pyproject.toml": "[project]\nname='mixed'\nversion='1.0'\n",
        },
    )

    profile = RepositoryReconnaissance().inspect(root)

    assert [item.identity for item in profile.toolchains] == ["node", "python"]
    assert not hasattr(profile, "primary_language")


def test_rust_marker_is_supported_without_inventing_commands(tmp_path: Path) -> None:
    root = repository(tmp_path, {"Cargo.toml": "[package]\nname='demo'\nversion='0.1.0'\n"})

    profile = RepositoryReconnaissance().inspect(root)

    assert [item.identity for item in profile.toolchains] == ["rust"]
    assert profile.candidate_commands == ()


def test_valid_p52_configuration_is_observed_but_not_treated_as_complete_init(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    config_path = write_configuration(root, valid_configuration())

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.agentic_os.state is AgenticOsInitializationState.PARTIAL_OR_INCONSISTENT
    assert profile.agentic_os.config_status is DocumentStatus.VALID
    assert profile.agentic_os.config_version == "1.0"
    assert len(profile.agentic_os.config_semantic_fingerprint or "") == 64
    assert config_path.exists()
    assert not (config_path.parent / "state.json").exists()
    assert not (config_path.parent / "mission.json").exists()


def write_minimum_integration(root: Path) -> None:
    (root / "AGENTS.md").write_text(AGENTS_MANAGED_SECTION, encoding="utf-8")
    (root / ".gitignore").write_text(GITIGNORE_MANAGED_SECTION, encoding="utf-8")


def test_repository_with_valid_config_and_minimum_integration_is_initialized(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    write_configuration(root, valid_configuration())
    write_minimum_integration(root)

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.agentic_os.state is AgenticOsInitializationState.INITIALIZED
    assert (
        profile.agentic_os.agents_managed_section.status
        is ManagedSectionStatus.CURRENT
    )
    assert (
        profile.agentic_os.gitignore_managed_section.status
        is ManagedSectionStatus.CURRENT
    )


def test_unicode_context_paths_and_repeated_scan_are_deterministic(tmp_path: Path) -> None:
    root = repository(tmp_path, {"docs/équipe/vision.md": "# Vision\n"})
    service = RepositoryReconnaissance()

    first = service.inspect(root)
    second = service.inspect(root)

    assert first == second
    assert "docs/équipe/vision.md" in [item.relative_path for item in first.context_sources]


def test_context_entry_limit_is_explicit_and_fail_closed(tmp_path: Path) -> None:
    root = repository(
        tmp_path,
        {
            "docs/a.txt": "a\n",
            "docs/b.txt": "b\n",
            "docs/c.md": "# C\n",
        },
    )

    profile = RepositoryReconnaissance(max_context_entries=2).inspect(root)

    assert profile.scan_complete is False
    assert [item.relative_path for item in profile.context_sources] == ["README.md"]
    assert "CONTEXT_ENTRY_LIMIT_EXCEEDED" in {item.code for item in profile.issues}


def test_reconnaissance_does_not_mutate_files_or_git_index(tmp_path: Path) -> None:
    root = repository(tmp_path, {"pyproject.toml": "[project]\nname='demo'\nversion='1'\n"})
    before_files = user_file_snapshot(root)
    before_status = git(root, "status", "--porcelain=v1", "--untracked-files=all")
    index = root / ".git" / "index"
    before_index = index.read_bytes()
    before_index_mtime = index.stat().st_mtime_ns

    RepositoryReconnaissance().inspect(root)

    after_index = index.read_bytes()
    after_index_mtime = index.stat().st_mtime_ns
    assert user_file_snapshot(root) == before_files
    assert git(root, "status", "--porcelain=v1", "--untracked-files=all") == before_status
    assert after_index == before_index
    assert after_index_mtime == before_index_mtime


def test_non_git_directory_is_explicitly_blocked(tmp_path: Path) -> None:
    root = tmp_path / "not-git"
    root.mkdir()

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.support_status is RepositorySupportStatus.BLOCKED
    assert profile.git.is_repository.classification is ObservationClassification.FACT
    assert profile.git.is_repository.value is False
    assert profile.git.head_commit.classification is ObservationClassification.UNKNOWN
    assert profile.git.errors == ("NOT_GIT_REPOSITORY",)


def test_dirty_repository_is_reported_as_fact(tmp_path: Path) -> None:
    root = repository(tmp_path)
    (root / "dirty.txt").write_text("dirty", encoding="utf-8")

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.git.clean.classification is ObservationClassification.FACT
    assert profile.git.clean.value is False


def test_detached_head_is_reported_without_git_error(tmp_path: Path) -> None:
    root = repository(tmp_path)
    git(root, "checkout", "--detach")

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.git.detached.value is True
    assert profile.git.branch.value is None
    assert profile.git.errors == ()


def test_branch_named_detached_is_not_misclassified(tmp_path: Path) -> None:
    root = repository(tmp_path)
    git(root, "switch", "-c", "(detached)")

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.git.detached.value is False
    assert profile.git.branch.value == "(detached)"
    assert profile.git.errors == ()


def test_multiple_worktrees_are_observed_in_canonical_order(tmp_path: Path) -> None:
    root = repository(tmp_path)
    secondary = tmp_path / "secondary-worktree"
    git(root, "worktree", "add", "--detach", str(secondary))

    profile = RepositoryReconnaissance().inspect(root)

    assert len(profile.git.worktrees) == 2
    assert [item.path for item in profile.git.worktrees] == sorted(
        (item.path for item in profile.git.worktrees), key=lambda value: value.casefold()
    )


def test_partial_agentic_directory_is_not_repaired(tmp_path: Path) -> None:
    root = repository(tmp_path)
    state = root / ".agentic-engineering-os" / "state.json"
    state.parent.mkdir()
    state.write_text('{"schema_version":"1.0"}', encoding="utf-8")
    before = state.read_bytes()

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.agentic_os.state is AgenticOsInitializationState.PARTIAL_OR_INCONSISTENT
    assert profile.agentic_os.config_status is DocumentStatus.ABSENT
    assert state.read_bytes() == before


@pytest.mark.parametrize(
    ("candidate", "expected_status", "expected_state"),
    [
        ("{", DocumentStatus.INVALID, AgenticOsInitializationState.PARTIAL_OR_INCONSISTENT),
        ('{"config_version":"2.0"}', DocumentStatus.UNKNOWN_VERSION, AgenticOsInitializationState.UPGRADE_REQUIRED),
        ('{"config_version":"1.0","config_version":"1.0"}', DocumentStatus.INVALID, AgenticOsInitializationState.PARTIAL_OR_INCONSISTENT),
    ],
)
def test_invalid_unknown_and_duplicate_configuration_are_classified_fail_closed(
    tmp_path: Path,
    candidate: str,
    expected_status: DocumentStatus,
    expected_state: AgenticOsInitializationState,
) -> None:
    root = repository(tmp_path)
    write_configuration(root, candidate)

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.agentic_os.config_status is expected_status
    assert profile.agentic_os.state is expected_state


def test_unknown_runtime_schema_requires_upgrade(tmp_path: Path) -> None:
    root = repository(tmp_path)
    write_configuration(root, valid_configuration())
    (root / ".agentic-engineering-os" / "state.json").write_text(
        '{"schema_version":"9.0"}', encoding="utf-8"
    )

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.agentic_os.state is AgenticOsInitializationState.UPGRADE_REQUIRED
    state = next(item for item in profile.agentic_os.runtime_files if item.relative_path.endswith("state.json"))
    assert state.status is DocumentStatus.UNKNOWN_VERSION
    assert state.schema_version == "9.0"


def test_all_current_runtime_version_fields_are_observed_compatibly(tmp_path: Path) -> None:
    root = repository(tmp_path)
    write_configuration(root, valid_configuration())
    write_minimum_integration(root)
    runtime = root / ".agentic-engineering-os"
    documents = {
        "state.json": {"schema_version": "1.0"},
        "mission.json": {"schema_version": "1.0"},
        "worktrees.json": {"schema_version": "1.0"},
        "negative-outcomes.json": {"version": "2.0"},
        "executions.json": {"schema_version": "1.1"},
        "maintenance.json": {"schema_version": "1.0"},
    }
    for filename, document in documents.items():
        (runtime / filename).write_text(json.dumps(document), encoding="utf-8")

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.agentic_os.state is AgenticOsInitializationState.INITIALIZED
    assert all(
        item.status is DocumentStatus.VERSION_OBSERVED
        for item in profile.agentic_os.runtime_files
    )


def test_simulated_symlink_escape_is_observed_and_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository(tmp_path, {"escape.md": "placeholder"})
    escape = root / "escape.md"
    outside = tmp_path / "outside-secret.md"
    secret = "credential=DO_NOT_CAPTURE"
    outside.write_text(secret, encoding="utf-8")
    original_is_symlink = Path.is_symlink
    original_resolve = Path.resolve

    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == escape or original_is_symlink(path),
    )
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda path, strict=False: outside if path == escape else original_resolve(path, strict=strict),
    )

    profile = RepositoryReconnaissance().inspect(root)

    observation = next(item for item in profile.symlinks if item.relative_path == "escape.md")
    assert observation.target_scope == "OUTSIDE_REPOSITORY"
    assert "escape.md" not in [item.relative_path for item in profile.context_sources]
    assert secret not in repr(profile)


def test_sensitive_file_presence_is_recorded_without_content(tmp_path: Path) -> None:
    secret = "password=DO_NOT_CAPTURE"
    root = repository(
        tmp_path,
        {
            ".env": secret,
            "private.pem": "-----BEGIN PRIVATE KEY-----\nDO_NOT_CAPTURE",
        },
    )

    profile = RepositoryReconnaissance().inspect(root)

    assert {item.relative_path for item in profile.sensitive_paths} >= {".env", "private.pem"}
    assert secret not in repr(profile)
    assert "DO_NOT_CAPTURE" not in repr(profile)


def test_giant_irrelevant_directory_is_not_traversed(tmp_path: Path) -> None:
    root = repository(tmp_path)
    modules = root / "node_modules"
    modules.mkdir()
    for index in range(600):
        (modules / f"module-{index}.js").write_text("x", encoding="utf-8")

    profile = RepositoryReconnaissance(max_top_level_entries=16).inspect(root)

    assert profile.scan_complete is True
    assert "node_modules" not in [item.relative_path for item in profile.top_level_entries]
    assert not any("module-" in item.relative_path for item in profile.top_level_entries)


def test_top_level_limit_is_reported_without_partial_listing(tmp_path: Path) -> None:
    root = repository(tmp_path)
    for index in range(5):
        (root / f"file-{index}.txt").write_text("x", encoding="utf-8")

    profile = RepositoryReconnaissance(max_top_level_entries=3).inspect(root)

    assert profile.scan_complete is False
    assert profile.top_level_entries == ()
    assert any(item.code == "TOP_LEVEL_LIMIT_EXCEEDED" for item in profile.issues)


def test_malformed_manifests_remain_unknown_but_markers_remain_inferences(
    tmp_path: Path,
) -> None:
    root = repository(
        tmp_path,
        {
            "package.json": '{"scripts":{"test":"ok"},"scripts":{}}',
            "pyproject.toml": "[project\n",
        },
    )

    profile = RepositoryReconnaissance().inspect(root)

    assert {item.status for item in profile.manifests} == {DocumentStatus.INVALID}
    assert [item.identity for item in profile.toolchains] == ["node", "python"]
    assert profile.candidate_commands == ()
    assert all(item.classification is ObservationClassification.UNKNOWN for item in profile.manifests)


def test_package_manager_ambiguity_does_not_invent_a_command(tmp_path: Path) -> None:
    root = repository(
        tmp_path,
        {
            "package.json": '{"scripts":{"test":"vitest"}}',
            "package-lock.json": "{}",
            "yarn.lock": "",
        },
    )

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.candidate_commands == ()
    assert any(item.code == "AMBIGUOUS_PACKAGE_MANAGER" for item in profile.issues)


def test_missing_package_manager_remains_unknown(tmp_path: Path) -> None:
    root = repository(tmp_path, {"package.json": '{"scripts":{"test":"vitest"}}'})

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.candidate_commands == ()
    assert any(item.code == "PACKAGE_MANAGER_UNKNOWN" for item in profile.issues)


def test_secret_like_package_script_is_redacted(tmp_path: Path) -> None:
    secret = "password=DO_NOT_CAPTURE"
    root = repository(
        tmp_path,
        {
            "package.json": json.dumps({"scripts": {"test": secret}}),
            "package-lock.json": "{}",
        },
    )

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.candidate_commands == ()
    assert any(item.code == "SECRET_LIKE_COMMAND_REDACTED" for item in profile.issues)
    assert secret not in repr(profile)


def test_no_recognizable_toolchain_remains_empty_not_unknown_default(tmp_path: Path) -> None:
    root = repository(tmp_path, {"notes.txt": "plain repository"})

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.toolchains == ()
    assert profile.candidate_commands == ()


def test_agents_and_gitignore_are_observed_without_claiming_managed_compliance(
    tmp_path: Path,
) -> None:
    root = repository(
        tmp_path,
        {
            "AGENTS.md": "Use AGENTIC_ENGINEERING_OS.\n",
            ".gitignore": ".agentic-engineering-os/worktrees.json\n",
        },
    )

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.agentic_os.agents_reference.value is True
    assert profile.agentic_os.agents_reference.classification is ObservationClassification.FACT
    assert profile.agentic_os.gitignore_rules == (
        ".agentic-engineering-os/worktrees.json",
    )
    assert "managed-section compliance is not inferred" in profile.agentic_os.agents_reference.detail


def test_managed_file_fingerprint_binds_exact_bytes(tmp_path: Path) -> None:
    raw = b"dist/\r\n# exact bytes\r\n"
    root = repository(tmp_path, {".gitignore": raw.decode("utf-8")})
    (root / ".gitignore").write_bytes(raw)

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.agentic_os.gitignore_managed_section.content_fingerprint == (
        hashlib.sha256(raw).hexdigest()
    )


def test_codex_availability_stays_machine_bound_unknown(tmp_path: Path) -> None:
    root = repository(tmp_path)

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.codex_availability.classification is ObservationClassification.UNKNOWN
    assert profile.codex_availability.value is None


def test_repository_root_symlink_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository(tmp_path)
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == root or original_is_symlink(path),
    )

    with pytest.raises(RepositoryReconnaissanceError) as captured:
        RepositoryReconnaissance().inspect(root)

    assert captured.value.code == "UNSAFE_REPOSITORY_ROOT"


def test_valid_configuration_serializer_is_compatible_with_reconnaissance(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    validator = ProjectConfigurationValidator()
    serialized = validator.serialize(validator.validate(valid_configuration()))
    write_configuration(root, serialized)

    profile = RepositoryReconnaissance().inspect(root)

    assert profile.agentic_os.config_status is DocumentStatus.VALID
    assert profile.agentic_os.config_version == "1.0"
