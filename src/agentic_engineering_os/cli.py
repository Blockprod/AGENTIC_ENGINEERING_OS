"""Thin, fail-closed command-line adapter for the Phase 5 boundaries."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Sequence

from agentic_engineering_os.application import ExistingRepositoryAdoption, UpgradePlanner
from agentic_engineering_os.domain import (
    AdoptionStatus,
    HumanOperationConfirmation,
    HumanUpgradeConfirmation,
    UpgradePlanStatus,
    UpgradeResultStatus,
)
from agentic_engineering_os.infrastructure import (
    ProjectConfigurationLoader,
    ProjectConfigurationValidator,
    RepositoryReconnaissance,
    RepositoryUpgradeService,
)
from agentic_engineering_os.infrastructure.project_configuration import (
    CONFIG_DIRECTORY,
    CONFIG_FILENAME,
    ProjectConfigurationError,
)
from agentic_engineering_os.diagnostics_cli import (
    DIAGNOSTIC_COMMANDS,
    OperatorDiagnosticError,
    add_diagnostic_subparsers,
    execute_diagnostic_command,
)


EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 2
_MAX_CONFIGURATION_BYTES = 1_000_000
_MAX_OUTPUT_BYTES = 1_000_000


class CliError(RuntimeError):
    """A bounded CLI input or product operation failed closed."""

    def __init__(self, code: str, message: str, *, exit_code: int = EXIT_BLOCKED) -> None:
        self.code = code
        self.message = message
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse argv, delegate to existing services, and return a stable exit code."""

    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        repository = _repository_root(arguments.repository)
        if arguments.command == "inspect":
            return _inspect(repository, arguments.json)
        if arguments.command == "status":
            return _status(repository, arguments.json)
        if arguments.command == "plan":
            return _plan(repository, arguments.configuration, arguments.json)
        if arguments.command == "init":
            return _init(repository, arguments)
        if arguments.command == "upgrade":
            return _upgrade(repository, arguments)
        if arguments.command in DIAGNOSTIC_COMMANDS:
            diagnostic = execute_diagnostic_command(repository, arguments)
            _emit(
                arguments.command,
                diagnostic.status,
                diagnostic.result,
                arguments.json,
            )
            return diagnostic.exit_code
        raise CliError("UNKNOWN_COMMAND", "command is not supported")
    except CliError as error:
        _emit(
            arguments.command,
            "BLOCKED" if error.exit_code == EXIT_BLOCKED else "ERROR",
            {"code": error.code, "detail": error.message},
            arguments.json,
            stream=sys.stderr,
        )
        return error.exit_code
    except OperatorDiagnosticError as error:
        _emit(
            arguments.command,
            "BLOCKED",
            {"code": error.code, "detail": error.message},
            arguments.json,
            stream=sys.stderr,
        )
        return EXIT_BLOCKED
    except (OSError, ProjectConfigurationError) as error:
        _emit(
            arguments.command,
            "BLOCKED",
            {
                "code": str(getattr(error, "code", type(error).__name__)),
                "detail": str(getattr(error, "message", "operation failed closed")),
            },
            arguments.json,
            stream=sys.stderr,
        )
        return EXIT_BLOCKED
    except Exception as error:  # pragma: no cover - defensive CLI boundary
        _emit(
            arguments.command,
            "ERROR",
            {"code": type(error).__name__, "detail": "unexpected product failure"},
            arguments.json,
            stream=sys.stderr,
        )
        return EXIT_ERROR


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentic-os",
        description="Inspect, plan, adopt, and explicitly upgrade an Agentic OS repository.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "status"):
        command = subparsers.add_parser(name)
        _common_arguments(command)

    plan = subparsers.add_parser("plan")
    _common_arguments(plan)
    _configuration_argument(plan)

    initialize = subparsers.add_parser("init")
    _common_arguments(initialize)
    _configuration_argument(initialize)
    initialize.add_argument(
        "--apply",
        action="store_true",
        help="apply the exact freshly prepared adoption plan",
    )
    _confirmation_arguments(initialize)

    upgrade = subparsers.add_parser("upgrade")
    _common_arguments(upgrade)
    upgrade.add_argument(
        "--apply",
        action="store_true",
        help="apply the exact freshly prepared upgrade plan",
    )
    _confirmation_arguments(upgrade)
    add_diagnostic_subparsers(subparsers)
    return parser


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repository",
        default=".",
        help="target repository root (default: current directory)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic compact JSON",
    )


def _configuration_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--configuration",
        help="explicit ProjectConfiguration JSON for an uninitialized repository",
    )


def _confirmation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--confirm",
        action="append",
        default=[],
        metavar="ID",
        help="confirm one exact operation/step ID; repeat for each required ID",
    )
    parser.add_argument(
        "--confirmed-by",
        help="attributable Human identity bound to each explicit confirmation",
    )


