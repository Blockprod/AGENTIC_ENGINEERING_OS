"""Private capability for one ledger-validated implementation commit."""

from __future__ import annotations

from dataclasses import dataclass
import re


_EXECUTION_ID_PATTERN = re.compile(r"^cx-[0-9a-f]{24}$")


@dataclass(frozen=True, slots=True)
class ValidatedImplementationBinding:
    assignment_id: str
    workflow_generation: int
    execution_id: str
    result_fingerprint: str
    files_changed: tuple[str, ...]


def _commit_boundary():
    @dataclass(frozen=True, slots=True)
    class CommitAuthorization:
        manager: object
        binding: ValidatedImplementationBinding

    def issue(
        *,
        manager: object,
        assignment_id: str,
        workflow_generation: int,
        execution_id: str,
        result_fingerprint: str,
        files_changed: tuple[str, ...],
    ) -> object:
        if manager is None:
            raise ValueError("WorktreeManager instance is required")
        binding = ValidatedImplementationBinding(
            assignment_id,
            workflow_generation,
            execution_id,
            result_fingerprint,
            files_changed,
        )
        _validate(binding)
        return CommitAuthorization(manager, binding)

    def consume(authorization: object, *, manager: object) -> ValidatedImplementationBinding | None:
        if not isinstance(authorization, CommitAuthorization) or authorization.manager is not manager:
            return None
        try:
            _validate(authorization.binding)
        except (TypeError, ValueError):
            return None
        return authorization.binding

    return issue, consume


def _validate(binding: ValidatedImplementationBinding) -> None:
    if not isinstance(binding.assignment_id, str) or not binding.assignment_id:
        raise ValueError("assignment_id is required")
    if (
        not isinstance(binding.workflow_generation, int)
        or isinstance(binding.workflow_generation, bool)
        or binding.workflow_generation < 0
    ):
        raise ValueError("workflow_generation must be a non-negative integer")
    if not isinstance(binding.execution_id, str) or _EXECUTION_ID_PATTERN.fullmatch(binding.execution_id) is None:
        raise ValueError("execution_id must be a canonical Codex execution ID")
    if (
        not isinstance(binding.result_fingerprint, str)
        or len(binding.result_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in binding.result_fingerprint)
    ):
        raise ValueError("result_fingerprint must be lowercase SHA-256")
    if not isinstance(binding.files_changed, tuple) or not binding.files_changed:
        raise ValueError("files_changed must be a non-empty tuple")
    if any(not isinstance(path, str) or not path for path in binding.files_changed):
        raise ValueError("files_changed contains an invalid path")


(
    _issue_validated_implementation_commit,
    _consume_validated_implementation_commit,
) = _commit_boundary()
