"""Shell-free subprocess adapter for the observed ``codex exec`` transport."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Protocol, cast

from agentic_engineering_os.application.codex_runtime import (
    CodexApprovalPolicy,
    CodexExecutionBinding,
    CodexExecutionObservation,
    CodexJsonlEvent,
    CodexSandboxMode,
    GitExecutionObservation,
    InvalidJsonlLine,
)
from agentic_engineering_os.application.codex_capabilities import (
    CODEX_V1_ALWAYS_REQUIRED,
    CodexCapability,
    CodexCapabilityStatus,
    CodexOperationalCapabilityClass,
    CodexOperationalCapabilityProof,
    CodexOperationalCapabilityStatus,
    create_operational_capability_proof,
    role_capability_requirements,
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
from .codex_capability_discovery import CodexCapabilityDiscovery


_SHA40 = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SECRET_ENV_TOKEN = re.compile(
    r"(?:api[_-]?key|token|secret|password|credential)", re.IGNORECASE
)
_DEFAULT_CAPABILITY_DISCOVERY = CodexCapabilityDiscovery()


class OperationalCapabilityProver(Protocol):
    def prove(
        self,
        *,
        configuration: "CodexRuntimeConfiguration",
        executable: Path,
        executable_sha256: str,
        executable_version: str,
        environment: Mapping[str, str],
        cwd: Path,
        binding: CodexExecutionBinding,
        capability_class: CodexOperationalCapabilityClass,
    ) -> CodexOperationalCapabilityProof: ...


class CodexOperationalCapabilityProver:
    """Actively prove the exact tool class before an engineering role starts."""

    def prove(
        self,
        *,
        configuration: "CodexRuntimeConfiguration",
        executable: Path,
        executable_sha256: str,
        executable_version: str,
        environment: Mapping[str, str],
        cwd: Path,
        binding: CodexExecutionBinding,
        capability_class: CodexOperationalCapabilityClass,
    ) -> CodexOperationalCapabilityProof:
        fingerprint = _environment_fingerprint(environment)
        status = CodexOperationalCapabilityStatus.UNPROVEN
        detail = "operational capability probe did not complete"
        diagnostic_code = "OPERATIONAL_PROBE_INCOMPLETE"
        if capability_class is CodexOperationalCapabilityClass.GIT_OBSERVATION:
            observed = _observe_git(cwd)
            if observed.error is None and observed.head_commit and observed.clean is not None:
                status = CodexOperationalCapabilityStatus.PROVEN
                detail = "runtime observed repository HEAD and cleanliness"
                diagnostic_code = "GIT_OBSERVATION_PROVEN"
            else:
                diagnostic_code = "GIT_OBSERVATION_FAILED"
            return create_operational_capability_proof(
                executable_path=str(executable), executable_sha256=executable_sha256,
                executable_version=executable_version, capability_class=capability_class,
                sandbox=binding.sandbox.value, approval_policy=binding.approval_policy.value,
                environment_fingerprint=fingerprint, status=status, detail=detail,
                diagnostic_code=diagnostic_code,
            )
        if capability_class is CodexOperationalCapabilityClass.STRUCTURED_RESULT:
            return create_operational_capability_proof(
                executable_path=str(executable), executable_sha256=executable_sha256,
                executable_version=executable_version, capability_class=capability_class,
                sandbox=binding.sandbox.value, approval_policy=binding.approval_policy.value,
                environment_fingerprint=fingerprint, status=status,
                detail="structured result is proven only by the admitted role and P4.6 intake",
                diagnostic_code="STRUCTURED_RESULT_PENDING_ROLE",
            )
        with tempfile.TemporaryDirectory(prefix="agentic-os-codex-probe-") as temporary:
            probe_root = Path(temporary).resolve(strict=True)
            try:
                initialized = subprocess.run(
                    ["git", "init", "-b", "main"],
                    shell=False,
                    cwd=probe_root,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=dict(environment),
                    timeout=configuration.version_timeout_seconds,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                initialized = None
            if initialized is None or initialized.returncode != 0:
                return create_operational_capability_proof(
                    executable_path=str(executable),
                    executable_sha256=executable_sha256,
                    executable_version=executable_version,
                    capability_class=capability_class,
                    sandbox=binding.sandbox.value,
                    approval_policy=binding.approval_policy.value,
                    environment_fingerprint=fingerprint,
                    status=status,
                    detail="disposable probe repository could not be initialized",
                    diagnostic_code="PROBE_REPOSITORY_INITIALIZATION_FAILED",
                )
            expected = hashlib.sha256(os.urandom(32)).hexdigest()
            if capability_class is CodexOperationalCapabilityClass.REPOSITORY_READ:
                (probe_root / "AGENTS.md").write_text(
                    f"Return this repository marker when explicitly requested: {expected}\n",
                    encoding="utf-8",
                )
                prompt = (
                    "Read the repository instruction file supplied by Codex for this working "
                    "directory without invoking a shell or modifying files. Return only a JSON "
                    "object with key `observed` containing its 64-character repository marker."
                )
            elif capability_class is CodexOperationalCapabilityClass.COMMAND_EXECUTION:
                prompt = (
                    "Use the shell tool to execute a command that prints exactly "
                    f"`{expected}`. Return only a JSON object with key `observed` containing "
                    "the command output. Do not create files."
                )
            else:
                prompt = (
                    "Use the workspace editing tool, not a shell command, to create "
                    f"`operational-write-proof.txt` containing exactly `{expected}`. "
                    "Return only a JSON object with key `observed` containing that value."
                )
            schema = Path(temporary) / "operational-proof.schema.json"
            schema.write_text(
                json.dumps(
                    {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["observed"],
                        "properties": {"observed": {"type": "string"}},
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            invocation = _invocation(
                executable,
                configuration.launcher_arguments,
                probe_root,
                binding.sandbox,
                binding.approval_policy,
                schema,
            )
            try:
                process = subprocess.run(
                    list(invocation),
                    shell=False,
                    cwd=probe_root,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=dict(environment),
                    timeout=configuration.operational_probe_timeout_seconds,
                    check=False,
                )
                events, invalid = _parse_jsonl(process.stdout)
                terminal, terminal_issue = _terminal_output(events)
                value = _strict_observed_value(terminal)
                command_succeeded = _successful_command_observed(events)
                write_succeeded = (
                    capability_class is CodexOperationalCapabilityClass.WORKSPACE_EDIT
                    and (probe_root / "operational-write-proof.txt").is_file()
                    and (probe_root / "operational-write-proof.txt").read_text(encoding="utf-8").strip() == expected
                )
                primitive_succeeded = {
                    CodexOperationalCapabilityClass.REPOSITORY_READ: value == expected,
                    CodexOperationalCapabilityClass.WORKSPACE_EDIT: write_succeeded,
                    CodexOperationalCapabilityClass.COMMAND_EXECUTION: command_succeeded,
                }.get(capability_class, False)
                if (
                    process.returncode == 0
                    and not invalid
                    and terminal_issue is None
                    and primitive_succeeded
                    and value == expected
                ):
                    status = CodexOperationalCapabilityStatus.PROVEN
                    detail = "exact active capability probe completed under bound policy"
                    diagnostic_code = f"{capability_class.value}_PROVEN"
                else:
                    detail = "active capability probe was blocked, malformed, or contradicted"
                    diagnostic_code = _probe_failure_code(
                        process.returncode,
                        process.stderr,
                        invalid,
                        terminal_issue,
                        _tool_failure_observed(events),
                    )
            except subprocess.TimeoutExpired:
                detail = "active capability probe timed out"
                diagnostic_code = "OPERATIONAL_PROBE_TIMEOUT"
            except (OSError, UnicodeError):
                detail = "active capability probe could not be completed"
                diagnostic_code = "OPERATIONAL_PROBE_EXECUTION_FAILED"
        return create_operational_capability_proof(
            executable_path=str(executable),
            executable_sha256=executable_sha256,
            executable_version=executable_version,
            capability_class=capability_class,
            sandbox=binding.sandbox.value,
            approval_policy=binding.approval_policy.value,
            environment_fingerprint=fingerprint,
            status=status,
            detail=detail,
            diagnostic_code=diagnostic_code,
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
    operational_probe_timeout_seconds: float = 180.0
    test_executable_injection: bool = False

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
        if (
            not isinstance(self.operational_probe_timeout_seconds, (int, float))
            or isinstance(self.operational_probe_timeout_seconds, bool)
            or self.operational_probe_timeout_seconds <= 0
        ):
            raise ValueError("operational probe timeout must be positive")
        if not isinstance(self.test_executable_injection, bool):
            raise ValueError("test_executable_injection must be boolean")


class CodexRuntimeAdapter:
    """Execute one compiled prompt and report transport facts without a verdict."""

    def __init__(
        self,
        configuration: CodexRuntimeConfiguration,
        *,
        parent_environment: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
        capability_discovery: CodexCapabilityDiscovery | None = None,
        operational_capability_prover: OperationalCapabilityProver | None = None,
    ) -> None:
        if not isinstance(configuration, CodexRuntimeConfiguration):
            raise TypeError("configuration must use CodexRuntimeConfiguration")
        self._configuration = configuration
        self._parent_environment = dict(
            os.environ if parent_environment is None else parent_environment
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._capabilities = capability_discovery or _DEFAULT_CAPABILITY_DISCOVERY
        self._operational = operational_capability_prover or CodexOperationalCapabilityProver()
        self._operational_cache: dict[
            tuple[str, str, str, str, str, str, str], CodexOperationalCapabilityProof
        ] = {}

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

        assessment = self._capabilities.assess(
            executable=self._configuration.executable,
            expected_path=self._configuration.expected_executable_path,
            expected_sha256=self._configuration.expected_executable_sha256,
            expected_version=self._configuration.expected_executable_version,
            launcher_arguments=self._configuration.launcher_arguments,
            environment=self._parent_environment,
            project_root=cwd_text,
            timeout_seconds=self._configuration.version_timeout_seconds,
            test_injection=self._configuration.test_executable_injection,
            _observed_identity=(str(executable), executable_sha, version),
        )
        if assessment is None or not assessment.authentically_discovered:
            return _not_started(
                compiled_prompt, cwd_text, now(), ("CAPABILITY_ASSESSMENT_UNAVAILABLE",),
                executable_path=str(executable), executable_version=version,
                executable_sha256=executable_sha,
            )
        required = set(CODEX_V1_ALWAYS_REQUIRED)
        required.add(
            CodexCapability.SANDBOX_READ_ONLY
            if binding.sandbox is CodexSandboxMode.READ_ONLY
            else CodexCapability.SANDBOX_WORKSPACE_WRITE
        )
        if schema is not None:
            required.add(CodexCapability.OUTPUT_SCHEMA)
        missing = tuple(
            item.value
            for item in sorted(required, key=lambda value: value.value)
            if assessment.status(item) is not CodexCapabilityStatus.SUPPORTED
        )
        if missing:
            return _not_started(
                compiled_prompt, cwd_text, now(),
                tuple(f"REQUIRED_CAPABILITY_NOT_SUPPORTED:{item}" for item in missing),
                executable_path=str(executable), executable_version=version,
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

        environment_fingerprint = _environment_fingerprint(child_environment)
        prelaunch_requirements = tuple(
            item
            for item in role_capability_requirements(binding.role)
            if item is not CodexOperationalCapabilityClass.STRUCTURED_RESULT
        )
        for capability_class in prelaunch_requirements:
            proof_key = (
                str(executable), executable_sha, version, binding.sandbox.value,
                binding.approval_policy.value, capability_class.value,
                environment_fingerprint,
            )
            proof = self._operational_cache.get(proof_key)
            if proof is None:
                proof = self._operational.prove(
                    configuration=self._configuration,
                    executable=executable,
                    executable_sha256=executable_sha,
                    executable_version=version,
                    environment=child_environment,
                    cwd=cwd,
                    binding=binding,
                    capability_class=capability_class,
                )
            if not _operational_proof_matches(
                proof,
                executable=executable,
                executable_sha256=executable_sha,
                executable_version=version,
                environment_fingerprint=environment_fingerprint,
                binding=binding,
                capability_class=capability_class,
            ):
                diagnostic = (
                    proof.diagnostic_code
                    if isinstance(proof, CodexOperationalCapabilityProof)
                    and proof.authentically_attested
                    else "INVALID_OPERATIONAL_PROOF"
                )
                return _not_started(
                    compiled_prompt,
                    cwd_text,
                    now(),
                    (
                        "REQUIRED_OPERATIONAL_CAPABILITY_UNPROVEN:"
                        f"{capability_class.value}:{diagnostic}:"
                        f"sandbox={binding.sandbox.value}:"
                        f"approval={binding.approval_policy.value}",
                    ),
                    executable_path=str(executable),
                    executable_version=version,
                    executable_sha256=executable_sha,
                    git_before=git_before,
                )
            self._operational_cache[proof_key] = proof

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
        final_output, terminal_issue = _terminal_output(events)
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
        if terminal_issue is not None:
            issues.append(terminal_issue)
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


def _terminal_output(
    events: tuple[CodexJsonlEvent, ...],
) -> tuple[str | None, str | None]:
    """Select the message ending one strictly ordered completed turn."""

    payloads = [_event_payload(event) for event in events]
    turn_started = [index for index, item in enumerate(payloads) if item.get("type") == "turn.started"]
    turn_completed = [index for index, item in enumerate(payloads) if item.get("type") == "turn.completed"]
    if len(turn_started) != 1 or len(turn_completed) != 1:
        return None, "AMBIGUOUS_JSONL_TERMINAL"
    start, completed = turn_started[0], turn_completed[0]
    if start >= completed or completed != len(payloads) - 1:
        return None, "MALFORMED_JSONL_SEQUENCE"
    messages: list[tuple[str, str]] = []
    seen_item_ids: set[str] = set()
    for index, payload in enumerate(payloads):
        item = payload.get("item")
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if item_id is not None:
            if not isinstance(item_id, str) or not item_id or item_id in seen_item_ids:
                return None, "AMBIGUOUS_JSONL_TERMINAL"
            seen_item_ids.add(item_id)
        if payload.get("type") == "item.completed" and item.get("type") == "agent_message":
            text = item.get("text")
            if not isinstance(text, str) or not text or not (start < index < completed):
                return None, "MALFORMED_JSONL_SEQUENCE"
            if not isinstance(item_id, str) or not item_id:
                return None, "AMBIGUOUS_JSONL_TERMINAL"
            messages.append((item_id, text))
    if not messages:
        return None, "MISSING_FINAL_OUTPUT"
    return messages[-1][1], None


def _successful_command_observed(events: tuple[CodexJsonlEvent, ...]) -> bool:
    for event in events:
        payload = _event_payload(event)
        item = payload.get("item")
        if (
            payload.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "command_execution"
            and item.get("status") in {"completed", "success", "succeeded"}
            and item.get("exit_code", 0) == 0
        ):
            return True
    return False


def _strict_observed_value(payload_text: str | None) -> str | None:
    if payload_text is None:
        return None
    try:
        value = json.loads(payload_text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(value, dict) or set(value) != {"observed"}:
        return None
    observed = value.get("observed")
    return observed.strip() if isinstance(observed, str) else None


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate key: {key}")
        value[key] = item
    return value


def _environment_fingerprint(environment: Mapping[str, str]) -> str:
    payload = json.dumps(
        sorted((key.casefold(), value) for key, value in environment.items()),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _operational_proof_matches(
    proof: object,
    *,
    executable: Path,
    executable_sha256: str,
    executable_version: str,
    environment_fingerprint: str,
    binding: CodexExecutionBinding,
    capability_class: CodexOperationalCapabilityClass,
) -> bool:
    return (
        isinstance(proof, CodexOperationalCapabilityProof)
        and proof.authentically_proven
        and os.path.normcase(proof.executable_path).casefold()
        == os.path.normcase(str(executable)).casefold()
        and proof.executable_sha256 == executable_sha256
        and proof.executable_version == executable_version
        and proof.capability_class is capability_class
        and proof.sandbox == binding.sandbox.value
        and proof.approval_policy == binding.approval_policy.value
        and proof.environment_fingerprint == environment_fingerprint
    )


def _probe_failure_code(
    exit_code: int,
    stderr: str,
    invalid_jsonl: tuple[InvalidJsonlLine, ...],
    terminal_issue: str | None,
    tool_failure: bool,
) -> str:
    lowered = stderr.casefold()
    if "not inside a trusted directory" in lowered:
        return "PROBE_REPOSITORY_NOT_TRUSTED"
    if "rejected" in lowered and "blocked by policy" in lowered:
        return "CAPABILITY_BLOCKED_BY_HOST_POLICY"
    if exit_code != 0:
        return "OPERATIONAL_PROBE_EXIT_NON_ZERO"
    if invalid_jsonl:
        return "OPERATIONAL_PROBE_MALFORMED_JSONL"
    if tool_failure:
        return "CAPABILITY_TOOL_EXECUTION_FAILED"
    if terminal_issue is not None:
        return terminal_issue
    return "OPERATIONAL_PROBE_RESULT_MISMATCH"


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
