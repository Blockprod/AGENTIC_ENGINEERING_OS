"""Stable operator CLI boundary for restart-safe missions."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from agentic_engineering_os.application import (
    ContractValidator,
    MissionRequest,
    MissionRunResult,
    MissionRunner,
    MissionRunnerError,
)
from agentic_engineering_os.domain import Evidence, EvidenceType
from agentic_engineering_os.infrastructure import (
    MissionStateStore,
    OrchestrationRecordStore,
    PersistenceError,
    ProjectStateStore,
    RepositoryOperationLock,
)


MISSION_STATUSES_WITHOUT_ATTENTION = frozenset({"ACTIVE", "COMPLETED"})
_MAX_OBJECTIVE_BYTES = 100_000
_MAX_HUMAN_EVIDENCE_BYTES = 100_000


class MissionCliError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class _Runner(Protocol):
    def run(self, request: MissionRequest, *, updated_at: datetime) -> MissionRunResult: ...
    def resume(
        self,
        mission_id: str,
        *,
        updated_at: datetime,
        human_evidence: Evidence | None = None,
    ) -> MissionRunResult: ...


def add_mission_parser(subparsers: argparse._SubParsersAction) -> None:
    mission = subparsers.add_parser(
        "mission",
        help="run, resume, or inspect a production mission",
        description="Stable v1 mission operator interface.",
    )
    commands = mission.add_subparsers(dest="mission_command", required=True)

    run = commands.add_parser("run", help="MUTATING: start and drive a mission")
    _repository_json_arguments(run)
    objective = run.add_mutually_exclusive_group(required=True)
    objective.add_argument("--objective")
    objective.add_argument("--objective-file")
    run.add_argument("--scope", action="append", default=[], metavar="RELATIVE_PATH")
    run.add_argument(
        "--verification-command", action="append", default=[], metavar="ID"
    )

    resume = commands.add_parser("resume", help="MUTATING: resume an exact mission")
    _repository_json_arguments(resume)
    resume.add_argument("--mission-id", required=True)
    resume.add_argument("--human-evidence")

    status = commands.add_parser("status", help="inspect mission state (read-only)")
    _repository_json_arguments(status)
    status.add_argument("--mission-id")


def execute_mission_command(repository: Path, arguments: argparse.Namespace) -> int:
    try:
        command = arguments.mission_command
        if command == "status":
            result = MissionRunner.read_status(
                mission_store=MissionStateStore(repository),
                project_store=ProjectStateStore(repository),
                record_store=OrchestrationRecordStore(repository),
                mission_id=arguments.mission_id,
            )
        elif command == "run":
            objective = _objective(arguments.objective, arguments.objective_file)
            request = MissionRequest(
                objective,
                str(repository),
                _canonical(arguments.scope, "scope"),
                _canonical(arguments.verification_command, "verification command"),
            )
            with RepositoryOperationLock(repository):
                result = build_mission_runner(repository).run(
                    request, updated_at=datetime.now(timezone.utc)
                )
        elif command == "resume":
            evidence = (
                _human_evidence(arguments.human_evidence)
                if arguments.human_evidence is not None
                else None
            )
            with RepositoryOperationLock(repository):
                result = build_mission_runner(repository).resume(
                    arguments.mission_id,
                    updated_at=datetime.now(timezone.utc),
                    human_evidence=evidence,
                )
        else:  # pragma: no cover - argparse closes this set
            raise MissionCliError("UNKNOWN_MISSION_COMMAND", "mission command is unsupported")
    except MissionRunnerError as error:
        raise MissionCliError(error.code, error.message) from error
    except (OSError, ValueError, PersistenceError) as error:
        code = str(getattr(error, "code", type(error).__name__))
        detail = str(getattr(error, "message", "mission input or store is invalid"))
        raise MissionCliError(code, detail) from error
    emit_mission_result(result, arguments.json)
    return 0 if result.status.value in MISSION_STATUSES_WITHOUT_ATTENTION else 2


def build_mission_runner(repository: Path) -> _Runner:
    """Build the production runtime lazily after the repository lock is held."""

    from agentic_engineering_os.application.mission_composition import (
        MissionCompositionError,
        build_production_mission_runner,
    )

    try:
        return build_production_mission_runner(repository)
    except MissionCompositionError as error:
        raise MissionCliError(error.code, error.message) from error
    except Exception as error:
        code = getattr(error, "code", None)
        message = getattr(error, "message", None)
        if isinstance(code, str) and isinstance(message, str):
            raise MissionCliError(code, message) from error
        raise


def emit_mission_result(result: MissionRunResult, json_output: bool) -> None:
    payload = asdict(result)
    payload["status"] = result.status.value
    payload["phase"] = result.phase.value
    payload["current_story_ids"] = list(result.current_story_ids)
    payload["completed_story_ids"] = list(result.completed_story_ids)
    payload["blockers"] = list(result.blockers)
    payload["evidence_references"] = list(result.evidence_references)
    serialized = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        indent=None if json_output else 2,
        separators=(",", ":") if json_output else None,
        allow_nan=False,
    )
    print(serialized)


def emit_mission_error(code: str, detail: str, json_output: bool) -> None:
    payload = {
        "blockers": [code],
        "completed_story_ids": [],
        "current_story_ids": [],
        "detail": detail,
        "evidence_references": [],
        "generation": None,
        "mission_id": None,
        "next_action": "Resolve the reported blocker before an explicit retry.",
        "phase": "ADMISSION",
        "repository_head": None,
        "status": "REFUSED",
    }
    print(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            indent=None if json_output else 2,
            separators=(",", ":") if json_output else None,
        ),
        file=sys.stderr,
    )


def _repository_json_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", required=True)
    parser.add_argument("--json", action="store_true")


def _objective(value: str | None, filename: str | None) -> str:
    if value is not None:
        return value
    assert filename is not None
    path = Path(filename)
    if path.is_symlink():
        raise MissionCliError("UNSAFE_OBJECTIVE_FILE", "objective file cannot be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise MissionCliError("OBJECTIVE_FILE_UNAVAILABLE", "objective file is unavailable") from error
    if not resolved.is_file() or resolved.stat().st_size > _MAX_OBJECTIVE_BYTES:
        raise MissionCliError("OBJECTIVE_FILE_INVALID", "objective file is invalid or too large")
    try:
        return resolved.read_text(encoding="utf-8")
    except UnicodeError as error:
        raise MissionCliError("OBJECTIVE_FILE_INVALID", "objective file is not UTF-8") from error


def _canonical(values: list[str], label: str) -> tuple[str, ...]:
    if any(not isinstance(item, str) or not item.strip() for item in values):
        raise MissionCliError("INVALID_MISSION_ARGUMENT", f"{label} values are invalid")
    normalized = tuple(sorted(set(values), key=lambda item: (item.casefold(), item)))
    if len(normalized) != len(values):
        raise MissionCliError("DUPLICATE_MISSION_ARGUMENT", f"{label} values must be unique")
    return normalized


def _human_evidence(filename: str) -> Evidence:
    path = Path(filename)
    if path.is_symlink():
        raise MissionCliError(
            "UNSAFE_HUMAN_EVIDENCE", "Human Evidence file cannot be a symlink"
        )
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise MissionCliError(
            "HUMAN_EVIDENCE_UNAVAILABLE", "Human Evidence file is unavailable"
        ) from error
    if not resolved.is_file() or resolved.stat().st_size > _MAX_HUMAN_EVIDENCE_BYTES:
        raise MissionCliError(
            "HUMAN_EVIDENCE_INVALID", "Human Evidence file is invalid or too large"
        )
    try:
        candidate = json.loads(
            resolved.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise MissionCliError(
            "HUMAN_EVIDENCE_INVALID", "Human Evidence is not strict UTF-8 JSON"
        ) from error
    validation = ContractValidator().validate("evidence", candidate)
    if not validation.is_valid or not isinstance(candidate, dict):
        raise MissionCliError(
            "HUMAN_EVIDENCE_INVALID", "Human Evidence violates its canonical schema"
        )
    try:
        timestamp = datetime.fromisoformat(str(candidate["timestamp"]).replace("Z", "+00:00"))
        return Evidence(
            evidence_id=str(candidate["evidence_id"]),
            evidence_type=EvidenceType(candidate["evidence_type"]),
            subject=str(candidate["subject"]),
            result=candidate["result"],
            source=str(candidate["source"]),
            command=candidate["command"],
            exit_code=candidate["exit_code"],
            artifact=candidate["artifact"],
            commit=candidate["commit"],
            timestamp=timestamp.astimezone(timezone.utc),
            producer=str(candidate["producer"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise MissionCliError(
            "HUMAN_EVIDENCE_INVALID", "Human Evidence fields are invalid"
        ) from error


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate key: {key}")
        value[key] = item
    return value


__all__ = [
    "MissionCliError",
    "add_mission_parser",
    "build_mission_runner",
    "emit_mission_error",
    "emit_mission_result",
    "execute_mission_command",
]
