"""
EchoLocate — file search specialist node.

Receives resolved intent entities from the router and calls the MCP
search_files tool to find matching files inside the sandbox.

Node responsibilities (Architecture Section 4.3):
  - Build filters from entities (filename fragment, file type, date, keyword)
  - Call search_files via the MCP toolset
  - Rank results (match-quality first, recency-weighted tiebreak) in pure
    Python -- no LLM call for ranking
  - Location-aware disambiguation: if the user said "root"/"root folder" or
    named a specific folder, prefer/filter candidates in that location
    before falling back to match quality and path depth
  - If single clear match: return spoken result WITHOUT committing
    last_referenced_file until confirmed (see the file_confirmation
    pending_intent flow below -- committing on an unconfirmed guess was a
    real bug: it left the agent stuck on a wrong file with no way to
    recover by voice)
  - If multiple close matches, INCLUDING same-named files in different
    folders: ask the user to choose, using enough path context to actually
    distinguish them -- "hello.txt" vs "hello.txt" is not an answerable
    question
  - If zero matches: broaden search progressively, then report clearly

The MCP toolset for this node has tool_filter=["search_files", "read_file",
"get_metadata"] -- move_file and delete_file are not in scope here.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

from echolocate.state import ClassifierOutput, PendingIntent, SessionState

# Single source of truth for filler-word stripping -- deliberately broad
# (not minimal) because natural spoken phrasing includes a lot of filler
# ("which is in the root directory") that a narrow stop-word list lets
# through, producing a multi-word search fragment that matches nothing.
_STOP_WORDS = {
    "that", "the", "a", "an", "as", "my", "me", "please", "this", "these", "those",
    "pdf", "word", "doc", "docx", "document", "text",
    "report", "note", "notes", "file", "files",
    "which", "who", "what", "where", "when", "is", "are", "was", "were",
    "in", "on", "at", "of", "to", "for", "with", "from", "by",
    "root", "directory", "folder", "sandbox",
    "located", "saved", "called", "named", "titled",
    "it", "one", "some", "any",
    "android", "sdk", ".gradle", ".android", ".cargo", ".rustup", ".m2", ".nuget",
    "vendor", ".terraform", "anaconda3", "miniconda3",
}

# Phrases that mean "the copy directly in the sandbox root, not nested in
# a subfolder" -- checked as a deterministic fallback independent of
# whether the classifier's location_hint entity caught it.
_ROOT_HINT_PHRASES = ("root directory", "root folder", "the root", "top level", "top-level", "topmost")


class FileSearchNode:
    """
    File search specialist. Called by the graph when router dispatches to
    "file_search".
    """

    def __init__(self, sandbox_root: Path) -> None:
        self.sandbox_root = sandbox_root

    def run(
        self,
        clf: ClassifierOutput,
        session_state: SessionState,
    ) -> str:
        """
        Execute the file search and return a spoken response.
        """
        return self._run(clf, session_state)

    def _run(
        self,
        clf: ClassifierOutput,
        session_state: SessionState,
    ) -> str:
        entities = clf.extracted_entities
        raw_utterance = getattr(clf, "_raw_utterance", "") or ""

        # --- Fast path: router already resolved a pending "is that the one
        # you meant?" confirmation deterministically. Nothing to search for
        # -- just act on it. ---
        if entities.get("_confirmed_file"):
            path = entities["file_reference"]
            session_state.last_referenced_file = path
            session_state.add_turn("assistant", f"Confirmed: {path}")
            return "Got it."

        exclude_path: Optional[str] = entities.get("_rejected_file")

        file_reference = entities.get("file_reference")

        # Check if the reference itself is an existing file (relative or absolute) under the sandbox
        if file_reference:
            ref_path = Path(file_reference)
            if not ref_path.is_absolute():
                abs_candidate = (self.sandbox_root / ref_path).resolve()
            else:
                abs_candidate = ref_path.resolve()
                
            try:
                if abs_candidate.exists() and abs_candidate.is_file():
                    # Security boundary: must be under sandbox_root
                    rel_path = abs_candidate.relative_to(self.sandbox_root.resolve()).as_posix()
                    session_state.last_referenced_file = rel_path
                    session_state.add_turn("assistant", f"Found exact path: {abs_candidate.name}")
                    return f"Found {abs_candidate.name} directly on disk at the specified path."
            except (OSError, ValueError):
                pass
        resolved_date = entities.get("relative_date")
        location_hint = entities.get("location_hint")

        raw_file_type = entities.get("file_type")
        file_type = (_extract_file_type_hint(raw_file_type) if raw_file_type else None) or raw_file_type
        file_type = file_type or _extract_file_type_hint(file_reference) or _extract_file_type_hint(raw_utterance)

        results = self._search(
            filename_fragment=_clean_reference(file_reference),
            file_type=file_type,
            modified_date=resolved_date,
            content_keyword=_extract_content_hint(file_reference),
        )
        if not results and file_reference:
            results = self._search(
                filename_fragment=_clean_reference(file_reference),
                file_type=file_type, modified_date=resolved_date, content_keyword=None,
            )
        if not results and resolved_date:
            results = self._search(
                filename_fragment=_clean_reference(file_reference),
                file_type=file_type, modified_date=None, content_keyword=None,
            )
        if not results and file_type:
            results = self._search(
                filename_fragment=_clean_reference(file_reference),
                file_type=None, modified_date=None, content_keyword=None,
            )

        # Query relaxation: a full cleaned fragment that matches nothing
        # doesn't mean nothing matches -- try the individual significant
        # words instead of giving up (standard "term dropping" technique).
        cleaned = _clean_reference(file_reference)
        if not results and cleaned and " " in cleaned:
            results = self._relaxed_search(cleaned, file_type)

        if exclude_path:
            results = [r for r in results if r["path"] != exclude_path]

        if not results:
            query_parts = []
            if file_reference:
                query_parts.append(f"'{file_reference}'")
            if resolved_date:
                query_parts.append(f"from {resolved_date}")
            query_desc = " ".join(query_parts) or "that file"
            return f"I couldn't find {query_desc} in your files. Can you describe it differently?"

        # Location-aware narrowing -- "root"/"top level" or a named folder
        results = _apply_location_hint(results, location_hint, raw_utterance or file_reference or "")

        if len(results) == 1:
            match = results[0]
            cleaned_ref = _clean_reference(file_reference) or ""
            exact_name_match = bool(cleaned_ref and cleaned_ref in match.get("name", "").lower())
            strong_match = match.get("match_score", 0) >= 2 or exact_name_match or not file_reference
            if strong_match:
                session_state.last_referenced_file = match["path"]
                session_state.add_turn("assistant", f"Found: {match['name']}")
                date_hint = _format_date_friendly(match.get("modified_iso", ""))
                return f"Found {_describe_location(match['path'])}{', saved ' + date_hint if date_hint else ''}."
            session_state.pending_intent = PendingIntent(
                raw_utterance=raw_utterance or (file_reference or ""),
                partial_entities={"candidate_file": match["path"], "candidate_name": match["name"]},
                awaiting="file_confirmation",
                original_intent=clf.intent,
            )
            session_state.add_turn("assistant", f"Proposed: {match['name']}")
            return f"The closest match I found is {_describe_location(match['path'])}. Is that the one you meant?"

        top, runner_up = results[0], results[1]
        if _clearly_better(top, runner_up, file_reference):
            session_state.pending_intent = PendingIntent(
                raw_utterance=raw_utterance or (file_reference or ""),
                partial_entities={"candidate_file": top["path"], "candidate_name": top["name"]},
                awaiting="file_confirmation",
                original_intent=clf.intent,
            )
            session_state.add_turn("assistant", f"Proposed: {top['name']}")
            return f"Found {_describe_location(top['path'])}. Is that the one you meant?"

        # Genuine ambiguity. If names collide (e.g. two files both
        # literally named "hello.txt" in different folders), a bare
        # filename list is unanswerable -- switch to path-qualified labels.
        top3 = results[:3]
        names = [r["name"] for r in top3]
        labels = [_describe_location(r["path"]) for r in top3] if len(set(names)) < len(names) else names

        session_state.pending_intent = PendingIntent(
            raw_utterance=raw_utterance or (file_reference or ""),
            partial_entities={"candidates": [r["path"] for r in top3]},
            awaiting="file_reference",
            original_intent=clf.intent,
        )
        label_list = ", ".join(f"'{c}'" for c in labels[:-1]) + f", or '{labels[-1]}'"
        return f"I found a few matching files: {label_list}. Which one?"

    def _search(
        self,
        filename_fragment: Optional[str] = None,
        file_type: Optional[str] = None,
        modified_date: Optional[str] = None,
        content_keyword: Optional[str] = None,
        max_results: int = 10,
    ) -> list[dict]:
        from echolocate.mcp_server.tools.search_files import search_files
        try:
            results = search_files(
                self.sandbox_root,
                filename_fragment=filename_fragment,
                file_type=file_type,
                modified_date=modified_date,
                content_keyword=content_keyword,
                max_results=max_results,
            )
            return results or _local_search(
                self.sandbox_root,
                filename_fragment=filename_fragment,
                file_type=file_type,
                modified_date=modified_date,
                content_keyword=content_keyword,
                max_results=max_results,
            )
        except Exception as exc:
            print(f"[FileSearch] search error: {exc}")
            return _local_search(
                self.sandbox_root,
                filename_fragment=filename_fragment,
                file_type=file_type,
                modified_date=modified_date,
                content_keyword=content_keyword,
                max_results=max_results,
            )

    def _relaxed_search(self, cleaned_fragment: str, file_type: Optional[str]) -> list[dict]:
        """Try each significant word individually, merge results, keep the
        best match_score seen per file. See run()'s docstring / _STOP_WORDS
        comment for why the full-phrase query can legitimately match
        nothing even when a file clearly matches the intent."""
        best_by_path: dict[str, dict] = {}
        for word in cleaned_fragment.split():
            if len(word) < 3:
                continue
            for r in self._search(filename_fragment=word, file_type=file_type, max_results=5):
                existing = best_by_path.get(r["path"])
                if existing is None or r.get("match_score", 0) > existing.get("match_score", 0):
                    best_by_path[r["path"]] = r
        return sorted(
            best_by_path.values(),
            key=lambda r: (r.get("match_score", 0), r.get("modified_iso", "")),
            reverse=True,
        )


def _extract_file_type_hint(reference: Optional[str]) -> Optional[str]:
    """Extract file type from a reference like 'that PDF report' -- fallback
    for when the classifier didn't populate the file_type entity directly."""
    if not reference:
        return None
    types = {"pdf": "pdf", "word": "docx", "docx": "docx", "docs": "docx", "text": "txt",
             "txt": "txt", "doc": "docx", "powerpoint": "pptx", "pptx": "pptx"}
    ref_lower = reference.lower()
    for keyword, ext in types.items():
        if keyword in ref_lower:
            return ext
    return None


