"""
EchoLocate — Windows Search (SystemIndex) as the primary search backend on
Windows, ahead of the custom SQLite index in index.py.

Why this exists: the SQLite index (index.py) works, but it means EchoLocate
re-implements full-drive indexing itself — walk the tree, maintain a
database, watch for changes — for a problem Windows already solves as a
persistent background OS service (the same index behind Start menu search
and File Explorer search). Querying it directly means no multi-minute
first-run scan blocking a live voice request (which is what actually
happened — see the transcript this module was written in response to: a
300K+ file synchronous build, holding SQLite locks long enough that a
concurrent read hit "database is locked" mid-scan), no watcher process, and
no risk of our own index and a live query racing each other, because
there's no build in progress to race — it's not our database.

How: the documented OLE DB provider for Windows Search
(Provider=Search.CollatorDSO), accessed from Python via pywin32's ADODB COM
automation. SELECT ... FROM SystemIndex WHERE ... using Windows Search's
SQL-like dialect — FREETEXT/CONTAINS for text matching, System.ItemPathDisplay
/ System.ItemNameDisplay / System.DateModified / System.FileExtension /
System.Size as the relevant columns. SCOPE = '<path>' restricts results to
the sandbox root, same containment guarantee at the query level (actual
filesystem access still goes through sandbox.py regardless — this module
only decides WHAT to search for, never touches files itself).

Real caveat this module handles explicitly: Windows Search only indexes
locations it's configured to index — typically the OS drive's user profile
folders, NOT an arbitrary second drive like D:\\ by default. A query against
an unindexed location returns an empty result, not an error, so trusting an
empty result blindly would look identical to "the file doesn't exist."
ensure_indexed() checks whether the sandbox root is in an indexed scope and,
if not, registers it (AddUserScopeRule — a per-user scope rule, does not
require administrator rights) so Windows starts indexing it going forward.
That first-time registration doesn't make results appear instantly; Windows
indexes it in the background over time, same as it would for any new
location added to search. The practical effect across runs: the FIRST run
after registering falls back to the local SQLite index (below) as a bridge;
by the NEXT run, Windows has very likely finished indexing it in the
background (it keeps working even while EchoLocate isn't running), so
ensure_indexed() returns True immediately and the local index is never
touched again for that root.

Fallback: if pywin32 isn't installed, the Windows Search service isn't
running, or the sandbox root can't be confirmed indexed, search_files.py
falls back to index.py's SQLite index automatically. This module being
unavailable is a normal, expected state (non-Windows platforms, or a
locked-down machine with Search disabled) — not an error condition.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

IS_WINDOWS = sys.platform == "win32"


def is_available() -> bool:
    """Cheap check: Windows + pywin32 installed + can actually open a
    connection to the Search OLE DB provider. Does NOT check whether any
    particular path is indexed — see ensure_indexed() for that.

    Uses importlib.util.find_spec() instead of a bare `import win32com`
    specifically because a bare import triggers Python's namespace-package
    fallback, which recursively scans sys.path directories — on a sandbox
    root with hundreds of thousands of files, that scan alone took 4-10
    minutes. find_spec() avoids that fallback scan for the "not installed"
    case. It does NOT avoid the need to actually import the module once we
    know it exists and intend to use it — skipping that step (as an earlier
    version of this function did) left win32com unbound, so calling
    win32com.client.Dispatch() below raised NameError on every call, which
    the broad except Exception at the bottom silently swallowed. That made
    this function return False unconditionally, on every machine, forever
    — Windows Search was never actually being used despite appearing to
    "just fall back" quietly. The import below is real and required."""
    if not IS_WINDOWS:
        return False
    import importlib.util
    if importlib.util.find_spec("win32com") is None:
        return False
    try:
        import win32com.client
        conn = win32com.client.Dispatch("ADODB.Connection")
        conn.CommandTimeout = 5
        conn.ConnectionTimeout = 5
        conn.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
        conn.Close()
        return True
    except Exception as exc:
        print(f"[WindowsSearch] is_available() check failed: {exc}")
        return False


def ensure_indexed(sandbox_root: Path) -> bool:
    """
    Best-effort: confirm sandbox_root is actually queryable via Windows
    Search right now. Returns True only if a live probe query against it
    succeeds; False means "use the local index for this run" — either
    because Windows Search itself isn't reachable, or because this root
    genuinely isn't in an indexed location yet.

    Rewritten after a real failure: the original version tried to
    PROGRAMMATICALLY REGISTER the sandbox root as an indexed crawl scope
    via Microsoft.Search.Interop.CSearchManager — a .NET COM-interop class
    that isn't reliably instantiable through plain win32com.client.Dispatch
    across machines (confirmed failing with HRESULT -2147221005, "Invalid
    class string" — the ProgID couldn't be resolved to a registered COM
    class on this particular install, likely a bitness/registration
    mismatch specific to how that assembly is exposed). Rather than debug
    a fragile, poorly-documented COM interop path further, this version
    drops registration entirely and just PROBES with a real query using
    the SAME "ADODB.Connection" + "Search.CollatorDSO" mechanism already
    confirmed working in is_available() — if a lightweight probe query
    against SystemIndex scoped to this root returns any row at all, the
    root is indexed; if it errors or comes back empty, fall back to the
    local index. This trades "can proactively ask Windows to start
    indexing a new location" for "actually works reliably" — if you want
    D:\\ indexed by Windows, the dependable path is enabling it manually via
    Control Panel > Indexing Options > Modify > check the drive, which
    sidesteps needing fragile programmatic registration at all.
    """
    if not IS_WINDOWS:
        return False
    try:
        import win32com.client
        root_str = str(sandbox_root).rstrip("\\") + "\\"
        conn = win32com.client.Dispatch("ADODB.Connection")
        conn.CommandTimeout = 5
        conn.ConnectionTimeout = 5
        conn.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
        try:
            sql = (
                f"SELECT TOP 1 System.ItemPathDisplay FROM SystemIndex "
                f"WHERE SCOPE = '{_escape(root_str)}'"
            )
            recordset, _ = conn.Execute(sql)
            has_row = not recordset.EOF
            recordset.Close()
            
            if not has_row:
                print(f"[WindowsSearch] '{root_str}' is not yet indexed. Attempting to register it in the background...")
                try:
                    import pythoncom
                    # Bypass the broken 'Search.SearchManager' ProgID by calling the CLSID directly.
                    clsid = pythoncom.MakeIID("{7D096C5F-AC08-4F1F-BEB7-5C22C517CE39}")
                    sm = win32com.client.Dispatch(clsid)
                    csm = sm.GetCatalog("SystemIndex").GetCrawlScopeManager()
                    target_url = f"file:///{str(sandbox_root).replace(chr(92), '/')}/"
                    # AddUserScopeRule(URL, fInclude, fOverrideChildren, fFollowFlags)
                    csm.AddUserScopeRule(target_url, True, True, 0)
                    csm.SaveAll()
                    print(f"[WindowsSearch] Successfully registered '{root_str}'. Windows will now index it in the background.")
                except Exception as register_exc:
                    print(f"[WindowsSearch] Background registration failed: {register_exc}. You may need to add it manually in Indexing Options.")

            return has_row
        finally:
            conn.Close()
    except Exception as exc:
        print(
            f"[WindowsSearch] Probe query failed ({exc}) — using the local "
            f"index. To use Windows Search for this drive, enable it "
            f"manually: Control Panel > Indexing Options > Modify > check "
            f"the drive."
        )
        return False


def search(
    sandbox_root: Path,
    *,
    filename_fragment: Optional[str] = None,
    file_type: Optional[str] = None,
    modified_date: Optional[str] = None,
    content_keyword: Optional[str] = None,
    max_results: int = 10,
) -> list[dict]:
    """Query the Windows Search index, scoped to sandbox_root, returning
    results in the same shape as index.py's FileIndex.search() so this is
    a drop-in alternative backend for search_files.py."""
    import win32com.client

    root_str = str(sandbox_root).rstrip("\\") + "\\"
    where_clauses = [f"SCOPE = '{_escape(root_str)}'"]

    if filename_fragment:
        # FREETEXT tolerates natural, multi-word phrasing without hand-built
        # boolean syntax -- CONTAINS chokes on plain-language queries like
        # "where is my hello file" the way a literal boolean AND would.
        where_clauses.append(f"FREETEXT(System.ItemNameDisplay, '{_escape(filename_fragment)}')")
    if file_type:
        where_clauses.append(f"System.FileExtension = '.{_escape(file_type.lstrip('.'))}'")
    if modified_date:
        where_clauses.append(
            f"System.DateModified >= '{modified_date}' AND System.DateModified < '{modified_date} 23:59:59'"
        )
    if content_keyword:
        where_clauses.append(f"CONTAINS(System.Search.Contents, '\"{_escape(content_keyword)}\"')")

    sql = (
        f"SELECT TOP {max_results} System.ItemPathDisplay, System.ItemNameDisplay, "
        f"System.Size, System.DateModified, System.FileExtension "
        f"FROM SystemIndex WHERE " + " AND ".join(where_clauses)
    )

    conn = win32com.client.Dispatch("ADODB.Connection")
    conn.CommandTimeout = 5
    conn.ConnectionTimeout = 5
    conn.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
    try:
        recordset, _ = conn.Execute(sql)
        results = []
        while not recordset.EOF:
            abs_path = recordset.Fields.Item("System.ItemPathDisplay").Value
            name = recordset.Fields.Item("System.ItemNameDisplay").Value
            size = recordset.Fields.Item("System.Size").Value
            modified = recordset.Fields.Item("System.DateModified").Value
            ext = (recordset.Fields.Item("System.FileExtension").Value or "").lstrip(".").lower()

            try:
                rel_path = str(Path(abs_path).relative_to(sandbox_root)).replace("\\", "/")
            except ValueError:
                recordset.MoveNext()
                continue

            results.append({
                "name": name,
                "path": rel_path,
                "size_bytes": int(size) if size else 0,
                "modified_iso": _to_iso(modified),
                "file_type": ext,
                # Windows Search already applied its own relevance ranking
                # (FREETEXT ordering) -- 2 here just keeps this compatible
                # with index.py's match_score-based ambiguity thresholds
                # in file_search.py / file_resolution.py.
                "match_score": 2 if filename_fragment else 0,
            })
            recordset.MoveNext()
        recordset.Close()
        return results
    finally:
        conn.Close()


def _escape(s: str) -> str:
    return s.replace("'", "''")


def _to_iso(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return str(value)
