"""Closed registry of historically justified, deterministic migrations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable

from agentic_engineering_os.application.execution_state import EXECUTION_LEDGER_VERSION
from agentic_engineering_os.domain import (
    AGENTS_MANAGED_SECTION,
    AGENTS_MANAGED_SECTION_VERSION,
    GITIGNORE_MANAGED_SECTION_VERSION,
    GITIGNORE_SECTION_END,
    GITIGNORE_SECTION_START,
    GITIGNORE_V1_SECTION_END,
    GITIGNORE_V1_SECTION_START,
    MAINTENANCE_SCHEMA_VERSION,
    MigrationArtifact,
    MissionStateGitPolicy,
    gitignore_managed_section,
    gitignore_managed_section_v1,
)

from ._negative_outcome_store import _validate_document as _validate_negative_current
from .agents_integration import AgentsIntegrationService
from .project_configuration import CONFIG_VERSION
from .project_state_store import SCHEMA_VERSION
from .worktree_registry_store import WORKTREE_REGISTRY_VERSION


_AGENTS_V1 = "\n".join(
    (
        "<!-- BEGIN AGENTIC_ENGINEERING_OS MANAGED SECTION v1 -->",
        "## Agentic Engineering OS",
        "",
        "Follow the installed AGENTIC_ENGINEERING_OS operating contract.",
        "Repository facts and Control Plane decisions prevail over agent declarations.",
        "<!-- END AGENTIC_ENGINEERING_OS MANAGED SECTION v1 -->",
        "",
    )
)


class MigrationRegistryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class MigrationCandidate:
    content: bytes
    authority_fingerprint_before: str | None
    authority_fingerprint_after: str | None


@dataclass(frozen=True, slots=True)
class _MigrationDefinition:
    artifact: MigrationArtifact
    target_path: str
    source_version: str
    target_version: str
    versioned_in_git: bool
    volatile: bool
    human_confirmation_required: bool
    transform: Callable[[bytes], MigrationCandidate]
    validate_current: Callable[[bytes], None]


class RepositoryMigrationRegistry:
    """Resolve only explicit source-to-target edges from the product history."""

    def __init__(self) -> None:
        definitions = (
            _MigrationDefinition(
                MigrationArtifact.AGENTS_MANAGED_SECTION,
                "AGENTS.md",
                "1",
                AGENTS_MANAGED_SECTION_VERSION,
                True,
                False,
                True,
                _upgrade_agents_v1,
                _validate_agents_current,
            ),
            _MigrationDefinition(
                MigrationArtifact.GITIGNORE_MANAGED_SECTION,
                ".gitignore",
                "1",
                GITIGNORE_MANAGED_SECTION_VERSION,
                True,
                False,
                True,
                _upgrade_gitignore_v1,
                _validate_gitignore_current,
            ),
            _MigrationDefinition(
                MigrationArtifact.NEGATIVE_OUTCOME_LEDGER,
                ".agentic-engineering-os/negative-outcomes.json",
                "1.0",
                "2.0",
                False,
                True,
                False,
                _upgrade_negative_v1,
                _validate_negative_v2,
            ),
        )
        self._definitions = {
            (item.artifact, item.source_version, item.target_version): item
            for item in definitions
        }

    @property
    def supported_edges(
        self,
    ) -> tuple[tuple[MigrationArtifact, str, str], ...]:
        return tuple(sorted(self._definitions, key=lambda item: item[0].value))

    @property
    def target_versions(
        self,
    ) -> tuple[tuple[MigrationArtifact, str, bool | None, bool], ...]:
        return (
            (
                MigrationArtifact.AGENTS_MANAGED_SECTION,
                AGENTS_MANAGED_SECTION_VERSION,
                True,
                False,
            ),
            (
                MigrationArtifact.GITIGNORE_MANAGED_SECTION,
                GITIGNORE_MANAGED_SECTION_VERSION,
                True,
                False,
            ),
            (MigrationArtifact.PROJECT_CONFIGURATION, CONFIG_VERSION, True, False),
            (MigrationArtifact.PROJECT_STATE, SCHEMA_VERSION, True, False),
            (MigrationArtifact.MISSION_STATE, "1.0", None, False),
            (MigrationArtifact.WORKTREE_REGISTRY, WORKTREE_REGISTRY_VERSION, False, True),
            (MigrationArtifact.NEGATIVE_OUTCOME_LEDGER, "2.0", False, True),
            (MigrationArtifact.EXECUTION_LEDGER, EXECUTION_LEDGER_VERSION, False, True),
            (MigrationArtifact.MAINTENANCE_STATE, MAINTENANCE_SCHEMA_VERSION, False, True),
        )

    def definition(
        self,
        artifact: MigrationArtifact,
        source_version: str,
        target_version: str,
    ) -> _MigrationDefinition | None:
        if not isinstance(artifact, MigrationArtifact):
            return None
        return self._definitions.get((artifact, source_version, target_version))

    def prepare_candidate(
        self,
        artifact: MigrationArtifact,
        source_version: str,
        target_version: str,
        source: bytes,
    ) -> MigrationCandidate:
        definition = self.definition(artifact, source_version, target_version)
        if definition is None:
            raise MigrationRegistryError(
                "UNSUPPORTED_MIGRATION",
                f"unsupported edge {artifact.value}:{source_version}->{target_version}",
            )
        candidate = definition.transform(source)
        definition.validate_current(candidate.content)
        if (
            candidate.authority_fingerprint_before is not None
            and candidate.authority_fingerprint_before
            != candidate.authority_fingerprint_after
        ):
            raise MigrationRegistryError(
                "AUTHORITY_CHANGED",
                "migration changed authoritative semantic content",
            )
        return candidate

    def validate_current(
        self, artifact: MigrationArtifact, target_version: str, content: bytes
    ) -> None:
        matches = [
            item
            for item in self._definitions.values()
            if item.artifact is artifact and item.target_version == target_version
        ]
        if len(matches) != 1:
            raise MigrationRegistryError(
                "UNSUPPORTED_MIGRATION", "current artifact validator is unavailable"
            )
        matches[0].validate_current(content)


def _upgrade_agents_v1(source: bytes) -> MigrationCandidate:
    try:
        source.decode("utf-8")
    except UnicodeError as error:
        raise MigrationRegistryError("CORRUPT_SOURCE", "AGENTS.md is not UTF-8") from error
    replacements: list[tuple[bytes, bytes]] = []
    for newline in (b"\r\n", b"\n", b"\r"):
        old = _AGENTS_V1.encode("utf-8").replace(b"\n", newline)
        current = AGENTS_MANAGED_SECTION.encode("utf-8").replace(b"\n", newline)
        replacements.extend(((old, current), (old.rstrip(newline), current.rstrip(newline))))
    candidates = {
        source.replace(old, new, 1)
        for old, new in replacements
        if source.count(old) == 1
    }
    if len(candidates) != 1:
        raise MigrationRegistryError(
            "CORRUPT_SOURCE", "AGENTS v1 section is absent, ambiguous, or noncanonical"
        )
    candidate = candidates.pop()
    return MigrationCandidate(candidate, None, None)


def _validate_agents_current(content: bytes) -> None:
    if AgentsIntegrationService().inspect(content).status.value != "CURRENT":
        raise MigrationRegistryError("POST_VALIDATION_FAILED", "AGENTS v2 is not canonical")


def _upgrade_gitignore_v1(source: bytes) -> MigrationCandidate:
    try:
        source.decode("utf-8")
    except UnicodeError as error:
        raise MigrationRegistryError("CORRUPT_SOURCE", ".gitignore is not UTF-8") from error
    candidates: set[bytes] = set()
    for policy in MissionStateGitPolicy:
        old_section = gitignore_managed_section_v1(policy).encode("utf-8")
        new_section = gitignore_managed_section(policy).encode("utf-8")
        for newline in (b"\r\n", b"\n", b"\r"):
            old = old_section.replace(b"\n", newline)
            new = new_section.replace(b"\n", newline)
            for old_form, new_form in (
                (old, new),
                (old.rstrip(newline), new.rstrip(newline)),
            ):
                if source.count(old_form) == 1:
                    candidates.add(source.replace(old_form, new_form, 1))
    if len(candidates) != 1:
        raise MigrationRegistryError(
            "CORRUPT_SOURCE",
            ".gitignore v1 section is absent, ambiguous, or noncanonical",
        )
    return MigrationCandidate(candidates.pop(), None, None)


def _validate_gitignore_current(content: bytes) -> None:
    try:
        text = content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError as error:
        raise MigrationRegistryError("POST_VALIDATION_FAILED", ".gitignore is not UTF-8") from error
    matches = 0
    for policy in MissionStateGitPolicy:
        canonical = gitignore_managed_section(policy).rstrip("\n")
        if text.count(canonical) == 1:
            matches += 1
    if (
        matches != 1
        or text.count(GITIGNORE_SECTION_START) != 1
        or text.count(GITIGNORE_SECTION_END) != 1
        or GITIGNORE_V1_SECTION_START in text
        or GITIGNORE_V1_SECTION_END in text
    ):
        raise MigrationRegistryError(
            "POST_VALIDATION_FAILED", ".gitignore v2 section is not canonical"
        )


def _upgrade_negative_v1(source: bytes) -> MigrationCandidate:
    data = _strict_json(source, maximum=16_000_000)
    if not isinstance(data, dict) or set(data) != {"version", "outcomes"}:
        raise MigrationRegistryError("CORRUPT_SOURCE", "negative ledger v1 shape is invalid")
    if data["version"] != "1.0" or not isinstance(data["outcomes"], list):
        raise MigrationRegistryError("CORRUPT_SOURCE", "negative ledger v1 version is invalid")
    outcomes = _validate_old_outcomes(data["outcomes"])
    authority = _sha256(_canonical_json(outcomes, compact=True).encode("utf-8"))
    candidate_data = {"version": "2.0", "outcomes": outcomes, "transactions": []}
    normalized = _validate_negative_current(candidate_data)
    content = _canonical_json(normalized).encode("utf-8")
    after = _sha256(
        _canonical_json(normalized["outcomes"], compact=True).encode("utf-8")
    )
    return MigrationCandidate(content, authority, after)


def _validate_negative_v2(content: bytes) -> None:
    candidate = _strict_json(content, maximum=16_000_000)
    normalized = _validate_negative_current(candidate)
    if content != _canonical_json(normalized).encode("utf-8"):
        raise MigrationRegistryError("POST_VALIDATION_FAILED", "negative ledger is not canonical")


def _validate_old_outcomes(candidate: list[object]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in candidate:
        if not isinstance(item, dict) or set(item) != {
            "fingerprint",
            "result",
            "consumed",
        }:
            raise MigrationRegistryError("CORRUPT_SOURCE", "negative outcome shape is invalid")
        result = item["result"]
        consumed = item["consumed"]
        fingerprint = item["fingerprint"]
        if not isinstance(result, dict) or not isinstance(consumed, bool):
            raise MigrationRegistryError("CORRUPT_SOURCE", "negative outcome value is invalid")
        result_normalized = json.loads(_canonical_json(result, compact=True))
        expected = _sha256(_canonical_json(result_normalized, compact=True).encode("utf-8"))
        if fingerprint != expected or fingerprint in seen:
            raise MigrationRegistryError(
                "CORRUPT_SOURCE", "negative outcome fingerprint is invalid"
            )
        seen.add(expected)
        normalized.append(
            {
                "fingerprint": expected,
                "result": result_normalized,
                "consumed": consumed,
            }
        )
    return sorted(normalized, key=lambda item: str(item["fingerprint"]))


def _strict_json(source: bytes, *, maximum: int) -> object:
    if len(source) > maximum:
        raise MigrationRegistryError("SOURCE_TOO_LARGE", "migration source exceeds policy")
    try:
        text = source.decode("utf-8")
        return json.loads(text, object_pairs_hook=_strict_object, parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise MigrationRegistryError("CORRUPT_SOURCE", "source is not strict JSON") from error


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"invalid constant: {value}")


def _canonical_json(value: object, *, compact: bool = False) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=None if compact else 2,
        separators=(",", ":") if compact else (",", ": "),
        allow_nan=False,
    ) + ("" if compact else "\n")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
