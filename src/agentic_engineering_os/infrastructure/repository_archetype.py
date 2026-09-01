"""Read-only repository archetype profiling and readiness evaluation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tomllib
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

from agentic_engineering_os.domain import (
    ArchetypeAssessment,
    ArchetypeComponent,
    ArchetypeSupportLevel,
    CapabilityState,
    PlatformCapabilities,
    ProjectConfiguration,
    RepositoryArchetype,
    RepositoryArchetypeProfile,
    RepositoryProfile,
    RepositorySupportStatus,
    ToolchainAvailability,
    ToolchainMachineFact,
    VerificationCommand,
    VerificationCommandContract,
    VerificationKind,
)

from .platform_environment import (
    RUNTIME_ENVIRONMENT_ALLOWLIST,
    build_bounded_environment,
    discover_executable,
)
from .project_configuration import (
    ProjectConfigurationError,
    ProjectConfigurationValidator,
)


_ARCHETYPE_EXECUTABLES = {
    RepositoryArchetype.PYTHON: frozenset({"py", "pytest", "python", "python3"}),
    RepositoryArchetype.NODE: frozenset(
        {"node", "npm", "npm.cmd", "npx", "npx.cmd", "pnpm", "yarn"}
    ),
    RepositoryArchetype.RUST: frozenset({"cargo", "rustc"}),
}
_TOOLCHAIN_IDENTITIES = {
    RepositoryArchetype.PYTHON: "python",
    RepositoryArchetype.NODE: "node",
    RepositoryArchetype.RUST: "rust",
}
_NODE_LOCKFILES = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock")
_PYTHON_LOCKFILES = ("Pipfile.lock", "poetry.lock", "uv.lock")
_SHELL_METACHARACTERS = frozenset(";&|<>")
_AMBIGUITY_CODES = frozenset(
    {
        "AMBIGUOUS_COMMAND_OWNERSHIP",
        "INVALID_MANIFEST",
        "MULTIPLE_NODE_LOCKFILES",
        "OVERLAPPING_COMPONENT_SCOPES",
        "PROFILE_COMMAND_MISMATCH",
    }
)


class RepositoryArchetypeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class _DuplicateJsonKeyError(ValueError):
    pass


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(key)
        result[key] = value
    return result


class RepositoryArchetypeProfiler:
    """Build an immutable profile from repository facts and explicit config."""

    def __init__(self, *, max_manifest_bytes: int = 256_000) -> None:
        if (
            not isinstance(max_manifest_bytes, int)
            or isinstance(max_manifest_bytes, bool)
            or max_manifest_bytes <= 0
        ):
            raise ValueError("manifest size limit must be a positive integer")
        self._max_manifest_bytes = max_manifest_bytes

    def build(
        self,
        repository_profile: RepositoryProfile,
        configuration: ProjectConfiguration,
    ) -> RepositoryArchetypeProfile:
        if not isinstance(repository_profile, RepositoryProfile):
            raise RepositoryArchetypeError(
                "INVALID_REPOSITORY_PROFILE", "canonical RepositoryProfile is required"
            )
        configuration_fingerprint = _configuration_fingerprint(configuration)
        root = Path(repository_profile.requested_root)
        try:
            resolved_root = root.resolve(strict=True)
        except OSError as error:
            raise RepositoryArchetypeError(
                "INVALID_REPOSITORY_ROOT", "profile repository root is unavailable"
            ) from error
        if not resolved_root.is_dir():
            raise RepositoryArchetypeError(
                "INVALID_REPOSITORY_ROOT", "profile repository root is not a directory"
            )

        blockers: list[str] = []
        if repository_profile.support_status is not RepositorySupportStatus.SUPPORTED:
            blockers.append("REPOSITORY_NOT_SUPPORTED")
        if not repository_profile.scan_complete:
            blockers.append("RECONNAISSANCE_INCOMPLETE")

        scopes = tuple(
            sorted(
                {
                    ".",
                    *(command.cwd for command in configuration.verification_commands),
                    *(
                        item.relative_path
                        for item in repository_profile.top_level_entries
                        if item.kind == "DIRECTORY"
                    ),
                },
                key=_path_sort_key,
            )
        )
        components: list[ArchetypeComponent] = []
        for scope in scopes:
            components.extend(
                _components_at_scope(
                    resolved_root, scope, self._max_manifest_bytes
                )
            )

        for left_index, left in enumerate(components):
            blockers.extend(left.blockers)
            for right in components[left_index + 1 :]:
                if left.archetype is right.archetype:
                    continue
                if _scopes_overlap(left.root, right.root):
                    blockers.append("OVERLAPPING_COMPONENT_SCOPES")

        contracts, command_blockers = _bind_commands(
            configuration.verification_commands,
            tuple(components),
            configuration,
        )
        blockers.extend(command_blockers)
        configured_by_component: dict[str, list[str]] = {}
        for contract in contracts:
            if contract.owner_component_id is not None:
                configured_by_component.setdefault(contract.owner_component_id, []).append(
                    contract.command_id
                )

        components = [
            replace(
                component,
                configured_command_ids=tuple(
                    sorted(configured_by_component.get(component.component_id, ()), key=_key)
                ),
                source_scopes=_owned_paths(
                    component.root, configuration.path_policy.allowed_paths
                ),
                test_scopes=tuple(
                    sorted(
                        {
                            command.cwd
                            for command in configuration.verification_commands
                            if command.kind is VerificationKind.TEST
                            and _scope_contains(component.root, command.cwd)
                        },
                        key=_path_sort_key,
                    )
                ),
                build_scopes=tuple(
                    sorted(
                        {
                            command.cwd
                            for command in configuration.verification_commands
                            if command.kind is VerificationKind.BUILD
                            and _scope_contains(component.root, command.cwd)
                        },
                        key=_path_sort_key,
                    )
                ),
            )
            for component in components
        ]
        if not components:
            blockers.append("UNKNOWN_ARCHETYPE")
        return RepositoryArchetypeProfile(
            repository_root=str(resolved_root),
            project_id=configuration.project_id,
            configuration_fingerprint=configuration_fingerprint,
            components=tuple(
                sorted(components, key=lambda item: (_path_sort_key(item.root), item.archetype.value))
            ),
            command_contracts=contracts,
            blockers=tuple(sorted(set(blockers), key=_key)),
        )


class RepositoryToolchainProbe:
    """Observe fresh executable facts; never persist or authorize them."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        executable_locator: Callable[[str, str | None], str | None] | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("toolchain probe timeout must be positive")
        self._environment = dict(os.environ if environment is None else environment)
        self._locator = executable_locator
        self._timeout_seconds = float(timeout_seconds)

    def observe(
        self, profile: RepositoryArchetypeProfile
    ) -> tuple[ToolchainMachineFact, ...]:
        root = Path(profile.repository_root)
        requests = {
            (contract.owner_archetype, contract.executable)
            for contract in profile.command_contracts
            if contract.owner_archetype is not None
        }
        facts = [
            self._observe_one(root, archetype, executable)
            for archetype, executable in sorted(
                requests,
                key=lambda item: (item[0].value if item[0] else "", _key(item[1])),
            )
            if archetype is not None
        ]
        return tuple(facts)

    def _observe_one(
        self,
        root: Path,
        archetype: RepositoryArchetype,
        requested: str,
    ) -> ToolchainMachineFact:
        discovered = discover_executable(
            requested,
            self._environment,
            locator=self._locator,
            identity=requested,
        )
        if discovered.state is not CapabilityState.SUPPORTED or discovered.path is None:
            return ToolchainMachineFact(
                archetype,
                requested,
                ToolchainAvailability.UNAVAILABLE,
                None,
                None,
                discovered.discovery_method,
                None,
                None,
                None,
            )
        path = Path(discovered.path)
        try:
            stat = path.stat()
            result = subprocess.run(
                [str(path), "--version"],
                shell=False,
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=build_bounded_environment(
                    self._environment, RUNTIME_ENVIRONMENT_ALLOWLIST
                ),
                timeout=self._timeout_seconds,
                check=False,
            )
            observed_sha256 = _sha256_file(path)
        except (OSError, subprocess.TimeoutExpired):
            return ToolchainMachineFact(
                archetype,
                requested,
                ToolchainAvailability.UNKNOWN,
                str(path),
                None,
                discovered.discovery_method,
                None,
                None,
                None,
            )
        version = (result.stdout.strip() or result.stderr.strip()) if result.returncode == 0 else None
        if version is not None and not _version_matches(archetype, requested, version):
            version = None
        return ToolchainMachineFact(
            archetype,
            requested,
            ToolchainAvailability.AVAILABLE if version else ToolchainAvailability.UNKNOWN,
            str(path),
            version,
            discovered.discovery_method,
            stat.st_size,
            stat.st_mtime_ns,
            observed_sha256,
        )


