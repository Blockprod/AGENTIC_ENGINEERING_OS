"""Codex transport schemas for the five canonical RoleResult contracts.

The transport schema constrains model output.  It is deliberately not an
authority boundary: :mod:`result_intake` still applies the full canonical
schema and role validator before accepting a RoleResult.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import cast

from agentic_engineering_os.domain import MissionRole
from agentic_engineering_os.resources.product import (
    ProductResourceError,
    product_resource_path,
)


_SCHEMA_FILES = {
    MissionRole.ARCHITECT: "architect-result.codex.schema.json",
    MissionRole.IMPLEMENTER: "implementer-result.codex.schema.json",
    MissionRole.TESTER: "tester-result.codex.schema.json",
    MissionRole.REVIEWER: "reviewer-result.codex.schema.json",
    MissionRole.CERTIFIER: "certifier-result.codex.schema.json",
}
_OMITTED_TRANSPORT_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "$defs",
        "title",
        "allOf",
        "if",
        "then",
        "else",
        "not",
        "contains",
        "uniqueItems",
    }
)


class CodexOutputSchemaError(ValueError):
    """A canonical schema cannot be projected without ambiguity."""


def codex_output_schema_path(role: MissionRole) -> Path:
    """Resolve the immutable packaged transport schema for one closed role."""

    try:
        filename = _SCHEMA_FILES[role]
    except (KeyError, TypeError) as error:
        raise CodexOutputSchemaError("unsupported Codex role") from error
    try:
        return product_resource_path(f"schemas/{filename}")
    except ProductResourceError as error:
        raise CodexOutputSchemaError(
            f"packaged Codex output schema is unavailable: {filename}"
        ) from error


def build_codex_output_schema(
    canonical_schema: Mapping[str, object],
    *,
    external_schemas: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Project one canonical role schema into the supported transport subset.

    References are fully expanded and conflicting reference siblings fail
    closed.  Conditional and uniqueness constraints unsupported by Codex are
    omitted only from transport; the canonical validator remains authoritative.
    """

    registry = dict(external_schemas or {})
    schema = _mapping_copy(canonical_schema)
    identifier = schema.get("$id")
    if isinstance(identifier, str):
        registry.setdefault(identifier, schema)
    projected = _project(schema, schema, registry, ())
    _assert_strict_objects(projected, ())
    return projected


def _project(
    node: object,
    document: Mapping[str, object],
    registry: Mapping[str, Mapping[str, object]],
    reference_stack: tuple[str, ...],
) -> object:
    if isinstance(node, list):
        return [_project(item, document, registry, reference_stack) for item in node]
    if not isinstance(node, Mapping):
        return deepcopy(node)

    reference = node.get("$ref")
    if reference is not None:
        if not isinstance(reference, str) or not reference:
            raise CodexOutputSchemaError("$ref must be a non-empty string")
        if reference in reference_stack:
            raise CodexOutputSchemaError(f"recursive $ref is unsupported: {reference}")
        target, target_document = _resolve_reference(reference, document, registry)
        expanded = cast(
            dict[str, object],
            _project(target, target_document, registry, (*reference_stack, reference)),
        )
        siblings = {
            key: value for key, value in node.items() if key != "$ref"
        }
        sibling_projection = cast(
            dict[str, object],
            _project(siblings, document, registry, reference_stack),
        )
        for key, value in sibling_projection.items():
            if key in expanded and expanded[key] != value:
                raise CodexOutputSchemaError(
                    f"conflicting constraint beside $ref {reference}: {key}"
                )
            expanded[key] = value
        return expanded

    result: dict[str, object] = {}
    for key, value in node.items():
        if key in _OMITTED_TRANSPORT_KEYWORDS:
            continue
        if key == "properties":
            if not isinstance(value, Mapping):
                raise CodexOutputSchemaError("properties must be an object")
            result[key] = {
                str(property_name): _project(
                    property_schema, document, registry, reference_stack
                )
                for property_name, property_schema in value.items()
            }
            continue
        if key == "const":
            if "enum" in node and node["enum"] != [value]:
                raise CodexOutputSchemaError("const conflicts with enum")
            result["enum"] = [deepcopy(value)]
            continue
        result[key] = _project(value, document, registry, reference_stack)

    if "enum" in result and "type" not in result:
        result["type"] = _enum_type(result["enum"])
    return result


def _resolve_reference(
    reference: str,
    document: Mapping[str, object],
    registry: Mapping[str, Mapping[str, object]],
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    if reference.startswith("#/"):
        target: object = document
        for encoded in reference[2:].split("/"):
            token = encoded.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, Mapping) or token not in target:
                raise CodexOutputSchemaError(f"unresolvable local $ref: {reference}")
            target = target[token]
        if not isinstance(target, Mapping):
            raise CodexOutputSchemaError(f"$ref target is not a schema: {reference}")
        return target, document
    target_document = registry.get(reference)
    if target_document is None:
        raise CodexOutputSchemaError(f"unresolvable external $ref: {reference}")
    return target_document, target_document


def _assert_strict_objects(node: object, path: tuple[str | int, ...]) -> None:
    if isinstance(node, list):
        for index, item in enumerate(node):
            _assert_strict_objects(item, (*path, index))
        return
    if not isinstance(node, Mapping):
        return
    if node.get("type") == "object":
        properties = node.get("properties")
        required = node.get("required")
        if not isinstance(properties, Mapping):
            raise CodexOutputSchemaError(f"object has no properties at {path}")
        if node.get("additionalProperties") is not False:
            raise CodexOutputSchemaError(
                f"object is not closed with additionalProperties=false at {path}"
            )
        if not isinstance(required, list) or set(required) != set(properties):
            raise CodexOutputSchemaError(
                f"object properties are not all required at {path}"
            )
    for key, value in node.items():
        _assert_strict_objects(value, (*path, key))


def _enum_type(value: object) -> str:
    if not isinstance(value, list) or not value:
        raise CodexOutputSchemaError("enum must be a non-empty list")
    types = {_json_type(item) for item in value}
    if len(types) != 1:
        raise CodexOutputSchemaError("mixed-type enum cannot be projected")
    return types.pop()


def _json_type(value: object) -> str:
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if value is None:
        return "null"
    raise CodexOutputSchemaError(f"unsupported enum value type: {type(value).__name__}")


def _mapping_copy(value: Mapping[str, object]) -> dict[str, object]:
    return {str(key): deepcopy(item) for key, item in value.items()}
