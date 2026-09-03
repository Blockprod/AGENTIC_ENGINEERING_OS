from dataclasses import replace

import pytest

from agentic_engineering_os.application import (
    CertifierInput,
    CertifierInputError,
    IntegratedStoryContext,
    ReviewerInput,
    ReviewerInputError,
    TesterInput,
    TesterInputError,
)
from agentic_engineering_os.application.integrated_story_context import (
    role_result_fingerprint,
)
from agentic_engineering_os.domain import EvidenceType

from test_certifier import (
    architect as certifier_architect,
    evidence as certifier_evidence,
    gate as certifier_gate,
    handoff as certifier_handoff,
    implementer as certifier_implementer,
    make_tester_result as certifier_tester,
    reviewer as certifier_reviewer,
    story as certifier_story,
)
from test_reviewer import (
    handoff as reviewer_handoff,
    implementer_result as reviewer_implementer,
    make_tester_result as reviewer_tester,
    story as reviewer_story,
)
from test_tester import (
    handoff as _tester_handoff,
    implementer_result as _tester_implementer,
    story as _tester_story,
)


INTEGRATED = "b" * 40


def context(*, mission: str, implementation, architecture_fingerprint: str = "a" * 64):
    return IntegratedStoryContext(
        mission_id=mission,
        workflow_generation=0,
        user_story_id="US-0001",
        assignment_id="assignment-one",
        architect_subject="US-0001",
        architect_baseline_commit=implementation.observed_commit,
        architect_result_fingerprint=architecture_fingerprint,
        implementer_execution_id="execution-one",
        implementer_result_fingerprint=role_result_fingerprint(implementation),
        worktree_baseline_commit=implementation.observed_commit,
        implementation_commit="c" * 40,
        integration_gate_fingerprint="d" * 64,
        integrated_commit=INTEGRATED,
    )


def test_post_merge_inputs_preserve_historical_commits() -> None:
    testing_implementation = _tester_implementer()
    tester_input = TesterInput.from_integrated_handoff(
        _tester_handoff(observed_commit=INTEGRATED),
        _tester_story(),
        testing_implementation,
        context(mission="P2.6", implementation=testing_implementation),
    )
    assert tester_input.observed_commit == INTEGRATED
    assert tester_input.implementer_result.observed_commit != INTEGRATED

    review_implementation = reviewer_implementer()
    tested = reviewer_tester(observed_commit=INTEGRATED)
    reviewer_input = ReviewerInput.from_integrated_handoff(
        reviewer_handoff(observed_commit=INTEGRATED),
        reviewer_story(),
        review_implementation,
        tested,
        context(mission="P2.7", implementation=review_implementation),
    )
    assert reviewer_input.observed_commit == INTEGRATED
    assert reviewer_input.implementer_result.observed_commit != INTEGRATED
    assert reviewer_input.tester_result.observed_commit == INTEGRATED

    architecture = certifier_architect()
    implementation = certifier_implementer()
    tester_result = certifier_tester(observed_commit=INTEGRATED)
    reviewer_result = certifier_reviewer(observed_commit=INTEGRATED)
    integrated = context(
        mission="P2.8",
        implementation=implementation,
        architecture_fingerprint=role_result_fingerprint(architecture),
    )
    certifier_input = CertifierInput.from_integrated_handoff(
        certifier_handoff(observed_commit=INTEGRATED),
        certifier_story(),
        architecture,
        implementation,
        tester_result,
        reviewer_result,
        (
            certifier_evidence(commit=INTEGRATED),
            certifier_evidence(
                "EV-GATE",
                kind=EvidenceType.TEST_RESULT,
                subject="US-0001",
                commit=INTEGRATED,
            ),
        ),
        (certifier_gate(),),
        integrated,
    )
    assert certifier_input.observed_commit == INTEGRATED
    assert certifier_input.architect_result.observed_commit != INTEGRATED
    assert certifier_input.implementer_result.observed_commit != INTEGRATED


@pytest.mark.parametrize("layer", ["tester", "reviewer", "certifier"])
def test_post_merge_inputs_reject_forged_implementer_fingerprint(layer: str) -> None:
    if layer == "tester":
        implementation = _tester_implementer()
        with pytest.raises(TesterInputError, match="fingerprint"):
            TesterInput.from_integrated_handoff(
                _tester_handoff(observed_commit=INTEGRATED),
                _tester_story(),
                implementation,
                replace(
                    context(mission="P2.6", implementation=implementation),
                    implementer_result_fingerprint="f" * 64,
                ),
            )
    elif layer == "reviewer":
        implementation = reviewer_implementer()
        with pytest.raises(ReviewerInputError, match="fingerprint"):
            ReviewerInput.from_integrated_handoff(
                reviewer_handoff(observed_commit=INTEGRATED),
                reviewer_story(),
                implementation,
                reviewer_tester(observed_commit=INTEGRATED),
                replace(
                    context(mission="P2.7", implementation=implementation),
                    implementer_result_fingerprint="f" * 64,
                ),
            )
    else:
        architecture = certifier_architect()
        implementation = certifier_implementer()
        with pytest.raises(CertifierInputError, match="fingerprint"):
            CertifierInput.from_integrated_handoff(
                certifier_handoff(observed_commit=INTEGRATED),
                certifier_story(),
                architecture,
                implementation,
                certifier_tester(observed_commit=INTEGRATED),
                certifier_reviewer(observed_commit=INTEGRATED),
                (),
                (),
                replace(
                    context(
                        mission="P2.8",
                        implementation=implementation,
                        architecture_fingerprint=role_result_fingerprint(architecture),
                    ),
                    implementer_result_fingerprint="f" * 64,
                ),
            )
