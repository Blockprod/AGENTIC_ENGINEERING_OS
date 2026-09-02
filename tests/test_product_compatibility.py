from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from agentic_engineering_os import __version__
from agentic_engineering_os.application import (
    ARTIFACT_CONTRACTS,
    PRODUCT_VERSION,
    ArtifactRequirement,
    ArtifactVersionObservation,
    CompatibilityArtifact,
    CompatibilityAssessment,
    CompatibilityClassification,
    CompatibilityEvaluationContext,
    CompatibilityEvaluationError,
    CompatibilityEvaluator,
    InstalledProduct,
)
from agentic_engineering_os.domain import MigrationArtifact
from agentic_engineering_os.infrastructure import RepositoryMigrationRegistry


HEAD = "a" * 40
CONFIG = "b" * 64
DIGEST = "c" * 64


def observation(root: Path, artifact: CompatibilityArtifact, *, version: str | None = None, present: bool = True, valid: bool | None = True, aligned: bool | None = True, project_id: str = "demo", fingerprint: str = DIGEST) -> ArtifactVersionObservation:
    contract = next(item for item in ARTIFACT_CONTRACTS if item.artifact is artifact)
    return ArtifactVersionObservation(
        artifact, project_id, str(root.resolve()), HEAD, CONFIG,
        contract.relative_path, present,
        version or contract.current_version if present else None,
        fingerprint if present else None,
        valid if present else None, aligned if present else None,
    )


def context(root: Path, *, observations=None, product: str = PRODUCT_VERSION, project_id: str = "demo") -> CompatibilityEvaluationContext:
    values = observations if observations is not None else [
        observation(root, item.artifact, project_id=project_id)
        for item in ARTIFACT_CONTRACTS
        if item.requirement is ArtifactRequirement.REQUIRED
    ]
    return CompatibilityEvaluationContext(
        InstalledProduct(product, "d" * 64), project_id, str(root.resolve()),
        HEAD, CONFIG, tuple(sorted(values, key=lambda item: item.artifact.value)),
    )


def assessment_for(result, artifact):
    return next(item for item in result.artifacts if item.artifact is artifact)


def test_package_version_is_separate_and_all_current_is_current(tmp_path: Path) -> None:
    result = CompatibilityEvaluator().evaluate(context(tmp_path))
    assert PRODUCT_VERSION == __version__ == "0.1.0"
    assert result.product_classification is CompatibilityClassification.CURRENT
    assert result.global_compatibility is CompatibilityClassification.CURRENT
    assert not result.required_explicit_migrations
    assert all(
        item.classification in {
            CompatibilityClassification.CURRENT,
            CompatibilityClassification.NOT_PRESENT_LAZY,
        }
        for item in result.artifacts
    )
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert pyproject["project"]["version"] == PRODUCT_VERSION


def test_supported_known_supported_history_requires_registered_migration(tmp_path: Path) -> None:
    values = list(context(tmp_path).artifacts)
    index = next(i for i, item in enumerate(values) if item.artifact is CompatibilityArtifact.AGENTS_MANAGED_SECTION)
    values[index] = observation(tmp_path, CompatibilityArtifact.AGENTS_MANAGED_SECTION, version="1")
    result = CompatibilityEvaluator().evaluate(context(tmp_path, observations=values))
    item = assessment_for(result, CompatibilityArtifact.AGENTS_MANAGED_SECTION)
    assert item.classification is CompatibilityClassification.MIGRATION_REQUIRED
    assert item.required_migration == ("1", "2")
    assert result.global_compatibility is CompatibilityClassification.MIGRATION_REQUIRED


def test_negative_outcome_history_is_the_only_other_supported_edge(tmp_path: Path) -> None:
    values = list(context(tmp_path).artifacts)
    values.append(observation(tmp_path, CompatibilityArtifact.NEGATIVE_OUTCOME_LEDGER, version="1.0"))
    result = CompatibilityEvaluator().evaluate(context(tmp_path, observations=values))
    assert assessment_for(result, CompatibilityArtifact.NEGATIVE_OUTCOME_LEDGER).classification is CompatibilityClassification.MIGRATION_REQUIRED
    registry = RepositoryMigrationRegistry()
    assert registry.supported_edges == (
        (MigrationArtifact.AGENTS_MANAGED_SECTION, "1", "2"),
        (MigrationArtifact.NEGATIVE_OUTCOME_LEDGER, "1.0", "2.0"),
    )


