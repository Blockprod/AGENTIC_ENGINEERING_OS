"""Immutable results and Human bindings for safe repository initialization."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .initialization_planning import (
    InitializationOperationType,
    PlannedCurrentState,
)
from .repository_reconnaissance import AgenticOsInitializationState


class InitializationApplyStatus(str, Enum):
    APPLIED = "APPLIED"
    NO_OP = "NO_OP"
    REFUSED = "REFUSED"
    FAILED = "FAILED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"


class OperationApplyStatus(str, Enum):
    APPLIED = "APPLIED"
    NO_OP = "NO_OP"
    FAILED = "FAILED"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


@dataclass(frozen=True, slots=True)
class HumanOperationConfirmation:
    plan_fingerprint: str
    operation_id: str
    target_path: str
    expected_current_state: PlannedCurrentState
    confirmed_by: str


@dataclass(frozen=True, slots=True)
class InitializationOperationResult:
    operation_id: str
    operation_type: InitializationOperationType
    target_path: str
    status: OperationApplyStatus
    detail: str


@dataclass(frozen=True, slots=True)
class InitializationApplyFinding:
    code: str
    operation_id: str | None
    target_path: str
    detail: str


@dataclass(frozen=True, slots=True)
class InitializationResult:
    plan_fingerprint: str
    repository_root: str
    status: InitializationApplyStatus
    operation_results: tuple[InitializationOperationResult, ...]
    findings: tuple[InitializationApplyFinding, ...]
    profile_fingerprint_before: str | None
    profile_fingerprint_after: str | None
    git_head_before: str | None
    git_head_after: str | None
    initialization_state_after: AgenticOsInitializationState | None
