"""Shell-free subprocess adapter for the observed ``codex exec`` transport."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import cast

from agentic_engineering_os.application.codex_runtime import (
    CodexApprovalPolicy,
    CodexExecutionBinding,
    CodexExecutionObservation,
    CodexJsonlEvent,
    CodexSandboxMode,
    GitExecutionObservation,
    InvalidJsonlLine,
)
from agentic_engineering_os.application.prompt_compiler import CompiledPrompt
from agentic_engineering_os.resources.product import (
    ProductResourceError,
    product_schema_directory,
)

from .git_adapter import GitAdapter, GitOperationError
from .platform_environment import (
    RUNTIME_ENVIRONMENT_ALLOWLIST,
    build_bounded_environment,
    discover_executable,
)


_SHA40 = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SECRET_ENV_TOKEN = re.compile(
    r"(?:api[_-]?key|token|secret|password|credential)", re.IGNORECASE
)
@dataclass(frozen=True, slots=True)
class CodexRuntimeConfiguration:
    """Pinned infrastructure configuration, separate from application bindings."""

    executable: str
    expected_executable_path: str
    expected_executable_version: str
    expected_executable_sha256: str
    launcher_arguments: tuple[str, ...] = ()
    environment_allowlist: tuple[str, ...] = RUNTIME_ENVIRONMENT_ALLOWLIST
    max_output_characters: int = 1_000_000
    version_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        text_values = (
            self.executable,
            self.expected_executable_path,
            self.expected_executable_version,
        )
        if not all(isinstance(value, str) and value.strip() for value in text_values):
            raise ValueError("executable configuration must be explicit")
        digest = self.expected_executable_sha256.casefold()
        if not _SHA256.fullmatch(digest):
            raise ValueError("expected executable SHA-256 must be lowercase hexadecimal")
        object.__setattr__(self, "expected_executable_sha256", digest)
        if (
            not isinstance(self.launcher_arguments, tuple)
            or any(not isinstance(item, str) or "\0" in item for item in self.launcher_arguments)
        ):
            raise ValueError("launcher arguments must be a safe tuple")
        if (
            not isinstance(self.environment_allowlist, tuple)
            or not self.environment_allowlist
            or any(
                not isinstance(name, str)
                or not name
                or "=" in name
                or _SECRET_ENV_TOKEN.search(name)
                for name in self.environment_allowlist
            )
        ):
            raise ValueError("environment allowlist contains an unsafe name")
        normalized = tuple(name.casefold() for name in self.environment_allowlist)
        if len(normalized) != len(set(normalized)):
            raise ValueError("environment allowlist contains duplicate names")
        if (
            not isinstance(self.max_output_characters, int)
            or isinstance(self.max_output_characters, bool)
            or self.max_output_characters <= 0
        ):
            raise ValueError("max output characters must be positive")
        if (
            not isinstance(self.version_timeout_seconds, (int, float))
            or isinstance(self.version_timeout_seconds, bool)
            or self.version_timeout_seconds <= 0
        ):
            raise ValueError("version timeout must be positive")


class CodexRuntimeAdapter:
    """Execute one compiled prompt and report transport facts without a verdict."""

    def __init__(
        self,
        configuration: CodexRuntimeConfiguration,
        *,
        parent_environment: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(configuration, CodexRuntimeConfiguration):
            raise TypeError("configuration must use CodexRuntimeConfiguration")
        self._configuration = configuration
        self._parent_environment = dict(
            os.environ if parent_environment is None else parent_environment
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def execute(
        self,
        compiled_prompt: CompiledPrompt,
        binding: CodexExecutionBinding,
        *,
        cancellation: Event | None = None,
    ) -> CodexExecutionObservation:
        now = self._utc_now
        issues: list[str] = []
        cwd_text = binding.cwd if isinstance(binding, CodexExecutionBinding) else ""
        if not isinstance(compiled_prompt, CompiledPrompt):
            raise TypeError("compiled_prompt must use CompiledPrompt")
        if not isinstance(binding, CodexExecutionBinding):
            raise TypeError("binding must use CodexExecutionBinding")

        binding_issue = _validate_binding(compiled_prompt, binding)
        if binding_issue is not None:
            return _not_started(compiled_prompt, cwd_text, now(), (binding_issue,))

        cwd, cwd_issue = _resolve_cwd(compiled_prompt, binding)
        if cwd_issue is not None or cwd is None:
            return _not_started(
                compiled_prompt, cwd_text, now(), (cwd_issue or "INVALID_CWD",)
            )
        cwd_text = str(cwd)

        schema, schema_issue = _resolve_output_schema(cwd, binding.output_schema_path)
        if schema_issue is not None:
            return _not_started(compiled_prompt, cwd_text, now(), (schema_issue,))

        child_environment = _build_environment(
            self._parent_environment, self._configuration.environment_allowlist
        )
        executable, executable_issue = _resolve_executable(
            self._configuration, self._parent_environment
        )
        if executable_issue is not None or executable is None:
            return _not_started(
                compiled_prompt,
                cwd_text,
                now(),
                (executable_issue or "EXECUTABLE_UNAVAILABLE",),
            )

        executable_sha = _file_sha256(executable)
        if executable_sha != self._configuration.expected_executable_sha256:
            return _not_started(
                compiled_prompt,
                cwd_text,
                now(),
                ("EXECUTABLE_DIGEST_MISMATCH",),
                executable_path=str(executable),
                executable_sha256=executable_sha,
            )

        version, version_issue = self._observe_version(executable, child_environment)
        if version_issue is not None:
            return _not_started(
                compiled_prompt,
                cwd_text,
                now(),
                (version_issue,),
                executable_path=str(executable),
                executable_sha256=executable_sha,
            )
        if version != self._configuration.expected_executable_version:
            return _not_started(
                compiled_prompt,
                cwd_text,
                now(),
                ("EXECUTABLE_VERSION_MISMATCH",),
                executable_path=str(executable),
                executable_version=version,
                executable_sha256=executable_sha,
            )

        git_before = _observe_git(cwd)
        if git_before.error is not None:
            issues.append("GIT_OBSERVATION_BEFORE_FAILED")
        if git_before.head_commit != binding.expected_commit:
            issues.append("GIT_HEAD_MISMATCH_BEFORE")
        if binding.require_clean_git and git_before.clean is not True:
            issues.append("GIT_NOT_CLEAN_BEFORE")
        if issues:
            return _not_started(
                compiled_prompt,
                cwd_text,
                now(),
                tuple(issues),
                executable_path=str(executable),
                executable_version=version,
                executable_sha256=executable_sha,
                git_before=git_before,
            )
        if cancellation is not None and cancellation.is_set():
            return _not_started(
                compiled_prompt,
                cwd_text,
                now(),
                ("INTERRUPTED_BEFORE_START",),
                executable_path=str(executable),
                executable_version=version,
                executable_sha256=executable_sha,
                git_before=git_before,
                interrupted=True,
            )

        invocation = _invocation(
            executable,
            self._configuration.launcher_arguments,
            cwd,
            binding.sandbox,
            binding.approval_policy,
            schema,
        )
        started_at = now()
        try:
            process = subprocess.Popen(
                list(invocation),
                shell=False,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=child_environment,
            )
        except OSError as error:
            return _not_started(
                compiled_prompt,
                cwd_text,
                now(),
                (f"PROCESS_SPAWN_FAILED:{type(error).__name__}",),
                executable_path=str(executable),
                executable_version=version,
                executable_sha256=executable_sha,
                invocation=invocation,
                git_before=git_before,
                started_at=started_at,
            )

        stop_monitor = Event()
        interrupted_by_parent = Event()
        monitor: threading.Thread | None = None
        if cancellation is not None:
            monitor = threading.Thread(
                target=_monitor_cancellation,
                args=(process, cancellation, stop_monitor, interrupted_by_parent),
                daemon=True,
            )
            monitor.start()

        timed_out = False
        try:
            stdout, stderr = process.communicate(
                input=compiled_prompt.prompt_text,
                timeout=binding.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            if process.poll() is None:
                process.kill()
            stdout, stderr = process.communicate()
        finally:
            stop_monitor.set()
            if monitor is not None:
                monitor.join(timeout=1.0)

        ended_at = now()
        interrupted = interrupted_by_parent.is_set()
        stdout, stdout_truncated = _bounded(
            stdout, self._configuration.max_output_characters
        )
        stderr, stderr_truncated = _bounded(
            stderr, self._configuration.max_output_characters
        )
        events, invalid_lines = _parse_jsonl(stdout)
        thread_id = _thread_id(events)
        final_output = _final_output(events)
        tool_failure = _tool_failure_observed(events)
        git_after = _observe_git(cwd)

        if timed_out:
            issues.append("PROCESS_TIMED_OUT")
        if interrupted:
            issues.append("PROCESS_INTERRUPTED")
        if process.returncode != 0:
            issues.append("PROCESS_EXIT_NON_ZERO")
        if stderr:
            issues.append("STDERR_OBSERVED")
        if stdout_truncated:
            issues.append("STDOUT_TRUNCATED")
        if stderr_truncated:
            issues.append("STDERR_TRUNCATED")
        if invalid_lines:
            issues.append("MALFORMED_JSONL")
        if tool_failure:
            issues.append("TOOL_FAILURE_OBSERVED")
        if final_output is None:
            issues.append("MISSING_FINAL_OUTPUT")
        if git_after.error is not None:
            issues.append("GIT_OBSERVATION_AFTER_FAILED")
        git_changed = git_before != git_after
        if git_changed:
            issues.append("GIT_STATE_CHANGED")
        if git_changed and final_output is None:
            issues.append("GIT_CHANGED_WITHOUT_FINAL_OUTPUT")
        if git_changed and process.returncode != 0:
            issues.append("GIT_CHANGED_AFTER_PROCESS_FAILURE")

        return CodexExecutionObservation(
            request_id=compiled_prompt.request_id,
            context_fingerprint=compiled_prompt.context_fingerprint,
            executable_path=str(executable),
            executable_version=version,
            executable_sha256=executable_sha,
            cwd=cwd_text,
            invocation=invocation,
            started_at=started_at,
            ended_at=ended_at,
            process_id=process.pid,
            thread_id=thread_id,
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            events=events,
            invalid_jsonl_lines=invalid_lines,
            final_output=final_output,
            timed_out=timed_out,
            interrupted=interrupted,
            tool_failure_observed=tool_failure,
            git_before=git_before,
            git_after=git_after,
            issues=tuple(dict.fromkeys(issues)),
        )

    def _observe_version(
        self, executable: Path, environment: Mapping[str, str]
    ) -> tuple[str | None, str | None]:
        command = (
            str(executable),
            *self._configuration.launcher_arguments,
            "--version",
        )
        try:
            result = subprocess.run(
                list(command),
                shell=False,
                cwd=executable.parent,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=dict(environment),
                timeout=self._configuration.version_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return None, f"EXECUTABLE_VERSION_UNOBSERVABLE:{type(error).__name__}"
        version = result.stdout.strip()
        if result.returncode != 0 or not version:
            return None, "EXECUTABLE_VERSION_UNOBSERVABLE"
        return version, None

    def _utc_now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise RuntimeError("clock must return an aware datetime")
        return value.astimezone(timezone.utc)


def _validate_binding(
    compiled: CompiledPrompt, binding: CodexExecutionBinding
) -> str | None:
    text_values = (
        binding.request_id,
        binding.context_fingerprint,
        binding.mission_id,
        binding.subject,
        binding.cwd,
    )
    if not all(isinstance(value, str) and value.strip() for value in text_values):
        return "INVALID_EXECUTION_BINDING"
    if (
        not isinstance(binding.workflow_generation, int)
        or isinstance(binding.workflow_generation, bool)
        or binding.workflow_generation < 0
        or not isinstance(binding.role, type(compiled.role))
        or not isinstance(binding.sandbox, CodexSandboxMode)
        or not isinstance(binding.approval_policy, CodexApprovalPolicy)
        or not isinstance(binding.require_clean_git, bool)
        or not isinstance(binding.timeout_seconds, (int, float))
        or isinstance(binding.timeout_seconds, bool)
        or binding.timeout_seconds <= 0
        or not _SHA40.fullmatch(binding.expected_commit)
    ):
        return "INVALID_EXECUTION_BINDING"
    comparisons = (
        (binding.request_id, compiled.request_id),
        (binding.context_fingerprint, compiled.context_fingerprint),
        (binding.mission_id, compiled.mission_id),
        (binding.workflow_generation, compiled.workflow_generation),
        (binding.role, compiled.role),
        (binding.subject, compiled.subject),
        (binding.expected_commit, compiled.observed_commit),
    )
    if any(actual != expected for actual, expected in comparisons):
        return "COMPILED_PROMPT_BINDING_MISMATCH"
    return None


def _resolve_cwd(
    compiled: CompiledPrompt, binding: CodexExecutionBinding
) -> tuple[Path | None, str | None]:
    requested = Path(binding.cwd)
    expected = Path(compiled.worktree_path or compiled.repository_root)
    if not requested.is_absolute() or not expected.is_absolute():
        return None, "INVALID_CWD"
    try:
        resolved = requested.resolve(strict=True)
        expected_resolved = expected.resolve(strict=True)
    except OSError:
        return None, "INVALID_CWD"
    if not resolved.is_dir() or _path_key(resolved) != _path_key(expected_resolved):
        return None, "BINDING_CWD_MISMATCH"
    return resolved, None


def _resolve_output_schema(
    cwd: Path, schema_path: str | None
) -> tuple[Path | None, str | None]:
    if schema_path is None:
        return None, None
    candidate = Path(schema_path)
    if not candidate.is_absolute() or candidate.is_symlink():
        return None, "INVALID_OUTPUT_SCHEMA"
    try:
        resolved = candidate.resolve(strict=True)
        installed_schemas = product_schema_directory()
    except (OSError, ProductResourceError):
        return None, "INVALID_OUTPUT_SCHEMA"
    if (
        not resolved.is_file()
        or resolved.suffix.casefold() != ".json"
        or not (_contains(cwd, resolved) or _contains(installed_schemas, resolved))
    ):
        return None, "INVALID_OUTPUT_SCHEMA"
    return resolved, None


def _resolve_executable(
    configuration: CodexRuntimeConfiguration,
    parent_environment: Mapping[str, str],
) -> tuple[Path | None, str | None]:
    fact = discover_executable(
        configuration.executable,
        parent_environment,
        identity="codex",
    )
    if fact.path is None:
        return None, "EXECUTABLE_UNAVAILABLE"
    try:
        resolved = Path(fact.path).resolve(strict=True)
        expected = Path(configuration.expected_executable_path).resolve(strict=True)
    except OSError:
        return None, "EXECUTABLE_UNAVAILABLE"
    if not resolved.is_file() or _path_key(resolved) != _path_key(expected):
        return None, "EXECUTABLE_PATH_MISMATCH"
    return resolved, None


def _build_environment(
    parent: Mapping[str, str], allowlist: tuple[str, ...]
) -> dict[str, str]:
    return build_bounded_environment(parent, allowlist)


def _invocation(
    executable: Path,
    launcher_arguments: tuple[str, ...],
    cwd: Path,
    sandbox: CodexSandboxMode,
    approval: CodexApprovalPolicy,
    output_schema: Path | None,
) -> tuple[str, ...]:
    arguments = [
        str(executable),
        *launcher_arguments,
        "-a",
        approval.value,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--json",
        "--color",
        "never",
        "--sandbox",
        sandbox.value,
        "-C",
        str(cwd),
    ]
    if output_schema is not None:
        arguments.extend(("--output-schema", str(output_schema)))
    arguments.append("-")
    return tuple(arguments)


def _observe_git(cwd: Path) -> GitExecutionObservation:
    adapter = GitAdapter(cwd)
    head: str | None = None
    clean: bool | None = None
    changed_paths: tuple[str, ...] | None = None
    errors: list[str] = []
    try:
        head = adapter.current_head(cwd)
    except GitOperationError as error:
        errors.append(error.code)
    try:
        clean = adapter.is_clean(cwd)
    except GitOperationError as error:
        errors.append(error.code)
    try:
        changed_paths = adapter.worktree_changed_paths(cwd)
    except GitOperationError as error:
        errors.append(error.code)
    return GitExecutionObservation(
        head_commit=head,
        clean=clean,
        error=",".join(errors) if errors else None,
        changed_paths=changed_paths,
    )


def _parse_jsonl(
    stdout: str,
) -> tuple[tuple[CodexJsonlEvent, ...], tuple[InvalidJsonlLine, ...]]:
    events: list[CodexJsonlEvent] = []
    invalid: list[InvalidJsonlLine] = []
    for line_number, raw_line in enumerate(stdout.splitlines(), 1):
        try:
            value = json.loads(raw_line, object_pairs_hook=_reject_duplicate_keys)
            if not isinstance(value, dict):
                raise ValueError("event must be a JSON object")
            event_type = value.get("type")
            if event_type is not None and not isinstance(event_type, str):
                raise ValueError("event type must be a string")
            events.append(
                CodexJsonlEvent(
                    line_number=line_number,
                    event_type=event_type,
                    raw_line=raw_line,
                    payload_json=json.dumps(
                        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ),
                )
            )
        except (json.JSONDecodeError, ValueError) as error:
            invalid.append(
                InvalidJsonlLine(line_number, raw_line, f"{type(error).__name__}: {error}")
            )
    return tuple(events), tuple(invalid)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _event_payload(event: CodexJsonlEvent) -> dict[str, object]:
    return cast(dict[str, object], json.loads(event.payload_json))


def _thread_id(events: tuple[CodexJsonlEvent, ...]) -> str | None:
    for event in events:
        payload = _event_payload(event)
        if payload.get("type") == "thread.started" and isinstance(
            payload.get("thread_id"), str
        ):
            return cast(str, payload["thread_id"])
    return None


def _final_output(events: tuple[CodexJsonlEvent, ...]) -> str | None:
    final: str | None = None
    for event in events:
        payload = _event_payload(event)
        item = payload.get("item")
        if (
            payload.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            final = cast(str, item["text"])
    return final


def _tool_failure_observed(events: tuple[CodexJsonlEvent, ...]) -> bool:
    for event in events:
        payload = _event_payload(event)
        item = payload.get("item")
        if isinstance(item, dict) and item.get("status") in {"failed", "error"}:
            return True
        if payload.get("type") in {"error", "turn.failed"}:
            return True
    return False


def _bounded(value: str, maximum: int) -> tuple[str, bool]:
    if len(value) <= maximum:
        return value, False
    return value[:maximum], True


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _monitor_cancellation(
    process: subprocess.Popen[str],
    cancellation: Event,
    stop: Event,
    interrupted: Event,
) -> None:
    while not stop.wait(0.05):
        if cancellation.is_set():
            if process.poll() is None:
                interrupted.set()
                try:
                    process.kill()
                except OSError:
                    pass
            return


def _not_started(
    compiled: CompiledPrompt,
    cwd: str,
    ended_at: datetime,
    issues: tuple[str, ...],
    *,
    executable_path: str | None = None,
    executable_version: str | None = None,
    executable_sha256: str | None = None,
    invocation: tuple[str, ...] = (),
    git_before: GitExecutionObservation | None = None,
    interrupted: bool = False,
    started_at: datetime | None = None,
) -> CodexExecutionObservation:
    return CodexExecutionObservation(
        request_id=compiled.request_id,
        context_fingerprint=compiled.context_fingerprint,
        executable_path=executable_path,
        executable_version=executable_version,
        executable_sha256=executable_sha256,
        cwd=cwd,
        invocation=invocation,
        started_at=started_at,
        ended_at=ended_at,
        process_id=None,
        thread_id=None,
        exit_code=None,
        stdout="",
        stderr="",
        stdout_truncated=False,
        stderr_truncated=False,
        events=(),
        invalid_jsonl_lines=(),
        final_output=None,
        timed_out=False,
        interrupted=interrupted,
        tool_failure_observed=False,
        git_before=git_before,
        git_after=None,
        issues=issues,
    )


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False))).casefold()


def _contains(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
