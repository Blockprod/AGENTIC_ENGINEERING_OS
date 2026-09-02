"""Test-only construction of a strict persisted validated execution record."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from agentic_engineering_os.application.codex_runtime import (
    CodexExecutionObservation,
    GitExecutionObservation,
)
from agentic_engineering_os.application.execution_state import (
    EXECUTION_LEDGER_VERSION,
    CodexExecutionRecord,
    CodexExecutionStatus,
    ExecutionExecutableIdentity,
    _record_execution_id,
    _record_semantic_fingerprint,
    canonical_result_json,
    record_to_data,
    result_json_fingerprint,
)
from agentic_engineering_os.domain import MissionRole, to_dict


def write_validated_implementer_execution(context, result, *, observed_at: datetime) -> str:
    request_id = f"request-{context.assignment_id}"
    context_fingerprint = "c" * 64
    root = str(Path(context.worktree_path).resolve())
    observation = CodexExecutionObservation(
        request_id,
        context_fingerprint,
        None,
        None,
        None,
        root,
        (),
        observed_at,
        observed_at,
        None,
        None,
        0,
        "",
        "",
        False,
        False,
        (),
        (),
        None,
        False,
        False,
        False,
        GitExecutionObservation(context.baseline_commit, True, None, ()),
        GitExecutionObservation(
            context.baseline_commit, False, None, result.files_changed
        ),
        (),
    )
    result_json = canonical_result_json(to_dict(result))
    provisional = CodexExecutionRecord(
        "cx-" + "0" * 24,
        "0" * 64,
        request_id,
        context_fingerprint,
        context.handoff.mission_id,
        context.workflow_generation,
        MissionRole.IMPLEMENTER,
        context.user_story_id,
        root,
        root,
        root,
        context.baseline_commit,
        "f" * 64,
        "implementer-result@1.0",
        ExecutionExecutableIdentity(
            str((Path(root) / "codex.exe").resolve()),
            "test-codex 1.0",
            "e" * 64,
        ),
        CodexExecutionStatus.VALIDATED,
        observed_at,
        observed_at,
        observation=observation,
        validated_result_json=result_json,
        validated_result_fingerprint=result_json_fingerprint(result_json),
    )
    with_semantic = replace(
        provisional,
        semantic_fingerprint=_record_semantic_fingerprint(provisional),
    )
    record = replace(
        with_semantic,
        execution_id=_record_execution_id(with_semantic),
    )
    directory = Path(root) / ".agentic-engineering-os"
    directory.mkdir(exist_ok=True)
    (directory / "executions.json").write_text(
        json.dumps(
            {
                "schema_version": EXECUTION_LEDGER_VERSION,
                "records": [record_to_data(record)],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return record.execution_id
