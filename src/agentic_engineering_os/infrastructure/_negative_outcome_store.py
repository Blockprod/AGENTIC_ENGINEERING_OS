"""Private, strict persistence for authoritative negative workflow outcomes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .project_state_store import PersistenceError, STATE_DIRECTORY


_FILENAME = "negative-outcomes.json"
_VERSION = "1.0"


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
            return {"version": _VERSION, "outcomes": []}
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
    if not isinstance(candidate, Mapping) or set(candidate) != {"version", "outcomes"}:
        raise PersistenceError(
            "INVALID_DOMAIN_DATA", "negative outcome registry has unknown fields"
        )
    if candidate["version"] != _VERSION or not isinstance(candidate["outcomes"], list):
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
    normalized_outcomes.sort(key=lambda item: item["fingerprint"])
    return {"version": _VERSION, "outcomes": normalized_outcomes}
