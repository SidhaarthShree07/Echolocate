"""
MCP tool: search_files

Searches the sandbox directory by filename fragment, file type, modification
date, and/or content keyword. Returns a ranked list of matches, best-match
first.

Backend selection:
  1. echolocate.mcp_server.index (local SQLite + FTS5 trigram) — the primary
     search engine. It handles full-drive indexing in a background thread,
     providing 0.01s retrieval times with highly accurate fuzzy matching.

See index.py's module docstring for the full rationale — this design exists
specifically because reinventing full-drive indexing at the application level
(using SQLite) guarantees zero third-party dependencies and no COM/registry 
flakiness on modern Windows machines.

ToolAnnotations:
  readOnlyHint: true
  destructiveHint: false
  idempotentHint: true (within Windows' own indexing cadence, or the local
  index's real-time-watcher/periodic-refresh cadence — see index.py).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from echolocate.mcp_server.index import ensure_built


def search_files(
    sandbox_root: Path,
    *,
    filename_fragment: Optional[str] = None,
    file_type: Optional[str] = None,        # extension without dot, e.g. "pdf"
    modified_date: Optional[str] = None,    # ISO date string "YYYY-MM-DD"
    content_keyword: Optional[str] = None,  # basic text search for TXT/MD content
    max_results: int = 10,
) -> list[dict]:
    """
    Search the sandbox directory for files matching the given filters.

    Signature is unchanged from earlier versions, so file_search.py and
    file_resolution.py need no changes — only the backend selection above
    them changed.

    Returns:
        List of dicts with keys: name, path (sandbox-relative), size_bytes,
        modified_iso, file_type, match_score. Sorted best-match-first.
    """
    if _is_drive_root(sandbox_root):
        print(f"[search_files] Using custom SQLite full-drive index for {sandbox_root}. "
              f"This will build in the background and may take a moment for the first run, "
              f"but searches will be instant thereafter.")

    index = ensure_built(sandbox_root)
    return index.search(
        filename_fragment=filename_fragment,
        file_type=file_type,
        modified_date=modified_date,
        content_keyword=content_keyword,
        max_results=max_results,
    )


def _is_drive_root(path: Path) -> bool:
    resolved = path.resolve()
    return resolved == Path(resolved.anchor)


def is_still_indexing(sandbox_root: Path) -> bool:
    """
    True if the currently-active backend's results might be incomplete
    right now -- used to attach an honest caveat to a resolution that
    happened to land while the index was still catching up (this is
    exactly what happened in the transcript that motivated this function:
    a fresh D:\\ scan had JUST started, so a single-candidate "confident"
    match may only have been the sole copy found SO FAR, not the only copy
    that exists). Windows Search's own background indexing progress isn't
    cheaply introspectable from here, so this only reports True for the
    local SQLite index backend -- a reasonable, honest scope: local
    indexing has a clear start/end this process controls, Windows' own
    indexer is the OS's concern and callers already know registering a new
    scope means "results may be incomplete for now" from ensure_indexed()'s
    log message.
    """
    try:
        from echolocate.mcp_server.index import get_index
        idx = get_index(sandbox_root)
        return bool(idx.is_building)
    except Exception:
        return False
