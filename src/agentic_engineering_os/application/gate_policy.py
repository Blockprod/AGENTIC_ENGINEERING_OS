"""Deterministic story-scoped instances of repository Gate policies."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from agentic_engineering_os.domain import (
    GateAggregation,
    ProjectConfiguration,
    UserStory,
    VerificationCommand,
)


_POLICY_ID = re.compile(r"^[^\s/\\:]+$")
_STORY_ID = re.compile(r"^US-[0-9]{4}$")


class GatePolicyResolutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class ResolvedGatePolicy:
    policy_id: str
    gate_id: str
    user_story_id: str
    verification_commands: tuple[VerificationCommand, ...]
    aggregation: GateAggregation
    repository_dependent: bool


def instantiate_gate_id(policy_id: str, user_story_id: str) -> str:
    """Return the sole canonical Gate identity for one policy/story pair."""

    if (
        not isinstance(policy_id, str)
        or policy_id != policy_id.strip()
        or unicodedata.normalize("NFC", policy_id) != policy_id
        or not _POLICY_ID.fullmatch(policy_id)
    ):
        raise GatePolicyResolutionError(
            "INVALID_POLICY_ID", "policy_id must be trimmed NFC text without ':'"
        )
    if not isinstance(user_story_id, str) or not _STORY_ID.fullmatch(user_story_id):
        raise GatePolicyResolutionError(
            "INVALID_USER_STORY_ID", "user_story_id must use canonical US-NNNN form"
        )
    return f"{policy_id}::{user_story_id}"


def resolve_story_policies(
    configuration: ProjectConfiguration,
    user_story: UserStory,
) -> tuple[ResolvedGatePolicy, ...]:
    """Resolve exactly the configured policies named by one User Story."""

    if not isinstance(configuration, ProjectConfiguration):
        raise GatePolicyResolutionError(
            "INVALID_CONFIGURATION", "canonical ProjectConfiguration is required"
        )
    if not isinstance(user_story, UserStory):
        raise GatePolicyResolutionError(
            "INVALID_USER_STORY", "canonical UserStory is required"
        )
    policies = configuration.gate_policies
    if not policies and user_story.required_gates:
        raise GatePolicyResolutionError(
            "GATE_POLICY_MISSING",
            "User Story requires Gates but configuration has no Gate policies",
        )
    commands = {item.command_id: item for item in configuration.verification_commands}
    instances = {
        instantiate_gate_id(item.policy_id, user_story.id): item for item in policies
    }
    if len(instances) != len(policies):
        raise GatePolicyResolutionError(
            "AMBIGUOUS_GATE_POLICY", "multiple policies produce the same Gate identity"
        )
    supplied = tuple(user_story.required_gates)
    if len(set(supplied)) != len(supplied):
        raise GatePolicyResolutionError(
            "AMBIGUOUS_REQUIRED_GATE", "User Story required Gates are duplicated"
        )
    unknown = tuple(item for item in supplied if item not in instances)
    if unknown:
        raise GatePolicyResolutionError(
            "UNKNOWN_GATE_POLICY_INSTANCE",
            "User Story contains an unknown, malformed, or cross-story Gate: "
            + ", ".join(sorted(unknown)),
        )
    missing = tuple(
        instantiate_gate_id(item.policy_id, user_story.id)
        for item in policies
        if item.required
        and instantiate_gate_id(item.policy_id, user_story.id) not in supplied
    )
    if missing:
        raise GatePolicyResolutionError(
            "REQUIRED_GATE_POLICY_MISSING",
            "User Story omits required Gate policies: " + ", ".join(missing),
        )
    selected = set(supplied)
    return tuple(
        ResolvedGatePolicy(
            policy_id=policy.policy_id,
            gate_id=gate_id,
            user_story_id=user_story.id,
            verification_commands=tuple(
                commands[identifier] for identifier in policy.verification_command_ids
            ),
            aggregation=policy.aggregation,
            repository_dependent=policy.repository_dependent,
        )
        for gate_id, policy in instances.items()
        if gate_id in selected
    )
