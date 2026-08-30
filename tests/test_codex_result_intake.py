from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentic_engineering_os.application import (
    AcceptanceCheck,
    ArchitectInput,
    ArchitectResult,
    ArchitectVerdict,
    ArtifactCheck,
    CertifierInput,
    CertifierRecommendedAction,
    CertifierResult,
    CertifierVerdict,
    CodexExecutionObservation,
    CodexJsonlEvent,
    CodexResultIntake,
    CompiledPrompt,
    GateCheck,
    GitExecutionObservation,
    HumanApprovalCheck,
    ImplementerInput,
    ImplementerResult,
    ImplementerVerdict,
    ResultIntakeRefusalCode,
    ResultIntakeValidationContext,
    ReviewDimension,
    ReviewerInput,
    ReviewerResult,
    ReviewerVerdict,
    RoleHandoff,
    TestCaseType,
    TesterAcceptanceResult,
    TesterInput,
    TesterPlan,
    TesterResult,
    TesterTestCase,
    TesterVerdict,
    TesterVerificationResult,
    VerificationOutcome,
    VerificationResult,
)
from agentic_engineering_os.domain import (
    AcceptanceCriterion,
    Evidence,
    EvidenceType,
    Gate,
    GateResult,
    HumanApproval,
    MissionRole,
    OperatingStep,
    RiskLevel,
    UserStory,
    UserStoryMetadata,
    UserStoryScope,
    UserStoryStatus,
    to_dict,
)


NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
MISSION = "P4.6"
GENERATION = 6
SUBJECT = "US-0001"
COMMAND = "python -m pytest tests/test_feature.py"
CONTRACTS = {
    role: f"{role.value.casefold()}-result@1.0"
    for role in (
        MissionRole.ARCHITECT,
        MissionRole.IMPLEMENTER,
        MissionRole.TESTER,
        MissionRole.REVIEWER,
        MissionRole.CERTIFIER,
    )
}

for _contract_type in (
    TestCaseType,
    TesterAcceptanceResult,
    TesterInput,
    TesterPlan,
    TesterResult,
    TesterTestCase,
    TesterVerdict,
    TesterVerificationResult,
):
    _contract_type.__test__ = False


@dataclass(frozen=True)
class IntakeCase:
    compiled: CompiledPrompt
    observation: CodexExecutionObservation
    context: ResultIntakeValidationContext
    payload: dict[str, object]


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def story(status: UserStoryStatus) -> UserStory:
    return UserStory(
        "1.0",
        SUBJECT,
        "Bounded feature",
        "Exercise structured result intake.",
        status,
        1,
        RiskLevel.MEDIUM,
        (),
        UserStoryScope(("src/", "tests/"), ("src/forbidden.py",)),
        (AcceptanceCriterion("AC-001", "Behavior is observable.", True),),
        ("GATE-TESTS",),
        HumanApproval(False, False, None, None),
        UserStoryMetadata(NOW, "Codex/Architect", NOW),
    )


def handoff(role: MissionRole, commit: str) -> RoleHandoff:
    steps = {
        MissionRole.ARCHITECT: OperatingStep.UNDERSTAND_CONTRACT,
        MissionRole.IMPLEMENTER: OperatingStep.ACT,
        MissionRole.TESTER: OperatingStep.VERIFY,
        MissionRole.REVIEWER: OperatingStep.REPORT,
        MissionRole.CERTIFIER: OperatingStep.CONTROLLED_TRANSITION,
    }
    return RoleHandoff(
        MissionRole.ORCHESTRATOR,
        role,
        MISSION,
        GENERATION,
        SUBJECT,
        "Execute the bounded role contract.",
        commit,
        steps[role],
        (),
        "Return only the assigned RoleResult.",
    )


def implementer_result(commit: str) -> ImplementerResult:
    return ImplementerResult(
        MISSION,
        GENERATION,
        SUBJECT,
        SUBJECT,
        commit,
        "Implemented within scope.",
        ("src/feature.py", "tests/test_feature.py"),
        ("tests/test_feature.py",),
        (COMMAND,),
        (VerificationResult(COMMAND, True, VerificationOutcome.PASS, 0, "1 passed"),),
        (),
        (),
        (),
        MissionRole.TESTER,
        ImplementerVerdict.READY_FOR_TEST,
    )


