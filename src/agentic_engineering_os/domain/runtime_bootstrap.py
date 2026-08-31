"""Immutable facts returned by the minimal runtime-state bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .repository_reconnaissance import DocumentStatus


class RuntimeBootstrapStatus(str, Enum):
    BOOTSTRAPPED = "BOOTSTRAPPED"
    ALREADY_BOOTSTRAPPED = "ALREADY_BOOTSTRAPPED"
    REFUSED = "REFUSED"
    FAILED = "FAILED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"


class RuntimeStoreDisposition(str, Enum):
    REQUIRED_AT_BOOTSTRAP = "REQUIRED_AT_BOOTSTRAP"
    LAZY_INITIALIZED_ON_FIRST_USE = "LAZY_INITIALIZED_ON_FIRST_USE"
    AUTHORIZED_EVENT_ONLY = "AUTHORIZED_EVENT_ONLY"


@dataclass(frozen=True, slots=True)
class RuntimeFileFact:
    relative_path: str
    status: DocumentStatus


@dataclass(frozen=True, slots=True)
class RuntimeBootstrapFinding:
    code: str
    target_path: str
    detail: str


@dataclass(frozen=True, slots=True)
class RuntimeBootstrapResult:
    repository_root: str
    status: RuntimeBootstrapStatus
    expected_profile_fingerprint: str | None
    profile_fingerprint_before: str | None
    profile_fingerprint_after: str | None
    git_head_before: str | None
    git_head_after: str | None
    runtime_files_before: tuple[RuntimeFileFact, ...]
    runtime_files_after: tuple[RuntimeFileFact, ...]
    created_paths: tuple[str, ...]
    findings: tuple[RuntimeBootstrapFinding, ...]