class RepositoryArchetypeEvaluator:
    """Assess readiness without executing any configured project command."""

    def evaluate(
        self,
        profile: RepositoryArchetypeProfile,
        configuration: ProjectConfiguration,
        platform_capabilities: PlatformCapabilities,
        machine_facts: tuple[ToolchainMachineFact, ...],
    ) -> ArchetypeAssessment:
        configuration_fingerprint = _configuration_fingerprint(configuration)
        blockers = list(profile.blockers)
        if profile.project_id != configuration.project_id:
            blockers.append("PROJECT_CONFIGURATION_MISMATCH")
        if profile.configuration_fingerprint != configuration_fingerprint:
            blockers.append("PROFILE_CONFIGURATION_MISMATCH")
        if _path_key(profile.repository_root) != _path_key(
            platform_capabilities.project.repository_root
        ):
            blockers.append("CROSS_REPOSITORY_PROFILE")
        expected_contracts, binding_blockers = _bind_commands(
            configuration.verification_commands,
            profile.components,
            configuration,
        )
        blockers.extend(binding_blockers)
        if profile.command_contracts != expected_contracts:
            blockers.append("PROFILE_COMMAND_MISMATCH")
        try:
            platform_capabilities.require_windows_v1_local_safety()
        except ValueError as error:
            blockers.append(f"PLATFORM_CAPABILITY:{error}")

        fact_index: dict[tuple[RepositoryArchetype, str], list[ToolchainMachineFact]] = {}
        for fact in machine_facts:
            fact_index.setdefault(
                (fact.archetype, _key(fact.requested_executable)), []
            ).append(fact)

        executable: list[ToolchainMachineFact] = []
        for contract in profile.command_contracts:
            if any(character in argument for argument in contract.args for character in _SHELL_METACHARACTERS):
                blockers.append(f"SHELL_METACHARACTER:{contract.command_id}")
            if contract.owner_archetype is None or contract.owner_component_id is None:
                if contract.required:
                    blockers.append(f"COMMAND_WITHOUT_OWNER:{contract.command_id}")
                continue
            facts = fact_index.get(
                (contract.owner_archetype, _key(contract.executable)), []
            )
            if len(facts) != 1:
                if contract.required:
                    blockers.append(
                        f"{'AMBIGUOUS' if facts else 'MISSING'}_MACHINE_FACT:{contract.command_id}"
                    )
                continue
            fact = facts[0]
            if fact.availability is not ToolchainAvailability.AVAILABLE:
                if contract.required:
                    blockers.append(f"TOOLCHAIN_{fact.availability.value}:{contract.command_id}")
                continue
            if not _fact_is_current(fact):
                blockers.append(f"STALE_MACHINE_FACT:{contract.command_id}")
                continue
            executable.append(fact)

        declarations = {
            _key(item.identity): item.version_constraint
            for item in configuration.toolchains
        }
        for component in profile.components:
            constraint = declarations.get(_key(_TOOLCHAIN_IDENTITIES[component.archetype]))
            if constraint is not None:
                blockers.append(
                    f"VERSION_CONSTRAINT_NOT_EVALUATED:{component.archetype.value}"
                )

        normalized_blockers = tuple(sorted(set(blockers), key=_key))
        level = _support_level(profile, normalized_blockers)
        return ArchetypeAssessment(
            repository_root=profile.repository_root,
            project_id=profile.project_id,
            detected_archetypes=tuple(
                sorted({item.archetype for item in profile.components}, key=lambda item: item.value)
            ),
            configured_commands=profile.command_contracts,
            executable_toolchains=tuple(
                sorted(
                    set(executable),
                    key=lambda item: (item.archetype.value, _key(item.requested_executable)),
                )
            ),
            blockers=normalized_blockers,
            support_level=level,
        )


