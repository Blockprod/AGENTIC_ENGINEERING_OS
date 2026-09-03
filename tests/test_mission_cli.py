from __future__ import annotations

import json

from agentic_engineering_os import cli, mission_cli
from agentic_engineering_os.application import mission_composition
from test_mission_runner import harness
from agentic_engineering_os.domain import HumanApproval


def invoke(capsys, *arguments: str):
    code = cli.main(["mission", *arguments, "--json"])
    captured = capsys.readouterr()
    return code, json.loads(captured.out or captured.err), captured


def test_mission_run_exposes_the_stable_flat_json_contract(
    tmp_path, capsys, monkeypatch
) -> None:
    (tmp_path / ".agentic-engineering-os").mkdir()
    _, runner, *_ = harness(tmp_path)
    monkeypatch.setattr(mission_cli, "build_mission_runner", lambda repository: runner)

    code, payload, captured = invoke(
        capsys,
        "run",
        "--repository",
        str(tmp_path),
        "--objective",
        "Complete two stories",
    )

    assert code == 0 and not captured.err
    assert payload["status"] == "COMPLETED"
    assert payload["phase"] == "REPORT"
    assert payload["mission_id"] == "mission-1"
    assert payload["generation"] == 0
    assert payload["current_story_ids"] == []
    assert payload["completed_story_ids"] == ["US-0001", "US-0002"]
    assert "repository_head" in payload and "evidence_references" in payload


def test_unconfigured_repository_is_a_structured_expected_refusal(
    tmp_path, capsys, monkeypatch
) -> None:
    (tmp_path / ".agentic-engineering-os").mkdir()
    monkeypatch.setattr(mission_composition.shutil, "which", lambda executable: None)
    code, payload, captured = invoke(
        capsys,
        "run",
        "--repository",
        str(tmp_path),
        "--objective",
        "Do bounded work",
    )

    assert code == 2
    assert payload["status"] == "REFUSED"
    assert payload["blockers"] == ["CONFIG_ABSENT"]
    assert "Traceback" not in captured.err


def test_resume_refuses_invalid_human_evidence_before_any_runtime_call(
    tmp_path, capsys
) -> None:
    evidence = tmp_path / "human.json"
    evidence.write_text("{}", encoding="utf-8")

    code, payload, _ = invoke(
        capsys,
        "resume",
        "--repository",
        str(tmp_path),
        "--mission-id",
        "mission-1",
        "--human-evidence",
        str(evidence),
    )

    assert code == 2
    assert payload["blockers"] == ["HUMAN_EVIDENCE_INVALID"]


def test_resume_accepts_one_canonical_attributable_human_evidence(
    tmp_path, capsys, monkeypatch
) -> None:
    (tmp_path / ".agentic-engineering-os").mkdir()
    _, runner, _, projects, *_ = harness(tmp_path)
    projects.value.user_stories[0].human_approval = HumanApproval(
        True, False, None, None
    )
    monkeypatch.setattr(mission_cli, "build_mission_runner", lambda repository: runner)
    evidence = tmp_path / "human.json"
    evidence.write_text(
        json.dumps(
            {
                "evidence_id": "EV-HUMAN-CLI",
                "evidence_type": "HUMAN_APPROVAL",
                "subject": "US-0001",
                "result": True,
                "source": "Human",
                "command": None,
                "exit_code": None,
                "artifact": None,
                "commit": "a" * 40,
                "timestamp": "2026-09-03T12:00:00Z",
                "producer": "Human/Alice",
            }
        ),
        encoding="utf-8",
    )

    code, payload, _ = invoke(
        capsys,
        "resume",
        "--repository",
        str(tmp_path),
        "--mission-id",
        "mission-1",
        "--human-evidence",
        str(evidence),
    )

    assert code == 0 and payload["status"] == "COMPLETED"
    assert projects.value.user_stories[0].human_approval.approved_by == "Human/Alice"
