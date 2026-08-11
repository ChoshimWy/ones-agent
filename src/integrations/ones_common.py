"""Shared safety helpers for ONES integration paths."""

from __future__ import annotations

import re
from urllib.parse import quote


_WIKI_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


def validate_wiki_segment(value: object, *, label: str) -> str:
    """Return a safe ONES Wiki path segment or fail without echoing input."""
    if not isinstance(value, str) or _WIKI_SEGMENT_PATTERN.fullmatch(value) is None:
        raise ValueError(f"Invalid ONES Wiki {label} identifier")
    return value


def quote_wiki_segment(value: object, *, label: str) -> str:
    """Validate and percent-encode an ONES Wiki path segment."""
    return quote(validate_wiki_segment(value, label=label), safe="")


__all__ = ["quote_wiki_segment", "validate_wiki_segment"]
