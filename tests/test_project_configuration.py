import copy
import json
from pathlib import Path

import pytest

from agentic_engineering_os.domain import (
    CodexApprovalConstraint,
    CodexSandboxConstraint,
    MissionStateGitPolicy,
    RepositoryRootPolicy,
    VerificationKind,
    WorkingDirectoryPolicy,
    to_dict,
)
from agentic_engineering_os.infrastructure import (
    ProjectConfigurationError,
    ProjectConfigurationLoader,
    ProjectConfigurationValidator,
)
from agentic_engineering_os.resources.project_configuration import (
    project_configuration_schema_text,
)


def valid_candidate() -> dict[str, object]:
    return {
        "config_version": "1.0",
        "project_id": "système-démo",
        "repository_root_policy": "CONFIG_PARENT_GIT_ROOT",
        "toolchains": [
            {"identity": "node", "version_constraint": ">=22"},
            {"identity": "python", "version_constraint": ">=3.11"},
        ],
        "verification_commands": [
            {
                "command_id": "lint",
                "kind": "LINT",
                "executable": "ruff",
                "args": ["check", "."],
                "cwd": ".",
                "cwd_policy": "REPOSITORY_RELATIVE",
                "required": False,
            },
            {
                "command_id": "tests",
                "kind": "TEST",
                "executable": "python",
                "args": ["-m", "pytest", "tests"],
                "cwd": ".",
                "cwd_policy": "REPOSITORY_RELATIVE",
                "required": True,
            },
        ],
        "path_policy": {
            "allowed_paths": ["docs", "src", "tests"],
            "protected_paths": ["pyproject.toml"],
            "forbidden_paths": ["src/generated"],
        },
        "context_sources": ["AGENTS.md", "docs/architecture.md", "README.md"],
        "codex_constraints": {
            "maximum_sandbox": "workspace-write",
            "approval_policy": "never",
            "require_clean_git": True,
            "maximum_parallel_executions": 2,
        },
        "mission_state_git_policy": "TRACKED",
    }


def write_config(root: Path, candidate: object) -> Path:
    path = root / ".agentic-engineering-os" / "config.json"
    path.parent.mkdir()
    path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
    return path


def test_minimal_configuration_is_valid_without_invented_commands() -> None:
    candidate = valid_candidate()
    candidate["toolchains"] = []
    candidate["verification_commands"] = []
    candidate["path_policy"] = {
        "allowed_paths": [],
        "protected_paths": [],
        "forbidden_paths": [],
    }
    candidate["context_sources"] = []

    config = ProjectConfigurationValidator().validate(candidate)

    assert config.config_version == "1.0"
    assert config.repository_root_policy is RepositoryRootPolicy.CONFIG_PARENT_GIT_ROOT
    assert config.toolchains == ()
    assert config.verification_commands == ()
    assert config.mission_state_git_policy is MissionStateGitPolicy.TRACKED


def test_round_trip_preserves_unicode_and_structured_commands() -> None:
    validator = ProjectConfigurationValidator()
    first = validator.validate(valid_candidate())
    serialized = validator.serialize(first)
    second = validator.parse(serialized)

    assert second == first
    assert "système-démo" in serialized
    assert second.verification_commands[0].kind is VerificationKind.LINT
    assert second.verification_commands[1].args == ("-m", "pytest", "tests")
    assert second.verification_commands[1].cwd_policy is WorkingDirectoryPolicy.REPOSITORY_RELATIVE
    assert second.codex_constraints.maximum_sandbox is CodexSandboxConstraint.WORKSPACE_WRITE
    assert second.codex_constraints.approval_policy is CodexApprovalConstraint.NEVER


def test_serialization_is_deterministic_and_canonical() -> None:
    validator = ProjectConfigurationValidator()
    config = validator.validate(valid_candidate())

    first = validator.serialize(config)
    second = validator.serialize(config)

    assert first == second
    assert first.endswith("\n")
    assert first == json.dumps(
        to_dict(config), ensure_ascii=False, sort_keys=True, indent=2, separators=(",", ": ")
    ) + "\n"


def test_loader_uses_only_canonical_repository_local_path(tmp_path: Path) -> None:
    expected_path = write_config(tmp_path, valid_candidate())

    loader = ProjectConfigurationLoader(tmp_path)

    assert loader.config_path == expected_path
    assert loader.load().project_id == "système-démo"