def _components_at_scope(
    root: Path, scope: str, max_manifest_bytes: int
) -> list[ArchetypeComponent]:
    directory = root if scope == "." else root.joinpath(*PurePosixPath(scope).parts)
    try:
        resolved = directory.resolve(strict=True)
    except OSError:
        return []
    if not resolved.is_dir() or not _contains(root, resolved) or directory.is_symlink():
        return []
    names = {item.name for item in directory.iterdir() if not item.is_symlink() and item.is_file()}
    result: list[ArchetypeComponent] = []
    python_manifests = tuple(
        name for name in ("pyproject.toml", "setup.cfg", "setup.py") if name in names
    )
    if python_manifests:
        blockers = _manifest_blockers(
            directory, python_manifests, max_manifest_bytes
        )
        result.append(
            _component(
                RepositoryArchetype.PYTHON,
                scope,
                python_manifests,
                tuple(name for name in _PYTHON_LOCKFILES if name in names),
                None,
                False,
                (),
                blockers,
            )
        )
    if "package.json" in names:
        blockers, scripts = _node_manifest(
            directory / "package.json", max_manifest_bytes
        )
        locks = tuple(name for name in _NODE_LOCKFILES if name in names)
        if len(locks) > 1:
            blockers = (*blockers, "MULTIPLE_NODE_LOCKFILES")
        manager = (
            {
                "package-lock.json": "npm",
                "pnpm-lock.yaml": "pnpm",
                "yarn.lock": "yarn",
            }[locks[0]]
            if len(locks) == 1
            else None
        )
        result.append(
            _component(
                RepositoryArchetype.NODE,
                scope,
                ("package.json",),
                locks,
                manager,
                False,
                scripts,
                blockers,
            )
        )
    if "Cargo.toml" in names:
        blockers = _manifest_blockers(
            directory, ("Cargo.toml",), max_manifest_bytes
        )
        workspace = _rust_workspace(
            directory / "Cargo.toml", max_manifest_bytes
        )
        result.append(
            _component(
                RepositoryArchetype.RUST,
                scope,
                ("Cargo.toml",),
                ("Cargo.lock",) if "Cargo.lock" in names else (),
                "cargo",
                workspace,
                (),
                blockers,
            )
        )
    return result


