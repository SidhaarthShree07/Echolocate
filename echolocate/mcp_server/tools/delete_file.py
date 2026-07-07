"""
MCP tool: delete_file

Permanently deletes a file inside the sandbox. This is a DESTRUCTIVE action —
confirmation via ToolConfirmation is REQUIRED before this tool is called.
The system executor node manages the confirmation gate.

ToolAnnotations:
  readOnlyHint: false
  destructiveHint: true
  idempotentHint: false
"""
from __future__ import annotations

import os
from pathlib import Path

from echolocate.mcp_server.sandbox import SandboxViolation, resolve_and_check


def delete_file(
    sandbox_root: Path,
    path: str,
    *,
    session_id: str = "unknown",
    confirmation_result: str = "confirmed",
) -> dict:
    """
    Delete the file at *path* (sandbox-relative). Directories are NOT deleted
    by this tool — only files. This prevents accidentally deleting an entire
    directory tree from a single voice command.

    PRECONDITION: The calling node MUST have invoked tool_context.request_confirmation()
    and received an affirmative ToolConfirmation before calling this function.

    Args:
        sandbox_root: Absolute path to the sandbox directory.
        path: Sandbox-relative path to the file to delete.
        session_id: Current session ID for audit logging.
        confirmation_result: Should be "confirmed" when called normally.

    Returns:
        Dict with keys: path, outcome.
    """
    validated = resolve_and_check(path, sandbox_root, must_exist=True)

    # Safety: refuse to delete directories
    if validated.is_dir():
        raise SandboxViolation(
            f"delete_file refuses to delete directories — use a file path: {path!r}"
        )

    os.remove(str(validated))

    return {
        "path": path.replace("\\", "/"),
        "resolved_path": str(validated),
        "outcome": "success",
        "confirmation_result": confirmation_result,
    }