def test_packaged_schema_resolution_does_not_depend_on_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    schema = json.loads(project_configuration_schema_text())
    config = ProjectConfigurationValidator().validate(valid_candidate())

    assert schema["$id"] == "urn:agentic-engineering-os:schema:v1:project-configuration"
    assert config.project_id == "système-démo"


def test_absent_configuration_fails_without_creating_any_file(tmp_path: Path) -> None:
    loader = ProjectConfigurationLoader(tmp_path)

    with pytest.raises(ProjectConfigurationError) as captured:
        loader.load()

    assert captured.value.code == "CONFIG_ABSENT"
    assert not loader.config_path.parent.exists()


def test_corrupted_configuration_is_refused(tmp_path: Path) -> None:
    path = tmp_path / ".agentic-engineering-os" / "config.json"
    path.parent.mkdir()
    path.write_text("{", encoding="utf-8")

    with pytest.raises(ProjectConfigurationError) as captured:
        ProjectConfigurationLoader(tmp_path).load()

    assert captured.value.code == "INVALID_JSON"
    assert path.read_text(encoding="utf-8") == "{"


def test_configuration_must_be_utf8(tmp_path: Path) -> None:
    path = tmp_path / ".agentic-engineering-os" / "config.json"
    path.parent.mkdir()
    path.write_bytes(b"\xff\xfe")

    with pytest.raises(ProjectConfigurationError) as captured:
        ProjectConfigurationLoader(tmp_path).load()

    assert captured.value.code == "READ_FAILED"


def test_configuration_directory_symlink_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loader = ProjectConfigurationLoader(tmp_path)
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == loader.config_path.parent or original_is_symlink(path),
    )

    with pytest.raises(ProjectConfigurationError) as captured:
        loader.load()

    assert captured.value.code == "UNSAFE_PATH"


def test_duplicate_json_keys_are_refused() -> None:
    with pytest.raises(ProjectConfigurationError) as captured:
        ProjectConfigurationValidator().parse(
            '{"config_version":"1.0","config_version":"1.0"}'
        )

    assert captured.value.code == "DUPLICATE_JSON_KEY"


def test_unknown_version_is_refused_explicitly() -> None:
    candidate = valid_candidate()
    candidate["config_version"] = "2.0"

    with pytest.raises(ProjectConfigurationError) as captured:
        ProjectConfigurationValidator().validate(candidate)

    assert captured.value.code == "UNKNOWN_CONFIG_VERSION"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.pop("project_id"),
        lambda item: item.update({"unexpected": True}),
        lambda item: item["codex_constraints"].update({"approval_policy": "ask"}),
        lambda item: item["verification_commands"][0].update({"required": "yes"}),
    ],
)
def test_missing_extra_invalid_enum_and_invalid_type_are_refused(mutation: object) -> None:
    candidate = valid_candidate()
    mutation(candidate)  # type: ignore[operator]

    with pytest.raises(ProjectConfigurationError) as captured:
        ProjectConfigurationValidator().validate(candidate)

    assert captured.value.code == "INVALID_SCHEMA"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("allowed_paths", "../outside", "PATH_TRAVERSAL"),
        ("protected_paths", "C:/source/AGENTIC_ENGINEERING_OS/docs", "ABSOLUTE_PATH"),
        ("allowed_paths", "/opt/source/AGENTIC_ENGINEERING_OS", "ABSOLUTE_PATH"),
        ("allowed_paths", "src\\package", "ABSOLUTE_PATH"),
        ("allowed_paths", ".", "INVALID_PATH"),
        ("allowed_paths", ".git/hooks", "RESERVED_PATH"),
        ("protected_paths", ".agentic-engineering-os/state.json", "RESERVED_PATH"),
        ("allowed_paths", "credentials/private.pem", "RESERVED_PATH"),
    ],
)
def test_unsafe_paths_are_refused(field: str, value: str, code: str) -> None:
    candidate = valid_candidate()
    candidate["path_policy"][field] = [value]  # type: ignore[index]

    with pytest.raises(ProjectConfigurationError) as captured:
        ProjectConfigurationValidator().validate(candidate)

    assert captured.value.code == code


def test_duplicate_normalized_paths_are_refused() -> None:
    candidate = valid_candidate()
    candidate["path_policy"]["allowed_paths"] = ["SRC", "src"]  # type: ignore[index]

    with pytest.raises(ProjectConfigurationError) as captured:
        ProjectConfigurationValidator().validate(candidate)

    assert captured.value.code == "DUPLICATE_NORMALIZED_VALUE"