def test_execution_ledger_1_0_is_intentionally_unsupported(tmp_path: Path) -> None:
    values = list(context(tmp_path).artifacts)
    values.append(observation(tmp_path, CompatibilityArtifact.EXECUTION_LEDGER, version="1.0"))
    result = CompatibilityEvaluator().evaluate(context(tmp_path, observations=values))
    item = assessment_for(result, CompatibilityArtifact.EXECUTION_LEDGER)
    assert item.classification is CompatibilityClassification.UNSUPPORTED
    assert item.required_migration is None
    assert result.global_compatibility is CompatibilityClassification.UNSUPPORTED


@pytest.mark.parametrize(
    ("version", "expected"),
    (("99.0", CompatibilityClassification.FUTURE_VERSION), ("legacy", CompatibilityClassification.UNKNOWN)),
)
def test_future_and_unknown_artifact_versions_fail_closed(tmp_path: Path, version, expected) -> None:
    values = list(context(tmp_path).artifacts)
    values.append(observation(tmp_path, CompatibilityArtifact.MAINTENANCE_STATE, version=version))
    result = CompatibilityEvaluator().evaluate(context(tmp_path, observations=values))
    assert assessment_for(result, CompatibilityArtifact.MAINTENANCE_STATE).classification is expected
    assert result.global_compatibility is expected


def test_corrupt_and_schema_model_mismatch_fail_closed(tmp_path: Path) -> None:
    for kwargs in ({"valid": False}, {"aligned": False}, {"valid": None}):
        values = list(context(tmp_path).artifacts)
        index = next(i for i, item in enumerate(values) if item.artifact is CompatibilityArtifact.PROJECT_STATE)
        values[index] = observation(tmp_path, CompatibilityArtifact.PROJECT_STATE, **kwargs)
        result = CompatibilityEvaluator().evaluate(context(tmp_path, observations=values))
        expected = CompatibilityClassification.UNKNOWN if kwargs.get("valid", True) is None else CompatibilityClassification.CORRUPT
        assert result.global_compatibility is expected


def test_mixed_current_and_migration_required_never_becomes_current(tmp_path: Path) -> None:
    values = list(context(tmp_path).artifacts)
    values.append(observation(tmp_path, CompatibilityArtifact.NEGATIVE_OUTCOME_LEDGER, version="1.0"))
    result = CompatibilityEvaluator().evaluate(context(tmp_path, observations=values))
    assert result.global_compatibility is CompatibilityClassification.MIGRATION_REQUIRED
    assert result.required_explicit_migrations == ("NEGATIVE_OUTCOME_LEDGER:1.0->2.0",)


def test_missing_lazy_is_absent_but_missing_required_is_corrupt(tmp_path: Path) -> None:
    current = CompatibilityEvaluator().evaluate(context(tmp_path))
    assert assessment_for(current, CompatibilityArtifact.EXECUTION_LEDGER).classification is CompatibilityClassification.NOT_PRESENT_LAZY
    values = [item for item in context(tmp_path).artifacts if item.artifact is not CompatibilityArtifact.PROJECT_STATE]
    missing = CompatibilityEvaluator().evaluate(context(tmp_path, observations=values))
    assert assessment_for(missing, CompatibilityArtifact.PROJECT_STATE).classification is CompatibilityClassification.CORRUPT
    assert missing.global_compatibility is CompatibilityClassification.CORRUPT


def test_newer_package_does_not_change_or_migrate_project_artifacts(tmp_path: Path) -> None:
    current_context = context(tmp_path, product="0.2.0")
    before = repr(current_context.artifacts)
    result = CompatibilityEvaluator().evaluate(current_context)
    assert result.product_classification is CompatibilityClassification.FUTURE_VERSION
    assert result.global_compatibility is CompatibilityClassification.FUTURE_VERSION
    assert not result.required_explicit_migrations
    assert repr(current_context.artifacts) == before


