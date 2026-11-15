"""Conservative text normalization (SPEC.md §3).

Comparison keys only. Original text is always preserved by callers. Identifier
normalization keeps punctuation; the loose form is used solely to *suggest*
suspected duplicates, never to merge identities.
"""

from __future__ import annotations

import re
import unicodedata

_WS = re.compile(r"\s+")


def norm_text(value: str | None) -> str:
    if value is None:
        return ""
    return _WS.sub(" ", unicodedata.normalize("NFKC", value)).strip().casefold()


def norm_identifier(value: str | None) -> str:
    if value is None:
        return ""
    return _WS.sub(" ", unicodedata.normalize("NFKC", value)).strip().upper()


def loose_identifier(value: str | None) -> str:
    """Alphanumerics only. For suspected-duplicate warnings, not identity."""
    return re.sub(r"[^0-9A-Z]", "", norm_identifier(value))