def _inspect(repository: Path, json_output: bool) -> int:
    profile = RepositoryReconnaissance().inspect(repository)
    status = profile.support_status.value
    _emit("inspect", status, profile, json_output)
    return EXIT_SUCCESS if status == "SUPPORTED" else EXIT_BLOCKED


def _status(repository: Path, json_output: bool) -> int:
    preparation = ExistingRepositoryAdoption().prepare_adoption(repository)
    status = _display_status(preparation)
    _emit("status", status, preparation, json_output)
    return (
        EXIT_SUCCESS
        if status in {AdoptionStatus.ADOPTED.value, AdoptionStatus.READY_TO_APPLY.value}
        else EXIT_BLOCKED
    )


def _plan(repository: Path, configuration_path: str | None, json_output: bool) -> int:
    configuration = _configuration(repository, configuration_path)
    preparation = ExistingRepositoryAdoption().prepare_adoption(
        repository, configuration
    )
    _emit("plan", preparation.status.value, preparation, json_output)
    return (
        EXIT_SUCCESS
        if preparation.status
        in {
            AdoptionStatus.READY_TO_APPLY,
            AdoptionStatus.NEEDS_HUMAN_CONFIRMATION,
        }
        else EXIT_BLOCKED
    )


def _init(repository: Path, arguments: argparse.Namespace) -> int:
    configuration = _configuration(repository, arguments.configuration)
    adoption = ExistingRepositoryAdoption()
    preparation = adoption.prepare_adoption(repository, configuration)
    if not arguments.apply:
        _emit("init", preparation.status.value, preparation, arguments.json)
        return (
            EXIT_SUCCESS
            if preparation.status
            in {
                AdoptionStatus.READY_TO_APPLY,
                AdoptionStatus.NEEDS_HUMAN_CONFIRMATION,
            }
            else EXIT_BLOCKED
        )
    if preparation.status not in {
        AdoptionStatus.READY_TO_APPLY,
        AdoptionStatus.NEEDS_HUMAN_CONFIRMATION,
    }:
        _emit("init", preparation.status.value, preparation, arguments.json)
        return EXIT_BLOCKED
    confirmations = _initialization_confirmations(
        preparation, arguments.confirm, arguments.confirmed_by
    )
    result = adoption.apply_adoption(
        preparation, human_confirmations=confirmations
    )
    _emit("init", result.status.value, result, arguments.json)
    return EXIT_SUCCESS if result.status is AdoptionStatus.ADOPTED else EXIT_BLOCKED


def _upgrade(repository: Path, arguments: argparse.Namespace) -> int:
    planner = UpgradePlanner()
    plan = planner.plan(repository)
    if not arguments.apply:
        _emit("upgrade", plan.status.value, plan, arguments.json)
        return EXIT_BLOCKED if plan.status is UpgradePlanStatus.BLOCKED else EXIT_SUCCESS
    if plan.status is UpgradePlanStatus.BLOCKED:
        _emit("upgrade", plan.status.value, plan, arguments.json)
        return EXIT_BLOCKED
    confirmations = _upgrade_confirmations(
        plan, arguments.confirm, arguments.confirmed_by
    )
    result = RepositoryUpgradeService().apply(plan, confirmations=confirmations)
    _emit("upgrade", result.status.value, result, arguments.json)
    return (
        EXIT_SUCCESS
        if result.status
        in {UpgradeResultStatus.MIGRATED, UpgradeResultStatus.ALREADY_CURRENT}
        else EXIT_BLOCKED
    )


def _configuration(repository: Path, explicit_path: str | None):
    if explicit_path is None:
        canonical = repository / CONFIG_DIRECTORY / CONFIG_FILENAME
        if not canonical.exists() and not canonical.is_symlink():
            return None
        return ProjectConfigurationLoader(repository).load()
    source = _explicit_file(explicit_path)
    if source.stat().st_size > _MAX_CONFIGURATION_BYTES:
        raise CliError("CONFIGURATION_TOO_LARGE", "configuration exceeds CLI policy")
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeError as error:
        raise CliError("INVALID_CONFIGURATION", "configuration is not UTF-8") from error
    return ProjectConfigurationValidator().parse(text)


def _initialization_confirmations(preparation, identifiers, confirmed_by):
    plan = preparation.initialization_plan
    if plan is None:
        raise CliError("PLAN_UNAVAILABLE", "initialization plan is unavailable")
    required = {
        item.operation_id: item
        for item in plan.operations
        if item.human_confirmation_required
    }
    supplied = _confirmation_selection(required, identifiers, confirmed_by)
    return tuple(
        HumanOperationConfirmation(
            plan.input_fingerprint,
            operation.operation_id,
            operation.target_path,
            operation.expected_current_state,
            str(operation.expected_target_fingerprint),
            str(confirmed_by),
        )
        for operation in supplied
    )


