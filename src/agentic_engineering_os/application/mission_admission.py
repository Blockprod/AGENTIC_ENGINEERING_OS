"""Read-only, fail-closed admission for a user mission request."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Callable, Protocol

from agentic_engineering_os.domain import (
    AgenticOsInitializationState,
    CodexApprovalConstraint,
    CodexSandboxConstraint,
    MaintenanceAdmission as MaintenanceStartAdmission,
    MaintenanceAdmissionDecision,
    MaintenanceOperation,
    MissionState,
    MissionStatus,
    ObservationClassification,
    ProjectConfiguration,
    ProjectState,
    RepositoryProfile,
    RepositorySupportStatus,
)
from agentic_engineering_os.infrastructure import (
    ExecutionStateStore,
    MissionStateStore,
    ProjectConfigurationLoader,
    ProjectStateStore,
    RepositoryReconnaissance,
)

from .codex_capabilities import (
    CODEX_V1_ALWAYS_REQUIRED,
    CodexCapability,
    CodexCapabilityAssessment,
    CodexCapabilityStatus,
    CodexOperationalCapabilityClass,
    CodexOperationalCapabilityProof,
)
from .configuration_resolver import project_configuration_fingerprint
from .execution_state import CodexExecutionLedger, CodexExecutionStatus


MISSION_CAPABILITY_MAX_AGE = timedelta(minutes=5)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECOVERY_EXECUTION_STATES = frozenset(
    {
        CodexExecutionStatus.RUNNING,
        CodexExecutionStatus.OBSERVED,
        CodexExecutionStatus.INTERRUPTED,
    }
)
_REQUIRED_OPERATIONAL_CAPABILITIES = (
    CodexOperationalCapabilityClass.REPOSITORY_READ,
    CodexOperationalCapabilityClass.WORKSPACE_EDIT,
    CodexOperationalCapabilityClass.COMMAND_EXECUTION,
    CodexOperationalCapabilityClass.GIT_OBSERVATION,
)


class MissionAdmissionStatus(str, Enum):
    ADMITTED = "ADMITTED"
    BLOCKED = "BLOCKED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"


@dataclass(frozen=True, slots=True)
class MissionRequest:
    """Minimal canonical user intent; it carries no control-plane authority."""

    objective: str
    repository_root: str
    requested_scope: tuple[str, ...] = ()
    verification_command_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        objective = _canonical_text(self.objective, "objective")
        if len(objective) > 10_000:
            raise ValueError("objective exceeds the bounded mission policy")
        root = Path(self.repository_root)
        if root.is_symlink():
            raise ValueError("repository_root cannot be a symlink")
        try:
            canonical_root = root.resolve(strict=True)
        except OSError as error:
            raise ValueError("repository_root cannot be resolved") from error
        if not canonical_root.is_dir() or canonical_root.is_symlink():
            raise ValueError("repository_root must be a real directory")
        scopes = tuple(_canonical_relative_path(item) for item in self.requested_scope)
        commands = tuple(_canonical_identifier(item) for item in self.verification_command_ids)
        if scopes != tuple(sorted(set(scopes), key=lambda item: (item.casefold(), item))):
            raise ValueError("requested_scope must be unique and canonically sorted")
        if commands != tuple(sorted(set(commands), key=lambda item: (item.casefold(), item))):
            raise ValueError("verification_command_ids must be unique and canonically sorted")
        object.__setattr__(self, "objective", objective)
        object.__setattr__(self, "repository_root", str(canonical_root))
        object.__setattr__(self, "requested_scope", scopes)
        object.__setattr__(self, "verification_command_ids", commands)


@dataclass(frozen=True, slots=True)
class MissionCapabilitySnapshot:
    """Exact static and operational facts supplied by the runtime boundary."""

    assessment: CodexCapabilityAssessment
    operational_proofs: tuple[CodexOperationalCapabilityProof, ...]
    environment_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.assessment, CodexCapabilityAssessment):
            raise TypeError("assessment must be a CodexCapabilityAssessment")
        if not isinstance(self.operational_proofs, tuple) or any(
            not isinstance(item, CodexOperationalCapabilityProof)
            for item in self.operational_proofs
        ):
            raise TypeError("operational_proofs must be an immutable proof tuple")
        if not _SHA256.fullmatch(self.environment_fingerprint):
            raise ValueError("environment_fingerprint must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class MissionAdmissionBlocker:
    code: str
    detail: str

    def __post_init__(self) -> None:
        if not _REQUEST_ID.fullmatch(self.code) or not self.detail.strip():
            raise ValueError("mission blocker must be explicit")


@dataclass(frozen=True, slots=True)
class MissionAdmission:
    """Non-authoritative projection of observed readiness facts."""

    status: MissionAdmissionStatus
    request_fingerprint: str
    observed_facts_fingerprint: str
    repository_head: str | None
    project_id: str | None
    blockers: tuple[MissionAdmissionBlocker, ...]
    missing_capabilities: tuple[CodexOperationalCapabilityClass, ...]
    next_action: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, MissionAdmissionStatus):
            raise TypeError("status must use MissionAdmissionStatus")
        if not _SHA256.fullmatch(self.request_fingerprint):
            raise ValueError("request_fingerprint must be lowercase SHA-256")
        if not _SHA256.fullmatch(self.observed_facts_fingerprint):
            raise ValueError("observed_facts_fingerprint must be lowercase SHA-256")
        if self.repository_head is not None and not _SHA40.fullmatch(self.repository_head):
            raise ValueError("repository_head must be lowercase SHA-1")
        if self.blockers != tuple(
            sorted(set(self.blockers), key=lambda item: (item.code, item.detail))
        ):
            raise ValueError("blockers must be unique and canonical")
        if self.missing_capabilities != tuple(
            sorted(set(self.missing_capabilities), key=lambda item: item.value)
        ):
            raise ValueError("missing_capabilities must be unique and canonical")
        if self.status is MissionAdmissionStatus.ADMITTED and (
            self.blockers or self.missing_capabilities
        ):
            raise ValueError("ADMITTED cannot retain blockers")
        if self.status is not MissionAdmissionStatus.ADMITTED and not self.blockers:
            raise ValueError("non-admitted results require blockers")
        if not self.next_action.strip():
            raise ValueError("next_action is required")


class MissionCapabilityProvider(Protocol):
    def inspect(
        self,
        request: MissionRequest,
        configuration: ProjectConfiguration,
    ) -> MissionCapabilitySnapshot: ...


class MissionMaintenanceProvider(Protocol):
    def evaluate_start_mission(
        self,
        *,
        repository_root: str,
        repository_head: str,
        project_state: ProjectState,
        mission_state: MissionState | None,
        evaluated_at: datetime,
    ) -> MaintenanceStartAdmission: ...


class _ReconnaissancePort(Protocol):
    def inspect(self, repository_root: str) -> RepositoryProfile: ...


class _LoadPort(Protocol):
    def load(self) -> object: ...


class MissionReadinessPrecheck:
    """Compose existing readers and stop before every mutation or role launch."""

    def __init__(
        self,
        *,
        capability_provider: MissionCapabilityProvider,
        maintenance_provider: MissionMaintenanceProvider,
        reconnaissance: _ReconnaissancePort | None = None,
        configuration_loader_factory: Callable[[str], _LoadPort] = ProjectConfigurationLoader,
        project_state_store_factory: Callable[[str], _LoadPort] = ProjectStateStore,
        mission_state_store_factory: Callable[[str], _LoadPort] = MissionStateStore,
        execution_state_store_factory: Callable[[str], _LoadPort] = ExecutionStateStore,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._capabilities = capability_provider
        self._maintenance = maintenance_provider
        self._reconnaissance = reconnaissance or RepositoryReconnaissance()
        self._configuration_loader_factory = configuration_loader_factory
        self._project_state_store_factory = project_state_store_factory
        self._mission_state_store_factory = mission_state_store_factory
        self._execution_state_store_factory = execution_state_store_factory
        self._now = now or (lambda: datetime.now(timezone.utc))

    def evaluate(self, request: MissionRequest) -> MissionAdmission:
        if not isinstance(request, MissionRequest):
            raise TypeError("request must be MissionRequest")
        blockers: list[MissionAdmissionBlocker] = []
        missing: set[CodexOperationalCapabilityClass] = set()
        facts: dict[str, object] = {"request": _request_data(request)}
        head: str | None = None
        project_id: str | None = None

        try:
            profile = self._reconnaissance.inspect(request.repository_root)
        except Exception as error:
            return _blocked(request, [_error_blocker("REPOSITORY_RECONNAISSANCE_FAILED", error)], facts)
        try:
            facts["repository"] = _profile_facts(profile)
            blockers.extend(_repository_blockers(profile, request.repository_root))
        except Exception as error:
            return _blocked(request, [_error_blocker("REPOSITORY_PROFILE_INVALID", error)], facts)
        if blockers:
            return _blocked(request, blockers, facts)

        try:
            configuration = self._configuration_loader_factory(request.repository_root).load()
        except Exception as error:
            return _blocked(request, [_error_blocker("PROJECT_CONFIGURATION_UNAVAILABLE", error)], facts)
        if not isinstance(configuration, ProjectConfiguration):
            return _blocked(request, [MissionAdmissionBlocker("PROJECT_CONFIGURATION_INVALID", "loader returned an invalid configuration")], facts)
        project_id = configuration.project_id
        configuration_fingerprint = project_configuration_fingerprint(configuration)
        facts["configuration"] = configuration_fingerprint
        if profile.agentic_os.config_semantic_fingerprint != configuration_fingerprint:
            blockers.append(MissionAdmissionBlocker("PROJECT_CONFIGURATION_DRIFT", "reconnaissance and loaded configuration fingerprints differ"))
        blockers.extend(_request_policy_blockers(request, configuration))
        head = profile.git.head_commit.value if isinstance(profile.git.head_commit.value, str) else None
        blockers.extend(_git_blockers(profile, configuration))
        if blockers:
            return _blocked(request, blockers, facts, head=head, project_id=project_id)

        try:
            project_state = self._project_state_store_factory(request.repository_root).load()
        except Exception as error:
            return _blocked(request, [_error_blocker("PROJECT_STATE_UNAVAILABLE", error)], facts, head=head, project_id=project_id)
        if not isinstance(project_state, ProjectState) or project_state.project_id != project_id:
            return _blocked(request, [MissionAdmissionBlocker("PROJECT_STATE_BINDING_MISMATCH", "ProjectState does not match ProjectConfiguration")], facts, head=head, project_id=project_id)
        facts["project_state"] = _project_state_facts(project_state)

        mission_state, mission_error = self._load_optional_mission(request.repository_root)
        if mission_error is not None:
            return _blocked(request, [mission_error], facts, head=head, project_id=project_id)
        if mission_state is not None:
            facts["mission_state"] = _mission_state_facts(mission_state)
            if mission_state.status not in {MissionStatus.COMPLETED, MissionStatus.CANCELLED}:
                blockers.append(MissionAdmissionBlocker("MISSION_NOT_TERMINAL", f"mission {mission_state.mission_id} is {mission_state.status.value}"))
            if mission_state.observed_commit != head:
                blockers.append(MissionAdmissionBlocker("STALE_MISSION_BASELINE", "persisted mission commit differs from current Git HEAD"))

        ledger, ledger_error = self._load_optional_ledger(request.repository_root)
        if ledger_error is not None:
            blockers.append(ledger_error)
        elif ledger is not None:
            facts["execution_ledger"] = _ledger_facts(ledger)
            if mission_state is not None and any(
                record.mission_id == mission_state.mission_id
                and record.status in _RECOVERY_EXECUTION_STATES
                for record in ledger.records
            ):
                blockers.append(MissionAdmissionBlocker("EXECUTION_RECOVERY_PENDING", "current mission has an incomplete execution record"))
        if blockers:
            return _blocked(request, blockers, facts, head=head, project_id=project_id)

        assert head is not None
        try:
            evaluated_at = self._utc_now()
        except Exception as error:
            return _blocked(request, [_error_blocker("PRECISION_CLOCK_UNAVAILABLE", error)], facts, head=head, project_id=project_id)
        try:
            maintenance = self._maintenance.evaluate_start_mission(
                repository_root=request.repository_root,
                repository_head=head,
                project_state=project_state,
                mission_state=mission_state,
                evaluated_at=evaluated_at,
            )
        except Exception as error:
            return _blocked(request, [_error_blocker("MAINTENANCE_ADMISSION_UNAVAILABLE", error)], facts, head=head, project_id=project_id)
        facts["maintenance"] = _maintenance_facts(maintenance)
        if not isinstance(maintenance, MaintenanceStartAdmission):
            blockers.append(MissionAdmissionBlocker("MAINTENANCE_ADMISSION_INVALID", "maintenance provider returned an invalid value"))
        elif maintenance.operation is not MaintenanceOperation.START_MISSION:
            blockers.append(MissionAdmissionBlocker("MAINTENANCE_ADMISSION_MISMATCH", "maintenance decision is not bound to START_MISSION"))
        elif maintenance.decision is MaintenanceAdmissionDecision.HUMAN_REQUIRED:
            blockers.append(MissionAdmissionBlocker("HUMAN_AUTHORITY_REQUIRED", ",".join(item.value for item in maintenance.reasons)))
        elif maintenance.decision is not MaintenanceAdmissionDecision.ADMITTED:
            blockers.append(MissionAdmissionBlocker("MAINTENANCE_REFUSED", ",".join(item.value for item in maintenance.reasons)))
        if blockers:
            human = all(item.code == "HUMAN_AUTHORITY_REQUIRED" for item in blockers)
            return _result(request, MissionAdmissionStatus.HUMAN_REQUIRED if human else MissionAdmissionStatus.BLOCKED, blockers, (), facts, head, project_id)

        try:
            snapshot = self._capabilities.inspect(request, configuration)
        except Exception as error:
            return _blocked(request, [_error_blocker("CODEX_CAPABILITY_ASSESSMENT_UNAVAILABLE", error)], facts, head=head, project_id=project_id)
        facts["capabilities"] = _capability_facts(snapshot)
        if not isinstance(snapshot, MissionCapabilitySnapshot):
            return _blocked(request, [MissionAdmissionBlocker("CODEX_CAPABILITY_ASSESSMENT_INVALID", "capability provider returned an invalid value")], facts, head=head, project_id=project_id)
        capability_blockers, missing = _capability_blockers(snapshot, configuration, evaluated_at)
        blockers.extend(capability_blockers)
        if blockers:
            return _result(request, MissionAdmissionStatus.BLOCKED, blockers, missing, facts, head, project_id)
        return _result(
            request,
            MissionAdmissionStatus.ADMITTED,
            (),
            (),
            facts,
            head,
            project_id,
        )

    def _load_optional_mission(self, root: str) -> tuple[MissionState | None, MissionAdmissionBlocker | None]:
        try:
            value = self._mission_state_store_factory(root).load()
        except Exception as error:
            if getattr(error, "code", None) == "MISSION_ABSENT":
                return None, None
            return None, _error_blocker("MISSION_STATE_UNAVAILABLE", error)
        if not isinstance(value, MissionState):
            return None, MissionAdmissionBlocker("MISSION_STATE_INVALID", "mission store returned an invalid value")
        return value, None

    def _load_optional_ledger(self, root: str) -> tuple[CodexExecutionLedger | None, MissionAdmissionBlocker | None]:
        try:
            value = self._execution_state_store_factory(root).load()
        except Exception as error:
            if getattr(error, "code", None) == "LEDGER_ABSENT":
                return None, None
            return None, _error_blocker("EXECUTION_LEDGER_UNAVAILABLE", error)
        if not isinstance(value, CodexExecutionLedger):
            return None, MissionAdmissionBlocker("EXECUTION_LEDGER_INVALID", "execution store returned an invalid value")
        return value, None

    def _utc_now(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("precheck clock must return an aware UTC datetime")
        return value


def _repository_blockers(profile: RepositoryProfile, root: str) -> list[MissionAdmissionBlocker]:
    blockers: list[MissionAdmissionBlocker] = []
    if profile.support_status is not RepositorySupportStatus.SUPPORTED or not profile.scan_complete:
        blockers.append(MissionAdmissionBlocker("REPOSITORY_NOT_SUPPORTED", "repository reconnaissance is blocked or incomplete"))
    if profile.agentic_os.state is not AgenticOsInitializationState.INITIALIZED:
        blockers.append(MissionAdmissionBlocker("REPOSITORY_NOT_ADOPTED", f"Agentic OS state is {profile.agentic_os.state.value}"))
    if profile.git.is_repository.classification is not ObservationClassification.FACT or profile.git.is_repository.value is not True:
        blockers.append(MissionAdmissionBlocker("GIT_REPOSITORY_UNPROVEN", "Git repository status is not a fact"))
    if profile.git.top_level.classification is not ObservationClassification.FACT or not isinstance(profile.git.top_level.value, str) or _path_key(profile.git.top_level.value) != _path_key(root):
        blockers.append(MissionAdmissionBlocker("GIT_ROOT_MISMATCH", "Git top-level does not match the requested repository"))
    return blockers


def _git_blockers(profile: RepositoryProfile, configuration: ProjectConfiguration) -> list[MissionAdmissionBlocker]:
    blockers: list[MissionAdmissionBlocker] = []
    head = profile.git.head_commit
    if head.classification is not ObservationClassification.FACT or not isinstance(head.value, str) or not _SHA40.fullmatch(head.value):
        blockers.append(MissionAdmissionBlocker("GIT_HEAD_UNKNOWN", "Git HEAD is not an exact lowercase SHA-1 fact"))
    if profile.git.detached.classification is not ObservationClassification.FACT or profile.git.detached.value is not False:
        blockers.append(MissionAdmissionBlocker("GIT_DETACHED_OR_UNKNOWN", "mission start requires an attached branch"))
    if configuration.codex_constraints.require_clean_git and (
        profile.git.clean.classification is not ObservationClassification.FACT
        or profile.git.clean.value is not True
    ):
        blockers.append(MissionAdmissionBlocker("GIT_NOT_CLEAN", "project policy requires a clean working tree"))
    return blockers


def _request_policy_blockers(request: MissionRequest, configuration: ProjectConfiguration) -> list[MissionAdmissionBlocker]:
    blockers: list[MissionAdmissionBlocker] = []
    allowed = configuration.path_policy.allowed_paths
    protected = configuration.path_policy.protected_paths
    forbidden = configuration.path_policy.forbidden_paths
    for path in request.requested_scope:
        if not any(_contains(item, path) for item in allowed):
            blockers.append(MissionAdmissionBlocker("REQUEST_SCOPE_NOT_ALLOWED", f"requested path is outside allowed policy: {path}"))
        if any(_overlaps(item, path) for item in (*protected, *forbidden)):
            blockers.append(MissionAdmissionBlocker("REQUEST_SCOPE_PROTECTED", f"requested path intersects protected policy: {path}"))
    known_commands = {item.command_id for item in configuration.verification_commands}
    for command_id in request.verification_command_ids:
        if command_id not in known_commands:
            blockers.append(MissionAdmissionBlocker("UNKNOWN_VERIFICATION_COMMAND", f"verification command is not configured: {command_id}"))
    return blockers


def _capability_blockers(
    snapshot: MissionCapabilitySnapshot,
    configuration: ProjectConfiguration,
    evaluated_at: datetime,
) -> tuple[list[MissionAdmissionBlocker], set[CodexOperationalCapabilityClass]]:
    blockers: list[MissionAdmissionBlocker] = []
    missing: set[CodexOperationalCapabilityClass] = set()
    assessment = snapshot.assessment
    if not assessment.authentically_discovered or not _fresh(assessment.observed_at, evaluated_at):
        blockers.append(MissionAdmissionBlocker("CODEX_CAPABILITY_ASSESSMENT_STALE_OR_FORGED", "static Codex capability assessment is not authentic and fresh"))
    required_static = set(CODEX_V1_ALWAYS_REQUIRED) | {
        CodexCapability.SANDBOX_WORKSPACE_WRITE,
        CodexCapability.OUTPUT_SCHEMA,
    }
    unsupported = tuple(
        sorted(
            (
                item.value
                for item in required_static
                if assessment.status(item) is not CodexCapabilityStatus.SUPPORTED
            )
        )
    )
    if unsupported:
        blockers.append(MissionAdmissionBlocker("CODEX_STATIC_CAPABILITY_UNAVAILABLE", ",".join(unsupported)))
    if configuration.codex_constraints.maximum_sandbox is not CodexSandboxConstraint.WORKSPACE_WRITE:
        blockers.append(MissionAdmissionBlocker("WORKSPACE_WRITE_FORBIDDEN_BY_PROJECT", "project sandbox ceiling forbids mutating missions"))
        missing.add(CodexOperationalCapabilityClass.WORKSPACE_EDIT)
    if configuration.codex_constraints.approval_policy is not CodexApprovalConstraint.NEVER:
        blockers.append(MissionAdmissionBlocker("CODEX_APPROVAL_POLICY_UNSUPPORTED", "mission runtime requires approval policy never"))

    for capability in _REQUIRED_OPERATIONAL_CAPABILITIES:
        matching = tuple(item for item in snapshot.operational_proofs if item.capability_class is capability)
        if len(matching) != 1 or not _proof_matches(matching[0], snapshot, evaluated_at):
            missing.add(capability)
            detail = matching[0].diagnostic_code if len(matching) == 1 and matching[0].authentically_attested else "MISSING_OR_INVALID_PROOF"
            blockers.append(MissionAdmissionBlocker("REQUIRED_CAPABILITY_UNAVAILABLE", f"{capability.value}:{detail}"))
    return blockers, missing


def _proof_matches(proof: CodexOperationalCapabilityProof, snapshot: MissionCapabilitySnapshot, now: datetime) -> bool:
    assessment = snapshot.assessment
    return (
        proof.authentically_proven
        and _path_key(proof.executable_path) == _path_key(assessment.executable_path)
        and proof.executable_sha256 == assessment.executable_sha256
        and proof.executable_version == assessment.executable_version
        and proof.sandbox == CodexSandboxConstraint.WORKSPACE_WRITE.value
        and proof.approval_policy == CodexApprovalConstraint.NEVER.value
        and proof.environment_fingerprint == snapshot.environment_fingerprint
        and _fresh(proof.observed_at, now)
    )


def _fresh(observed_at: datetime, now: datetime) -> bool:
    if observed_at.tzinfo is None or observed_at.utcoffset() != timedelta(0):
        return False
    age = now - observed_at
    return timedelta(0) <= age <= MISSION_CAPABILITY_MAX_AGE


def _result(
    request: MissionRequest,
    status: MissionAdmissionStatus,
    blockers: tuple[MissionAdmissionBlocker, ...] | list[MissionAdmissionBlocker],
    missing: tuple[CodexOperationalCapabilityClass, ...] | set[CodexOperationalCapabilityClass],
    facts: dict[str, object],
    head: str | None,
    project_id: str | None,
) -> MissionAdmission:
    ordered_blockers = tuple(sorted(set(blockers), key=lambda item: (item.code, item.detail)))
    ordered_missing = tuple(sorted(set(missing), key=lambda item: item.value))
    next_action = {
        MissionAdmissionStatus.ADMITTED: "Create the mission through the controlled lifecycle service.",
        MissionAdmissionStatus.HUMAN_REQUIRED: "Obtain the exact Human action required by maintenance governance, then re-run admission.",
        MissionAdmissionStatus.BLOCKED: "Resolve the reported repository, state, policy, or capability blockers before starting a mission.",
    }[status]
    return MissionAdmission(
        status,
        _fingerprint(_request_data(request)),
        _fingerprint(facts),
        head,
        project_id,
        ordered_blockers,
        ordered_missing,
        next_action,
    )


def _blocked(
    request: MissionRequest,
    blockers: list[MissionAdmissionBlocker],
    facts: dict[str, object],
    *,
    head: str | None = None,
    project_id: str | None = None,
) -> MissionAdmission:
    return _result(request, MissionAdmissionStatus.BLOCKED, blockers, (), facts, head, project_id)


def _request_data(request: MissionRequest) -> dict[str, object]:
    return {
        "objective": request.objective,
        "repository_root": _path_key(request.repository_root),
        "requested_scope": list(request.requested_scope),
        "verification_command_ids": list(request.verification_command_ids),
    }


def _profile_facts(profile: RepositoryProfile) -> dict[str, object]:
    return {
        "requested_root": _path_key(profile.requested_root),
        "support_status": profile.support_status.value,
        "scan_complete": profile.scan_complete,
        "agentic_os_state": profile.agentic_os.state.value,
        "config_fingerprint": profile.agentic_os.config_semantic_fingerprint,
        "git_head": profile.git.head_commit.value,
        "git_clean": profile.git.clean.value,
        "git_detached": profile.git.detached.value,
    }


def _project_state_facts(state: ProjectState) -> dict[str, object]:
    return {
        "schema_version": state.schema_version,
        "project_id": state.project_id,
        "user_story_ids": sorted(item.id for item in state.user_stories),
        "evidence_ids": sorted(item.evidence_id for item in state.evidence),
        "gate_ids": sorted(item.gate_id for item in state.gates),
        "certification_ids": sorted(item.certification_id for item in state.certifications),
    }


def _mission_state_facts(state: MissionState) -> dict[str, object]:
    return {
        "mission_id": state.mission_id,
        "generation": state.workflow_generation,
        "status": state.status.value,
        "role": state.role.value,
        "operating_step": state.operating_step.value,
        "observed_commit": state.observed_commit,
    }


def _ledger_facts(ledger: CodexExecutionLedger) -> dict[str, object]:
    return {
        "schema_version": ledger.schema_version,
        "records": [
            {
                "execution_id": item.execution_id,
                "mission_id": item.mission_id,
                "generation": item.workflow_generation,
                "status": item.status.value,
            }
            for item in ledger.records
        ],
    }


def _maintenance_facts(admission: MaintenanceStartAdmission) -> dict[str, object]:
    if not isinstance(admission, MaintenanceStartAdmission):
        return {"invalid": type(admission).__name__}
    return {
        "operation": admission.operation.value,
        "state": admission.state.value,
        "decision": admission.decision.value,
        "reasons": [item.value for item in admission.reasons],
        "maintenance_fingerprint": admission.maintenance_fingerprint,
        "evaluated_at": admission.evaluated_at.isoformat(),
    }


def _capability_facts(snapshot: MissionCapabilitySnapshot) -> dict[str, object]:
    if not isinstance(snapshot, MissionCapabilitySnapshot):
        return {"invalid": type(snapshot).__name__}
    assessment = snapshot.assessment
    return {
        "executable_path": _path_key(assessment.executable_path),
        "executable_sha256": assessment.executable_sha256,
        "executable_version": assessment.executable_version,
        "assessment_observed_at": assessment.observed_at.isoformat(),
        "environment_fingerprint": snapshot.environment_fingerprint,
        "operational_proofs": [
            {
                "class": item.capability_class.value,
                "status": item.status.value,
                "diagnostic_code": item.diagnostic_code,
                "observed_at": item.observed_at.isoformat(),
            }
            for item in sorted(snapshot.operational_proofs, key=lambda proof: proof.capability_class.value)
        ],
    }


def _error_blocker(code: str, error: Exception) -> MissionAdmissionBlocker:
    error_code = getattr(error, "code", type(error).__name__)
    return MissionAdmissionBlocker(code, str(error_code))


def _canonical_text(value: str, field: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise ValueError(f"{field} must be non-empty trimmed text")
    if unicodedata.normalize("NFC", value) != value or "\0" in value:
        raise ValueError(f"{field} must be canonical NFC text")
    return value


def _canonical_relative_path(value: str) -> str:
    value = _canonical_text(value, "requested_scope")
    if "\\" in value or value.startswith("/") or ":" in value:
        raise ValueError("requested_scope must be repository-relative POSIX paths")
    parts = value.split("/")
    if any(item in {"", ".", ".."} for item in parts) or str(PurePosixPath(value)) != value:
        raise ValueError("requested_scope must use canonical POSIX syntax")
    return value


def _canonical_identifier(value: str) -> str:
    value = _canonical_text(value, "verification_command_ids")
    if not _REQUEST_ID.fullmatch(value):
        raise ValueError("verification command ID is invalid")
    return value


def _contains(parent: str, child: str) -> bool:
    parent_key = parent.casefold()
    child_key = child.casefold()
    return parent_key == child_key or child_key.startswith(parent_key + "/")


def _overlaps(left: str, right: str) -> bool:
    return _contains(left, right) or _contains(right, left)


def _path_key(value: str) -> str:
    return os.path.normcase(str(Path(value).resolve(strict=False))).casefold()


def _fingerprint(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
