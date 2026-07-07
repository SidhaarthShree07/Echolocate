"""
EchoLocate — MCP filesystem server entry point.

This is the stdio subprocess launched by the ADK orchestrator via
StdioConnectionParams. It exposes exactly 6 allowlisted tools with accurate
ToolAnnotations, enforced via the sandbox module.

Launched as: python -m echolocate.mcp_server.server --sandbox-root <path>

The tool_filter parameter in McpToolset on the orchestrator side further
restricts which tools are visible to each specialist node:
  - File Search node:    search_files, read_file, get_metadata
  - Document node:       read_file, get_metadata
  - System Executor node: open_file, move_file, delete_file
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
    CallToolResult,
    ToolAnnotations,
)

from echolocate.mcp_server.audit import init_logger, get_logger
from echolocate.mcp_server.sandbox import SandboxViolation, resolve_and_check
from echolocate.mcp_server.tools.search_files import search_files
from echolocate.mcp_server.tools.read_file import read_file
from echolocate.mcp_server.tools.get_metadata import get_metadata
from echolocate.mcp_server.tools.open_file import open_file
from echolocate.mcp_server.tools.move_file import move_file
from echolocate.mcp_server.tools.delete_file import delete_file

import json


def build_server(sandbox_root: Path) -> Server:
    """Build and configure the MCP server with all tools registered."""
    server = Server("echolocate-filesystem")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="search_files",
                description=(
                    "Search the sandboxed directory by filename, file type, "
                    "modification date, and/or content keyword. Returns a ranked "
                    "list of matching files (most recent first). All paths in results "
                    "are sandbox-relative."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "filename_fragment": {
                            "type": "string",
                            "description": "Substring or glob pattern to match against filename.",
                        },
                        "file_type": {
                            "type": "string",
                            "description": "File extension without dot (e.g. 'pdf', 'txt', 'docx').",
                        },
                        "modified_date": {
                            "type": "string",
                            "description": "ISO date string 'YYYY-MM-DD'. Return files modified on this date.",
                        },
                        "content_keyword": {
                            "type": "string",
                            "description": "Keyword to search for in text file contents (TXT/MD only).",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum results to return (default 10).",
                            "default": 10,
                        },
                    },
                    "required": [],
                },
                annotations=ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                ),
            ),
            Tool(
                name="read_file",
                description=(
                    "Read the content of a file inside the sandbox. Supports chunked "
                    "reading for large files. Returns text content or hex for binary files."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Sandbox-relative path to the file.",
                        },
                        "encoding": {
                            "type": "string",
                            "description": "Text encoding (default 'utf-8'). Use 'binary' for hex output.",
                            "default": "utf-8",
                        },
                        "chunk_start": {
                            "type": "integer",
                            "description": "Byte offset to start reading from.",
                            "default": 0,
                        },
                        "chunk_size": {
                            "type": "integer",
                            "description": "Bytes to read (max 2MB).",
                        },
                    },
                    "required": ["path"],
                },
                annotations=ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                ),
            ),
            Tool(
                name="get_metadata",
                description=(
                    "Get metadata (name, size, dates, type) for a file or directory "
                    "inside the sandbox without reading its content."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Sandbox-relative path to the file or directory.",
                        },
                    },
                    "required": ["path"],
                },
                annotations=ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                ),
            ),
            Tool(
                name="open_file",
                description=(
                    "Open a file or folder using the system's default application. "
                    "Non-destructive — no confirmation required."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Sandbox-relative path to open.",
                        },
                    },
                    "required": ["path"],
                },
                annotations=ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                ),
            ),
            Tool(
                name="move_file",
                description=(
                    "Move a file from source to destination inside the sandbox. "
                    "DESTRUCTIVE — requires ToolConfirmation before calling. "
                    "Destination parent directory must already exist."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "description": "Sandbox-relative source path.",
                        },
                        "destination": {
                            "type": "string",
                            "description": "Sandbox-relative destination path.",
                        },
                        "session_id": {
                            "type": "string",
                            "description": "Current session ID for audit logging.",
                        },
                    },
                    "required": ["source", "destination"],
                },
                annotations=ToolAnnotations(
                    readOnlyHint=False,
                    destructiveHint=True,
                    idempotentHint=False,
                ),
            ),
            Tool(
                name="delete_file",
                description=(
                    "Permanently delete a file inside the sandbox. "
                    "DESTRUCTIVE — requires ToolConfirmation before calling. "
                    "Refuses to delete directories."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Sandbox-relative path to the file to delete.",
                        },
                        "session_id": {
                            "type": "string",
                            "description": "Current session ID for audit logging.",
                        },
                    },
                    "required": ["path"],
                },
                annotations=ToolAnnotations(
                    readOnlyHint=False,
                    destructiveHint=True,
                    idempotentHint=False,
                ),
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> CallToolResult:
        """Route a tool call to the appropriate implementation."""
        logger = get_logger()
        session_id = arguments.get("session_id", "unknown")

        try:
            if name == "search_files":
                result = search_files(
                    sandbox_root,
                    filename_fragment=arguments.get("filename_fragment"),
                    file_type=arguments.get("file_type"),
                    modified_date=arguments.get("modified_date"),
                    content_keyword=arguments.get("content_keyword"),
                    max_results=arguments.get("max_results", 10),
                )
                logger.log(
                    tool=name,
                    args={k: v for k, v in arguments.items() if k != "session_id"},
                    session_id=session_id,
                    destructive=False,
                    confirmation_required=False,
                    outcome="success",
                )
                return CallToolResult(
                    content=[TextContent(type="text", text=json.dumps(result))]
                )

            elif name == "read_file":
                result = read_file(
                    sandbox_root,
                    arguments["path"],
                    encoding=arguments.get("encoding", "utf-8"),
                    chunk_start=arguments.get("chunk_start", 0),
                    chunk_size=arguments.get("chunk_size"),
                )
                logger.log(
                    tool=name,
                    args={"path": arguments["path"]},
                    session_id=session_id,
                    destructive=False,
                    confirmation_required=False,
                    outcome="success",
                    resolved_source_path=result.get("path"),
                )
                return CallToolResult(
                    content=[TextContent(type="text", text=json.dumps(result))]
                )

            elif name == "get_metadata":
                result = get_metadata(sandbox_root, arguments["path"])
                logger.log(
                    tool=name,
                    args={"path": arguments["path"]},
                    session_id=session_id,
                    destructive=False,
                    confirmation_required=False,
                    outcome="success",
                )
                return CallToolResult(
                    content=[TextContent(type="text", text=json.dumps(result))]
                )

            elif name == "open_file":
                result = open_file(sandbox_root, arguments["path"])
                logger.log(
                    tool=name,
                    args={"path": arguments["path"]},
                    session_id=session_id,
                    destructive=False,
                    confirmation_required=False,
                    outcome=result.get("outcome", "success"),
                    error=result.get("error"),
                )
                return CallToolResult(
                    content=[TextContent(type="text", text=json.dumps(result))]
                )

            elif name == "move_file":
                result = move_file(
                    sandbox_root,
                    arguments["source"],
                    arguments["destination"],
                    session_id=session_id,
                    confirmation_result=arguments.get("confirmation_result", "confirmed"),
                )
                logger.log(
                    tool=name,
                    args={"source": arguments["source"], "destination": arguments["destination"]},
                    session_id=session_id,
                    destructive=True,
                    confirmation_required=True,
                    confirmation_result=arguments.get("confirmation_result", "confirmed"),
                    outcome=result.get("outcome", "success"),
                    resolved_source_path=result.get("resolved_source"),
                    resolved_dest_path=result.get("resolved_destination"),
                )
                return CallToolResult(
                    content=[TextContent(type="text", text=json.dumps(result))]
                )

            elif name == "delete_file":
                result = delete_file(
                    sandbox_root,
                    arguments["path"],
                    session_id=session_id,
                    confirmation_result=arguments.get("confirmation_result", "confirmed"),
                )
                logger.log(
                    tool=name,
                    args={"path": arguments["path"]},
                    session_id=session_id,
                    destructive=True,
                    confirmation_required=True,
                    confirmation_result=arguments.get("confirmation_result", "confirmed"),
                    outcome=result.get("outcome", "success"),
                    resolved_source_path=result.get("resolved_path"),
                )
                return CallToolResult(
                    content=[TextContent(type="text", text=json.dumps(result))]
                )

            else:
                raise ValueError(f"Unknown tool: {name!r}")

        except SandboxViolation as exc:
            logger.log(
                tool=name,
                args=arguments,
                session_id=session_id,
                destructive=name in {"move_file", "delete_file"},
                confirmation_required=name in {"move_file", "delete_file"},
                outcome="rejected",
                error=str(exc),
            )
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=json.dumps({"outcome": "rejected", "error": str(exc)})
                )],
                isError=True,
            )
        except Exception as exc:
            logger.log(
                tool=name,
                args=arguments,
                session_id=session_id,
                destructive=name in {"move_file", "delete_file"},
                confirmation_required=name in {"move_file", "delete_file"},
                outcome="error",
                error=str(exc),
            )
            return CallToolResult(
                content=[TextContent(
                    type="text",
                    text=json.dumps({"outcome": "error", "error": str(exc)})
                )],
                isError=True,
            )

    return server


async def main(sandbox_root: Path, log_path: Path | None = None) -> None:
    init_logger(log_path)
    server = build_server(sandbox_root)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _check_sandbox_root(sandbox_root: Path, allow_broad_root: bool) -> None:
    """
    Warn (and require explicit opt-in) for a sandbox root that's a drive or
    filesystem root, rather than silently accepting it.

    This used to be a hard sys.exit(1) block. It's now an opt-in gate
    instead: broad roots are a legitimate, supported configuration —
    echolocate.mcp_server.index builds a SQLite index (instead of walking
    the tree on every call) specifically so this stays fast, and
    automatically excludes OS-critical directories (Windows/, Program
    Files/, /etc, /proc, etc.) when the root is broad. What this guard still
    protects against is the *accidental* case — someone typing "D:" during
    setup without meaning to hand the whole drive over — by requiring a
    deliberate flag rather than accepting it silently.
    """
    resolved = Path(os.path.realpath(str(sandbox_root)))
    is_broad = resolved.parent == resolved or len(resolved.parts) <= 2

    if is_broad and not allow_broad_root:
        print(
            f"[FATAL] Sandbox root is a drive/filesystem root: {resolved}\n"
            f"        This is a supported configuration, but requires explicit\n"
            f"        opt-in so it can't happen by accident. Re-run with:\n"
            f"          --sandbox-root \"{resolved}\" --allow-broad-root\n"
            f"        Search stays fast at this scale via a maintained SQLite\n"
            f"        index (echolocate.mcp_server.index) rather than a live walk,\n"
            f"        and OS-critical folders are excluded from it automatically.\n",
            file=sys.stderr,
        )
        sys.exit(1)
    elif is_broad:
        print(
            f"[WARNING] Sandbox root is broad: {resolved} — every destructive "
            f"command's blast radius is this entire scope, not a small folder.",
            file=sys.stderr,
        )



def cli() -> None:
    parser = argparse.ArgumentParser(description="EchoLocate MCP Filesystem Server")
    parser.add_argument(
        "--sandbox-root",
        required=True,
        help="Absolute path to the sandbox directory.",
    )
    parser.add_argument(
        "--audit-log",
        default=None,
        help="Path to audit log file (default: ~/.echolocate/audit.log).",
    )
    parser.add_argument(
        "--allow-broad-root",
        action="store_true",
        help="Required to use a drive/filesystem root as --sandbox-root. "
             "Search stays fast via a maintained index; OS-critical "
             "directories are still excluded automatically.",
    )
    args = parser.parse_args()

    sandbox_root = Path(args.sandbox_root).expanduser().resolve()
    if not sandbox_root.exists():
        print(f"[ERROR] Sandbox root does not exist: {sandbox_root}", file=sys.stderr)
        sys.exit(1)

    _check_sandbox_root(sandbox_root, args.allow_broad_root)

    log_path = Path(args.audit_log).expanduser() if args.audit_log else None
    asyncio.run(main(sandbox_root, log_path))


if __name__ == "__main__":
    cli()