def test_stale_artifact_and_foreign_project_assessments_are_refused(tmp_path: Path) -> None:
    evaluator = CompatibilityEvaluator()
    original = context(tmp_path)
    result = evaluator.evaluate(original)
    changed_values = list(original.artifacts)
    changed_values[0] = replace(changed_values[0], content_fingerprint="e" * 64)
    with pytest.raises(CompatibilityEvaluationError, match="STALE_OR_FOREIGN_ASSESSMENT"):
        evaluator.verify_current(result, context(tmp_path, observations=changed_values))
    foreign_values = [replace(item, project_id="foreign") for item in original.artifacts]
    with pytest.raises(CompatibilityEvaluationError, match="STALE_OR_FOREIGN_ASSESSMENT"):
        evaluator.verify_current(result, context(tmp_path, observations=foreign_values, project_id="foreign"))


def test_foreign_artifact_and_wrong_path_fail_before_assessment(tmp_path: Path) -> None:
    values = list(context(tmp_path).artifacts)
    values[0] = replace(values[0], project_id="foreign")
    with pytest.raises(CompatibilityEvaluationError, match="FOREIGN_ARTIFACT"):
        CompatibilityEvaluator().evaluate(context(tmp_path, observations=values))
    values = list(context(tmp_path).artifacts)
    values[0] = replace(values[0], relative_path="other.json")
    with pytest.raises(CompatibilityEvaluationError, match="ARTIFACT_PATH_MISMATCH"):
        CompatibilityEvaluator().evaluate(context(tmp_path, observations=values))


def test_forged_assessment_has_no_migration_authority(tmp_path: Path) -> None:
    evaluator = CompatibilityEvaluator()
    current = context(tmp_path)
    result = evaluator.evaluate(current)
    forged = replace(result, global_compatibility=CompatibilityClassification.CURRENT, fingerprint="0" * 64)
    assert not forged.authentically_evaluated
    with pytest.raises(CompatibilityEvaluationError, match="FORGED_ASSESSMENT"):
        evaluator.verify_current(forged, current)
    for forbidden in ("migrate", "rewrite", "certify", "unfreeze", "save"):
        assert not hasattr(evaluator, forbidden)


def test_no_generic_or_inferred_migration_exists(tmp_path: Path) -> None:
    values = list(context(tmp_path).artifacts)
    index = next(i for i, item in enumerate(values) if item.artifact is CompatibilityArtifact.PROJECT_CONFIGURATION)
    values[index] = observation(tmp_path, CompatibilityArtifact.PROJECT_CONFIGURATION, version="0.9")
    result = CompatibilityEvaluator().evaluate(context(tmp_path, observations=values))
    item = assessment_for(result, CompatibilityArtifact.PROJECT_CONFIGURATION)
    assert item.classification is CompatibilityClassification.UNSUPPORTED
    assert item.required_migration is None


def test_current_persisted_schemas_are_closed_and_version_aligned() -> None:
    root = Path(__file__).parents[1] / "src/agentic_engineering_os/resources/schemas"
    expected = {
        "project-configuration.schema.json": ("config_version", "1.0"),
        "project-state.schema.json": ("schema_version", "1.0"),
        "mission-state.schema.json": ("schema_version", "1.0"),
        "worktree-registry.schema.json": ("schema_version", "1.0"),
        "operational-event.schema.json": ("schema_version", "1.0"),
    }
    for filename, (field, version) in expected.items():
        schema = json.loads((root / filename).read_text(encoding="utf-8"))
        assert schema["additionalProperties"] is False
        assert schema["properties"][field]["const"] == version


def test_taxonomy_is_closed_and_resolution_is_deterministic(tmp_path: Path) -> None:
    assert tuple(item.value for item in CompatibilityClassification) == (
        "CURRENT", "BACKWARD_COMPATIBLE", "MIGRATION_REQUIRED", "UNSUPPORTED",
        "FUTURE_VERSION", "CORRUPT", "UNKNOWN", "NOT_PRESENT_LAZY",
    )
    current = context(tmp_path)
    first = CompatibilityEvaluator().evaluate(current)
    second = CompatibilityEvaluator().evaluate(current)
    assert first.fingerprint == second.fingerprint
    assert first.authentically_evaluated and second.authentically_evaluated
