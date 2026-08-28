"""Private exact-mutation capabilities for the worktree registry."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from agentic_engineering_os.domain import WorktreeRegistry, to_dict


def _registry_write_boundary():
    @dataclass(frozen=True, slots=True)
    class RegistryWriteAuthorization:
        store: object
        operation: str
        before_fingerprint: str
        candidate_fingerprint: str

    def issue(
        *,
        store: object,
        before: WorktreeRegistry,
        candidate: WorktreeRegistry,
        operation: str,
    ) -> object:
        _require_inputs(store, before, candidate, operation)
        return RegistryWriteAuthorization(
            store=store,
            operation=operation,
            before_fingerprint=_fingerprint(before),
            candidate_fingerprint=_fingerprint(candidate),
        )

    def matches(
        authorization: object,
        *,
        store: object,
        before: WorktreeRegistry,
        candidate: WorktreeRegistry,
        operation: str,
    ) -> bool:
        try:
            _require_inputs(store, before, candidate, operation)
        except (TypeError, ValueError):
            return False
        return (
            isinstance(authorization, RegistryWriteAuthorization)
            and authorization.store is store
            and authorization.operation == operation
            and authorization.before_fingerprint == _fingerprint(before)
            and authorization.candidate_fingerprint == _fingerprint(candidate)
        )

    return issue, matches


def _require_inputs(
    store: object,
    before: WorktreeRegistry,
    candidate: WorktreeRegistry,
    operation: str,
) -> None:
    if store is None:
        raise ValueError("registry store instance is required")
    if not isinstance(before, WorktreeRegistry) or not isinstance(
        candidate, WorktreeRegistry
    ):
        raise TypeError("registry write requires WorktreeRegistry snapshots")
    if not isinstance(operation, str) or not operation.strip():
        raise ValueError("registry write operation is required")


def _fingerprint(registry: WorktreeRegistry) -> str:
    payload = json.dumps(
        to_dict(registry),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


(_issue_registry_write, _matches_registry_write) = _registry_write_boundary()