def make_tester_result(commit: str) -> TesterResult:
    cases = tuple(
        TesterTestCase(
            f"TC-{index:03}",
            case_type,
            "Exercise behavior.",
            "Expected result.",
            "Observed result.",
            True,
            True,
            GateResult.PASS,
        )
        for index, case_type in enumerate(TestCaseType, 1)
    )
    return TesterResult(
        MISSION,
        GENERATION,
        SUBJECT,
        SUBJECT,
        commit,
        "Verified adversarially.",
        TesterPlan(("AC-001",), ("positive",), ("negative",), ("edge",), ("regression",), (COMMAND,)),
        (TesterAcceptanceResult("AC-001", GateResult.PASS, ("TC-001",), "Observed."),),
        cases,
        ("tests/test_feature.py",),
        (COMMAND,),
        (TesterVerificationResult(COMMAND, True, True, GateResult.PASS, 0, "1 passed"),),
        (),
        (),
        MissionRole.REVIEWER,
        TesterVerdict.READY_FOR_REVIEW,
    )


def reviewer_result(commit: str) -> ReviewerResult:
    return ReviewerResult(
        MISSION,
        GENERATION,
        SUBJECT,
        SUBJECT,
        commit,
        "Reviewed all required dimensions.",
        tuple(ReviewDimension),
        ("src/feature.py", "tests/test_feature.py"),
        (),
        (),
        MissionRole.CERTIFIER,
        ReviewerVerdict.READY_FOR_CERTIFICATION,
    )


def architect_result(commit: str) -> ArchitectResult:
    return ArchitectResult(
        MISSION,
        GENERATION,
        SUBJECT,
        commit,
        "Architecture is ready.",
        (),
        (),
        (),
        (),
        (story(UserStoryStatus.PROPOSED),),
        MissionRole.IMPLEMENTER,
        ArchitectVerdict.READY,
    )


def certifier_input(commit: str) -> CertifierInput:
    evidence = (
        Evidence("EV-AC", EvidenceType.ACCEPTANCE_CRITERION_CHECK, "AC-001", True, "pytest", None, None, None, commit, NOW, "Codex/Tester"),
        Evidence("EV-GATE", EvidenceType.TEST_RESULT, SUBJECT, True, "pytest", None, None, None, commit, NOW, "Codex/Tester"),
    )
    gates = (Gate("GATE-TESTS", SUBJECT, True, GateResult.PASS, ("EV-GATE",), NOW, "Codex/Tester"),)
    return CertifierInput.from_handoff(
        handoff(MissionRole.CERTIFIER, commit),
        story(UserStoryStatus.CERTIFICATION),
        architect_result(commit),
        implementer_result(commit),
        make_tester_result(commit),
        reviewer_result(commit),
        evidence,
        gates,
    )


def certifier_result(commit: str) -> CertifierResult:
    return CertifierResult(
        MISSION,
        GENERATION,
        SUBJECT,
        SUBJECT,
        commit,
        "Dossier is ready for deterministic control.",
        tuple(ArtifactCheck(role, True, True, "Present and coherent.") for role in (MissionRole.ARCHITECT, MissionRole.IMPLEMENTER, MissionRole.TESTER, MissionRole.REVIEWER)),
        (AcceptanceCheck("AC-001", True, GateResult.PASS, ("EV-AC",), "Supported."),),
        (GateCheck("GATE-TESTS", True, True, GateResult.PASS, ("EV-GATE",), True, False, "Supported."),),
        ("EV-AC", "EV-GATE"),
        HumanApprovalCheck(False, False, True, None, "Not required."),
        (),
        (),
        CertifierRecommendedAction.SUBMIT_TO_CONTROL_PLANE,
        CertifierVerdict.READY_FOR_CONTROL_PLANE,
    )


def role_contract(role: MissionRole, commit: str):
    if role is MissionRole.ARCHITECT:
        return ArchitectInput.from_handoff(handoff(role, commit)), architect_result(commit)
    if role is MissionRole.IMPLEMENTER:
        return ImplementerInput.from_handoff(handoff(role, commit), story(UserStoryStatus.IN_PROGRESS)), implementer_result(commit)
    if role is MissionRole.TESTER:
        return TesterInput.from_handoff(handoff(role, commit), story(UserStoryStatus.TESTING), implementer_result(commit)), make_tester_result(commit)
    if role is MissionRole.REVIEWER:
        return ReviewerInput.from_handoff(handoff(role, commit), story(UserStoryStatus.REVIEW), implementer_result(commit), make_tester_result(commit)), reviewer_result(commit)
    if role is MissionRole.CERTIFIER:
        return certifier_input(commit), certifier_result(commit)
    raise AssertionError(role)


