"""
MCP tool: get_metadata

Returns metadata for a file or directory inside the sandbox without reading
its content. Used by the file search node to confirm dates/sizes before
presenting results to the user.

ToolAnnotations:
  readOnlyHint: true
  destructiveHint: false
  idempotentHint: true
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from echolocate.mcp_server.sandbox import SandboxViolation, resolve_and_check


def get_metadata(sandbox_root: Path, path: str) -> dict:
    """
    Return file or directory metadata for *path* (sandbox-relative).

    Returns:
        Dict with keys: name, path, is_dir, size_bytes, modified_iso,
        created_iso, file_type, exists.
    """
    validated = resolve_and_check(path, sandbox_root, must_exist=True)

    stat = validated.stat()
    is_dir = validated.is_dir()

    return {
        "name": validated.name,
        "path": path.replace("\\", "/"),
        "is_dir": is_dir,
        "size_bytes": stat.st_size if not is_dir else None,
        "modified_iso": _iso(stat.st_mtime),
        "created_iso": _iso(stat.st_ctime),
        "file_type": validated.suffix.lstrip(".").lower() if not is_dir else None,
        "exists": True,
    }


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
