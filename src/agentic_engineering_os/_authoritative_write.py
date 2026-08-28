"""Private, exact-mutation capabilities for authoritative state stores."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from agentic_engineering_os.domain import MissionState, ProjectState, to_dict


AuthoritativeState = ProjectState | MissionState


def _write_authorization_boundary():
    @dataclass(frozen=True, slots=True)
    class WriteAuthorization:
        store_kind: str
        store: object
        operation: str
        before_fingerprint: str
        candidate_fingerprint: str
        mission_before: tuple[str, int] | None
        mission_candidate: tuple[str, int] | None

    def issue(
        *,
        store_kind: str,
        store: object,
        before_state: AuthoritativeState,
        candidate_state: AuthoritativeState,
        operation: str,
    ) -> object:
        _require_boundary_input(
            store_kind,
            store,
            before_state,
            candidate_state,
            operation,
        )
        return WriteAuthorization(
            store_kind=store_kind,
            store=store,
            operation=operation,
            before_fingerprint=_state_fingerprint(before_state),
            candidate_fingerprint=_state_fingerprint(candidate_state),
            mission_before=_mission_binding(before_state),
            mission_candidate=_mission_binding(candidate_state),
        )

    def matches(
        candidate: object,
        *,
        store_kind: str,
        store: object,
        before_state: AuthoritativeState,
        candidate_state: AuthoritativeState,
        operation: str,
    ) -> bool:
        try:
            _require_boundary_input(
                store_kind,
                store,
                before_state,
                candidate_state,
                operation,
            )
        except (TypeError, ValueError):
            return False
        return (
            isinstance(candidate, WriteAuthorization)
            and candidate.store_kind == store_kind
            and candidate.store is store
            and candidate.operation == operation
            and candidate.before_fingerprint == _state_fingerprint(before_state)
            and candidate.candidate_fingerprint
            == _state_fingerprint(candidate_state)
            and candidate.mission_before == _mission_binding(before_state)
            and candidate.mission_candidate == _mission_binding(candidate_state)
        )

    return issue, matches


def _require_boundary_input(
    store_kind: str,
    store: object,
    before_state: AuthoritativeState,
    candidate_state: AuthoritativeState,
    operation: str,
) -> None:
    if store_kind not in {"PROJECT_STATE", "MISSION_STATE"}:
        raise ValueError("unknown authoritative store kind")
    if store is None:
        raise ValueError("authoritative store instance is required")
    if not isinstance(operation, str) or not operation.strip():
        raise ValueError("authoritative write operation is required")
    expected_type = ProjectState if store_kind == "PROJECT_STATE" else MissionState
    if not isinstance(before_state, expected_type) or not isinstance(
        candidate_state, expected_type
    ):
        raise TypeError("authoritative states do not match the store kind")


def _state_fingerprint(state: AuthoritativeState) -> str:
    """Fingerprint canonical state content; this function grants no authority."""

    if not isinstance(state, (ProjectState, MissionState)):
        raise TypeError("state fingerprint requires an authoritative state model")
    payload = json.dumps(
        to_dict(state),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _mission_binding(state: AuthoritativeState) -> tuple[str, int] | None:
    if not isinstance(state, MissionState):
        return None
    return state.mission_id, state.workflow_generation


(
    _issue_authoritative_write,
    _matches_authoritative_write,
) = _write_authorization_boundary()
