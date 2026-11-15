"""PDF intake validation before any model call (SPEC.md §9, §14)."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from pypdf import PdfReader
from pypdf.errors import PdfReadError


class InputRejected(ValueError):
    """Actionable upload error. No model call is made."""


@dataclass(frozen=True)
class DocumentMeta:
    sha256: str
    size_bytes: int
    page_count: int


def validate_pdf(data: bytes, *, max_bytes: int, max_pages: int) -> DocumentMeta:
    if not data:
        raise InputRejected("The file is empty.")
    if len(data) > max_bytes:
        raise InputRejected(f"The file is {len(data) / 1048576:.1f} MiB; the limit is {max_bytes / 1048576:.0f} MiB.")
    if not data.lstrip()[:5] == b"%PDF-":
        raise InputRejected("This is not a PDF file (missing %PDF signature).")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise InputRejected("The PDF is encrypted or password-protected. Ask the supplier for an unprotected copy.")
        pages = len(reader.pages)
    except InputRejected:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError) as exc:
        raise InputRejected(f"The PDF could not be read ({type(exc).__name__}); it may be corrupt.") from exc
    if pages == 0:
        raise InputRejected("The PDF has no pages.")
    if pages > max_pages:
        raise InputRejected(f"The PDF has {pages} pages; the supported maximum is {max_pages}.")
    return DocumentMeta(sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data), page_count=pages)
