"""
EchoLocate — DOCX/PPTX/TXT extraction via markitdown.

markitdown (Microsoft, MIT license) converts Office Open XML formats to
structured Markdown. Offline, lightweight, no OCR/ML dependency.

Handles:
  - .docx (Word documents)
  - .pptx (PowerPoint presentations)
  - .txt  (plain text, no-op conversion)
  - .md   (Markdown, returned as-is)

Architecture Section 4.3 explains the extraction choice.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional
import re
import zipfile
import xml.etree.ElementTree as ET


def extract_document(
    file_path: Path,
    *,
    max_chars: Optional[int] = None,
) -> str:
    """
    Extract text from a DOCX, PPTX, TXT, or MD file as Markdown.

    Args:
        file_path: Absolute path to the file (already validated by sandbox).
        max_chars: Truncate output to this many characters.

    Returns:
        Markdown string representation of the document content.
        Returns an empty string if extraction yields nothing.
    """
    suffix = file_path.suffix.lower()

    # Plain text files — just read directly
    if suffix in {".txt", ".md"}:
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if max_chars:
                content = content[:max_chars]
            return content
        except OSError as exc:
            return f"[Text file read error: {exc}]"

    # Office formats — use markitdown
    try:
        from markitdown import MarkItDown  # type: ignore
    except ImportError:
        if suffix == ".docx":
            return _extract_docx_basic(file_path, max_chars=max_chars)
        raise ImportError(
            "markitdown is required for PPTX extraction. "
            "Install with: pip install markitdown"
        )

    try:
        md = MarkItDown()
        result = md.convert(str(file_path))
        content = result.text_content if hasattr(result, "text_content") else str(result)

        if not content.strip():
            return ""

        if max_chars:
            content = content[:max_chars]

        return content

    except Exception as exc:
        if suffix == ".docx":
            fallback = _extract_docx_basic(file_path, max_chars=max_chars)
            if fallback.strip():
                return fallback
        return f"[DOCX/PPTX extraction error: {exc}]"


def get_parser_for(file_path: Path):
    """Return the appropriate extraction function for the given file type."""
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        from echolocate.parsers.pdf_parser import extract_pdf
        return extract_pdf
    elif suffix in {".docx", ".pptx", ".txt", ".md"}:
        return extract_document
    else:
        return None


def _extract_docx_basic(file_path: Path, *, max_chars: Optional[int] = None) -> str:
    """Minimal dependency-free DOCX text extraction fallback."""
    try:
        with zipfile.ZipFile(file_path) as z:
            xml_bytes = z.read("word/document.xml")
    except Exception as exc:
        return f"[DOCX extraction error: {exc}]"

    try:
        root = ET.fromstring(xml_bytes)
        paragraphs = []
        ns_text_suffix = "}t"
        ns_para_suffix = "}p"
        for para in root.iter():
            if not para.tag.endswith(ns_para_suffix):
                continue
            chunks = []
            for node in para.iter():
                if node.tag.endswith(ns_text_suffix) and node.text:
                    chunks.append(node.text)
            text = "".join(chunks).strip()
            if text:
                paragraphs.append(text)
        content = "\n".join(paragraphs)
    except Exception:
        raw = xml_bytes.decode("utf-8", errors="ignore")
        content = " ".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", raw, flags=re.DOTALL))
        content = re.sub(r"<[^>]+>", "", content)

    if max_chars:
        content = content[:max_chars]
    return content
