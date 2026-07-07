"""
EchoLocate — SQLite-backed file index for fast search at any sandbox scale.

search_files.py originally walked the sandbox tree fresh on every call.
That's fine for a small folder, but becomes a 40+ second operation once the
sandbox root is expanded to cover an entire drive with hundreds of
thousands of files — and that's a deliberate, supported choice now, not a
misconfiguration to block. This module is what makes that choice viable:
walk the tree ONCE (or on a periodic refresh) into a SQLite index, then
answer every search as an indexed query instead of a fresh filesystem walk.

This is the same fundamental approach real full-drive search tools use —
Windows Search and Spotlight maintain a persistent index for exactly this
reason, and voidtools' Everything goes further and reads the NTFS Master
File Table directly for near-instant results. Reading the MFT directly is
out of scope here (it needs low-level NTFS parsing and administrator
privileges on Windows); a SQLite index gets most of the practical benefit
for a fraction of the complexity, which is the right tradeoff for this
project.

Matching: uses SQLite FTS5 with the trigram tokenizer (SQLite >= 3.34,
present in every Python 3.10+ build's bundled sqlite3 on all three
platforms) for fast arbitrary-substring search. A plain "LIKE '%frag%'"
can't use a B-tree index for a leading wildcard, so at drive scale it
degrades to a full table scan on every query — trigram FTS5 indexes
3-character sequences instead, so substring search stays an index lookup
regardless of table size. If the installed SQLite lacks FTS5/trigram
support (rare on modern Python, possible on some Linux system builds), this
module transparently falls back to an indexed LIKE/stem query — slower on
huge tables but still correct, and still vastly faster than a live walk.

Safety note: this module is search/read-only. It does not change what a
destructive tool call is allowed to touch — sandbox.py's resolve_and_check()
and safe_open() remain the actual enforcement boundary (Architecture
Section 4.4) and run exactly as before, independent of whether results came
from a live walk or this index. What this module DOES add is an automatic,
broader exclusion list (OS-critical directories) applied at index-build
time whenever the sandbox root looks "broad" — see is_broad_root() — so
indexing (and therefore ever surfacing as a search result) stays away from
Windows/, Program Files/, /proc, /etc, etc. even when the root is an entire
drive.
Why not a vector database: what this index answers ("find the file named
roughly X", "files of type Y modified on Z") is exact/fuzzy LEXICAL
matching, not semantic similarity — trigram FTS is the right-fitted tool,
and a vector DB would add real cost (an embedding call per file, a larger
index, approximate-nearest-neighbor imprecision) for no benefit on this
specific task. If EchoLocate later needs genuine semantic content search
("find the file where I discussed X" with no literal keyword overlap),
the right extension is `sqlite-vec` (github.com/asg017/sqlite-vec) added
as a second virtual table in this SAME database file — not a separate
vector database. sqlite-vec is a mature, cross-platform SQLite extension
specifically so vector search can live next to FTS5 in one file with one
connection, which fits this project's "no extra services" constraint far
better than LanceDB/Chroma/Qdrant would. Not implemented here because
nothing in EchoLocate currently needs semantic content recall — search_files
does filename/type/date/literal-keyword matching, and the Document node
handles content reasoning once a specific file is already identified.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Optional

IS_WINDOWS = sys.platform == "win32"

# Always skipped: noise/build directories common in almost any real tree.
_SKIP_DIR_NAMES = {
    ".git", ".hg", ".svn",
    "node_modules", "__pycache__",
    ".venv", "venv", "env",
    ".cache", ".mypy_cache", ".pytest_cache",
    "dist", "build",
    "$recycle.bin", "system volume information",
    # Dev-toolchain/SDK directories -- never something a person means by
    # "find my hello file." Added after a real result surfaced a Fortran
    # compiler-detection test file buried in an Android SDK's cmake
    # install (Android/Sdk/cmake/.../hello.f), which not only isn't what
    # anyone asked for but also, at TTS read-out length, meaningfully
    # slowed down the spoken response.
    "sdk", ".gradle", ".android", ".cargo", ".rustup", ".m2", ".nuget",
    "vendor", ".terraform",
}

# Applied automatically on top of the above when the root is "broad" (see
# is_broad_root) — a safety floor, not a search-quality optimization. These
# directories are never indexed, so they can never appear as a search
# result or become a destination for a move/delete call, regardless of how
# permissive the user wants search scope to be.
_OS_CRITICAL_DIR_NAMES_WINDOWS = {
    "windows", "program files", "program files (x86)", "programdata",
    "$windows.~bt", "$windows.~ws", "recovery", "perflogs",
}

# OS system files that sit directly at a drive root, NOT inside any of the
# directories above -- the directory-name skip list never catches these.
# pagefile.sys/hiberfil.sys/swapfile.sys are actively rewritten by Windows
# essentially continuously (virtual memory activity), which means a
# real-time filesystem watcher pointed at a whole drive gets a nonstop
# flood of change events for them specifically. That flood is what was
# actually causing sustained "database is locked" errors during a big
# scan -- not a busy_timeout that was too short, but a genuinely
# never-ending stream of write attempts for files nobody would ever
# actually search for. Excluding them at the source fixes both the
# correctness issue (irrelevant results) and the reliability issue
# (watcher write-contention) at once.
_SKIP_FILE_NAMES = {
    "pagefile.sys", "hiberfil.sys", "swapfile.sys",
    "desktop.ini", "thumbs.db", ".ds_store",
    "file_index.sqlite3", "file_index.sqlite3-wal", "file_index.sqlite3-shm",
}
_OS_CRITICAL_DIR_NAMES_UNIX = {
    "proc", "sys", "dev", "boot", "etc", "bin", "sbin", "lib", "lib64",
    "usr", "var", "run",
}


def is_broad_root(sandbox_root: Path) -> bool:
    """A drive root, filesystem root, or near-root path (e.g. D:\\, /, C:\\Users)."""
    resolved = Path(os.path.realpath(str(sandbox_root)))
    if resolved.parent == resolved:
        return True
    return len(resolved.parts) <= 2


class FileIndex:
    """
    One instance per sandbox root. Owns a SQLite database mapping every
    indexed file to its metadata. Single-writer (one background rescan
    thread)/many-reader (search() calls from node code) — safe under
    SQLite's WAL mode.
    """

    def __init__(self, sandbox_root: Path, db_path: Optional[Path] = None) -> None:
        self.sandbox_root = Path(os.path.realpath(str(sandbox_root)))
        self.db_path = db_path or (self.sandbox_root / "file_index.sqlite3")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._trigram_available = False
        self._watcher = None
        self._monitoring_started = False
        self._build_started = False
        self.is_building = False
        self.indexed_so_far = 0
        self._skip_names = set(_SKIP_DIR_NAMES) | set(_SKIP_FILE_NAMES)
        if is_broad_root(self.sandbox_root):
            self._skip_names |= (
                _OS_CRITICAL_DIR_NAMES_WINDOWS if IS_WINDOWS else _OS_CRITICAL_DIR_NAMES_UNIX
            )
        self._init_db()

    @contextlib.contextmanager
    def _connect(self):
        """
        Context manager: opens a connection, yields it, and CLOSES it on
        exit (including on exception). Every call site already uses
        `with self._connect() as conn:`, which worked before this change
        too -- but a plain sqlite3.Connection used as `with conn:` only
        manages the transaction (commit/rollback), not the connection's
        lifecycle. None of the 7 call sites in this class ever explicitly
        closed their connection, so a long build() (dozens of short-lived
        batch-commit connections) plus a busy real-time watcher (one
        connection per upsert) could leave many connections open
        simultaneously, relying on garbage collection to eventually close
        them. That's sloppy resource handling on any platform, and a
        contributing factor (alongside the pagefile.sys flood fixed above)
        in the sustained "database is locked" errors seen under load.

        timeout= sets SQLite's busy handler so a reader/writer arriving
        mid-write waits up to 8s instead of raising "database is locked"
        immediately (Python's sqlite3.connect(timeout=...) IS the
        busy-timeout knob; the PRAGMA below is redundant insurance, not
        double config). synchronous=NORMAL is the standard, safe pairing
        with WAL mode for bulk-write workloads.
        """
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=8.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=8000")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    path TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    stem TEXT NOT NULL,
                    ext TEXT,
                    size_bytes INTEGER,
                    mtime REAL,
                    root TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_root ON files(root)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_ext ON files(root, ext)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(root, mtime)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_files_stem ON files(root, stem)")
            # Cursor for usn_catchup.py's incremental re-indexing: lets a
            # startup catch-up ask NTFS "what changed after this point"
            # instead of a full build() re-walking every file. See
            # usn_catchup.py's module docstring for the full strategy.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS journal_cursor (
                    root TEXT PRIMARY KEY,
                    journal_id INTEGER NOT NULL,
                    next_usn INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            try:
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS files_fts
                    USING fts5(path UNINDEXED, name, tokenize='trigram')
                """)
                self._trigram_available = True
            except sqlite3.OperationalError:
                self._trigram_available = False  # fall back to LIKE queries

    def _is_skipped(self, rel_path: str) -> bool:
        """True if any path component of rel_path is in the skip list."""
        parts = [p.lower() for p in Path(rel_path).parts]
        return any(p in self._skip_names for p in parts)

    def upsert_file(self, abs_path: str) -> None:
        """
        Insert or update a single file's row — the incremental counterpart
        to build(). Used by IndexWatcher on create/modify events so a change
        is reflected in seconds, not at the next periodic rescan.
        """
        root_str = str(self.sandbox_root)
        try:
            rel_path = os.path.relpath(abs_path, root_str).replace("\\", "/")
        except ValueError:
            return  # different drive — not under this root
        if self._is_skipped(rel_path):
            return
        try:
            stat = os.stat(abs_path)
        except OSError:
            return  # gone already (e.g. a save-then-immediately-delete temp file)

        filename = os.path.basename(abs_path)
        stem = Path(filename).stem.lower()
        ext = Path(filename).suffix.lstrip(".").lower()
        row = (rel_path, filename, stem, ext, stat.st_size, stat.st_mtime, root_str)

        with self._lock, self._connect() as conn:
            # Same class of bug as flush()'s old per-batch delete (see
            # build()'s docstring): `path` is UNINDEXED in files_fts, so an
            # unconditional DELETE here is O(current table size) on EVERY
            # call. That was fine when upsert_file() only ran occasionally
            # from IndexWatcher, but usn_catchup.py can now call this
            # hundreds of times in one catch-up pass -- at 346K existing
            # rows, an unconditional delete-per-call would turn a "few
            # seconds" catch-up into something scaling with total index
            # size again. Same fix: only touch files_fts for paths that
            # don't already have a row (an existing path's name can't be
            # stale without its path also changing).
            row_existed = conn.execute(
                "SELECT 1 FROM files WHERE root = ? AND path = ?", (root_str, rel_path)
            ).fetchone() is not None
            conn.execute(
                "INSERT OR REPLACE INTO files (path, name, stem, ext, size_bytes, mtime, root) "
                "VALUES (?,?,?,?,?,?,?)",
                row,
            )
            if self._trigram_available and not row_existed:
                conn.execute(
                    "INSERT INTO files_fts (path, name) VALUES (?, ?)", (rel_path, filename)
                )
            conn.commit()

    def remove_path(self, abs_path: str) -> None:
        """
        Remove a single file's row, OR every row under a directory prefix if
        abs_path was a directory (watchdog reports directory deletes as a
        single event, not one per contained file — see docstring note in
        IndexWatcher.on_deleted for the Windows-specific wrinkle here).
        """
        root_str = str(self.sandbox_root)
        try:
            rel_path = os.path.relpath(abs_path, root_str).replace("\\", "/")
        except ValueError:
            return

        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM files WHERE root = ? AND path = ?", (root_str, rel_path))
            conn.execute(
                "DELETE FROM files WHERE root = ? AND path LIKE ?",
                (root_str, rel_path.rstrip("/") + "/%"),
            )
            if self._trigram_available:
                conn.execute("DELETE FROM files_fts WHERE path = ? OR path LIKE ?",
                             (rel_path, rel_path.rstrip("/") + "/%"))
            conn.commit()

    def get_journal_cursor(self, root_str: str) -> Optional[dict]:
        """Last-saved USN journal position for this root, or None if we've
        never successfully recorded one (first run, or catch-up/cursor
        recording has never succeeded here before)."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT journal_id, next_usn FROM journal_cursor WHERE root = ?", (root_str,)
            ).fetchone()
        if row is None:
            return None
        return {"journal_id": row[0], "next_usn": row[1]}

    def save_journal_cursor(self, root_str: str, journal_id: int, next_usn: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO journal_cursor (root, journal_id, next_usn, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (root_str, journal_id, next_usn, time.time()),
            )
            conn.commit()

    def start_background_refresh(self, interval_seconds: int = 3600) -> None:
        """
        Periodic safety net -- catches anything IndexWatcher could
        plausibly miss (a watch silently dropped, a burst of events during
        a bulk copy, a network drive that doesn't emit native change
        notifications). Prefers the fast USN-journal catch-up over a full
        rebuild for this periodic pass too, same as at startup -- there's
        no reason the safety net needs to pay the O(total files) cost when
        the same "what changed" answer is available in seconds. Falls back
        to a full build() automatically whenever catch-up can't run (see
        usn_catchup.catch_up()'s return value).

        NOTE: this method used to be defined twice in this class (a second,
        stale copy further down silently shadowed this one, so the ACTUAL
        running behavior was an unconditional full build() every 10
        minutes regardless of this docstring's intent). The duplicate has
        been removed -- this is now the only definition.
        """
        def _loop():
            while True:
                time.sleep(interval_seconds)
                try:
                    caught_up = False
                    try:
                        from echolocate.mcp_server.usn_catchup import catch_up
                        caught_up = catch_up(self, self.sandbox_root, verbose=False)
                    except ImportError:
                        pass
                    if not caught_up:
                        self.build()
                        self._record_journal_cursor_best_effort()
                except Exception as exc:
                    print(f"[FileIndex] background refresh failed: {exc}")
        threading.Thread(target=_loop, daemon=True).start()

    def _record_journal_cursor_best_effort(self) -> None:
        try:
            from echolocate.mcp_server.usn_catchup import record_initial_cursor
            record_initial_cursor(self, self.sandbox_root, verbose=False)
        except ImportError:
            pass
        except Exception as exc:
            print(f"[FileIndex] could not record journal cursor: {exc}")

    def start_watching(self) -> bool:
        """
        Start real-time incremental updates via watchdog. Returns True if
        the native watcher started successfully, False if it fell back to
        polling-only (watchdog not installed, or the platform's native
        watch mechanism failed to start — e.g. Linux inotify's per-user
        watch limit, commonly 8192, can be exhausted by a very large/broad
        sandbox root). On failure this does NOT crash the agent — it logs
        a clear reason and leaves the caller to rely on the periodic
        rescan (start_background_refresh) at a shorter interval instead.
        """
        try:
            from echolocate.mcp_server.watcher import IndexWatcher
        except ImportError:
            print("[FileIndex] 'watchdog' not installed — falling back to periodic "
                  "rescan only. Install with: pip install watchdog")
            return False

        try:
            watcher = IndexWatcher(self)
            watcher.start()
            self._watcher = watcher
            return True
        except Exception as exc:
            print(f"[FileIndex] real-time watcher failed to start ({exc}) — "
                  f"falling back to periodic rescan only.")
            return False

    def count_files(self) -> int:
        """Fast parallel count of files under sandbox_root (no DB writes)."""
        root_str = str(self.sandbox_root)
        total = 0
        lock = threading.Lock()
        visited = set()
        
        def scan_dir(dirpath: str):
            nonlocal total
            try:
                real_dirpath = os.path.realpath(dirpath)
            except OSError:
                return []
            with lock:
                if real_dirpath in visited:
                    return []
                visited.add(real_dirpath)
            
            subdirs = []
            local_count = 0
            try:
                with os.scandir(dirpath) as it:
                    for entry in it:
                        name_lower = entry.name.lower()
                        if name_lower in self._skip_names:
                            continue
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            continue
                        if is_dir:
                            subdirs.append(entry.path)
                        else:
                            local_count += 1
            except OSError:
                return []
            
            if local_count > 0:
                with lock:
                    total += local_count
            return subdirs

        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
            futures = set()
            futures.add(executor.submit(scan_dir, root_str))
            while futures:
                done, futures = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED)
                for fut in done:
                    try:
                        for subdir in fut.result():
                            futures.add(executor.submit(scan_dir, subdir))
                    except Exception:
                        pass
        return total

    def _root_is_empty(self, root_str: str) -> bool:
        """True if this root has no rows yet -- i.e. this build() call is a
        genuine cold start for this root, not a rebuild/refresh of an
        already-populated index. Cheap: uses idx_files_root."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM files WHERE root = ? LIMIT 1)", (root_str,)
            ).fetchone()
            return not bool(row[0])

    def build(self, progress_cb=None, batch_size: int = 2000) -> int:
        """
        Full (re)scan of sandbox_root into the index.

        Rewritten after a real failure: the original version did ONE
        transaction for the entire scan (delete-all, then insert-all,
        commit at the very end). For a 300K+ file root that transaction
        stayed open for minutes — during which (a) nothing was queryable
        at all despite files existing on disk, and (b) a concurrent search
        query hit "database is locked" because the writer held the lock
        far longer than any reasonable busy_timeout would wait.

        This version commits every `batch_size` files (short, sub-second
        transactions) using INSERT OR REPLACE, so results for already-
        scanned files become searchable WHILE the scan is still running,
        and no single write transaction is ever long enough to meaningfully
        block a concurrent reader. Deletion is handled as mark-and-sweep at
        the end (remove rows for paths not seen in this pass) instead of
        an upfront DELETE — that upfront delete was the specific thing that
        made concurrent search see a suddenly-EMPTY table on every refresh,
        not just on the first build.

        SECOND FIX (this version): `files_fts` declares `path UNINDEXED`
        (see _init_db) so FTS5 has no lookup structure for it at all --
        `DELETE FROM files_fts WHERE path = ?` is a full scan of the
        *entire current table*, not an indexed lookup. flush() used to run
        one such DELETE per file, every batch, unconditionally. On a cold
        build that table grows from 0 to hundreds of thousands of rows
        over ~100-200 batches, so later batches were each scanning a
        table hundreds of thousands of rows deep, thousands of times per
        batch -- classic O(n^2), and the actual reason a "parallel" scan
        still took 10-20 minutes. On a true cold build there is nothing to
        delete (no row can possibly already exist for a path that's never
        been indexed), so cold_build skips that step entirely -- see
        flush() below. Warm rebuilds (index already populated) still do
        the delete-before-insert, since there a path genuinely might
        already have an FTS row to retire.
        """
        root_str = str(self.sandbox_root)
        cold_build = self._root_is_empty(root_str)
        if cold_build:
            print(f"[FileIndex] Cold build for {root_str} -- skipping per-row "
                  f"FTS5 delete-before-insert (nothing exists yet to delete). "
                  f"This was the actual source of multi-minute build times.")
        visited_real_dirs: set[str] = set()
        seen_paths: set[str] = set()
        batch: list[tuple] = []
        total = 0
        batch_lock = threading.Lock()

        def flush(rows: list[tuple]) -> None:
            if not rows:
                return
            with self._lock, self._connect() as conn:
                # IMPORTANT: this existing-path check must run BEFORE the
                # INSERT OR REPLACE below. If it ran after, every path in the
                # batch would already have just been (re)written to `files`
                # and would incorrectly look "existing" every time.
                if self._trigram_available:
                    # A path's (path, name) row in files_fts only ever needs to
                    # change together with the path itself -- mtime/content
                    # changes on an EXISTING path never make its files_fts row
                    # stale, because `name` is derived purely from `path`, not
                    # from mtime. So the only rows that ever need a files_fts
                    # insert are ones NEW to this root; existing paths already
                    # have a correct row from the first time they were seen.
                    #
                    # cold_build short-circuits the "is this path new" check
                    # entirely (we already know the table was empty), which
                    # matters for the FIRST build. But the check itself is
                    # what matters for every build AFTER that (periodic
                    # refreshes, manual rebuilds) -- those used to re-run the
                    # same unconditional "DELETE FROM files_fts WHERE path=?"
                    # for every single file on every single rescan, and since
                    # `path` is UNINDEXED (see _init_db), each delete scanned
                    # the ENTIRE current table. On an already-populated
                    # 300-400K row index, that's not a one-time cost -- it's
                    # paid again on every hourly refresh, indefinitely. This
                    # indexed existing-path lookup (root index + path PK)
                    # replaces that full-table-scan delete for good.
                    if cold_build:
                        new_rows = rows
                    else:
                        batch_paths = [r[0] for r in rows]
                        placeholders = ",".join("?" * len(batch_paths))
                        existing = {
                            r[0] for r in conn.execute(
                                f"SELECT path FROM files WHERE root = ? AND path IN ({placeholders})",
                                [root_str, *batch_paths],
                            ).fetchall()
                        }
                        new_rows = [r for r in rows if r[0] not in existing]
                else:
                    new_rows = []

                conn.executemany(
                    "INSERT OR REPLACE INTO files (path, name, stem, ext, size_bytes, mtime, root) "
                    "VALUES (?,?,?,?,?,?,?)",
                    rows,
                )
                if new_rows:
                    conn.executemany(
                        "INSERT INTO files_fts (path, name) VALUES (?, ?)",
                        [(r[0], r[1]) for r in new_rows],
                    )
                conn.commit()

        def flush_batch(force=False):
            nonlocal batch, total
            rows_to_flush = []
            with batch_lock:
                if not batch: return
                if not force and len(batch) < batch_size: return
                rows_to_flush = batch
                batch = []
                
            flush(rows_to_flush)
            
            with batch_lock:
                self.indexed_so_far = total
                if progress_cb:
                    progress_cb(total)

        # entry.path is always root_prefix + <suffix> by construction (os.scandir
        # builds it via plain concatenation of the scanned dirpath + entry name,
        # and dirpath itself only ever descends from root_str via that same
        # concatenation) -- so a slice is equivalent to os.path.relpath here,
        # without relpath's per-call abspath/split/join overhead. At 300K+ calls
        # that overhead is small individually but adds up; the fallback keeps
        # this safe if the prefix invariant is ever violated (e.g. odd casing).
        root_prefix = root_str if root_str.endswith(os.sep) else root_str + os.sep
        root_prefix_len = len(root_prefix)

        def _rel_path(entry_path: str) -> str:
            if entry_path.startswith(root_prefix):
                return entry_path[root_prefix_len:].replace("\\", "/")
            return os.path.relpath(entry_path, root_str).replace("\\", "/")

        def scan_dir(dirpath: str):
            nonlocal total
            try:
                real_dirpath = os.path.realpath(dirpath)
            except OSError:
                return []
                
            with batch_lock:
                if real_dirpath in visited_real_dirs:
                    return []
                visited_real_dirs.add(real_dirpath)
                
            subdirs = []
            local_files = []
            local_rel_paths = []
            
            try:
                with os.scandir(dirpath) as it:
                    for entry in it:
                        name_lower = entry.name.lower()
                        if name_lower in self._skip_names:
                            continue
                            
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                        except OSError:
                            continue
                            
                        if is_dir:
                            subdirs.append(entry.path)
                        else:
                            try:
                                stat = entry.stat(follow_symlinks=False)
                                rel_path = _rel_path(entry.path)
                            except (OSError, ValueError):
                                continue
                                
                            stem = Path(entry.name).stem.lower()
                            ext = Path(entry.name).suffix.lstrip(".").lower()
                            
                            local_files.append((rel_path, entry.name, stem, ext, stat.st_size, stat.st_mtime, root_str))
                            local_rel_paths.append(rel_path)
            except OSError:
                return []
                
            if local_files:
                # One lock acquisition per DIRECTORY instead of one per FILE --
                # with 32 worker threads sharing batch_lock, per-file locking
                # was serializing this bookkeeping roughly as often as there
                # were files, not as often as there were directories.
                with batch_lock:
                    seen_paths.update(local_rel_paths)
                    total += len(local_files)
                    batch.extend(local_files)
                flush_batch()
                
            return subdirs

        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
            futures = set()
            futures.add(executor.submit(scan_dir, root_str))
            
            while futures:
                done, futures = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED)
                for fut in done:
                    try:
                        for subdir in fut.result():
                            futures.add(executor.submit(scan_dir, subdir))
                    except Exception:
                        pass

        flush_batch(force=True)
        self.indexed_so_far = total

        # Mark-and-sweep: remove rows for this root that weren't seen in
        # this pass (files/directories deleted since the last build).
        # Skipped on a cold-start build against an empty table — nothing to
        # sweep, and it would otherwise cost one extra full-table read for
        # no reason on the exact case (first run) where speed matters most.
        with self._lock, self._connect() as conn:
            existing = {
                r[0] for r in conn.execute(
                    "SELECT path FROM files WHERE root = ?", (root_str,)
                ).fetchall()
            }
        stale = existing - seen_paths
        if stale:
            with self._lock, self._connect() as conn:
                conn.executemany(
                    "DELETE FROM files WHERE root = ? AND path = ?",
                    [(root_str, p) for p in stale],
                )
                if self._trigram_available:
                    conn.executemany(
                        "DELETE FROM files_fts WHERE path = ?", [(p,) for p in stale]
                    )
                conn.commit()

        return total

    def search(
        self,
        *,
        filename_fragment: Optional[str] = None,
        file_type: Optional[str] = None,
        modified_date: Optional[str] = None,
        content_keyword: Optional[str] = None,
        max_results: int = 10,
    ) -> list[dict]:
        root_str = str(self.sandbox_root)
        with self._lock, self._connect() as conn:
            conn.row_factory = sqlite3.Row

            candidate_paths: Optional[set[str]] = None
            if filename_fragment:
                frag = filename_fragment.lower().strip()
                if self._trigram_available and len(frag) >= 3:
                    # Clean and extract search terms of length >= 2
                    terms = [t for t in re.findall(r"[a-z0-9]+", frag) if len(t) >= 2]
                    processed_terms = []
                    
                    for t in terms:
                        if len(t) < 3:
                            processed_terms.append(t)
                            continue
                        
                        # Quick check: does this term match anything on its own?
                        cur_check = conn.execute(
                            "SELECT EXISTS(SELECT 1 FROM files_fts WHERE files_fts MATCH ? LIMIT 1)",
                            (f'"{t}"',),
                        ).fetchone()
                        
                        if cur_check and cur_check[0]:
                            processed_terms.append(t)
                        elif len(t) >= 6:
                            # If it matches nothing and is long, try splitting it into two sub-words
                            split_found = False
                            for i in range(3, len(t) - 2):
                                p1, p2 = t[:i], t[i:]
                                split_query = f"{p1} AND {p2}"
                                cur_split = conn.execute(
                                    "SELECT EXISTS(SELECT 1 FROM files_fts WHERE files_fts MATCH ? LIMIT 1)",
                                    (split_query,),
                                ).fetchone()
                                if cur_split and cur_split[0]:
                                    processed_terms.append(f"({p1} AND {p2})")
                                    split_found = True
                                    break
                            if not split_found:
                                processed_terms.append(t)
                        else:
                            processed_terms.append(t)
                            
                    fts_query = " AND ".join(processed_terms) if processed_terms else ""
                    if fts_query:
                        cur = conn.execute(
                            "SELECT path FROM files_fts WHERE files_fts MATCH ? LIMIT 500",
                            (fts_query,),
                        )
                        candidate_paths = {r["path"] for r in cur.fetchall()}
                    else:
                        candidate_paths = set()
                else:
                    cur = conn.execute(
                        "SELECT path FROM files WHERE root = ? AND (name LIKE ? OR stem = ?) LIMIT 500",
                        (root_str, f"%{frag}%", frag),
                    )
                    candidate_paths = {r["path"] for r in cur.fetchall()}
                if not candidate_paths:
                    return []

            query = "SELECT path, name, stem, ext, size_bytes, mtime FROM files WHERE root = ?"
            params: list = [root_str]
            if candidate_paths is not None:
                # candidate_paths came from the FTS5/LIKE narrowing above
                # (capped at 500 rows). Push it into SQL as an IN-list instead
                # of pulling EVERY row for this root into Python and discarding
                # non-matches afterward -- on a 300-400K row index that was the
                # difference between touching ~500 candidate rows and
                # materializing the entire table into Python on every
                # filename-fragment search (the most common voice-query shape).
                if not candidate_paths:
                    return []
                placeholders = ",".join("?" * len(candidate_paths))
                query += f" AND path IN ({placeholders})"
                params.extend(candidate_paths)
            if file_type:
                query += " AND ext = ?"
                params.append(file_type.lower().lstrip("."))
            if modified_date:
                query += " AND date(mtime, 'unixepoch') = ?"
                params.append(modified_date)

            rows = conn.execute(query, params).fetchall()

        results = []
        for r in rows:
            if content_keyword and r["ext"] in {"txt", "md"}:
                abs_path = self.sandbox_root / r["path"]
                try:
                    text = abs_path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if content_keyword.lower() not in text.lower():
                    continue

            score = _match_score(filename_fragment, r["name"]) if filename_fragment else 0
            results.append({
                "name": r["name"],
                "path": r["path"],
                "size_bytes": r["size_bytes"],
                "modified_iso": _iso_from_mtime(r["mtime"]),
                "file_type": r["ext"],
                "match_score": score,
            })

        results.sort(key=lambda r: (r["match_score"], r["modified_iso"]), reverse=True)
        return results[:max_results]


