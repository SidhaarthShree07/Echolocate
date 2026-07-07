"""
EchoLocate — append-only JSONL audit logger.

Every MCP tool invocation is recorded here before the operation runs and
updated after it completes. The log is written by the MCP server process
itself (not the orchestrator) so it remains authoritative even if the
orchestrator crashes mid-action.

Privacy rule (NFR-4): this log NEVER contains audio, transcripts, or
document text/content. Only action metadata: tool name, paths, outcome,
session ID, confirmation status. If a field could contain arbitrary file
content, it does not belong here.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _default_log_path() -> Path:
    return Path.home() / ".echolocate" / "audit.log"


class AuditLogger:
    """
    Append-only JSONL audit logger.

    Each line is a complete JSON object. The file is append-only — lines are
    never modified or deleted. If the log directory doesn't exist it is
    created on first write.
    """

    def __init__(self, log_path: Optional[Path] = None) -> None:
        self._log_path = Path(log_path) if log_path else _default_log_path()
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        *,
        tool: str,
        args: dict,
        session_id: str,
        destructive: bool,
        confirmation_required: bool,
        confirmation_result: Optional[str] = None,  # "confirmed" | "denied" | None
        outcome: str,  # "success" | "rejected" | "error" | "cancelled"
        resolved_source_path: Optional[str] = None,
        resolved_dest_path: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """
        Write one audit record.

        Args:
            tool: MCP tool name (e.g. "move_file")
            args: Tool arguments as provided (sanitized — no file content)
            session_id: Current session identifier
            destructive: Whether this tool call is destructive (move/delete)
            confirmation_required: Whether confirmation was required
            confirmation_result: "confirmed", "denied", or None
            outcome: Final result of the operation
            resolved_source_path: Canonicalized source path (no content)
            resolved_dest_path: Canonicalized destination path (no content)
            error: Error message if outcome is "error" or "rejected"
        """
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "args": args,
            "resolved_source_path": resolved_source_path,
            "resolved_dest_path": resolved_dest_path,
            "destructive": destructive,
            "confirmation_required": confirmation_required,
            "confirmation_result": confirmation_result,
            "outcome": outcome,
            "session_id": session_id,
        }
        if error:
            record["error"] = error

        # Remove None values to keep log lines compact
        record = {k: v for k, v in record.items() if v is not None}

        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            # Audit log failure must not crash the operation — log to stderr
            # but continue. The operation itself is the higher priority.
            import sys
            print(
                f"[EchoLocate AUDIT ERROR] Could not write to {self._log_path}: "
                f"{record}",
                file=sys.stderr,
            )

    def read_recent(self, n: int = 20) -> list[dict]:
        """Return the last *n* audit records (most recent last)."""
        if not self._log_path.exists():
            return []
        with open(self._log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        records = []
        for line in lines[-n:]:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return records


# Module-level singleton — initialized once by server.py with the configured
# log path; nodes should never instantiate their own AuditLogger.
_logger: Optional[AuditLogger] = None


def init_logger(log_path: Optional[Path] = None) -> AuditLogger:
    """Initialize the module-level audit logger. Call once at server startup."""
    global _logger
    _logger = AuditLogger(log_path)
    return _logger


def get_logger() -> AuditLogger:
    """Return the module-level audit logger (must be initialized first)."""
    global _logger
    if _logger is None:
        _logger = AuditLogger()
    return _logger
