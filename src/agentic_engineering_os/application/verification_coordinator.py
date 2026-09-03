"""Trusted execution of configured verification commands and Gate evaluation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from agentic_engineering_os.domain import (
    Evidence,
    EvidenceType,
    GateAggregation,
    GateResult,
    ProjectConfiguration,
    UserStory,
)

from .control_loop import ControlLoop, ControlLoopError
from .evidence_recorder import (
    EvidenceObservation,
    EvidenceProvenance,
    ProvenanceKind,
)
from .gate_evaluator import (
    GateContract,
    GateEvaluation,
    GateEvaluationContext,
    GateEvaluator,
)
from .gate_policy import (
    GatePolicyResolutionError,
    ResolvedGatePolicy,
    resolve_story_policies,
)


@dataclass(frozen=True, slots=True)
class VerificationProcessResult:
    argv: tuple[str, ...]
    cwd: Path
    started: bool
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    failure_code: str | None = None


class VerificationCommandRunner(Protocol):
    def run(self, argv: tuple[str, ...], cwd: Path) -> VerificationProcessResult: ...


class GitPrimaryObservation(Protocol):
    head_commit: str
    clean: bool


class VerificationGitObserver(Protocol):
    def verify_repository(self) -> Path: ...

    def primary_state(self) -> GitPrimaryObservation: ...


@dataclass(frozen=True, slots=True)
class VerificationRunResult:
    mission_id: str
    workflow_generation: int
    user_story_id: str
    commit: str
    evidence: tuple[Evidence, ...]
    gates: tuple[GateEvaluation, ...]
    blockers: tuple[str, ...]


class VerificationCoordinationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class VerificationCoordinator:
    """Execute only repository-configured argv and persist existing authorities."""

    def __init__(
        self,
        repository_root: Path | str,
        *,
        control_loop: ControlLoop,
        runner: VerificationCommandRunner,
        git_observer: VerificationGitObserver,
        gate_evaluator: GateEvaluator | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        try:
            self._root = Path(repository_root).resolve(strict=True)
        except OSError as error:
            raise VerificationCoordinationError(
                "INVALID_REPOSITORY", "repository root cannot be resolved"
            ) from error
        if not self._root.is_dir():
            raise VerificationCoordinationError(
                "INVALID_REPOSITORY", "repository root must be a directory"
            )
        if not isinstance(control_loop, ControlLoop):
            raise VerificationCoordinationError(
                "INVALID_AUTHORITY", "ControlLoop authority is required"
            )
        self._control_loop = control_loop
        self._runner = runner
        self._git = git_observer
        self._gate_evaluator = gate_evaluator or GateEvaluator()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def verify(
        self,
        configuration: ProjectConfiguration,
        user_story: UserStory,
        *,
        mission_id: str,
        workflow_generation: int,
        integrated_commit: str,
    ) -> VerificationRunResult:
        if not isinstance(mission_id, str) or not mission_id.strip():
            raise VerificationCoordinationError(
                "INVALID_MISSION_ID", "mission identity is required"
            )
        if (
            isinstance(workflow_generation, bool)
            or not isinstance(workflow_generation, int)
            or workflow_generation < 0
        ):
            raise VerificationCoordinationError(
                "INVALID_GENERATION", "workflow generation must be non-negative"
            )
        commit = _canonical_commit(integrated_commit)
        try:
            policies = resolve_story_policies(configuration, user_story)
        except GatePolicyResolutionError as error:
            raise VerificationCoordinationError(error.code, error.message) from error
        self._require_primary(commit)

        evidence_by_command: dict[str, Evidence] = {}
        evidence_ids_by_command: dict[str, str] = {}
        blockers: list[str] = []
        commands = {
            command.command_id: command
            for policy in policies
            for command in policy.verification_commands
        }
        for command_id, command in commands.items():
            evidence_id = _evidence_id(
                mission_id,
                workflow_generation,
                user_story.id,
                commit,
                command_id,
            )
            evidence_ids_by_command[command_id] = evidence_id
            existing = self._existing_evidence(evidence_id)
            argv = (command.executable, *command.args)
            cwd = self._resolve_cwd(command.cwd)
            if existing is not None:
                _require_reusable_evidence(
                    existing,
                    mission_id=mission_id,
                    generation=workflow_generation,
                    story_id=user_story.id,
                    commit=commit,
                    command_id=command_id,
                    argv=argv,
                )
                evidence_by_command[command_id] = existing
                continue

            self._require_primary(commit)
            try:
                process = self._runner.run(argv, cwd)
            except Exception as error:
                raise VerificationCoordinationError(
                    "COMMAND_EXECUTION_ERROR",
                    f"configured command runner failed: {type(error).__name__}: {error}",
                ) from error
            if process.argv != argv or process.cwd.resolve(strict=False) != cwd:
                raise VerificationCoordinationError(
                    "COMMAND_BINDING_MISMATCH",
                    "runner observation differs from configured argv or cwd",
                )
            self._require_primary(commit)
            if not process.started or process.exit_code is None:
                blockers.append(process.failure_code or "COMMAND_NOT_EXECUTED")
                continue

            repository_dependent = any(
                policy.repository_dependent
                for policy in policies
                if command_id
                in tuple(item.command_id for item in policy.verification_commands)
            )
            result = {
                "mission_id": mission_id,
                "workflow_generation": workflow_generation,
                "user_story_id": user_story.id,
                "command_id": command_id,
                "argv": list(argv),
                "exit_code": process.exit_code,
                "passed": process.exit_code == 0,
                "stdout_sha256": hashlib.sha256(process.stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(process.stderr).hexdigest(),
            }
            observation = EvidenceObservation(
                evidence_type=EvidenceType.COMMAND_RESULT,
                subject=user_story.id,
                result=result,
                provenance=EvidenceProvenance(
                    kind=ProvenanceKind.TOOL,
                    source="agentic-engineering-os/verification",
                    producer="ControlPlane/VerificationCoordinator",
                ),
                repository_dependent=repository_dependent,
                command=_canonical_argv(argv),
                exit_code=process.exit_code,
                artifact=(
                    f"stdout-sha256:{result['stdout_sha256']};"
                    f"stderr-sha256:{result['stderr_sha256']}"
                ),
                commit=commit,
            )
            try:
                recorded = self._control_loop.record_evidence(
                    observation,
                    evidence_id=evidence_id,
                    timestamp=self._clock(),
                )
            except ControlLoopError as error:
                raise VerificationCoordinationError(error.code, error.message) from error
            evidence_by_command[command_id] = recorded

        gates = tuple(
            self._evaluate_policy(
                policy,
                evidence_by_command,
                evidence_ids_by_command,
                commit=commit,
            )
            for policy in policies
        )
        return VerificationRunResult(
            mission_id=mission_id,
            workflow_generation=workflow_generation,
            user_story_id=user_story.id,
            commit=commit,
            evidence=tuple(evidence_by_command.values()),
            gates=gates,
            blockers=tuple(dict.fromkeys(blockers)),
        )

    def _resolve_cwd(self, relative: str) -> Path:
        candidate = self._root if relative == "." else self._root / relative
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise VerificationCoordinationError(
                "COMMAND_CWD_UNAVAILABLE", "configured command cwd cannot be resolved"
            ) from error
        if not resolved.is_dir() or not resolved.is_relative_to(self._root):
            raise VerificationCoordinationError(
                "COMMAND_CWD_ESCAPE", "configured command cwd escapes repository root"
            )
        return resolved

    def _require_primary(self, commit: str) -> None:
        try:
            self._git.verify_repository()
            state = self._git.primary_state()
        except Exception as error:
            raise VerificationCoordinationError(
                getattr(error, "code", "GIT_OBSERVATION_FAILED"),
                getattr(error, "message", "primary Git state could not be observed"),
            ) from error
        if state.head_commit != commit:
            raise VerificationCoordinationError(
                "HEAD_DIVERGED", "primary HEAD differs from integrated commit"
            )
        if not state.clean:
            raise VerificationCoordinationError(
                "VERIFICATION_MUTATED_REPOSITORY",
                "primary worktree must remain clean during verification",
            )

    def _existing_evidence(self, evidence_id: str) -> Evidence | None:
        matches = [
            item
            for item in self._control_loop.load_state().evidence
            if item.evidence_id == evidence_id
        ]
        if len(matches) > 1:
            raise VerificationCoordinationError(
                "EVIDENCE_AMBIGUOUS", "deterministic Evidence identity is ambiguous"
            )
        return matches[0] if matches else None

    def _evaluate_policy(
        self,
        policy: ResolvedGatePolicy,
        evidence_by_command: Mapping[str, Evidence],
        evidence_ids_by_command: Mapping[str, str],
        *,
        commit: str,
    ) -> GateEvaluation:
        if policy.aggregation is not GateAggregation.ALL_REQUIRED_PASS:
            raise VerificationCoordinationError(
                "UNSUPPORTED_GATE_AGGREGATION",
                "only ALL_REQUIRED_PASS is supported by the V1 coordinator",
            )
        evidence_ids = tuple(
            evidence_ids_by_command[command.command_id]
            for command in policy.verification_commands
        )
        contract = GateContract(
            gate_id=policy.gate_id,
            subject=policy.user_story_id,
            required=True,
            evidence_ids=evidence_ids,
            condition=_all_required_pass,
            repository_dependent=policy.repository_dependent,
            evaluator="ControlPlane/GateEvaluator",
        )
        context = GateEvaluationContext(expected_commit=commit)
        existing = [
            item
            for item in self._control_loop.load_state().gates
            if item.gate_id == policy.gate_id
        ]
        if len(existing) > 1:
            raise VerificationCoordinationError(
                "GATE_AMBIGUOUS", "story-scoped Gate identity is ambiguous"
            )
        if existing:
            evaluated = self._gate_evaluator.evaluate(
                contract,
                self._control_loop.load_state().evidence,
                context=context,
                evaluated_at=existing[0].evaluated_at,
            )
            if evaluated.gate != existing[0]:
                raise VerificationCoordinationError(
                    "GATE_COLLISION", "persisted Gate differs from current policy evaluation"
                )
            return evaluated
        try:
            return self._control_loop.evaluate_gate(
                contract,
                context=context,
                evaluated_at=self._clock(),
            )
        except ControlLoopError as error:
            raise VerificationCoordinationError(error.code, error.message) from error


def _canonical_commit(value: str) -> str:
    normalized = value.casefold() if isinstance(value, str) else ""
    if len(normalized) != 40 or any(char not in "0123456789abcdef" for char in normalized):
        raise VerificationCoordinationError(
            "INVALID_COMMIT", "integrated commit must be a full Git SHA"
        )
    return normalized


def _canonical_argv(argv: tuple[str, ...]) -> str:
    return json.dumps(argv, ensure_ascii=False, separators=(",", ":"))


def _evidence_id(
    mission_id: str,
    generation: int,
    story_id: str,
    commit: str,
    command_id: str,
) -> str:
    payload = json.dumps(
        [mission_id, generation, story_id, commit, command_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"EV-VERIFY-{hashlib.sha256(payload).hexdigest()}"


def _require_reusable_evidence(
    evidence: Evidence,
    *,
    mission_id: str,
    generation: int,
    story_id: str,
    commit: str,
    command_id: str,
    argv: tuple[str, ...],
) -> None:
    result = evidence.result
    expected = {
        "mission_id": mission_id,
        "workflow_generation": generation,
        "user_story_id": story_id,
        "command_id": command_id,
        "argv": argv,
    }
    result_keys = {
        *expected,
        "exit_code",
        "passed",
        "stdout_sha256",
        "stderr_sha256",
    }
    valid_result = (
        isinstance(result, Mapping)
        and set(result) == result_keys
        and all(result.get(key) == value for key, value in expected.items())
    )
    stdout_digest = result.get("stdout_sha256") if isinstance(result, Mapping) else None
    stderr_digest = result.get("stderr_sha256") if isinstance(result, Mapping) else None
    if not (
        evidence.evidence_type is EvidenceType.COMMAND_RESULT
        and evidence.subject == story_id
        and evidence.source == "agentic-engineering-os/verification"
        and evidence.producer == "ControlPlane/VerificationCoordinator"
        and evidence.command == _canonical_argv(argv)
        and evidence.exit_code is not None
        and evidence.commit == commit
        and valid_result
        and result.get("exit_code") == evidence.exit_code
        and isinstance(result.get("passed"), bool)
        and result.get("passed") == (evidence.exit_code == 0)
        and _is_sha256(stdout_digest)
        and _is_sha256(stderr_digest)
        and evidence.artifact
        == f"stdout-sha256:{stdout_digest};stderr-sha256:{stderr_digest}"
    ):
        raise VerificationCoordinationError(
            "EVIDENCE_COLLISION", "persisted Evidence differs from configured execution"
        )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _all_required_pass(evidence: tuple[Evidence, ...]) -> GateResult:
    if not evidence:
        return GateResult.UNKNOWN
    for item in evidence:
        if item.evidence_type is not EvidenceType.COMMAND_RESULT:
            return GateResult.UNKNOWN
        if not isinstance(item.result, Mapping):
            return GateResult.UNKNOWN
        passed = item.result.get("passed")
        if passed is False and item.exit_code not in (None, 0):
            return GateResult.FAIL
        if passed is not True or item.exit_code != 0:
            return GateResult.UNKNOWN
    return GateResult.PASS
