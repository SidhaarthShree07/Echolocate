"""
EchoLocate — PDF text extraction via pymupdf4llm.

Uses pymupdf4llm instead of raw pdftotext-style extraction because:
  - Multi-column layouts are read in correct order (not left-column then
    right-column text mangled together)
  - Headers, bold text, and tables are preserved as Markdown structure
  - Reading order is preserved even for complex academic/report PDFs
  - Output is Markdown that a 4B model can reason over more accurately
    than a flat text blob

Architecture Section 4.3 explains the extraction choice.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def extract_pdf(
    file_path: Path,
    *,
    max_chars: Optional[int] = None,
    page_range: Optional[tuple[int, int]] = None,
) -> str:
    """
    Extract text from a PDF file as structured Markdown.

    Args:
        file_path: Absolute path to the PDF file (already validated by sandbox).
        max_chars: Truncate output to this many characters. None = no limit.
                   Used when pre-chunking before EventsCompactionConfig.
        page_range: Optional (start, end) page indices (0-based, inclusive).
                    None = all pages.

    Returns:
        Markdown string with headers, tables, and reading order preserved.
        Returns an empty string if extraction yields nothing (e.g. image-only PDF).
    """
    try:
        import pymupdf4llm  # type: ignore
    except ImportError:
        raise ImportError(
            "pymupdf4llm is required for PDF extraction. "
            "Install with: pip install pymupdf4llm"
        )

    try:
        kwargs: dict = {"show_progress": False}
        if page_range is not None:
            start, end = page_range
            kwargs["pages"] = list(range(start, end + 1))

        md_text: str = pymupdf4llm.to_markdown(str(file_path), **kwargs)

        if not md_text.strip():
            return ""

        if max_chars:
            md_text = md_text[:max_chars]

        return md_text

    except Exception as exc:
        # Surface extraction errors as content with a clear marker so the
        # Document node can speak a sensible fallback instead of crashing
        return f"[PDF extraction error: {exc}]"


def is_image_only_pdf(file_path: Path) -> bool:
    """
    Heuristic check: returns True if the PDF has no extractable text
    (likely a scanned image). Used to give the user a clear spoken message
    rather than returning an empty or near-empty Markdown string.
    """
    try:
        import fitz  # PyMuPDF, available via pymupdf4llm's dependency  # type: ignore
        doc = fitz.open(str(file_path))
        total_chars = sum(len(page.get_text()) for page in doc)
        doc.close()
        return total_chars < 100  # fewer than 100 chars total = likely image-only
    except Exception:
        return False