def repository(tmp_path: Path, role: MissionRole) -> tuple[Path, str, Path]:
    root = tmp_path / "repository"
    root.mkdir(parents=True)
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "P4.6 Test Operator")
    git(root, "config", "user.email", "p4.6@example.invalid")
    (root / "README.md").write_text("intake repository\n", encoding="utf-8")
    schemas = root / "schemas"
    schemas.mkdir()
    contract_name = CONTRACTS[role].removesuffix("@1.0")
    schema = schemas / f"{contract_name}.schema.json"
    source = Path(__file__).parents[1] / "schemas" / schema.name
    shutil.copyfile(source, schema)
    git(root, "add", ".")
    git(root, "commit", "-m", "test: intake baseline")
    return root.resolve(), git(root, "rev-parse", "HEAD").casefold(), schema.resolve()


def intake_case(tmp_path: Path, role: MissionRole) -> IntakeCase:
    root, commit, schema = repository(tmp_path, role)
    role_input, role_result = role_contract(role, commit)
    payload = to_dict(role_result)
    payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    event_payload = {"type": "item.completed", "item": {"type": "agent_message", "text": payload_text}}
    event_json = json.dumps(event_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    event = CodexJsonlEvent(1, "item.completed", event_json, event_json)
    changed = role in {MissionRole.IMPLEMENTER, MissionRole.TESTER}
    before = GitExecutionObservation(commit, True, None)
    after = GitExecutionObservation(commit, not changed, None)
    observation = CodexExecutionObservation(
        "request-p4-6",
        "a" * 64,
        "C:/tools/codex.exe",
        "codex-cli test",
        "b" * 64,
        str(root),
        ("codex", "exec", "--json", "-C", str(root), "--output-schema", str(schema), "-"),
        NOW,
        NOW,
        123,
        "thread-p4-6",
        0,
        event_json,
        "",
        False,
        False,
        (event,),
        (),
        payload_text,
        False,
        False,
        False,
        before,
        after,
        ("GIT_STATE_CHANGED",) if changed else (),
    )
    compiled = CompiledPrompt(
        "request-p4-6",
        "a" * 64,
        MISSION,
        GENERATION,
        role,
        SUBJECT,
        str(root),
        None,
        commit,
        CONTRACTS[role],
        "compiled prompt",
        15,
        10,
        0,
    )
    return IntakeCase(
        compiled,
        observation,
        ResultIntakeValidationContext(role_input, str(schema)),
        payload,
    )


def with_payload(case: IntakeCase, payload_text: str) -> IntakeCase:
    event_payload = {"type": "item.completed", "item": {"type": "agent_message", "text": payload_text}}
    event_json = json.dumps(event_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    event = CodexJsonlEvent(1, "item.completed", event_json, event_json)
    return replace(case, observation=replace(case.observation, stdout=event_json, events=(event,), final_output=payload_text))


def codes(outcome) -> set[ResultIntakeRefusalCode]:
    return {reason.code for reason in outcome.refusal_reasons}


@pytest.mark.parametrize("role", tuple(CONTRACTS))
def test_accepts_each_existing_role_result_deterministically(tmp_path: Path, role: MissionRole) -> None:
    case = intake_case(tmp_path, role)
    first = CodexResultIntake().process(case.compiled, case.observation, case.context)
    second = CodexResultIntake().process(case.compiled, case.observation, case.context)

    assert first.accepted and first.validated_result is not None
    assert first.role is role
    assert first == second
    assert first.refusal_reasons == ()
    assert not hasattr(first, "evidence")
    assert not hasattr(first, "gate")
    assert not hasattr(first, "certification")


@pytest.mark.parametrize(
    ("payload_text", "expected"),
    [
        ("not-json", ResultIntakeRefusalCode.PAYLOAD_MALFORMED),
        ('{"role":"IMPLEMENTER","role":"ARCHITECT"}', ResultIntakeRefusalCode.PAYLOAD_MALFORMED),
        ('{"role":"IMPLEMENTER"', ResultIntakeRefusalCode.PAYLOAD_MALFORMED),
        ('{"role":"IMPLEMENTER","value":NaN}', ResultIntakeRefusalCode.PAYLOAD_MALFORMED),
        ("PASS — everything is certified", ResultIntakeRefusalCode.PAYLOAD_MALFORMED),
    ],
)
def test_refuses_malformed_duplicate_truncated_and_textual_claims(tmp_path: Path, payload_text: str, expected: ResultIntakeRefusalCode) -> None:
    case = with_payload(intake_case(tmp_path, MissionRole.IMPLEMENTER), payload_text)
    outcome = CodexResultIntake().process(case.compiled, case.observation, case.context)

    assert not outcome.accepted
    assert expected in codes(outcome)


def test_refuses_missing_and_multiple_conflicting_payloads(tmp_path: Path) -> None:
    case = intake_case(tmp_path, MissionRole.ARCHITECT)
    missing = replace(case.observation, events=(), final_output=None, stdout="")
    other = with_payload(case, json.dumps({**case.payload, "summary": "contradiction"}))
    second = replace(other.observation.events[0], line_number=2)
    multiple = replace(
        case.observation,
        events=case.observation.events + (second,),
        stdout=f"{case.observation.stdout}\n{second.raw_line}",
    )

    missing_outcome = CodexResultIntake().process(case.compiled, missing, case.context)
    multiple_outcome = CodexResultIntake().process(case.compiled, multiple, case.context)

    assert ResultIntakeRefusalCode.PAYLOAD_MISSING in codes(missing_outcome)
    assert ResultIntakeRefusalCode.PAYLOAD_AMBIGUOUS in codes(multiple_outcome)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mission_id", "OTHER"),
        ("workflow_generation", GENERATION - 1),
        ("subject", "US-9999"),
        ("user_story_id", "US-9999"),
        ("observed_commit", "0" * 40),
    ],
)
def test_refuses_cross_mission_story_generation_and_stale_commit(tmp_path: Path, field: str, value: object) -> None:
    case = intake_case(tmp_path, MissionRole.IMPLEMENTER)
    candidate = {**case.payload, field: value}
    outcome = CodexResultIntake().process(case.compiled, with_payload(case, json.dumps(candidate)).observation, case.context)

    assert not outcome.accepted
    assert ResultIntakeRefusalCode.PAYLOAD_BINDING_MISMATCH in codes(outcome)


