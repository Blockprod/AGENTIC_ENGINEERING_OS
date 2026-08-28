"""Portable deterministic identity primitives for worktree assignments."""

from __future__ import annotations

import hashlib
import re
import unicodedata


_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
_MISSION_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]+$")
_USER_STORY_PATTERN = re.compile(r"^US-[0-9]{4}$")


def validate_identity_inputs(
    mission_id: object,
    user_story_id: object,
    workflow_generation: object,
    baseline_commit: object,
) -> tuple[str, str, int, str]:
    if (
        not isinstance(mission_id, str)
        or not mission_id.strip()
        or mission_id != mission_id.strip()
        or len(mission_id) > 256
        or unicodedata.normalize("NFC", mission_id) != mission_id
        or _MISSION_PATTERN.fullmatch(mission_id) is None
    ):
        raise ValueError("mission_id must be non-empty, NFC, trimmed, and portable")
    if not isinstance(user_story_id, str) or not _USER_STORY_PATTERN.fullmatch(
        user_story_id
    ):
        raise ValueError("user_story_id must match US-0000")
    if (
        not isinstance(workflow_generation, int)
        or isinstance(workflow_generation, bool)
        or workflow_generation < 0
    ):
        raise ValueError("workflow_generation must be a non-negative integer")
    if not isinstance(baseline_commit, str) or not _COMMIT_PATTERN.fullmatch(
        baseline_commit
    ):
        raise ValueError("baseline_commit must be a full Git SHA")
    return mission_id, user_story_id, workflow_generation, baseline_commit.casefold()


def derive_assignment_id(
    mission_id: str,
    user_story_id: str,
    workflow_generation: int,
    baseline_commit: str,
) -> str:
    values = validate_identity_inputs(
        mission_id,
        user_story_id,
        workflow_generation,
        baseline_commit,
    )
    encoded = b"".join(
        len(part).to_bytes(4, "big") + part
        for part in (
            values[0].encode("utf-8"),
            values[1].encode("utf-8"),
            str(values[2]).encode("ascii"),
            values[3].encode("ascii"),
        )
    )
    return f"wa-{hashlib.sha256(encoded).hexdigest()[:24]}"


def derive_branch_name(
    user_story_id: str,
    workflow_generation: int,
    assignment_id: str,
) -> str:
    if not re.fullmatch(r"wa-[0-9a-f]{24}", assignment_id):
        raise ValueError("assignment_id is not canonical")
    branch = (
        f"agentic/g{workflow_generation}/"
        f"{user_story_id.casefold()}-{assignment_id.removeprefix('wa-')}"
    )
    if len(branch.encode("utf-8")) > 120:
        raise ValueError("derived branch exceeds the V1 length limit")
    return branch
