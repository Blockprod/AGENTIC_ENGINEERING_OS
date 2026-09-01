"""Private exact-mutation capabilities for maintenance state."""

from __future__ import annotations

from dataclasses import dataclass

from .domain.maintenance import MaintenanceRecord


def _maintenance_write_boundary():
    @dataclass(frozen=True, slots=True)
    class Authorization:
        store: object
        operation: str
        before_fingerprint: str | None
        after_fingerprint: str

    def issue(*, store: object, before: MaintenanceRecord | None, after: MaintenanceRecord, operation: str) -> object:
        return Authorization(store, operation, before.fingerprint if before else None, after.fingerprint)

    def matches(candidate: object, *, store: object, before: MaintenanceRecord | None, after: MaintenanceRecord, operation: str) -> bool:
        return (
            isinstance(candidate, Authorization)
            and candidate.store is store
            and candidate.operation == operation
            and candidate.before_fingerprint == (before.fingerprint if before else None)
            and candidate.after_fingerprint == after.fingerprint
        )

    return issue, matches


(_issue_maintenance_write, _matches_maintenance_write) = _maintenance_write_boundary()