def _component(
    archetype: RepositoryArchetype,
    root: str,
    manifests: tuple[str, ...],
    lockfiles: tuple[str, ...],
    package_manager: str | None,
    workspace: bool,
    scripts: tuple[str, ...],
    blockers: tuple[str, ...],
) -> ArchetypeComponent:
    return ArchetypeComponent(
        component_id=f"{archetype.value.casefold()}:{root}",
        archetype=archetype,
        root=root,
        detected_toolchains=(_TOOLCHAIN_IDENTITIES[archetype],),
        manifests=manifests,
        lockfiles=lockfiles,
        package_manager=package_manager,
        workspace=workspace,
        declared_scripts=scripts,
        configured_command_ids=(),
        source_scopes=(),
        test_scopes=(),
        build_scopes=(),
        required_capabilities=(
            "WINDOWS_V1_LOCAL",
            f"TOOLCHAIN:{_TOOLCHAIN_IDENTITIES[archetype]}",
        ),
        blockers=tuple(sorted(set(blockers), key=_key)),
    )


def _manifest_blockers(
    directory: Path, manifests: tuple[str, ...], maximum: int
) -> tuple[str, ...]:
    blockers: list[str] = []
    for name in manifests:
        if name == "setup.py":
            continue
        try:
            text = _read_bounded_text(directory / name, maximum)
            if name.endswith(".toml"):
                tomllib.loads(text)
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            blockers.append("INVALID_MANIFEST")
    return tuple(blockers)


