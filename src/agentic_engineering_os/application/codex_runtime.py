"""Application contract for factual Codex transport observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from threading import Event
from typing import TYPE_CHECKING, Protocol

from agentic_engineering_os.domain import MissionRole

if TYPE_CHECKING:
    from .prompt_compiler import CompiledPrompt


class CodexSandboxMode(str, Enum):
    """Closed set of sandbox modes allowed by the P4 runtime boundary."""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


class CodexApprovalPolicy(str, Enum):
    """Non-interactive approval policy supported by the initial adapter."""

    NEVER = "never"


@dataclass(frozen=True, slots=True)
class CodexExecutionBinding:
    """Authoritative values that one physical execution must match exactly."""

    request_id: str
    context_fingerprint: str
    mission_id: str
    workflow_generation: int
    role: MissionRole
    subject: str
    cwd: str
    expected_commit: str
    sandbox: CodexSandboxMode
    approval_policy: CodexApprovalPolicy
    timeout_seconds: float
    require_clean_git: bool = True
    output_schema_path: str | None = None


@dataclass(frozen=True, slots=True)
class CodexJsonlEvent:
    line_number: int
    event_type: str | None
    raw_line: str
    payload_json: str


@dataclass(frozen=True, slots=True)
class InvalidJsonlLine:
    line_number: int
    raw_line: str
    error: str


@dataclass(frozen=True, slots=True)
class GitExecutionObservation:
    head_commit: str | None
    clean: bool | None
    error: str | None


@dataclass(frozen=True, slots=True)
class CodexExecutionObservation:
    """Immutable transport facts; never a role or Control Plane verdict."""

    request_id: str
    context_fingerprint: str
    executable_path: str | None
    executable_version: str | None
    executable_sha256: str | None
    cwd: str
    invocation: tuple[str, ...]
    started_at: datetime | None
    ended_at: datetime
    process_id: int | None
    thread_id: str | None
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    events: tuple[CodexJsonlEvent, ...]
    invalid_jsonl_lines: tuple[InvalidJsonlLine, ...]
    final_output: str | None
    timed_out: bool
    interrupted: bool
    tool_failure_observed: bool
    git_before: GitExecutionObservation | None
    git_after: GitExecutionObservation | None
    issues: tuple[str, ...]


class CodexRuntimePort(Protocol):
    def execute(
        self,
        compiled_prompt: CompiledPrompt,
        binding: CodexExecutionBinding,
        *,
        cancellation: Event | None = None,
    ) -> CodexExecutionObservation: ...
