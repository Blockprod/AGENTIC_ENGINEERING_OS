"""Minimal canonicalization for attributable Human and reserved Codex identities."""

from dataclasses import dataclass
from unicodedata import category, normalize


_CODEX_IDENTITY = "codex"


@dataclass(frozen=True, slots=True)
class _CanonicalIdentity:
    actor: str
    comparison_actor: str
    separator: bool
    role: str


def is_codex_identity(identity: object) -> bool:
    """Return whether an identity uses the reserved Codex prefix."""

    canonical = _canonical_identity(identity)
    return (
        canonical is not None
        and canonical.comparison_actor == _CODEX_IDENTITY
    )


def has_attributable_codex_role(identity: object) -> bool:
    """Return whether Codex has a non-empty explicit role."""

    canonical = _canonical_identity(identity)
    return (
        canonical is not None
        and canonical.comparison_actor == _CODEX_IDENTITY
        and canonical.separator
        and _has_attributable_text(canonical.role)
    )


def is_attributable_human_identity(identity: object) -> bool:
    """Return whether an explicit, meaningful, non-reserved actor is present."""

    canonical = _canonical_identity(identity)
    return (
        canonical is not None
        and canonical.comparison_actor != _CODEX_IDENTITY
        and _has_attributable_text(canonical.actor)
    )


def _canonical_identity(identity: object) -> _CanonicalIdentity | None:
    if not isinstance(identity, str):
        return None
    normalized = normalize("NFKC", identity).strip()
    actor, separator, role = normalized.partition("/")
    actor = actor.strip()
    return _CanonicalIdentity(
        actor=actor,
        comparison_actor=_without_format_characters(actor).casefold(),
        separator=bool(separator),
        role=role.strip(),
    )


def _has_attributable_text(value: str) -> bool:
    visible = _without_format_characters(value).strip()
    return bool(visible) and any(character.isalnum() for character in visible)


def _without_format_characters(value: str) -> str:
    return "".join(character for character in value if category(character) != "Cf")
