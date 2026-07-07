"""
MCP tool: read_file

Reads the content of a file inside the sandbox. Returns raw bytes for binary
files or decoded text for text files. The document node uses this to pass
content to pymupdf4llm / markitdown for structured extraction.

ToolAnnotations:
  readOnlyHint: true
  destructiveHint: false
  idempotentHint: true
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from echolocate.mcp_server.sandbox import SandboxViolation, resolve_and_check, safe_open

# Maximum bytes to return in a single read call. Larger files are chunked
# by the document node via EventsCompactionConfig.
MAX_READ_BYTES = 2 * 1024 * 1024  # 2 MB


def read_file(
    sandbox_root: Path,
    path: str,
    *,
    encoding: str = "utf-8",
    chunk_start: int = 0,
    chunk_size: Optional[int] = None,
) -> dict:
    """
    Read the content of a file at *path* (sandbox-relative).

    Args:
        sandbox_root: Absolute path to the sandbox directory.
        path: Sandbox-relative path to the file.
        encoding: Text encoding (for text files). "binary" returns hex.
        chunk_start: Byte offset to start reading from (for chunked reads).
        chunk_size: Number of bytes to read. Defaults to MAX_READ_BYTES.

    Returns:
        Dict with keys:
          - path: sandbox-relative path (normalized)
          - content: file text (or hex string for binary)
          - encoding: encoding used
          - chunk_start: byte offset of this chunk
          - chunk_end: byte offset of end of chunk
          - total_bytes: total file size
          - truncated: True if the file was larger than one chunk
    """
    chunk_size = min(chunk_size or MAX_READ_BYTES, MAX_READ_BYTES)

    # Validate the path — resolve_and_check raises SandboxViolation if
    # the path escapes the sandbox
    validated = resolve_and_check(path, sandbox_root, must_exist=True)

    total_bytes = validated.stat().st_size

    with safe_open(path, sandbox_root, mode="rb") as f:
        f.seek(chunk_start)
        raw = f.read(chunk_size)

    chunk_end = chunk_start + len(raw)
    truncated = chunk_end < total_bytes

    if encoding == "binary":
        content = raw.hex()
    else:
        content = raw.decode(encoding, errors="replace")

    return {
        "path": path.replace("\\", "/"),
        "content": content,
        "encoding": encoding,
        "chunk_start": chunk_start,
        "chunk_end": chunk_end,
        "total_bytes": total_bytes,
        "truncated": truncated,
    }
