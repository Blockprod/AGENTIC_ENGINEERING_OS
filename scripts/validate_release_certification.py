"""Fail-closed validation for the external v1 release certification dossier."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


REQUIRED_EVIDENCE = frozenset(
    {
        "adversarial_audit",
        "clean_room",
        "license_review",
        "real_codex_sequential",
        "soak",
        "windows_ci",
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ROOT_KEYS = frozenset(
    {"schema_version", "release", "verdict", "candidate", "environment", "evidence"}
)


class CertificationDossierError(ValueError):
    """The dossier cannot authorize a production release."""


def validate_dossier(
    path: Path,
    *,
    expected_version: str,
    wheel_path: Path | None = None,
) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CertificationDossierError("dossier is absent, unreadable, or invalid JSON") from error
    if not isinstance(payload, dict) or frozenset(payload) != _ROOT_KEYS:
        raise CertificationDossierError("dossier root fields are not the closed v1 contract")
    if payload["schema_version"] != "1.0":
        raise CertificationDossierError("unsupported certification dossier version")
    if payload["release"] != f"v{expected_version}" or payload["verdict"] != "CERTIFIED":
        raise CertificationDossierError("release identity or verdict is not exact")

    candidate = _exact_object(
        payload["candidate"],
        {"package_version", "source_date_epoch", "wheel_sha256"},
        "candidate",
    )
    if candidate["package_version"] != expected_version:
        raise CertificationDossierError("candidate package version differs from the release")
    epoch = candidate["source_date_epoch"]
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
        raise CertificationDossierError("candidate source_date_epoch must be a positive integer")
    wheel_sha256 = candidate["wheel_sha256"]
    if not isinstance(wheel_sha256, str) or _SHA256.fullmatch(wheel_sha256) is None:
        raise CertificationDossierError("candidate wheel digest must be lowercase SHA-256")

    environment = _exact_object(
        payload["environment"], {"os", "architecture", "python", "git", "codex"}, "environment"
    )
    expected_environment = {
        "os": "Windows 11",
        "architecture": "x64",
        "python": "CPython 3.11",
        "git": "2.55",
    }
    for key, value in expected_environment.items():
        if environment[key] != value:
            raise CertificationDossierError(f"certified environment field {key} is not exact")
    if not _nonempty_text(environment["codex"]):
        raise CertificationDossierError("certified Codex identity is absent")

    evidence = payload["evidence"]
    if not isinstance(evidence, list):
        raise CertificationDossierError("evidence must be an array")
    observed: set[str] = set()
    for index, item in enumerate(evidence):
        record = _exact_object(item, {"id", "result", "reference"}, f"evidence[{index}]")
        evidence_id = record["id"]
        if not isinstance(evidence_id, str) or evidence_id not in REQUIRED_EVIDENCE:
            raise CertificationDossierError(f"evidence[{index}] has an unknown id")
        if evidence_id in observed:
            raise CertificationDossierError(f"duplicate evidence id: {evidence_id}")
        if record["result"] != "PASS" or not _nonempty_text(record["reference"]):
            raise CertificationDossierError(f"evidence {evidence_id} is not a referenced PASS")
        observed.add(evidence_id)
    if observed != REQUIRED_EVIDENCE:
        missing = ", ".join(sorted(REQUIRED_EVIDENCE - observed))
        raise CertificationDossierError(f"mandatory evidence is absent: {missing}")

    if wheel_path is not None:
        try:
            actual = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
        except OSError as error:
            raise CertificationDossierError("candidate wheel is absent or unreadable") from error
        if actual != wheel_sha256:
            raise CertificationDossierError("built wheel differs from the certified candidate digest")
    return payload


def _exact_object(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise CertificationDossierError(f"{label} fields are not exact")
    return value


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\0" not in value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dossier", type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--wheel", type=Path)
    arguments = parser.parse_args()
    try:
        validate_dossier(
            arguments.dossier,
            expected_version=arguments.expected_version,
            wheel_path=arguments.wheel,
        )
    except CertificationDossierError as error:
        parser.exit(2, f"release certification refused: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
