from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.resource_inventory import build_inventory
from scripts.validate_release_certification import (
    CertificationDossierError,
    REQUIRED_EVIDENCE,
    validate_dossier,
)


def _dossier(wheel_digest: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "release": "v1.0.0",
        "verdict": "CERTIFIED",
        "candidate": {
            "package_version": "1.0.0",
            "source_date_epoch": 1_788_451_200,
            "wheel_sha256": wheel_digest,
        },
        "environment": {
            "os": "Windows 11",
            "architecture": "x64",
            "python": "CPython 3.11",
            "git": "2.55",
            "codex": "codex-cli/example",
        },
        "evidence": [
            {"id": evidence_id, "result": "PASS", "reference": f"evidence/{evidence_id}.json"}
            for evidence_id in sorted(REQUIRED_EVIDENCE)
        ],
    }


def test_certification_dossier_is_closed_and_binds_exact_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(b"immutable wheel")
    path = tmp_path / "v1.0.0.json"
    path.write_text(
        json.dumps(_dossier(hashlib.sha256(wheel.read_bytes()).hexdigest())),
        encoding="utf-8",
    )

    validated = validate_dossier(path, expected_version="1.0.0", wheel_path=wheel)

    assert validated["verdict"] == "CERTIFIED"


@pytest.mark.parametrize("corruption", ("missing", "negative", "digest", "extra"))
def test_certification_dossier_refuses_incomplete_or_divergent_input(
    tmp_path: Path, corruption: str
) -> None:
    wheel = tmp_path / "candidate.whl"
    wheel.write_bytes(b"immutable wheel")
    payload = _dossier(hashlib.sha256(wheel.read_bytes()).hexdigest())
    if corruption == "missing":
        payload["evidence"] = list(payload["evidence"])[1:]
    elif corruption == "negative":
        list(payload["evidence"])[0]["result"] = "FAIL"
    elif corruption == "digest":
        payload["candidate"]["wheel_sha256"] = "0" * 64
    else:
        payload["unexpected"] = True
    path = tmp_path / "v1.0.0.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CertificationDossierError):
        validate_dossier(path, expected_version="1.0.0", wheel_path=wheel)


def test_resource_inventory_is_sorted_complete_and_content_bound() -> None:
    inventory = build_inventory()
    resources = inventory["resources"]
    paths = [record["path"] for record in resources]

    assert paths == sorted(paths, key=str.casefold)
    assert "schemas/project-state.schema.json" in paths
    assert "roles/architect.md" in paths
    assert "docs/78-test-release-strategy.md" in paths
    assert all(len(record["sha256"]) == 64 and record["size"] >= 0 for record in resources)