def test_refuses_wrong_request_role_worktree_contract_and_schema_channel(tmp_path: Path) -> None:
    case = intake_case(tmp_path, MissionRole.IMPLEMENTER)
    other = tmp_path / "other"
    other.mkdir()
    wrong_role = with_payload(case, json.dumps({**case.payload, "role": "TESTER"}))
    variants = (
        replace(case.observation, request_id="other-request"),
        wrong_role.observation,
        replace(case.observation, cwd=str(other)),
        replace(case.observation, invocation=tuple(item for item in case.observation.invocation if item not in {"--output-schema", case.context.output_schema_path})),
    )
    outcomes = [CodexResultIntake().process(case.compiled, item, case.context) for item in variants]
    wrong_contract = CodexResultIntake().process(replace(case.compiled, expected_result_contract="tester-result@1.0"), case.observation, case.context)

    assert ResultIntakeRefusalCode.OBSERVATION_BINDING_MISMATCH in codes(outcomes[0])
    assert ResultIntakeRefusalCode.ROLE_MISMATCH in codes(outcomes[1])
    assert ResultIntakeRefusalCode.OBSERVATION_BINDING_MISMATCH in codes(outcomes[2])
    assert ResultIntakeRefusalCode.STRUCTURED_CHANNEL_MISSING in codes(outcomes[3])
    assert ResultIntakeRefusalCode.EXPECTED_CONTRACT_MISMATCH in codes(wrong_contract)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: {**payload, "unexpected": True},
        lambda payload: {**payload, "workflow_generation": True},
        lambda payload: {**payload, "verdict": "PASS"},
        lambda payload: {key: value for key, value in payload.items() if key != "summary"},
    ],
)
def test_refuses_extra_missing_wrong_enum_and_wrong_primitive_types(tmp_path: Path, mutation) -> None:
    case = intake_case(tmp_path, MissionRole.ARCHITECT)
    changed = with_payload(case, json.dumps(mutation(case.payload)))
    outcome = CodexResultIntake().process(case.compiled, changed.observation, case.context)

    assert not outcome.accepted


def test_boolean_and_integer_primitive_types_are_not_interchangeable(tmp_path: Path) -> None:
    case = intake_case(tmp_path, MissionRole.ARCHITECT)
    changed_payload = json.loads(json.dumps(case.payload))
    changed_payload["user_stories"][0]["human_approval"]["required"] = 1
    changed = with_payload(case, json.dumps(changed_payload))

    outcome = CodexResultIntake().process(case.compiled, changed.observation, case.context)

    assert not outcome.accepted
    assert ResultIntakeRefusalCode.ROLE_VALIDATION_FAILED in codes(outcome)


