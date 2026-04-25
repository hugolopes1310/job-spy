"""Tests for cv_parser error handling.

Run from the repo root:
    PYTHONPATH=. python app/lib/test_cv_parser.py
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from app.lib import cv_parser  # noqa: E402
from app.lib.cv_parser import (  # noqa: E402
    CorruptedFileError,
    EmptyFileError,
    EncryptedPDFError,
    FileTooLargeError,
    ImageOnlyPDFError,
    UnsupportedFormatError,
    parse_cv,
)


# ---------------------------------------------------------------------------
# Trivial cases.
# ---------------------------------------------------------------------------
def test_empty_file():
    raised = False
    try:
        parse_cv(b"", "cv.pdf")
    except EmptyFileError:
        raised = True
    assert raised
    print("[OK] empty file → EmptyFileError")


def test_too_large():
    huge = b"x" * (cv_parser.MAX_FILE_BYTES + 1)
    raised = False
    try:
        parse_cv(huge, "cv.pdf")
    except FileTooLargeError as e:
        raised = True
        assert "Mo" in str(e), str(e)
    assert raised
    print("[OK] too large → FileTooLargeError with size in message")


def test_unsupported_extension():
    raised = False
    try:
        parse_cv(b"some bytes", "cv.exe")
    except UnsupportedFormatError as e:
        raised = True
        assert ".exe" in str(e), str(e)
    assert raised
    print("[OK] .exe → UnsupportedFormatError")


def test_doc_rejected_specifically():
    """`.doc` (binary) is opaque to python-docx — bail with a clear message."""
    raised = False
    try:
        parse_cv(b"any bytes", "cv.doc")
    except UnsupportedFormatError as e:
        raised = True
        assert "doc" in str(e).lower() and "docx" in str(e).lower(), str(e)
    assert raised
    print("[OK] .doc → UnsupportedFormatError suggesting .docx")


def test_txt_works():
    out = parse_cv("Hello, world.\n\nLine 2".encode("utf-8"), "cv.txt")
    assert "Hello, world." in out
    assert "Line 2" in out
    print("[OK] .txt parses fine")


def test_md_works():
    out = parse_cv(b"# Title\n\nContent here", "notes.md")
    assert "Content here" in out
    print("[OK] .md parses fine")


# ---------------------------------------------------------------------------
# PDF edge cases.
# ---------------------------------------------------------------------------
def test_corrupted_pdf():
    """A binary blob that doesn't even start with %PDF should fail cleanly."""
    raised = False
    try:
        parse_cv(b"\x00\x01\x02\x03 not a pdf at all", "cv.pdf")
    except CorruptedFileError as e:
        raised = True
        assert str(e), "expected a non-empty error message"
    assert raised
    print("[OK] corrupted PDF → CorruptedFileError")


def test_encrypted_pdf():
    """Build a real encrypted PDF using pypdf, then assert we detect it."""
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        print("[SKIP] pypdf not installed — encrypted PDF check skipped")
        return

    # Create a tiny PDF in memory (one blank page).
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt("hunter2")
    buf = io.BytesIO()
    writer.write(buf)
    raised = False
    try:
        parse_cv(buf.getvalue(), "secret.pdf")
    except EncryptedPDFError as e:
        raised = True
        assert "mot de passe" in str(e).lower(), str(e)
    assert raised, "expected EncryptedPDFError on a password-protected PDF"
    print("[OK] password-protected PDF → EncryptedPDFError")


def test_image_only_pdf():
    """A PDF with no extractable text raises ImageOnlyPDFError.

    We build a one-page blank PDF (no fonts, no text content stream) — pypdf
    extracts the empty string, which our parser treats as image-only.
    """
    try:
        from pypdf import PdfWriter
    except ImportError:
        print("[SKIP] pypdf not installed — image-only PDF check skipped")
        return
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    raised = False
    try:
        parse_cv(buf.getvalue(), "scan.pdf")
    except ImageOnlyPDFError as e:
        raised = True
        assert "image" in str(e).lower() or "scanné" in str(e).lower() or "scanne" in str(e).lower(), str(e)
    assert raised, "expected ImageOnlyPDFError on a blank/image-only PDF"
    print("[OK] image-only PDF → ImageOnlyPDFError")


# ---------------------------------------------------------------------------
# DOCX edge cases.
# ---------------------------------------------------------------------------
def test_corrupted_docx():
    """Random bytes with a .docx extension → CorruptedFileError."""
    raised = False
    try:
        parse_cv(b"not a docx file at all", "cv.docx")
    except CorruptedFileError as e:
        raised = True
        assert str(e)
    assert raised
    print("[OK] garbled DOCX → CorruptedFileError")


# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_empty_file()
    test_too_large()
    test_unsupported_extension()
    test_doc_rejected_specifically()
    test_txt_works()
    test_md_works()
    test_corrupted_pdf()
    test_encrypted_pdf()
    test_image_only_pdf()
    test_corrupted_docx()
    print("\nAll cv_parser tests passed.")