def test_non_canonical_collection_order_is_refused() -> None:
    candidate = valid_candidate()
    candidate["toolchains"] = list(reversed(candidate["toolchains"]))  # type: ignore[arg-type]

    with pytest.raises(ProjectConfigurationError) as captured:
        ProjectConfigurationValidator().validate(candidate)

    assert captured.value.code == "NON_CANONICAL_ORDER"


def test_toolchain_constraint_cannot_embed_machine_path() -> None:
    candidate = valid_candidate()
    candidate["toolchains"][0]["version_constraint"] = "file:///D:/toolchain"  # type: ignore[index]

    with pytest.raises(ProjectConfigurationError) as captured:
        ProjectConfigurationValidator().validate(candidate)

    assert captured.value.code == "INVALID_TOOLCHAIN"


@pytest.mark.parametrize(
    ("allowed", "forbidden"),
    [
        (["src"], ["src"]),
        (["src/generated/client"], ["src/generated"]),
    ],
)
def test_contradictory_path_rules_are_refused(
    allowed: list[str], forbidden: list[str]
) -> None:
    candidate = valid_candidate()
    candidate["path_policy"] = {
        "allowed_paths": allowed,
        "protected_paths": [],
        "forbidden_paths": forbidden,
    }

    with pytest.raises(ProjectConfigurationError) as captured:
        ProjectConfigurationValidator().validate(candidate)

    assert captured.value.code == "CONFLICTING_PATHS"


def test_nested_forbidden_path_validly_overrides_allowed_parent() -> None:
    candidate = valid_candidate()
    candidate["path_policy"] = {
        "allowed_paths": ["src"],
        "protected_paths": [],
        "forbidden_paths": ["src/generated"],
    }

    config = ProjectConfigurationValidator().validate(candidate)

    assert config.path_policy.allowed_paths == ("src",)
    assert config.path_policy.forbidden_paths == ("src/generated",)


@pytest.mark.parametrize(
    ("executable", "args", "code"),
    [
        ("python -m pytest", [], "INVALID_COMMAND"),
        ("powershell.exe", ["-File", "test.ps1"], "INVALID_COMMAND"),
        ("python", ["-m", "pytest", "&&", "echo"], "INVALID_COMMAND"),
        ("python", ["C:/source/tests.py"], "ABSOLUTE_PATH"),
        ("python", ["--schema=D:/source/AGENTIC_ENGINEERING_OS/schema.json"], "ABSOLUTE_PATH"),
        ("python", ["../outside.py"], "PATH_TRAVERSAL"),
    ],
)
def test_ambiguous_or_unsafe_commands_are_refused(
    executable: str, args: list[str], code: str
) -> None:
    candidate = valid_candidate()
    command = candidate["verification_commands"][0]  # type: ignore[index]
    command["executable"] = executable
    command["args"] = args

    with pytest.raises(ProjectConfigurationError) as captured:
        ProjectConfigurationValidator().validate(candidate)

    assert captured.value.code == code


def test_command_cwd_cannot_target_runtime_state() -> None:
    candidate = valid_candidate()
    candidate["verification_commands"][0]["cwd"] = ".agentic-engineering-os"  # type: ignore[index]

    with pytest.raises(ProjectConfigurationError) as captured:
        ProjectConfigurationValidator().validate(candidate)

    assert captured.value.code == "RESERVED_PATH"


@pytest.mark.parametrize(
    "secret",
    [
        "password=hunter2",
        "api_key:abcd1234",
        "https://user:password@example.invalid/repository",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_secret_like_values_are_refused_everywhere(secret: str) -> None:
    candidate = valid_candidate()
    candidate["verification_commands"][0]["args"] = [secret]  # type: ignore[index]

    with pytest.raises(ProjectConfigurationError) as captured:
        ProjectConfigurationValidator().validate(candidate)

    assert captured.value.code == "SECRET_VALUE"


def test_validation_never_mutates_or_supplies_missing_defaults() -> None:
    candidate = valid_candidate()
    del candidate["mission_state_git_policy"]
    before = copy.deepcopy(candidate)

    with pytest.raises(ProjectConfigurationError):
        ProjectConfigurationValidator().validate(candidate)

    assert candidate == before