def _extract_content_hint(reference: Optional[str]) -> Optional[str]:
    if not reference:
        return None
    words = [w for w in reference.lower().split() if w not in _STOP_WORDS]
    return " ".join(words) if words else None


def _clean_reference(reference: Optional[str]) -> Optional[str]:
    if not reference:
        return None
    words = [w for w in reference.lower().split() if w not in _STOP_WORDS]
    return " ".join(words) if words else None


def _detect_root_hint(text: Optional[str]) -> bool:
    """Deterministic fallback: true if the text asks for the top-level /
    root-directory copy, independent of whether the classifier's
    location_hint entity caught it.
    
    Carefully avoids false positives on compound folder names that contain
    'root' (e.g. 'sandbox root', 'sandbox_root') — only triggers when
    'root' clearly means 'the top-level / root directory'."""
    if not text:
        return False
    t = text.lower()
    # Exact phrase matches: unambiguously mean the top-level directory
    if any(phrase in t for phrase in _ROOT_HINT_PHRASES):
        # But NOT if preceded by a qualifier like 'sandbox'
        for phrase in _ROOT_HINT_PHRASES:
            idx = t.find(phrase)
            if idx >= 0:
                before = t[:idx].rstrip()
                # If the word right before the root phrase is part of a
                # compound folder name, this is NOT a root hint
                if before and not before.endswith((',', '.', ';', '!', '?')):
                    last_word = before.split()[-1] if before.split() else ''
                    if last_word in {'sandbox', 'sand', 'sacked', 'box', 'sub'}:
                        return False
        return True
    # Bare \broot\b — only if NOT preceded by sandbox-like words
    m = re.search(r'\broot\b', t)
    if m:
        before = t[:m.start()].rstrip()
        if before:
            last_word = before.split()[-1] if before.split() else ''
            if last_word in {'sandbox', 'sand', 'sacked', 'box', 'sub', 'sack'}:
                return False
        return True
    return False


