"""Immutable observations for the bounded AGENTS.md integration contract."""

from __future__ import annotations

from dataclasses import dataclass

from .repository_reconnaissance import ManagedSectionStatus


@dataclass(frozen=True, slots=True)
class AgentsIntegrationInspection:
    status: ManagedSectionStatus
    managed_version: str | None
    content_fingerprint: str | None