def _upgrade_confirmations(plan, identifiers, confirmed_by):
    required = {
        item.step_id: item for item in plan.steps if item.human_confirmation_required
    }
    supplied = _confirmation_selection(required, identifiers, confirmed_by)
    return tuple(
        HumanUpgradeConfirmation(
            plan.plan_fingerprint,
            step.step_id,
            step.artifact,
            step.source_fingerprint,
            step.target_version,
            str(confirmed_by),
        )
        for step in supplied
    )


def _confirmation_selection(required, identifiers, confirmed_by):
    if not isinstance(identifiers, list) or any(
        not isinstance(item, str) or not item for item in identifiers
    ):
        raise CliError("INVALID_CONFIRMATION", "confirmation IDs are invalid")
    if len(identifiers) != len(set(identifiers)):
        raise CliError("DUPLICATE_CONFIRMATION", "confirmation IDs must be unique")
    if set(identifiers) != set(required):
        raise CliError(
            "HUMAN_CONFIRMATION_REQUIRED",
            "confirmation IDs must exactly match the plan requirements",
        )
    if identifiers and not confirmed_by:
        raise CliError(
            "HUMAN_IDENTITY_REQUIRED",
            "an attributable Human identity is required",
        )
    if not identifiers and confirmed_by:
        raise CliError(
            "UNEXPECTED_HUMAN_IDENTITY",
            "Human identity was supplied without a required confirmation",
        )
    return tuple(required[item] for item in sorted(identifiers))


def _display_status(preparation) -> str:
    profile = preparation.repository_profile
    plan = preparation.initialization_plan
    if (
        profile is not None
        and profile.agentic_os.state.value == "INITIALIZED"
        and plan is not None
        and not plan.blockers
        and all(item.operation_type.value == "NO_OP" for item in plan.operations)
        and any(
            item.relative_path == ".agentic-engineering-os/state.json"
            and item.status.value == "VERSION_OBSERVED"
            for item in profile.agentic_os.runtime_files
        )
    ):
        return AdoptionStatus.ADOPTED.value
    return preparation.status.value


def _repository_root(raw: str) -> Path:
    candidate = Path(raw)
    if any(part == ".." for part in candidate.parts):
        raise CliError("UNSAFE_REPOSITORY_PATH", "repository traversal is refused")
    absolute = candidate if candidate.is_absolute() else Path.cwd() / candidate
    lexical = Path(os.path.abspath(absolute))
    if lexical.is_symlink() or _has_symlink_component(lexical):
        raise CliError("UNSAFE_REPOSITORY_PATH", "repository symlinks are refused")
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as error:
        raise CliError("REPOSITORY_NOT_FOUND", "repository root cannot be resolved") from error
    if not resolved.is_dir() or os.path.normcase(str(resolved)) != os.path.normcase(
        str(lexical)
    ):
        raise CliError("UNSAFE_REPOSITORY_PATH", "repository root is not canonical")
    return resolved


def _explicit_file(raw: str) -> Path:
    candidate = Path(raw)
    if any(part == ".." for part in candidate.parts):
        raise CliError("UNSAFE_CONFIGURATION_PATH", "configuration traversal is refused")
    absolute = candidate if candidate.is_absolute() else Path.cwd() / candidate
    lexical = Path(os.path.abspath(absolute))
    if lexical.is_symlink() or _has_symlink_component(lexical):
        raise CliError("UNSAFE_CONFIGURATION_PATH", "configuration symlinks are refused")
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as error:
        raise CliError("CONFIGURATION_NOT_FOUND", "configuration cannot be resolved") from error
    if not resolved.is_file() or os.path.normcase(str(resolved)) != os.path.normcase(
        str(lexical)
    ):
        raise CliError("UNSAFE_CONFIGURATION_PATH", "configuration is not a regular file")
    return resolved


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


def _emit(command: str, status: str, result, json_output: bool, *, stream=None) -> None:
    output = sys.stdout if stream is None else stream
    payload = {"command": command, "result": _json_value(result), "status": status}
    serialized = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        indent=None if json_output else 2,
        separators=(",", ":") if json_output else None,
        allow_nan=False,
    )
    if len(serialized.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        raise CliError("OUTPUT_LIMIT_EXCEEDED", "serialized CLI output exceeds policy")
    print(serialized, file=output)


def _json_value(value):
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if value is None or isinstance(value, str | bool | int | float):
        return value
    raise CliError("UNSERIALIZABLE_RESULT", "service result cannot be serialized")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