def _apply_location_hint(results: list[dict], location_hint: Optional[str], raw_text: str) -> list[dict]:
    """
    If a root/location hint is present, FILTER (not just re-rank)
    candidates to those matching it, when at least one candidate does --
    an explicit stated location beats every other heuristic, including
    exact-name match tier. Returns the original list unchanged if the hint
    doesn't narrow anything down (nothing lost by trying).
    """
    is_root_hint = (location_hint or "").strip().lower() in {"root", "top level", "top-level"} or _detect_root_hint(raw_text)
    if is_root_hint:
        top_level = [r for r in results if "/" not in r["path"]]
        if top_level:
            return top_level
    elif location_hint:
        hint_lower = location_hint.strip().lower()
        hint_norm = _path_words(hint_lower)
        matching = [
            r for r in results
            if hint_lower in r["path"].lower() or hint_norm in _path_words(r["path"])
        ]
        if matching:
            return matching
    return results


def _path_words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _describe_location(path: str) -> str:
    """'hello.txt' if at the sandbox root, 'hello.txt (in Documents)' for a
    shallow path, or a shortened form for anything deeper -- reading out
    6+ nested folder names is both hard to follow by ear and, at TTS
    length, a real contributor to how long a response takes to deliver.
    Enough context to distinguish same-named files without spelling out
    an entire filesystem path."""
    p = Path(path)
    if p.parent == Path("."):
        return f"{p.name} (in the root folder)"
    parts = p.parent.parts
    if len(parts) <= 2:
        return f"{p.name} (in {p.parent.as_posix()})"
    # Deep path: name the immediate containing folder for orientation and
    # say "nested" rather than reading out the full chain above it.
    return f"{p.name} (nested inside {parts[-1]})"


