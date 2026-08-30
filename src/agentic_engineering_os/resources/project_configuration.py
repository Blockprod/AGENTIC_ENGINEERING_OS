"""Installed resource access for the project configuration contract."""

from __future__ import annotations

from importlib.resources import files


class ProductResourceError(RuntimeError):
    """A required installed product resource is unavailable or unreadable."""


def project_configuration_schema_text() -> str:
    """Read the packaged P5.2 schema without consulting a source checkout."""

    resource = files("agentic_engineering_os.resources").joinpath(
        "schemas", "project-configuration.schema.json"
    )
    try:
        if not resource.is_file():
            raise ProductResourceError(
                "packaged project configuration schema is unavailable"
            )
        return resource.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ProductResourceError(
            "packaged project configuration schema cannot be read"
        ) from error
