"""
MCP tool: open_file

Opens a file or folder using the system's default application. This is a
NON-DESTRUCTIVE action — no confirmation required (FR-12).

ToolAnnotations:
  readOnlyHint: true   — does not modify any file
  destructiveHint: false
  idempotentHint: true — opening the same file repeatedly has the same effect
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from echolocate.mcp_server.sandbox import SandboxViolation, resolve_and_check


def open_file(sandbox_root: Path, path: str) -> dict:
    """
    Open the file or directory at *path* (sandbox-relative) using the
    system's default application.

    This is the ONLY place in EchoLocate that spawns a subprocess — and it
    does so with a fixed, allowlisted platform command (os.startfile on
    Windows, xdg-open on Linux, open on macOS), never with an LLM-generated
    command string.

    Args:
        sandbox_root: Absolute path to the sandbox directory.
        path: Sandbox-relative path to open.

    Returns:
        Dict with keys: path, action, outcome.
    """
    validated = resolve_and_check(path, sandbox_root, must_exist=True)

    try:
        if sys.platform == "win32":
            # os.startfile is Windows-only and uses ShellExecuteEx — the
            # OS determines the default handler, not us. No shell=True needed.
            os.startfile(str(validated))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(validated)], check=True)
        else:
            # Linux — xdg-open delegates to the desktop environment's handler
            subprocess.run(["xdg-open", str(validated)], check=True)
    except Exception as exc:
        return {
            "path": path.replace("\\", "/"),
            "action": "open",
            "outcome": "error",
            "error": str(exc),
        }

    return {
        "path": path.replace("\\", "/"),
        "action": "open",
        "outcome": "success",
    }