def _clearly_better(top: dict, runner_up: dict, reference: Optional[str]) -> bool:
    """
    Returns True if top is clearly better than runner_up.

    Primary signal: match_score (word-boundary-aware, from search_files'
    scoring). A strictly higher match tier is a much stronger signal than
    recency alone. Within the SAME tier -- which is exactly what happens
    for two files that are both literally named the same thing -- prefer
    the shallower path (closer to the sandbox root) before falling back to
    recency. Pure recency as the only tiebreaker was a poor default for
    genuine filename collisions: it has no relationship to which copy a
    person actually meant.
    """
    if not reference:
        return False

    top_score = top.get("match_score", 0)
    runner_score = runner_up.get("match_score", 0)

    if top_score > runner_score:
        return True

    if top_score == runner_score and top_score > 0:
        try:
            from datetime import datetime
            t1 = datetime.fromisoformat(top.get("modified_iso", ""))
            t2 = datetime.fromisoformat(runner_up.get("modified_iso", ""))
            # Only consider it clearly better if one is MUCH newer (e.g. edited within the last week vs months ago)
            if abs((t1 - t2).total_seconds()) > 7 * 86400:
                return True
        except Exception:
            pass

    return False


def _format_date_friendly(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        from datetime import datetime, date as date_t
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        today = date_t.today()
        diff = (today - dt.date()).days
        if diff == 0:
            return "today"
        elif diff == 1:
            return "yesterday"
        elif diff <= 7:
            return dt.strftime("%A")
        else:
            return dt.strftime("%B %d")
    except Exception:
        return ""


def _local_search(
    sandbox_root: Path,
    filename_fragment: Optional[str] = None,
    file_type: Optional[str] = None,
    modified_date: Optional[str] = None,
    content_keyword: Optional[str] = None,
    max_results: int = 10,
) -> list[dict]:
    """Filesystem fallback for when the index backend is unavailable."""
    fragment_words = [w for w in (filename_fragment or "").lower().split() if w]
    keyword = (content_keyword or "").strip().lower()
    wanted_ext = (file_type or "").strip().lower().lstrip(".")
    results: list[dict] = []
    deadline = time.monotonic() + 4.0
    visited = 0
    max_files = 25000 if fragment_words or wanted_ext else 5000
    seen_paths: set[Path] = set()

    for path in _exact_candidate_paths(sandbox_root, fragment_words, wanted_ext):
        seen_paths.add(path)
        result = _score_local_candidate(sandbox_root, path, fragment_words, wanted_ext, modified_date, keyword)
        if result:
            results.append(result)

    if _has_strong_enough_results(results, fragment_words):
        return _sort_local_results(results)[:max_results]

    for path in _iter_files_bounded(sandbox_root, deadline=deadline, max_files=max_files):
        if path in seen_paths:
            continue
        seen_paths.add(path)
        visited += 1
        result = _score_local_candidate(sandbox_root, path, fragment_words, wanted_ext, modified_date, keyword)
        if result:
            results.append(result)

    return _sort_local_results(results)[:max_results]


def _exact_candidate_paths(root: Path, fragment_words: list[str], wanted_ext: str) -> list[Path]:
    if not fragment_words:
        return []
    stems = {" ".join(fragment_words), "_".join(fragment_words), "-".join(fragment_words)}
    exts = [wanted_ext] if wanted_ext else ["txt", "md", "docx", "pdf", ""]
    preferred_dirs = [
        root,
        root / "Echolocate",
        root / "Echolocate" / "sandbox_root",
        root / "sandbox_root",
        root / "Documents",
        root / "docs",
    ]
    candidates: list[Path] = []
    for directory in preferred_dirs:
        for stem in stems:
            for ext in exts:
                candidates.append(directory / (f"{stem}.{ext}" if ext else stem))
    return [p for p in candidates if p.exists() and p.is_file()]


def _score_local_candidate(
    sandbox_root: Path,
    path: Path,
    fragment_words: list[str],
    wanted_ext: str,
    modified_date: Optional[str],
    keyword: str,
) -> Optional[dict]:
    from datetime import datetime
    try:
        rel = path.relative_to(sandbox_root).as_posix()
        name_lower = path.name.lower()
        ext = path.suffix.lower().lstrip(".")
        if wanted_ext and ext != wanted_ext:
            return None
        stat = path.stat()
        if modified_date:
            modified = datetime.fromtimestamp(stat.st_mtime).date().isoformat()
            if modified != modified_date:
                return None

        score = 0
        if fragment_words:
            stem_lower = path.stem.lower()
            if (
                " ".join(fragment_words) == stem_lower
                or "_".join(fragment_words) == stem_lower
                or "-".join(fragment_words) == stem_lower
            ):
                score += 100
            score += sum(10 for word in fragment_words if word == stem_lower)
            score += sum(4 for word in fragment_words if word in name_lower)
            score += sum(1 for word in fragment_words if word in rel.lower() and word not in name_lower)
            
            if score == 0:
                return None
        if keyword and ext in {"txt", "md"}:
            try:
                if stat.st_size > 5 * 1024 * 1024:
                    return None  # Skip keyword search for massive files to prevent OOM / hangs
                if keyword not in path.read_text(encoding="utf-8", errors="ignore").lower():
                    return None
                score += 1
            except Exception:
                return None

        return {
            "name": path.name,
            "path": rel,
            "modified_iso": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "size": stat.st_size,
            "match_score": score or 1,
        }
    except Exception:
        return None


def _has_strong_enough_results(results: list[dict], fragment_words: list[str]) -> bool:
    exact = [r for r in results if r.get("match_score", 0) >= 100]
    return len(exact) == 1 and bool(fragment_words)


def _sort_local_results(results: list[dict]) -> list[dict]:
    return sorted(
        results,
        key=lambda r: (r.get("match_score", 0), -r["path"].count("/"), r.get("modified_iso", "")),
        reverse=True,
    )


def _iter_files_bounded(root: Path, deadline: float, max_files: int):
    """Yield files breadth-first without letting a drive-root search run forever."""
    import os
    queue = [str(root)]
    visited_files = 0
    while queue and visited_files < max_files and time.monotonic() < deadline:
        current_str = queue.pop(0)
        try:
            with os.scandir(current_str) as it:
                entries = list(it)
        except Exception:
            continue

        dirs = []
        files = []
        for entry in entries:
            if time.monotonic() >= deadline:
                return
            try:
                # follow_symlinks=False is CRITICAL here to prevent multi-minute hangs
                # on unresponsive Windows junctions, OneDrive placeholders, or disconnected shares.
                if entry.is_file(follow_symlinks=False):
                    files.append(entry)
                elif entry.is_dir(follow_symlinks=False):
                    if entry.name.lower() in _STOP_WORDS or entry.name.lower() in {"$recycle.bin", "system volume information"}:
                        continue
                    dirs.append(entry)
            except Exception:
                continue
        
        for entry in sorted(files, key=lambda p: p.name.lower()):
            visited_files += 1
            yield Path(entry.path)
            if visited_files >= max_files or time.monotonic() >= deadline:
                return
        
        # queue.extend expects strings now, not Path objects
        for d in sorted(dirs, key=_dir_priority_scandir):
            queue.append(d.path)


def _dir_priority_scandir(entry) -> tuple[int, str]:
    name = entry.name.lower()
    preferred = {
        "echolocate": 0,
        "sandbox_root": 1,
        "documents": 2,
        "docs": 3,
        "users": 4,
    }
    if name in preferred:
        return (preferred[name], name)
    if name in {"android", "program files", "program files (x86)", "windows"}:
        return (100, name)
    return (20, name)
