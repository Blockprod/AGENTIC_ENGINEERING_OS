"""Closed Codex capability facts used only for runtime admission."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class CodexCapability(str, Enum):
    NON_INTERACTIVE_EXEC = "NON_INTERACTIVE_EXEC"
    STDIN_PROMPT = "STDIN_PROMPT"
    EXPLICIT_CWD = "EXPLICIT_CWD"
    JSONL = "JSONL"
    OUTPUT_SCHEMA = "OUTPUT_SCHEMA"
    SANDBOX_READ_ONLY = "SANDBOX_READ_ONLY"
    SANDBOX_WORKSPACE_WRITE = "SANDBOX_WORKSPACE_WRITE"
    APPROVAL_NEVER = "APPROVAL_NEVER"
    EXIT_STDOUT_STDERR_OBSERVATION = "EXIT_STDOUT_STDERR_OBSERVATION"
    TIMEOUT_PARENT_CONTROL = "TIMEOUT_PARENT_CONTROL"
    CANCELLATION_PARENT_CONTROL = "CANCELLATION_PARENT_CONTROL"
    SESSION_THREAD_IDENTITY = "SESSION_THREAD_IDENTITY"
    RESUME_INTERFACE_PRESENT = "RESUME_INTERFACE_PRESENT"
    RELIABLE_SIDE_EFFECT_RECOVERY = "RELIABLE_SIDE_EFFECT_RECOVERY"
    ENVIRONMENT_CONTROL = "ENVIRONMENT_CONTROL"
    INDEPENDENT_PROCESS_PARALLELISM = "INDEPENDENT_PROCESS_PARALLELISM"


class CodexCapabilityStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


class CodexDiscoveryProvenance(str, Enum):
    EXPLICIT_PATH_STATIC_HELP = "EXPLICIT_PATH_STATIC_HELP"
    PATH_LOOKUP_STATIC_HELP = "PATH_LOOKUP_STATIC_HELP"
    TEST_INJECTION_STATIC_HELP = "TEST_INJECTION_STATIC_HELP"


@dataclass(frozen=True, slots=True)
class CodexCapabilityFinding:
    capability: CodexCapability
    status: CodexCapabilityStatus
    detail: str


_ATTESTATION_KEY = secrets.token_bytes(32)


@dataclass(frozen=True, slots=True)
class CodexCapabilityAssessment:
    executable_path: str
    executable_sha256: str
    executable_version: str
    discovery_provenance: CodexDiscoveryProvenance
    platform: str
    observed_at: datetime
    findings: tuple[CodexCapabilityFinding, ...]
    tested_parallelism: int | None = None
    _attestation: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        if not Path(self.executable_path).is_absolute():
            raise ValueError("executable_path must be absolute")
        if len(self.executable_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.executable_sha256
        ):
            raise ValueError("executable_sha256 must be lowercase SHA-256")
        if not self.executable_version.strip() or self.observed_at.tzinfo is None:
            raise ValueError("version and aware observation timestamp are required")
        if tuple(item.capability for item in self.findings) != tuple(CodexCapability):
            raise ValueError("findings must cover the closed capability set in canonical order")
        if self.tested_parallelism is not None and self.tested_parallelism < 1:
            raise ValueError("tested_parallelism must be positive")

    def status(self, capability: CodexCapability) -> CodexCapabilityStatus:
        if not isinstance(capability, CodexCapability):
            raise TypeError("capability must use the closed CodexCapability set")
        return next(item.status for item in self.findings if item.capability is capability)

    @property
    def authentically_discovered(self) -> bool:
        return bool(self._attestation) and hmac.compare_digest(
            self._attestation, _sign(self)
        )


def create_discovered_assessment(
    *,
    executable_path: str,
    executable_sha256: str,
    executable_version: str,
    discovery_provenance: CodexDiscoveryProvenance,
    platform: str,
    findings: tuple[CodexCapabilityFinding, ...],
    observed_at: datetime | None = None,
    tested_parallelism: int | None = None,
) -> CodexCapabilityAssessment:
    """Infrastructure factory; the attestation prevents plain forged objects."""

    assessment = CodexCapabilityAssessment(
        executable_path=executable_path,
        executable_sha256=executable_sha256,
        executable_version=executable_version,
        discovery_provenance=discovery_provenance,
        platform=platform,
        observed_at=observed_at or datetime.now(timezone.utc),
        findings=findings,
        tested_parallelism=tested_parallelism,
    )
    object.__setattr__(assessment, "_attestation", _sign(assessment))
    return assessment


def record_parallel_probe(
    assessment: CodexCapabilityAssessment,
    *,
    status: CodexCapabilityStatus,
    tested_concurrency: int | None,
    detail: str,
) -> CodexCapabilityAssessment:
    """Attach one bounded active-probe fact to an authentic fresh assessment."""

    if not assessment.authentically_discovered:
        raise ValueError("a forged capability assessment cannot be extended")
    if status is CodexCapabilityStatus.SUPPORTED and (
        tested_concurrency is None or tested_concurrency < 2
    ):
        raise ValueError("supported parallelism requires tested concurrency >= 2")
    if status is not CodexCapabilityStatus.SUPPORTED and tested_concurrency is not None:
        raise ValueError("unproven parallelism cannot claim tested concurrency")
    findings = tuple(
        CodexCapabilityFinding(item.capability, status, detail)
        if item.capability is CodexCapability.INDEPENDENT_PROCESS_PARALLELISM
        else item
        for item in assessment.findings
    )
    return create_discovered_assessment(
        executable_path=assessment.executable_path,
        executable_sha256=assessment.executable_sha256,
        executable_version=assessment.executable_version,
        discovery_provenance=assessment.discovery_provenance,
        platform=assessment.platform,
        observed_at=assessment.observed_at,
        findings=findings,
        tested_parallelism=tested_concurrency,
    )


def record_session_identity_probe(
    assessment: CodexCapabilityAssessment, *, supported: bool, detail: str
) -> CodexCapabilityAssessment:
    """Record only the thread/session identity fact seen in a safe active probe."""

    if not assessment.authentically_discovered:
        raise ValueError("a forged capability assessment cannot be extended")
    status = (
        CodexCapabilityStatus.SUPPORTED
        if supported
        else CodexCapabilityStatus.UNKNOWN
    )
    findings = tuple(
        CodexCapabilityFinding(item.capability, status, detail)
        if item.capability is CodexCapability.SESSION_THREAD_IDENTITY
        else item
        for item in assessment.findings
    )
    return create_discovered_assessment(
        executable_path=assessment.executable_path,
        executable_sha256=assessment.executable_sha256,
        executable_version=assessment.executable_version,
        discovery_provenance=assessment.discovery_provenance,
        platform=assessment.platform,
        observed_at=assessment.observed_at,
        findings=findings,
        tested_parallelism=assessment.tested_parallelism,
    )


def _sign(assessment: CodexCapabilityAssessment) -> str:
    payload = json.dumps(
        {
            "path": assessment.executable_path,
            "sha256": assessment.executable_sha256,
            "version": assessment.executable_version,
            "provenance": assessment.discovery_provenance.value,
            "platform": assessment.platform,
            "observed_at": assessment.observed_at.astimezone(timezone.utc).isoformat(),
            "findings": [
                (item.capability.value, item.status.value, item.detail)
                for item in assessment.findings
            ],
            "tested_parallelism": assessment.tested_parallelism,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_ATTESTATION_KEY, payload, hashlib.sha256).hexdigest()


CODEX_V1_ALWAYS_REQUIRED = frozenset(
    {
        CodexCapability.NON_INTERACTIVE_EXEC,
        CodexCapability.STDIN_PROMPT,
        CodexCapability.EXPLICIT_CWD,
        CodexCapability.JSONL,
        CodexCapability.APPROVAL_NEVER,
        CodexCapability.EXIT_STDOUT_STDERR_OBSERVATION,
        CodexCapability.TIMEOUT_PARENT_CONTROL,
        CodexCapability.CANCELLATION_PARENT_CONTROL,
        CodexCapability.ENVIRONMENT_CONTROL,
    }
)
