"""Fail-closed access to immutable resources shipped in the product wheel."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path, PurePosixPath


class ProductResourceError(RuntimeError):
    """A required installed product resource is unavailable or unsafe."""


def product_resource_text(relative_path: str) -> str:
    """Read one allowlisted package resource without consulting the target."""

    resource = _resource(relative_path)
    try:
        return resource.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ProductResourceError(
            f"packaged resource cannot be read: {relative_path}"
        ) from error


def product_resource_path(relative_path: str) -> Path:
    """Return a persistent filesystem path from a normally installed wheel."""

    resource = _resource(relative_path)
    path = Path(str(resource))
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ProductResourceError(
            f"packaged resource has no persistent installed path: {relative_path}"
        ) from error
    if not resolved.is_file():
        raise ProductResourceError(
            f"packaged resource is not a regular file: {relative_path}"
        )
    return resolved


def product_schema_directory() -> Path:
    """Resolve the installed schema directory used by existing validators."""

    return product_resource_path("schemas/user-story.schema.json").parent


def _resource(relative_path: str):
    candidate = PurePosixPath(relative_path)
    if (
        not relative_path
        or str(candidate) != relative_path
        or candidate.is_absolute()
        or candidate.parts[0] not in {"docs", "roles", "schemas"}
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ProductResourceError(f"invalid packaged resource path: {relative_path}")
    resource = files("agentic_engineering_os.resources").joinpath(*candidate.parts)
    try:
        if not resource.is_file():
            raise ProductResourceError(
                f"packaged resource is unavailable: {relative_path}"
            )
    except OSError as error:
        raise ProductResourceError(
            f"packaged resource cannot be inspected: {relative_path}"
        ) from error
    return resource