def test_malformed_stored_jsonl_event_and_contradictory_final_output_are_refused(tmp_path: Path) -> None:
    case = intake_case(tmp_path, MissionRole.ARCHITECT)
    malformed_event = replace(case.observation.events[0], payload_json="not-json")
    malformed = replace(case.observation, events=(malformed_event,))
    contradictory = replace(case.observation, final_output="{}")

    malformed_outcome = CodexResultIntake().process(case.compiled, malformed, case.context)
    contradictory_outcome = CodexResultIntake().process(case.compiled, contradictory, case.context)

    assert ResultIntakeRefusalCode.PAYLOAD_MALFORMED in codes(malformed_outcome)
    assert ResultIntakeRefusalCode.PAYLOAD_AMBIGUOUS in codes(contradictory_outcome)


@pytest.mark.parametrize(
    "changes",
    [
        {"tool_failure_observed": True},
        {"exit_code": 7},
        {"timed_out": True},
        {"interrupted": True},
        {"stdout_truncated": True},
    ],
)
def test_transport_failure_never_promotes_success_looking_payload(tmp_path: Path, changes: dict[str, object]) -> None:
    case = intake_case(tmp_path, MissionRole.ARCHITECT)
    observation = replace(case.observation, **changes)
    outcome = CodexResultIntake().process(case.compiled, observation, case.context)

    assert not outcome.accepted
    assert ResultIntakeRefusalCode.TRANSPORT_FAILED in codes(outcome)


def test_stderr_warning_does_not_hide_or_invent_role_validity(tmp_path: Path) -> None:
    case = intake_case(tmp_path, MissionRole.ARCHITECT)
    observation = replace(case.observation, stderr="runtime warning", issues=("STDERR_OBSERVED",))
    outcome = CodexResultIntake().process(case.compiled, observation, case.context)

    assert outcome.accepted
    assert outcome.diagnostics.stderr_observed


def test_git_unknown_commit_drift_and_declared_changes_mismatch_fail_closed(tmp_path: Path) -> None:
    case = intake_case(tmp_path, MissionRole.IMPLEMENTER)
    unknown = replace(case.observation, git_after=GitExecutionObservation(None, None, "GIT_STATUS_FAILED"))
    drift = replace(case.observation, git_after=GitExecutionObservation("0" * 40, False, None))
    clean = replace(case.observation, git_after=GitExecutionObservation(case.compiled.observed_commit, True, None), issues=())

    outcomes = [CodexResultIntake().process(case.compiled, item, case.context) for item in (unknown, drift, clean)]

    assert ResultIntakeRefusalCode.GIT_OBSERVATION_REQUIRED in codes(outcomes[0])
    assert ResultIntakeRefusalCode.GIT_COMMIT_MISMATCH in codes(outcomes[1])
    assert ResultIntakeRefusalCode.GIT_SIDE_EFFECT_MISMATCH in codes(outcomes[2])


def test_forged_human_approval_and_certified_certifier_verdict_are_refused(tmp_path: Path) -> None:
    architect_case = intake_case(tmp_path / "architect", MissionRole.ARCHITECT)
    forged = json.loads(json.dumps(architect_case.payload))
    approval = forged["user_stories"][0]["human_approval"]
    approval.update({"required": True, "approved": True, "approved_by": "Codex/FakeHuman", "approved_at": NOW.isoformat(), "evidence_ref": "EV-FORGED"})
    forged_outcome = CodexResultIntake().process(architect_case.compiled, with_payload(architect_case, json.dumps(forged)).observation, architect_case.context)

    certifier_case = intake_case(tmp_path / "certifier", MissionRole.CERTIFIER)
    certified = with_payload(certifier_case, json.dumps({**certifier_case.payload, "verdict": "CERTIFIED"}))
    certified_outcome = CodexResultIntake().process(certifier_case.compiled, certified.observation, certifier_case.context)

    assert not forged_outcome.accepted
    assert ResultIntakeRefusalCode.ROLE_VALIDATION_FAILED in codes(forged_outcome)
    assert not certified_outcome.accepted
    assert ResultIntakeRefusalCode.ROLE_VALIDATION_FAILED in codes(certified_outcome)
