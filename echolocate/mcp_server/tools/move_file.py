"""
MCP tool: move_file

Moves (renames) a file inside the sandbox from source to destination. This is
a DESTRUCTIVE action — confirmation via ToolConfirmation is required BEFORE
this tool is called (FR-13). The system executor node is responsible for
triggering that confirmation; this tool trusts that the gate has already
passed and records it in the audit log.

ToolAnnotations:
  readOnlyHint: false
  destructiveHint: true
  idempotentHint: false
"""
from __future__ import annotations

import shutil
from pathlib import Path

from echolocate.mcp_server.sandbox import SandboxViolation, resolve_and_check


def move_file(
    sandbox_root: Path,
    source: str,
    destination: str,
    *,
    session_id: str = "unknown",
    confirmation_result: str = "confirmed",
) -> dict:
    """
    Move *source* to *destination* (both sandbox-relative paths).

    PRECONDITION: The calling node MUST have invoked tool_context.request_confirmation()
    and received an affirmative ToolConfirmation before calling this function.
    This function does NOT perform the confirmation itself — that responsibility
    belongs to the system executor node (Architecture Section 4.3).

    Destination constraints (Architecture Section 4.4):
    - The destination's PARENT DIRECTORY must already exist inside the sandbox.
    - EchoLocate does not auto-create destination directories from voice commands.

    Args:
        sandbox_root: Absolute path to the sandbox directory.
        source: Sandbox-relative source path.
        destination: Sandbox-relative destination path.
        session_id: Current session ID for audit logging.
        confirmation_result: Should be "confirmed" when called normally.

    Returns:
        Dict with keys: source, destination, outcome.
    """
    # Validate source — must exist
    validated_source = resolve_and_check(source, sandbox_root, must_exist=True)

    # Validate destination — parent must exist, file may or may not exist
    validated_dest = resolve_and_check(destination, sandbox_root, must_exist=False)

    # Extra check: destination parent must exist (no silent mkdir)
    if not validated_dest.parent.exists():
        raise SandboxViolation(
            f"destination directory does not exist: {str(validated_dest.parent)!r}. "
            f"EchoLocate does not create directories automatically."
        )

    # Perform the move
    shutil.move(str(validated_source), str(validated_dest))

    return {
        "source": source.replace("\\", "/"),
        "destination": destination.replace("\\", "/"),
        "resolved_source": str(validated_source),
        "resolved_destination": str(validated_dest),
        "outcome": "success",
        "confirmation_result": confirmation_result,
    }
