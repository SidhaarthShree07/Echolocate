"""
EchoLocate — shared fuzzy file-reference resolution.

DocumentNode (and any other node acting on a specific already-named file)
needs to turn a classifier's file_reference entity (e.g. "hello text file")
into an actual sandbox-relative path — the same problem FileSearchNode
solves via the indexed, word-boundary-scored search in file_search.py.
This module reuses those helpers (imported, not reimplemented) rather than
maintaining a second, competing matching implementation.

Two things this version fixes over the previous one:

1. No ambiguity handling. The prior resolve_fuzzy_file() picked a single
   best-guess path and returned it as a plain string — if two files share
   a name in different folders (which is exactly what happens once the
   sandbox root covers an entire drive), it silently picked one with no
   way to ask "which one?" and no way to tell the two apart even in an
   error message. FileResolution (below) carries an "ambiguous" status
   with path-qualified candidate labels instead.

2. No indexing-freshness awareness. A confident single-match resolution
   that happens while the background index is still mid-scan may just be
   "the only copy found SO FAR," not "the only copy that exists." This is
   flagged via FileResolution.possibly_incomplete so callers can add an
   honest caveat instead of presenting a possibly-premature answer as
   certain.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from echolocate.mcp_server.tools.search_files import search_files
from echolocate.nodes.file_search import (
    _apply_location_hint,
    _clean_reference,
    _clearly_better,
    _describe_location,
    _local_search,
)


@dataclass
class FileResolution:
    status: str  # "resolved" | "ambiguous" | "not_found"
    path: Optional[str] = None
    candidates: list = field(default_factory=list)      # raw result dicts, for ambiguous status
    possibly_incomplete: bool = False


def resolve_fuzzy_file(
    sandbox_root: Path,
    file_reference: Optional[str],
    location_hint: Optional[str] = None,
    file_type: Optional[str] = None,
) -> FileResolution:
    """
    Resolve a classifier-extracted file_reference to an actual
    sandbox-relative path. Tries, in order:

      1. Exact literal path — handles the case where file_reference already
         IS the real filename, or was already resolved to
         last_referenced_file by the caller.
      2. Indexed fuzzy search (word-boundary scoring from file_search.py),
         with location_hint applied as a filter when present, and query
         relaxation (word-by-word retry) if the full cleaned fragment
         matches nothing.

    Auto-resolves only on a strong, unambiguous match — same bar
    FileSearchNode uses for auto-committing. Genuinely ambiguous matches
    (including same-named files in different folders) come back as
    status="ambiguous" with path-qualified candidate info so the caller can
    ask a question that's actually answerable — "hello.txt in the root
    folder, or hello.txt in Echolocate/sandbox_root?" instead of "hello.txt
    or hello.txt?".
    """
    if not file_reference:
        return FileResolution(status="not_found")

    # Check if the reference itself is an existing file (relative or absolute) under the sandbox
    ref_path = Path(file_reference)
    if not ref_path.is_absolute():
        abs_candidate = (sandbox_root / ref_path).resolve()
    else:
        abs_candidate = ref_path.resolve()

    try:
        if abs_candidate.exists() and abs_candidate.is_file():
            # Security boundary: must be under sandbox_root
            try:
                rel_path = abs_candidate.relative_to(sandbox_root.resolve()).as_posix()
                return FileResolution(
                    status="resolved",
                    path=rel_path,
                    possibly_incomplete=False,
                )
            except ValueError:
                # Outside sandbox root - ignore for security
                pass
    except OSError:
        pass

    cleaned = _clean_reference(file_reference)
    try:
        results = search_files(sandbox_root, filename_fragment=cleaned, file_type=file_type, max_results=8)
    except Exception as exc:
        print(f"[FuzzyResolver] indexed search failed: {exc}")
        results = []
    if not results:
        results = _local_search(sandbox_root, filename_fragment=cleaned, file_type=file_type, max_results=8)

    if not results and cleaned and " " in cleaned:
        best_by_path: dict[str, dict] = {}
        for word in cleaned.split():
            if len(word) < 3:
                continue
            try:
                word_results = search_files(sandbox_root, filename_fragment=word, file_type=file_type, max_results=5)
            except Exception as exc:
                print(f"[FuzzyResolver] relaxed indexed search failed: {exc}")
                word_results = []
            if not word_results:
                word_results = _local_search(sandbox_root, filename_fragment=word, file_type=file_type, max_results=5)
            for r in word_results:
                existing = best_by_path.get(r["path"])
                if existing is None or r.get("match_score", 0) > existing.get("match_score", 0):
                    best_by_path[r["path"]] = r
        results = sorted(
            best_by_path.values(),
            key=lambda r: (r.get("match_score", 0), r.get("modified_iso", "")),
            reverse=True,
        )

    if not results:
        return FileResolution(status="not_found")

    results = _apply_location_hint(results, location_hint, file_reference)

    incomplete = _index_possibly_incomplete(sandbox_root)

    if len(results) == 1:
        match = results[0]
        if match.get("match_score", 0) >= 2:
            return FileResolution(status="resolved", path=match["path"], possibly_incomplete=incomplete)
        return FileResolution(status="ambiguous", candidates=[match], possibly_incomplete=incomplete)

    top, runner_up = results[0], results[1]
    if top.get("match_score", 0) >= 100 and runner_up.get("match_score", 0) >= 100:
        return FileResolution(status="ambiguous", candidates=results[:3], possibly_incomplete=incomplete)
    if _clearly_better(top, runner_up, file_reference):
        return FileResolution(status="resolved", path=top["path"], possibly_incomplete=incomplete)

    return FileResolution(status="ambiguous", candidates=results[:3], possibly_incomplete=incomplete)


def describe_candidates(candidates: list[dict]) -> list[str]:
    """Path-qualified labels for an ambiguous candidate list — used when
    presenting a clarification question. Always uses location context, not
    just bare names, since the whole point of calling this is that bare
    names weren't enough to disambiguate in the first place."""
    return [_describe_location(c["path"]) for c in candidates]


def _index_possibly_incomplete(sandbox_root: Path) -> bool:
    try:
        from echolocate.mcp_server.tools.search_files import is_still_indexing
        return is_still_indexing(sandbox_root)
    except Exception:
        return False