def _match_score(fragment: Optional[str], filename: str) -> int:
    """Same word-boundary-aware scoring as file_search.py's ranking fix:
    3 = exact stem match, 2 = whole-token match, 1 = incidental substring."""
    if not fragment:
        return 0
    frag = fragment.lower().strip()
    name = filename.lower()
    stem = Path(name).stem
    if frag == stem:
        return 3
    tokens = re.split(r"[_\-.\s]+", stem)
    if frag in tokens:
        return 2
        
    # Check if all words of the fragment match stem tokens
    frag_words = re.findall(r"[a-z0-9]+", frag)
    if frag_words and all(w in tokens for w in frag_words):
        return 2
        
    if frag in name:
        return 1
        
    # Normalize space/underscores for substring match fallback
    norm_frag = re.sub(r"[_\-.\s]+", "", frag)
    norm_name = re.sub(r"[_\-.\s]+", "", name)
    if norm_frag in norm_name:
        return 1
        
    return 0


def _iso_from_mtime(mtime: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


# One FileIndex per sandbox root, cached module-level (mirrors audit.py's
# singleton pattern) so repeated calls reuse the same object/connection
# pool rather than re-opening the database every search.
_indexes: dict[str, FileIndex] = {}
_indexes_lock = threading.Lock()


def get_index(sandbox_root: Path) -> FileIndex:
    key = str(os.path.realpath(str(sandbox_root)))
    with _indexes_lock:
        idx = _indexes.get(key)
        if idx is None:
            idx = FileIndex(sandbox_root)
            _indexes[key] = idx
    return idx


def ensure_built(sandbox_root: Path, background_refresh: bool = True) -> FileIndex:
    """
    Get-or-build: kicks off the initial scan in a BACKGROUND THREAD and
    returns immediately — it used to block synchronously here, which is
    exactly what turned a live voice request into a multi-minute hang on a
    300K+ file root (see build()'s docstring for the full failure this was
    rewritten in response to). search() against a FileIndex mid-build
    simply returns whatever's been indexed so far — a real, growing subset
    of results rather than nothing, thanks to build()'s batched commits.

    Prefer calling this once, early, at process startup (before any voice
    input is accepted) rather than relying solely on the lazy first-search
    trigger below — that gives the scan a head start during STT/TTS/VAD
    model loading, which already takes real time anyway. It's still safe
    to call lazily; the first search just won't see indexed results as
    quickly if the build hasn't had time to progress yet.
    """
    idx = get_index(sandbox_root)

    with idx._lock:
        start_build = not idx._build_started
        if start_build:
            idx._build_started = True

    if start_build:
        def _run_initial_build():
            idx.is_building = True
            caught_up = False
            try:
                from echolocate.mcp_server.usn_catchup import catch_up
                caught_up = catch_up(idx, idx.sandbox_root, verbose=False)
            except ImportError:
                pass  # pywin32 or usn_catchup unavailable -- full build below
            except Exception as exc:
                pass

            if not caught_up:
                is_empty = idx._root_is_empty(str(idx.sandbox_root))
                if not is_empty:
                    pass
                else:
                    try:
                        n = idx.build(progress_cb=lambda c: None)
                        idx._record_journal_cursor_best_effort()
                    except Exception as exc:
                        pass
            idx.is_building = False

            if background_refresh and not idx._monitoring_started:
                idx._monitoring_started = True
                watching = idx.start_watching()
                idx.start_background_refresh(interval_seconds=3600 if watching else 300)

        threading.Thread(target=_run_initial_build, daemon=True).start()

    return idx
