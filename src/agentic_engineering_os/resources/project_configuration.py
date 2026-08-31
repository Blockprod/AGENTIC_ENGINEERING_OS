"""Installed resource access for the project configuration contract."""

from __future__ import annotations

from .product import ProductResourceError, product_resource_text


def project_configuration_schema_text() -> str:
    """Read the packaged P5.2 schema without consulting a source checkout."""

    return product_resource_text("schemas/project-configuration.schema.json")
