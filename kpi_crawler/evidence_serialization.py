"""Canonical text serialization of evidence, one deterministic function per evidence_type.

The canonical text is the minimal, self-contained representation of an evidence
item used for downstream semantic consumers (for example, embeddings). It is a
pure function of the evidence's own fields: no source, document, or KPI-specific
configuration, and no relevance ranking. It supplements, and never replaces, the
richer stored payload (`supporting_context` and `structure`).
"""

from typing import Any, Callable


CANONICAL_SERIALIZER_VERSION = "1"

_SerializerFn = Callable[[str, str, str, dict[str, Any]], str]


def _prefixed(section: str | None, body: str) -> str:
    return f"{section}: {body}" if section else body


def _serialize_text(claim: str, value: str, supporting_context: str, structure: dict[str, Any]) -> str:
    if structure.get("role") == "heading":
        return supporting_context
    return _prefixed(structure.get("section"), supporting_context)


def _serialize_table(claim: str, value: str, supporting_context: str, structure: dict[str, Any]) -> str:
    return _prefixed(structure.get("section"), f"Table — {supporting_context}")


def _serialize_visual(claim: str, value: str, supporting_context: str, structure: dict[str, Any]) -> str:
    return _prefixed(structure.get("section"), supporting_context)


_SERIALIZERS: dict[str, _SerializerFn] = {
    "text": _serialize_text,
    "table": _serialize_table,
    "visual": _serialize_visual,
}


def serialize_evidence(
    evidence_type: str,
    claim: str,
    value: str,
    supporting_context: str,
    structure: dict[str, Any],
) -> str:
    """Produce the canonical embedding text for one evidence item."""
    try:
        serializer = _SERIALIZERS[evidence_type]
    except KeyError:
        raise ValueError(f"no canonical serializer for evidence_type {evidence_type!r}") from None
    return serializer(claim, value, supporting_context, structure)