def _node_manifest(
    path: Path, maximum: int
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        data = json.loads(
            _read_bounded_text(path, maximum),
            object_pairs_hook=_strict_json_object,
            parse_constant=_raise_invalid_constant,
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        _DuplicateJsonKeyError,
        ValueError,
    ):
        return ("INVALID_MANIFEST",), ()
    if not isinstance(data, Mapping):
        return ("INVALID_MANIFEST",), ()
    scripts = data.get("scripts", {})
    if not isinstance(scripts, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in scripts.items()
    ):
        return ("INVALID_MANIFEST",), ()
    return (), tuple(sorted(scripts, key=_key))


def _rust_workspace(path: Path, maximum: int) -> bool:
    try:
        data = tomllib.loads(_read_bounded_text(path, maximum))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return False
    return isinstance(data, Mapping) and "workspace" in data


def _read_bounded_text(path: Path, maximum: int) -> str:
    if path.stat().st_size > maximum:
        raise OSError("manifest exceeds bounded size")
    return path.read_text(encoding="utf-8")


def _raise_invalid_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _bind_commands(
    commands: tuple[VerificationCommand, ...],
    components: tuple[ArchetypeComponent, ...],
    configuration: ProjectConfiguration,
) -> tuple[tuple[VerificationCommandContract, ...], tuple[str, ...]]:
    declared = {_key(item.identity) for item in configuration.toolchains}
    contracts: list[VerificationCommandContract] = []
    blockers: list[str] = []
    for command in commands:
        candidates = tuple(
            component
            for component in components
            if _scope_contains(component.root, command.cwd)
        )
        compatible = tuple(
            component
            for component in candidates
            if _key(command.executable)[0]
            in _ARCHETYPE_EXECUTABLES[component.archetype]
        )
        owner: ArchetypeComponent | None = None
        if compatible:
            maximum_depth = max(_scope_depth(item.root) for item in compatible)
            nearest = tuple(
                item for item in compatible if _scope_depth(item.root) == maximum_depth
            )
            if len(nearest) == 1:
                owner = nearest[0]
            else:
                blockers.append("AMBIGUOUS_COMMAND_OWNERSHIP")
        elif len(candidates) > 1:
            blockers.append("AMBIGUOUS_COMMAND_OWNERSHIP")
        elif candidates:
            blockers.append("COMMAND_TOOLCHAIN_MISMATCH")
        else:
            blockers.append("CONFIGURED_COMMAND_WITHOUT_DETECTED_TOOLCHAIN")
        if owner is not None and _key(_TOOLCHAIN_IDENTITIES[owner.archetype]) not in declared:
            blockers.append("UNDECLARED_TOOLCHAIN")
        if owner is not None and owner.archetype is RepositoryArchetype.NODE:
            executable = _key(command.executable)[0]
            manager_locks = {
                "npm": "package-lock.json",
                "npm.cmd": "package-lock.json",
                "pnpm": "pnpm-lock.yaml",
                "yarn": "yarn.lock",
            }
            expected_lock = manager_locks.get(executable)
            if expected_lock is not None and expected_lock not in owner.lockfiles:
                blockers.append("PACKAGE_MANAGER_NOT_PROVEN")
            if expected_lock is not None and len(command.args) >= 2 and command.args[0] == "run":
                if command.args[1] not in owner.declared_scripts:
                    blockers.append("NODE_SCRIPT_NOT_DECLARED")
        contracts.append(
            VerificationCommandContract(
                command.command_id,
                command.kind,
                command.executable,
                command.args,
                command.cwd,
                command.required,
                owner.component_id if owner else None,
                owner.archetype if owner else None,
            )
        )
    return tuple(contracts), tuple(blockers)


def _fact_is_current(fact: ToolchainMachineFact) -> bool:
    if fact.resolved_path is None:
        return False
    try:
        path = Path(fact.resolved_path).resolve(strict=True)
        stat = path.stat()
        digest = _sha256_file(path)
    except OSError:
        return False
    return (
        stat.st_size == fact.observed_size
        and stat.st_mtime_ns == fact.observed_mtime_ns
        and digest == fact.observed_sha256
        and path.is_file()
    )


def _support_level(
    profile: RepositoryArchetypeProfile, blockers: tuple[str, ...]
) -> ArchetypeSupportLevel:
    if not profile.components:
        return ArchetypeSupportLevel.UNSUPPORTED
    if any(code.split(":", 1)[0] in _AMBIGUITY_CODES for code in blockers):
        return ArchetypeSupportLevel.AMBIGUOUS
    if not profile.command_contracts:
        return ArchetypeSupportLevel.RECOGNIZED
    if blockers:
        return ArchetypeSupportLevel.ADOPTABLE
    return ArchetypeSupportLevel.EXECUTION_READY


def _version_matches(
    archetype: RepositoryArchetype, requested: str, version: str
) -> bool:
    normalized = version.strip().casefold()
    executable = _key(requested)[0]
    if archetype is RepositoryArchetype.PYTHON:
        return normalized.startswith(("python ", "pytest "))
    if archetype is RepositoryArchetype.NODE:
        if executable.startswith(("npm", "npx", "pnpm", "yarn")):
            return bool(normalized) and normalized[0].isdigit()
        return normalized.startswith("v") and any(character.isdigit() for character in normalized)
    if archetype is RepositoryArchetype.RUST:
        return normalized.startswith(("cargo ", "rustc "))
    return False


def _configuration_fingerprint(configuration: ProjectConfiguration) -> str:
    try:
        canonical = ProjectConfigurationValidator().serialize(configuration)
    except ProjectConfigurationError as error:
        raise RepositoryArchetypeError(
            "INVALID_PROJECT_CONFIGURATION", error.message
        ) from error
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _owned_paths(component_root: str, paths: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {path for path in paths if _scope_contains(component_root, path)},
            key=_path_sort_key,
        )
    )


def _scope_contains(parent: str, child: str) -> bool:
    if parent == ".":
        return True
    return child == parent or child.startswith(f"{parent}/")


def _scopes_overlap(left: str, right: str) -> bool:
    return _scope_contains(left, right) or _scope_contains(right, left)


def _scope_depth(value: str) -> int:
    return 0 if value == "." else len(PurePosixPath(value).parts)


def _contains(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _path_key(value: str) -> str:
    return os.path.normcase(str(Path(value).resolve(strict=False)))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _key(value: str) -> tuple[str, str]:
    normalized = unicodedata.normalize("NFC", value)
    return normalized.casefold(), normalized


def _path_sort_key(value: str) -> tuple[int, tuple[str, str]]:
    return _scope_depth(value), _key(value)
