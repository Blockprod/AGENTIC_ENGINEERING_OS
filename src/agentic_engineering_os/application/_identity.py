"""Minimal normalization for the reserved Codex identity."""

from unicodedata import normalize


_CODEX_IDENTITY = "codex"


def is_codex_identity(identity: object) -> bool:
    """Return whether an identity uses the reserved Codex prefix."""

    prefix, _, _ = _identity_parts(identity)
    return prefix == _CODEX_IDENTITY


def has_attributable_codex_role(identity: object) -> bool:
    """Return whether Codex has a non-empty explicit role."""

    prefix, separator, role = _identity_parts(identity)
    return prefix == _CODEX_IDENTITY and separator and bool(role)


def is_attributable_non_codex_identity(identity: object) -> bool:
    """Return whether a non-Codex actor identity is present and attributable."""

    if not isinstance(identity, str) or not normalize("NFKC", identity).strip():
        return False
    return not is_codex_identity(identity)


def _identity_parts(identity: object) -> tuple[str, bool, str]:
    if not isinstance(identity, str):
        return "", False, ""
    normalized = normalize("NFKC", identity).strip()
    prefix, separator, role = normalized.partition("/")
    return prefix.strip().casefold(), bool(separator), role.strip()
