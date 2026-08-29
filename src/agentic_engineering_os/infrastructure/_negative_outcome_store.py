"""Private, strict persistence for authoritative negative workflow outcomes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .project_state_store import PersistenceError, STATE_DIRECTORY


_FILENAME = "negative-outcomes.json"
_VERSION = "2.0"
_TRANSACTION_FIELDS = {
    "authority_fingerprint",
    "consume_outcome",
    "mission_id",
    "source_generation",
    "target_generation",
    "triggering_stage",
    "affected_user_story_ids",
    "reexecution_user_story_ids",
    "baseline_commit",
    "operation",
    "updated_at",
    "project_before_fingerprint",
    "project_after_fingerprint",
    "mission_before_fingerprint",
    "mission_after_fingerprint",
}
_REMEDIATION_STAGES = {
    "IMPLEMENTER",
    "INTEGRATION_GATE",
    "MERGE",
    "TESTER",
    "REVIEWER",
    "CERTIFIER",
}
_TRANSACTION_OPERATIONS = {
    "BEGIN_PARALLEL_REMEDIATION",
    "BLOCK_PARALLEL_RECOVERY",
}


class _DuplicateJsonKeyError(ValueError):
    pass


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    candidate: dict[str, Any] = {}
    for key, value in pairs:
        if key in candidate:
            raise _DuplicateJsonKeyError(key)
        candidate[key] = value
    return candidate


class _NegativeOutcomeStore:
    """Record and consume exact outcomes; no public authority API is exported."""

    def __init__(self, repository_root: Path | str) -> None:
        try:
            self._root = Path(repository_root).resolve(strict=True)
        except OSError as error:
            raise PersistenceError(
                "INVALID_REPOSITORY_ROOT", "negative outcome root cannot be resolved"
            ) from error
        if not self._root.is_dir():
            raise PersistenceError(
                "INVALID_REPOSITORY_ROOT", "negative outcome root is not a directory"
            )
        self._state_directory = self._root / STATE_DIRECTORY
        self._path = self._state_directory / _FILENAME

    def _record(self, serialized_result: Mapping[str, object]) -> None:
        fingerprint, normalized = _fingerprint(serialized_result)
        document = self._load()
        outcomes = document["outcomes"]
        assert isinstance(outcomes, list)
        if any(
            isinstance(item, dict) and item.get("fingerprint") == fingerprint
            for item in outcomes
        ):
            return
        outcomes.append(
            {
                "fingerprint": fingerprint,
                "result": normalized,
                "consumed": False,
            }
        )
        outcomes.sort(key=lambda item: item["fingerprint"])
        self._write(document)

    def _claim(
        self,
        transaction: Mapping[str, object],
        *,
        authority: Mapping[str, object],
    ) -> str:
        """Persist one exact pending transaction after authority was validated."""

        normalized_transaction = _validate_transaction_intent(transaction)
        fingerprint, normalized_transaction = _fingerprint(normalized_transaction)
        authority_fingerprint, normalized_authority = _fingerprint(authority)
        if normalized_transaction["authority_fingerprint"] != authority_fingerprint:
            raise PersistenceError(
                "TRANSACTION_AUTHORITY_MISMATCH",
                "transaction is not bound to the supplied authority",
            )
        document = self._load()
        transactions = document["transactions"]
        assert isinstance(transactions, list)
        pending = [
            item
            for item in transactions
            if isinstance(item, dict)
            and item.get("status") == "PENDING"
        ]
        if pending:
            raise PersistenceError(
                "TRANSACTION_PENDING",
                "mission already has a pending remediation transaction",
            )
        if normalized_transaction["consume_outcome"]:
            outcomes = document["outcomes"]
            assert isinstance(outcomes, list)
            matches = [
                item
                for item in outcomes
                if isinstance(item, dict)
                and item.get("fingerprint") == authority_fingerprint
                and item.get("result") == normalized_authority
                and item.get("consumed") is False
            ]
            if len(matches) != 1:
                raise PersistenceError(
                    "NEGATIVE_OUTCOME_NOT_AUTHORIZED",
                    "transaction authority is absent, altered, or already consumed",
                )
        transactions.append(
            {
                "fingerprint": fingerprint,
                "intent": normalized_transaction,
                "status": "PENDING",
            }
        )
        transactions.sort(key=lambda item: item["fingerprint"])
        self._write(document)
        return fingerprint

    def _pending(self, mission_id: str | None = None) -> dict[str, object] | None:
        document = self._load()
        matches = [
            item
            for item in document["transactions"]
            if isinstance(item, dict)
            and item.get("status") == "PENDING"
            and (
                mission_id is None
                or (
                    isinstance(item.get("intent"), dict)
                    and item["intent"].get("mission_id") == mission_id
                )
            )
        ]
        if len(matches) > 1:
            raise PersistenceError(
                "INVALID_DOMAIN_DATA",
                "multiple pending transactions are not allowed",
            )
        return matches[0] if matches else None

    def _finalize(self, fingerprint: str) -> None:
        document = self._load()
        transactions = document["transactions"]
        assert isinstance(transactions, list)
        matches = [
            item
            for item in transactions
            if isinstance(item, dict)
            and item.get("fingerprint") == fingerprint
            and item.get("status") == "PENDING"
        ]
        if len(matches) != 1:
            raise PersistenceError(
                "TRANSACTION_NOT_PENDING",
                "transaction is absent, altered, or already finalized",
            )
        transaction = matches[0]
        intent = transaction["intent"]
        assert isinstance(intent, dict)
        if intent["consume_outcome"]:
            authority_fingerprint = intent["authority_fingerprint"]
            outcomes = document["outcomes"]
            assert isinstance(outcomes, list)
            outcome_matches = [
                item
                for item in outcomes
                if isinstance(item, dict)
                and item.get("fingerprint") == authority_fingerprint
                and item.get("consumed") is False
            ]
            if len(outcome_matches) != 1:
                raise PersistenceError(
                    "NEGATIVE_OUTCOME_NOT_AUTHORIZED",
                    "transaction outcome is absent, altered, or already consumed",
                )
            outcome_matches[0]["consumed"] = True
        transaction["status"] = "FINALIZED"
        self._write(document)

    def _contains_unconsumed(self, serialized_result: Mapping[str, object]) -> bool:
        fingerprint, normalized = _fingerprint(serialized_result)
        for item in self._load()["outcomes"]:
            if (
                isinstance(item, dict)
                and item.get("fingerprint") == fingerprint
                and item.get("result") == normalized
                and item.get("consumed") is False
            ):
                return True
        return False

    def _consume(self, serialized_result: Mapping[str, object]) -> None:
        fingerprint, normalized = _fingerprint(serialized_result)
        document = self._load()
        outcomes = document["outcomes"]
        assert isinstance(outcomes, list)
        matches = [
            item
            for item in outcomes
            if isinstance(item, dict)
            and item.get("fingerprint") == fingerprint
            and item.get("result") == normalized
            and item.get("consumed") is False
        ]
        if len(matches) != 1:
            raise PersistenceError(
                "NEGATIVE_OUTCOME_NOT_AUTHORIZED",
                "negative outcome is absent, altered, or already consumed",
            )
        matches[0]["consumed"] = True
        self._write(document)

    def _load(self) -> dict[str, object]:
        self._assert_safe_paths(for_write=False)
        if not self._path.exists():
            return {"version": _VERSION, "outcomes": [], "transactions": []}
        if not self._path.is_file():
            raise PersistenceError(
                "READ_FAILED", "negative outcome registry is not a regular file"
            )
        try:
            candidate = json.loads(
                self._path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except _DuplicateJsonKeyError as error:
            raise PersistenceError(
                "INVALID_JSON", f"duplicate negative outcome key: {error}"
            ) from error
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PersistenceError(
                "INVALID_JSON", "negative outcome registry is unreadable or invalid"
            ) from error
        return _validate_document(candidate)

    def _write(self, document: Mapping[str, object]) -> None:
        normalized = _validate_document(document)
        self._assert_safe_paths(for_write=True)
        try:
            self._state_directory.mkdir(parents=False, exist_ok=True)
        except OSError as error:
            raise PersistenceError(
                "WRITE_FAILED", "negative outcome directory cannot be created"
            ) from error
        self._assert_safe_paths(for_write=True)
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=".negative-outcomes.",
                suffix=".tmp",
                dir=self._state_directory,
                text=True,
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    normalized,
                    stream,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        except Exception as error:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            raise PersistenceError(
                "WRITE_FAILED", "atomic negative outcome write failed"
            ) from error

    def _assert_safe_paths(self, *, for_write: bool) -> None:
        if self._state_directory.is_symlink() or self._path.is_symlink():
            raise PersistenceError(
                "UNSAFE_PATH", "negative outcome paths cannot be symbolic links"
            )
        if self._state_directory.exists() and not self._state_directory.is_dir():
            raise PersistenceError(
                "UNSAFE_PATH", "negative outcome state path is not a directory"
            )
        if self._path.exists() and not self._path.is_file():
            raise PersistenceError(
                "UNSAFE_PATH", "negative outcome path is not a regular file"
            )
        if not for_write and self._state_directory.exists():
            try:
                if self._state_directory.resolve(strict=True).parent != self._root:
                    raise PersistenceError(
                        "UNSAFE_PATH", "negative outcome directory escaped the repository"
                    )
            except OSError as error:
                raise PersistenceError(
                    "UNSAFE_PATH", "negative outcome directory cannot be resolved"
                ) from error


def _fingerprint(
    serialized_result: Mapping[str, object],
) -> tuple[str, dict[str, object]]:
    try:
        encoded = json.dumps(
            serialized_result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        normalized = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise PersistenceError(
            "INVALID_DOMAIN_DATA", "negative outcome is not canonical JSON data"
        ) from error
    if not isinstance(normalized, dict):
        raise PersistenceError(
            "INVALID_DOMAIN_DATA", "negative outcome must be a JSON object"
        )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest(), normalized


def _validate_document(candidate: object) -> dict[str, object]:
    if not isinstance(candidate, Mapping) or set(candidate) != {
        "version",
        "outcomes",
        "transactions",
    }:
        raise PersistenceError(
            "INVALID_DOMAIN_DATA", "negative outcome registry has unknown fields"
        )
    if candidate["version"] != _VERSION:
        raise PersistenceError(
            "INCOMPATIBLE_VERSION",
            "negative outcome registry requires explicit version 2.0 migration",
        )
    if not isinstance(candidate["outcomes"], list) or not isinstance(
        candidate["transactions"], list
    ):
        raise PersistenceError(
            "INVALID_DOMAIN_DATA", "negative outcome registry version or outcomes is invalid"
        )
    seen: set[str] = set()
    normalized_outcomes: list[dict[str, object]] = []
    for item in candidate["outcomes"]:
        if not isinstance(item, Mapping) or set(item) != {
            "fingerprint",
            "result",
            "consumed",
        }:
            raise PersistenceError(
                "INVALID_DOMAIN_DATA", "negative outcome record has unknown fields"
            )
        fingerprint = item["fingerprint"]
        result = item["result"]
        consumed = item["consumed"]
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
            or not isinstance(result, Mapping)
            or not isinstance(consumed, bool)
        ):
            raise PersistenceError(
                "INVALID_DOMAIN_DATA", "negative outcome record is malformed"
            )
        expected, normalized_result = _fingerprint(result)
        if fingerprint != expected or fingerprint in seen:
            raise PersistenceError(
                "INVALID_DOMAIN_DATA", "negative outcome fingerprint is invalid or duplicated"
            )
        seen.add(fingerprint)
        normalized_outcomes.append(
            {
                "fingerprint": fingerprint,
                "result": normalized_result,
                "consumed": consumed,
            }
        )
    normalized_transactions: list[dict[str, object]] = []
    transaction_fingerprints: set[str] = set()
    pending_seen = False
    for item in candidate["transactions"]:
        if not isinstance(item, Mapping) or set(item) != {
            "fingerprint",
            "intent",
            "status",
        }:
            raise PersistenceError(
                "INVALID_DOMAIN_DATA", "transaction record has unknown fields"
            )
        fingerprint = item["fingerprint"]
        intent = _validate_transaction_intent(item["intent"])
        status = item["status"]
        expected, intent = _fingerprint(intent)
        if (
            not isinstance(fingerprint, str)
            or fingerprint != expected
            or fingerprint in transaction_fingerprints
            or status not in {"PENDING", "FINALIZED"}
        ):
            raise PersistenceError(
                "INVALID_DOMAIN_DATA", "transaction record is malformed"
            )
        mission_id = intent["mission_id"]
        assert isinstance(mission_id, str)
        if status == "PENDING" and pending_seen:
            raise PersistenceError(
                "INVALID_DOMAIN_DATA",
                "multiple pending transactions are forbidden",
            )
        transaction_fingerprints.add(fingerprint)
        if status == "PENDING":
            pending_seen = True
        normalized_transactions.append(
            {"fingerprint": fingerprint, "intent": intent, "status": status}
        )
    outcomes_by_fingerprint = {
        item["fingerprint"]: item for item in normalized_outcomes
    }
    for transaction in normalized_transactions:
        intent = transaction["intent"]
        assert isinstance(intent, dict)
        if not intent["consume_outcome"]:
            continue
        outcome = outcomes_by_fingerprint.get(intent["authority_fingerprint"])
        expected_consumed = transaction["status"] == "FINALIZED"
        if outcome is None or outcome["consumed"] is not expected_consumed:
            raise PersistenceError(
                "INVALID_DOMAIN_DATA",
                "transaction lifecycle conflicts with its authoritative outcome",
            )
    normalized_outcomes.sort(key=lambda item: item["fingerprint"])
    normalized_transactions.sort(key=lambda item: item["fingerprint"])
    return {
        "version": _VERSION,
        "outcomes": normalized_outcomes,
        "transactions": normalized_transactions,
    }


def _validate_transaction_intent(candidate: object) -> dict[str, object]:
    if not isinstance(candidate, Mapping) or set(candidate) != _TRANSACTION_FIELDS:
        raise PersistenceError(
            "INVALID_DOMAIN_DATA", "transaction intent has unknown or missing fields"
        )
    strings = {
        "authority_fingerprint",
        "mission_id",
        "triggering_stage",
        "baseline_commit",
        "operation",
        "updated_at",
        "project_before_fingerprint",
        "project_after_fingerprint",
        "mission_before_fingerprint",
        "mission_after_fingerprint",
    }
    for field in strings:
        value = candidate[field]
        if not isinstance(value, str) or not value:
            raise PersistenceError(
                "INVALID_DOMAIN_DATA", f"transaction field {field} is invalid"
            )
    hash_fields = {
        "authority_fingerprint",
        "project_before_fingerprint",
        "project_after_fingerprint",
        "mission_before_fingerprint",
        "mission_after_fingerprint",
    }
    if any(
        len(candidate[field]) != 64
        or any(character not in "0123456789abcdef" for character in candidate[field])
        for field in hash_fields
    ):
        raise PersistenceError(
            "INVALID_DOMAIN_DATA", "transaction fingerprints are invalid"
        )
    source = candidate["source_generation"]
    target = candidate["target_generation"]
    if (
        not isinstance(source, int)
        or isinstance(source, bool)
        or source < 0
        or not isinstance(target, int)
        or isinstance(target, bool)
        or target not in {source, source + 1}
    ):
        raise PersistenceError(
            "INVALID_DOMAIN_DATA", "transaction generations are invalid"
        )
    stage = candidate["triggering_stage"]
    operation = candidate["operation"]
    if stage not in _REMEDIATION_STAGES or operation not in _TRANSACTION_OPERATIONS:
        raise PersistenceError(
            "INVALID_DOMAIN_DATA", "transaction stage or operation is invalid"
        )
    if (
        operation == "BEGIN_PARALLEL_REMEDIATION" and target != source + 1
    ) or (
        operation == "BLOCK_PARALLEL_RECOVERY"
        and (target != source or stage not in {"INTEGRATION_GATE", "MERGE"})
    ):
        raise PersistenceError(
            "INVALID_DOMAIN_DATA", "transaction generation semantics are invalid"
        )
    if not isinstance(candidate["consume_outcome"], bool):
        raise PersistenceError(
            "INVALID_DOMAIN_DATA", "consume_outcome must be a strict boolean"
        )
    if candidate["consume_outcome"] is not (
        stage in {"MERGE", "TESTER", "REVIEWER", "CERTIFIER"}
    ):
        raise PersistenceError(
            "INVALID_DOMAIN_DATA", "transaction outcome-consumption policy is invalid"
        )
    normalized: dict[str, object] = dict(candidate)
    for field in ("affected_user_story_ids", "reexecution_user_story_ids"):
        values = candidate[field]
        if (
            not isinstance(values, (list, tuple))
            or any(not isinstance(value, str) or not value for value in values)
            or len(set(values)) != len(values)
        ):
            raise PersistenceError(
                "INVALID_DOMAIN_DATA", f"transaction field {field} is invalid"
            )
        normalized[field] = list(values)
    if not normalized["affected_user_story_ids"] or (
        operation == "BEGIN_PARALLEL_REMEDIATION"
        and not normalized["reexecution_user_story_ids"]
    ) or (
        operation == "BLOCK_PARALLEL_RECOVERY"
        and normalized["reexecution_user_story_ids"]
    ):
        raise PersistenceError(
            "INVALID_DOMAIN_DATA", "transaction story bindings are invalid"
        )
    baseline = candidate["baseline_commit"]
    if len(baseline) != 40 or any(
        character not in "0123456789abcdef" for character in baseline
    ):
        raise PersistenceError(
            "INVALID_DOMAIN_DATA", "transaction baseline commit is invalid"
        )
    try:
        timestamp = datetime.fromisoformat(str(candidate["updated_at"]).replace("Z", "+00:00"))
    except ValueError as error:
        raise PersistenceError(
            "INVALID_DOMAIN_DATA", "transaction updated_at is invalid"
        ) from error
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
        raise PersistenceError(
            "INVALID_DOMAIN_DATA", "transaction updated_at must be UTC"
        )
    return normalized
