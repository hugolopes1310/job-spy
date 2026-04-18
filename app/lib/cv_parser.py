"""CV / cover letter parsing — PDF and DOCX to plain text.

Intentionally simple: we trust the LLM downstream to understand structure,
so we just need clean-enough text (no section detection, no NER).
"""
from __future__ import annotations

import io
import re
from pathlib import Path


class UnsupportedFormatError(ValueError):
    """Raised when the uploaded file extension is not .pdf / .docx / .txt."""


def _clean(text: str) -> str:
    """Collapse whitespace, strip zero-width chars, normalize line breaks."""
    if not text:
        return ""
    # Remove common zero-width / bidi chars that mess up LLM tokenization.
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e\ufeff]", "", text)
    # Normalize line endings.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse 3+ newlines to 2 (preserve paragraph breaks).
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Collapse horizontal whitespace.
    text = re.sub(r"[ \t]+", " ", text)
    # Trim each line.
    text = "\n".join(line.strip() for line in text.split("\n"))
    return text.strip()


def _parse_pdf(file_bytes: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError(
            "pypdf missing — run `pip install pypdf` (it's in requirements.txt)"
        ) from e

    reader = PdfReader(io.BytesIO(file_bytes))
    parts: list[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            # Don't crash on one bad page — skip it.
            continue
    return "\n\n".join(parts)


def _parse_docx(file_bytes: bytes) -> str:
    try:
        from docx import Document
    except ImportError as e:
        raise RuntimeError(
            "python-docx missing — run `pip install python-docx`"
        ) from e

    doc = Document(io.BytesIO(file_bytes))
    parts: list[str] = []
    for para in doc.paragraphs:
        if para.text:
            parts.append(para.text)
    # Also pull text out of tables (CVs often use tables for layout).
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def parse_cv(file_bytes: bytes, filename: str) -> str:
    """Extract plain text from an uploaded CV / cover letter.

    Args:
        file_bytes: Raw file content.
        filename: Original name (used to detect extension).

    Returns:
        Cleaned plain text.

    Raises:
        UnsupportedFormatError: If the extension isn't .pdf / .docx / .txt.
    """
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext == "pdf":
        raw = _parse_pdf(file_bytes)
    elif ext in ("docx", "doc"):
        # .doc (binary old Word) isn't supported by python-docx; best-effort.
        raw = _parse_docx(file_bytes)
    elif ext in ("txt", "md"):
        raw = file_bytes.decode("utf-8", errors="replace")
    else:
        raise UnsupportedFormatError(
            f"Format .{ext} non supporté. Formats acceptés : PDF, DOCX, TXT."
        )
    return _clean(raw)


def parse_cv_from_path(path: str | Path) -> str:
    """Convenience wrapper for local CLI / smoke tests."""
    p = Path(path)
    return parse_cv(p.read_bytes(), p.name)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("usage: python -m app.lib.cv_parser <path/to/cv.pdf>")
        sys.exit(1)
    text = parse_cv_from_path(sys.argv[1])
    print(f"--- {len(text)} chars ---")
    print(text[:1500])
    if len(text) > 1500:
        print(f"\n... ({len(text) - 1500} more chars)")
