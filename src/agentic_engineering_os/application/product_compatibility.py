"""Read-only, fail-closed product and repository compatibility evaluation."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath

from agentic_engineering_os import __version__ as PRODUCT_VERSION
from agentic_engineering_os.domain import MigrationArtifact
from agentic_engineering_os.infrastructure.migration_registry import (
    RepositoryMigrationRegistry,
)


_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_FORMAT_VERSION = re.compile(r"^(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class CompatibilityClassification(str, Enum):
    CURRENT = "CURRENT"
    BACKWARD_COMPATIBLE = "BACKWARD_COMPATIBLE"
    MIGRATION_REQUIRED = "MIGRATION_REQUIRED"
    UNSUPPORTED = "UNSUPPORTED"
    FUTURE_VERSION = "FUTURE_VERSION"
    CORRUPT = "CORRUPT"
    UNKNOWN = "UNKNOWN"
    NOT_PRESENT_LAZY = "NOT_PRESENT_LAZY"


class CompatibilityArtifact(str, Enum):
    PROJECT_CONFIGURATION = "PROJECT_CONFIGURATION"
    PROJECT_STATE = "PROJECT_STATE"
    MISSION_STATE = "MISSION_STATE"
    AGENTS_MANAGED_SECTION = "AGENTS_MANAGED_SECTION"
    GITIGNORE_MANAGED_SECTION = "GITIGNORE_MANAGED_SECTION"
    WORKTREE_REGISTRY = "WORKTREE_REGISTRY"
    NEGATIVE_OUTCOME_LEDGER = "NEGATIVE_OUTCOME_LEDGER"
    EXECUTION_LEDGER = "EXECUTION_LEDGER"
    OPERATIONAL_EVENT = "OPERATIONAL_EVENT"
    OPERATIONAL_EVENT_STORE = "OPERATIONAL_EVENT_STORE"
    MAINTENANCE_STATE = "MAINTENANCE_STATE"


class ArtifactRequirement(str, Enum):
    REQUIRED = "REQUIRED"
    LAZY = "LAZY"


@dataclass(frozen=True, slots=True)
class CompatibilityArtifactContract:
    artifact: CompatibilityArtifact
    current_version: str
    relative_path: str
    persistence: str
    authority_class: str
    requirement: ArtifactRequirement
    migration_artifact: MigrationArtifact | None = None
    backward_compatible_versions: tuple[str, ...] = ()


ARTIFACT_CONTRACTS = (
    CompatibilityArtifactContract(CompatibilityArtifact.PROJECT_CONFIGURATION, "1.0", ".agentic-engineering-os/config.json", "JSON / Git-tracked", "PROJECT_CONFIGURATION", ArtifactRequirement.REQUIRED, MigrationArtifact.PROJECT_CONFIGURATION),
    CompatibilityArtifactContract(CompatibilityArtifact.PROJECT_STATE, "1.0", ".agentic-engineering-os/state.json", "JSON / Git-tracked", "SYSTEM_INVARIANT", ArtifactRequirement.REQUIRED, MigrationArtifact.PROJECT_STATE),
    CompatibilityArtifactContract(CompatibilityArtifact.MISSION_STATE, "1.0", ".agentic-engineering-os/mission.json", "JSON / tracked-or-ignored by project policy", "OPERATIONAL_STATE", ArtifactRequirement.LAZY, MigrationArtifact.MISSION_STATE),
    CompatibilityArtifactContract(CompatibilityArtifact.AGENTS_MANAGED_SECTION, "2", "AGENTS.md", "managed Markdown / Git-tracked", "SYSTEM_INVARIANT", ArtifactRequirement.REQUIRED, MigrationArtifact.AGENTS_MANAGED_SECTION),
    CompatibilityArtifactContract(CompatibilityArtifact.GITIGNORE_MANAGED_SECTION, "1", ".gitignore", "managed text / Git-tracked", "SYSTEM_INVARIANT", ArtifactRequirement.REQUIRED),
    CompatibilityArtifactContract(CompatibilityArtifact.WORKTREE_REGISTRY, "1.0", ".agentic-engineering-os/worktrees.json", "JSON / ignored volatile", "OPERATIONAL_STATE", ArtifactRequirement.LAZY, MigrationArtifact.WORKTREE_REGISTRY),
    CompatibilityArtifactContract(CompatibilityArtifact.NEGATIVE_OUTCOME_LEDGER, "2.0", ".agentic-engineering-os/negative-outcomes.json", "JSON / ignored volatile", "OPERATIONAL_STATE", ArtifactRequirement.LAZY, MigrationArtifact.NEGATIVE_OUTCOME_LEDGER),
    CompatibilityArtifactContract(CompatibilityArtifact.EXECUTION_LEDGER, "1.1", ".agentic-engineering-os/executions.json", "JSON / ignored volatile", "OPERATIONAL_STATE", ArtifactRequirement.LAZY, MigrationArtifact.EXECUTION_LEDGER),
    CompatibilityArtifactContract(CompatibilityArtifact.OPERATIONAL_EVENT, "1.0", ".agentic-engineering-os/operational-events/*.jsonl:event", "JSONL payload / ignored volatile", "NON_AUTHORITATIVE_OBSERVATION", ArtifactRequirement.LAZY),
    CompatibilityArtifactContract(CompatibilityArtifact.OPERATIONAL_EVENT_STORE, "1.0", ".agentic-engineering-os/operational-events/*.jsonl:record", "JSONL record / ignored volatile", "NON_AUTHORITATIVE_OBSERVATION", ArtifactRequirement.LAZY),
    CompatibilityArtifactContract(CompatibilityArtifact.MAINTENANCE_STATE, "1.0", ".agentic-engineering-os/maintenance.json", "JSON / ignored volatile", "SYSTEM_INVARIANT", ArtifactRequirement.LAZY, MigrationArtifact.MAINTENANCE_STATE),
)
_CONTRACT_BY_ARTIFACT = {item.artifact: item for item in ARTIFACT_CONTRACTS}


@dataclass(frozen=True, slots=True)
class InstalledProduct:
    version: str
    release_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or _SEMVER.fullmatch(self.version) is None:
            raise ValueError("installed product requires a canonical semantic version")
        if not isinstance(self.release_digest, str) or _SHA256.fullmatch(self.release_digest) is None:
            raise ValueError("installed product requires an immutable SHA-256 digest")


@dataclass(frozen=True, slots=True)
class ArtifactVersionObservation:
    artifact: CompatibilityArtifact
    project_id: str
    repository_root: str
    repository_head: str
    configuration_fingerprint: str
    relative_path: str
    present: bool
    version: str | None
    content_fingerprint: str | None
    structurally_valid: bool | None
    model_schema_aligned: bool | None

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, CompatibilityArtifact):
            raise ValueError("artifact must use the closed compatibility catalogue")
        _validate_binding(self.project_id, self.repository_root, self.repository_head, self.configuration_fingerprint)
        _validate_relative_path(self.relative_path)
        if not isinstance(self.present, bool):
            raise ValueError("present must be explicit")
        if not self.present:
            if any(value is not None for value in (self.version, self.content_fingerprint, self.structurally_valid, self.model_schema_aligned)):
                raise ValueError("absent artifact cannot carry observed content")
            return
        if not isinstance(self.version, str) or not self.version or len(self.version) > 32:
            raise ValueError("present artifact requires a bounded explicit version")
        if not isinstance(self.content_fingerprint, str) or _SHA256.fullmatch(self.content_fingerprint) is None:
            raise ValueError("present artifact requires a content SHA-256")
        allowed_facts = (True, False, None)
        if not any(self.structurally_valid is item for item in allowed_facts) or not any(
            self.model_schema_aligned is item for item in allowed_facts
        ):
            raise ValueError("validation facts must be true, false, or unknown")


@dataclass(frozen=True, slots=True)
class CompatibilityEvaluationContext:
    installed_product: InstalledProduct
    project_id: str
    repository_root: str
    repository_head: str
    configuration_fingerprint: str
    artifacts: tuple[ArtifactVersionObservation, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.installed_product, InstalledProduct):
            raise ValueError("installed product identity is required")
        _validate_binding(self.project_id, self.repository_root, self.repository_head, self.configuration_fingerprint)
        if not isinstance(self.artifacts, tuple) or any(not isinstance(item, ArtifactVersionObservation) for item in self.artifacts):
            raise ValueError("artifact observations must be immutable and typed")
        expected = tuple(sorted(self.artifacts, key=lambda item: item.artifact.value))
        if self.artifacts != expected:
            raise ValueError("artifact observations must be canonically ordered")
        identities = tuple(item.artifact for item in self.artifacts)
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate artifact observations are ambiguous")


@dataclass(frozen=True, slots=True)
class ArtifactCompatibilityAssessment:
    artifact: CompatibilityArtifact
    relative_path: str
    source_version: str | None
    target_version: str
    classification: CompatibilityClassification
    required_migration: tuple[str, str] | None
    diagnostic: str
    content_fingerprint: str | None


_ATTESTATION_KEY = secrets.token_bytes(32)


@dataclass(frozen=True, slots=True)
class CompatibilityAssessment:
    product_version: str
    product_digest: str
    product_classification: CompatibilityClassification
    project_id: str
    repository_root: str
    repository_head: str
    configuration_fingerprint: str
    artifacts: tuple[ArtifactCompatibilityAssessment, ...]
    global_compatibility: CompatibilityClassification
    required_explicit_migrations: tuple[str, ...]
    blockers: tuple[str, ...]
    diagnostics: tuple[str, ...]
    fingerprint: str
    _attestation: str = field(default="", repr=False, compare=False)

    @property
    def authentically_evaluated(self) -> bool:
        return bool(self._attestation) and hmac.compare_digest(self._attestation, _sign(self))


class CompatibilityEvaluationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class CompatibilityEvaluator:
    """Classify exact observations; never migrate or mutate repository state."""

    def __init__(self) -> None:
        self._registry = RepositoryMigrationRegistry()

    def evaluate(self, context: CompatibilityEvaluationContext) -> CompatibilityAssessment:
        if not isinstance(context, CompatibilityEvaluationContext):
            raise CompatibilityEvaluationError("INVALID_CONTEXT", "canonical context is required")
        observed = {item.artifact: item for item in context.artifacts}
        assessments: list[ArtifactCompatibilityAssessment] = []
        for contract in ARTIFACT_CONTRACTS:
            observation = observed.get(contract.artifact)
            if observation is None:
                observation = ArtifactVersionObservation(
                    contract.artifact, context.project_id, context.repository_root,
                    context.repository_head, context.configuration_fingerprint,
                    contract.relative_path, False, None, None, None, None,
                )
            self._validate_observation_binding(context, observation)
            assessments.append(self._assess_artifact(contract, observation))
        product_classification = _classify_version(context.installed_product.version, PRODUCT_VERSION, semver=True)
        classifications = [item.classification for item in assessments]
        classifications.append(product_classification)
        global_compatibility = _global_classification(classifications)
        migrations = tuple(
            f"{item.artifact.value}:{item.required_migration[0]}->{item.required_migration[1]}"
            for item in assessments if item.required_migration is not None
        )
        blockers = tuple(
            f"{item.artifact.value}:{item.classification.value}"
            for item in assessments
            if item.classification in {
                CompatibilityClassification.UNSUPPORTED,
                CompatibilityClassification.FUTURE_VERSION,
                CompatibilityClassification.CORRUPT,
                CompatibilityClassification.UNKNOWN,
            }
        ) + (() if product_classification is CompatibilityClassification.CURRENT else (f"INSTALLED_PRODUCT:{product_classification.value}",))
        diagnostics = tuple(f"{item.artifact.value}:{item.diagnostic}" for item in assessments)
        payload = _assessment_payload(context, product_classification, assessments, global_compatibility, migrations, blockers, diagnostics)
        result = CompatibilityAssessment(
            context.installed_product.version, context.installed_product.release_digest,
            product_classification, context.project_id,
            str(Path(context.repository_root).resolve(strict=True)), context.repository_head,
            context.configuration_fingerprint, tuple(assessments), global_compatibility,
            migrations, blockers, diagnostics, _sha256_json(payload),
        )
        object.__setattr__(result, "_attestation", _sign(result))
        return result

    def verify_current(self, assessment: CompatibilityAssessment, context: CompatibilityEvaluationContext) -> CompatibilityAssessment:
        if not isinstance(assessment, CompatibilityAssessment) or not assessment.authentically_evaluated:
            raise CompatibilityEvaluationError("FORGED_ASSESSMENT", "assessment is not evaluator-authentic")
        current = self.evaluate(context)
        if assessment.fingerprint != current.fingerprint:
            raise CompatibilityEvaluationError("STALE_OR_FOREIGN_ASSESSMENT", "compatibility inputs changed")
        return current

    def _validate_observation_binding(self, context: CompatibilityEvaluationContext, observation: ArtifactVersionObservation) -> None:
        if (
            observation.project_id != context.project_id
            or _path_key(observation.repository_root) != _path_key(context.repository_root)
            or observation.repository_head != context.repository_head
            or observation.configuration_fingerprint != context.configuration_fingerprint
        ):
            raise CompatibilityEvaluationError("FOREIGN_ARTIFACT", f"{observation.artifact.value} has a foreign or stale binding")

    def _assess_artifact(self, contract: CompatibilityArtifactContract, observation: ArtifactVersionObservation) -> ArtifactCompatibilityAssessment:
        if observation.relative_path != contract.relative_path:
            raise CompatibilityEvaluationError(
                "ARTIFACT_PATH_MISMATCH",
                f"{observation.artifact.value} is not bound to its canonical path",
            )
        if not observation.present:
            classification = (
                CompatibilityClassification.CORRUPT
                if contract.requirement is ArtifactRequirement.REQUIRED
                else CompatibilityClassification.NOT_PRESENT_LAZY
            )
            diagnostic = "required artifact is missing" if contract.requirement is ArtifactRequirement.REQUIRED else "lazy artifact is legitimately absent"
            return ArtifactCompatibilityAssessment(contract.artifact, observation.relative_path, None, contract.current_version, classification, None, diagnostic, None)
        if observation.structurally_valid is False or observation.model_schema_aligned is False:
            return ArtifactCompatibilityAssessment(contract.artifact, observation.relative_path, observation.version, contract.current_version, CompatibilityClassification.CORRUPT, None, "structure or model/schema relationship is invalid", observation.content_fingerprint)
        if observation.structurally_valid is None or observation.model_schema_aligned is None:
            return ArtifactCompatibilityAssessment(contract.artifact, observation.relative_path, observation.version, contract.current_version, CompatibilityClassification.UNKNOWN, None, "required validation is unknown", observation.content_fingerprint)
        classification = _classify_version(observation.version, contract.current_version)
        migration: tuple[str, str] | None = None
        if observation.version in contract.backward_compatible_versions:
            classification = CompatibilityClassification.BACKWARD_COMPATIBLE
        elif classification is CompatibilityClassification.UNSUPPORTED and contract.migration_artifact is not None:
            edge = self._registry.definition(contract.migration_artifact, observation.version, contract.current_version)
            if edge is not None:
                classification = CompatibilityClassification.MIGRATION_REQUIRED
                migration = (observation.version, contract.current_version)
        diagnostic = {
            CompatibilityClassification.CURRENT: "version and validation match current contract",
            CompatibilityClassification.BACKWARD_COMPATIBLE: "explicitly accepted historical version",
            CompatibilityClassification.MIGRATION_REQUIRED: "explicit registered migration edge exists",
            CompatibilityClassification.UNSUPPORTED: "historical version has no registered edge",
            CompatibilityClassification.FUTURE_VERSION: "version is newer than the installed contract",
            CompatibilityClassification.UNKNOWN: "version syntax is unknown",
        }[classification]
        return ArtifactCompatibilityAssessment(contract.artifact, observation.relative_path, observation.version, contract.current_version, classification, migration, diagnostic, observation.content_fingerprint)


def _classify_version(source: str, target: str, *, semver: bool = False) -> CompatibilityClassification:
    if source == target:
        return CompatibilityClassification.CURRENT
    pattern = _SEMVER if semver else _FORMAT_VERSION
    source_match = pattern.fullmatch(source)
    target_match = pattern.fullmatch(target)
    if source_match is None or target_match is None:
        return CompatibilityClassification.UNKNOWN
    width = 3 if semver else 2
    source_numeric = tuple(int(source_match.group(index) or 0) for index in range(1, width + 1))
    target_numeric = tuple(int(target_match.group(index) or 0) for index in range(1, width + 1))
    if source_numeric > target_numeric:
        return CompatibilityClassification.FUTURE_VERSION
    if source_numeric < target_numeric:
        return CompatibilityClassification.UNSUPPORTED
    return CompatibilityClassification.UNKNOWN


def _global_classification(values: list[CompatibilityClassification]) -> CompatibilityClassification:
    relevant = [value for value in values if value is not CompatibilityClassification.NOT_PRESENT_LAZY]
    for classification in (
        CompatibilityClassification.CORRUPT,
        CompatibilityClassification.FUTURE_VERSION,
        CompatibilityClassification.UNSUPPORTED,
        CompatibilityClassification.UNKNOWN,
        CompatibilityClassification.MIGRATION_REQUIRED,
        CompatibilityClassification.BACKWARD_COMPATIBLE,
    ):
        if classification in relevant:
            return classification
    return CompatibilityClassification.CURRENT


def _assessment_payload(context, product, artifacts, global_value, migrations, blockers, diagnostics):
    return {
        "product": {"version": context.installed_product.version, "digest": context.installed_product.release_digest, "classification": product.value},
        "project_id": context.project_id,
        "repository_root": str(Path(context.repository_root).resolve(strict=True)),
        "repository_head": context.repository_head,
        "configuration_fingerprint": context.configuration_fingerprint,
        "artifacts": [
            {"artifact": item.artifact.value, "path": item.relative_path, "source": item.source_version, "target": item.target_version, "classification": item.classification.value, "migration": item.required_migration, "diagnostic": item.diagnostic, "fingerprint": item.content_fingerprint}
            for item in artifacts
        ],
        "global": global_value.value,
        "migrations": migrations,
        "blockers": blockers,
        "diagnostics": diagnostics,
    }


def _sign(value: CompatibilityAssessment) -> str:
    payload = {
        "product_version": value.product_version, "product_digest": value.product_digest,
        "product_classification": value.product_classification.value,
        "project_id": value.project_id, "repository_root": value.repository_root,
        "repository_head": value.repository_head, "configuration_fingerprint": value.configuration_fingerprint,
        "artifacts": [repr(item) for item in value.artifacts], "global": value.global_compatibility.value,
        "migrations": value.required_explicit_migrations, "blockers": value.blockers,
        "diagnostics": value.diagnostics, "fingerprint": value.fingerprint,
    }
    return hmac.new(_ATTESTATION_KEY, json.dumps(payload, sort_keys=True).encode(), hashlib.sha256).hexdigest()


def _sha256_json(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _validate_binding(project_id: str, repository_root: str, head: str, config_fingerprint: str) -> None:
    if not isinstance(project_id, str) or _IDENTITY.fullmatch(project_id) is None:
        raise ValueError("project_id is invalid")
    path = Path(repository_root)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ValueError("repository root cannot be resolved") from error
    if not path.is_absolute() or not resolved.is_dir():
        raise ValueError("repository root must be an existing absolute directory")
    if not isinstance(head, str) or _SHA40.fullmatch(head) is None:
        raise ValueError("repository_head must be lowercase SHA-1")
    if not isinstance(config_fingerprint, str) or _SHA256.fullmatch(config_fingerprint) is None:
        raise ValueError("configuration fingerprint must be SHA-256")


def _validate_relative_path(value: str) -> None:
    if not isinstance(value, str) or not value or "\\" in value or len(value) > 512:
        raise ValueError("artifact path must be bounded repository-relative POSIX text")
    path = PurePosixPath(value.split(":", 1)[0])
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("artifact path escapes the repository")


def _path_key(value: str) -> str:
    return os.path.normcase(str(Path(value).resolve(strict=True))).casefold()
