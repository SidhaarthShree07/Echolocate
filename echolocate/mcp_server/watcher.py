"""
EchoLocate — real-time file index updates via watchdog.

Wraps watchdog's Observer + FileSystemEventHandler to keep index.py's
SQLite index in sync with the actual filesystem within seconds of a
change, instead of waiting for the periodic safety-net rescan
(index.py's start_background_refresh). See index.py's start_watching()
docstring for the fallback behavior when this can't start (watchdog
missing, or the platform's native watch mechanism fails to register).

Native mechanism per platform (all via watchdog — no direct OS API code
here): Windows ReadDirectoryChangesW (a single recursive watch, which
scales well even for a whole-drive sandbox root — this is the platform
that matters most for this project's actual usage); Linux inotify (one
watch PER DIRECTORY, subject to the fs.inotify.max_user_watches limit,
commonly 8192 by default — a real constraint on a huge/broad root on
Linux specifically, raise it via /etc/sysctl.conf if needed); macOS
FSEvents (watchdog prefers this over kqueue, which needs one open file
descriptor per watched file and doesn't scale to large trees).

Debouncing: filesystem events often arrive in bursts (an editor doing
multiple writes on save, an app extracting many files at once). Instead
of hitting SQLite once per raw event, changes are queued and flushed in
small batches on a short interval — same end result, cheaper under burst
load, and avoids interleaving a flood of tiny writes with a concurrent
search query.
"""
from __future__ import annotations

import os
import threading
import time
from queue import Empty, Queue

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class IndexWatcher:
    """
    Owns a watchdog Observer scheduled recursively on a FileIndex's
    sandbox root, batching filesystem events into incremental index
    updates (FileIndex.upsert_file / remove_path).
    """

    def __init__(self, file_index, flush_interval: float = 0.3) -> None:
        self._index = file_index
        self._flush_interval = flush_interval
        self._queue: Queue = Queue()
        self._observer = Observer()
        self._handler = _Handler(self._queue)

    def start(self) -> None:
        # recursive=True is what makes this ONE call cover the entire
        # sandbox tree — on Windows this compiles down to a single
        # ReadDirectoryChangesW watch with the subtree flag set, not one
        # watch per folder, which is why a whole-drive root stays viable.
        self._observer.schedule(self._handler, str(self._index.sandbox_root), recursive=True)
        self._observer.start()
        threading.Thread(target=self._flush_loop, daemon=True).start()

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join(timeout=5)

    def _flush_loop(self) -> None:
        pending_upserts: dict[str, str] = {}
        pending_removals: dict[str, str] = {}
        while True:
            time.sleep(self._flush_interval)

            while True:
                try:
                    kind, path = self._queue.get_nowait()
                except Empty:
                    break
                if kind == "upsert":
                    pending_removals.pop(path, None)
                    pending_upserts[path] = path
                elif kind == "remove":
                    pending_upserts.pop(path, None)
                    pending_removals[path] = path
                elif kind == "rebuild":
                    # Directory move — cheapest correct response is a full
                    # rebuild rather than hand-walking the moved subtree.
                    pending_upserts.clear()
                    pending_removals.clear()
                    try:
                        self._index.build()
                    except Exception as exc:
                        print(f"[IndexWatcher] rebuild-after-move failed: {exc}")

            if not pending_upserts and not pending_removals:
                continue

            for path in list(pending_removals):
                try:
                    self._index.remove_path(path)
                except Exception as exc:
                    print(f"[IndexWatcher] remove failed for {path}: {exc}")
            pending_removals.clear()

            for path in list(pending_upserts):
                try:
                    if os.path.isfile(path):
                        self._index.upsert_file(path)
                except Exception as exc:
                    print(f"[IndexWatcher] upsert failed for {path}: {exc}")
            pending_upserts.clear()


class _Handler(FileSystemEventHandler):
    """
    Translates raw watchdog events into (kind, path) tuples on the shared
    queue. Directory create/modify events are ignored — a newly created
    subdirectory is automatically covered by watchdog's own recursive
    watch (it registers a native watch for it; no action needed here),
    and there's nothing to upsert about a directory itself (only files get
    index rows).
    """

    def __init__(self, queue: Queue) -> None:
        self._queue = queue

    def on_created(self, event):
        if not event.is_directory:
            self._queue.put(("upsert", event.src_path))

    def on_modified(self, event):
        if not event.is_directory:
            self._queue.put(("upsert", event.src_path))

    def on_deleted(self, event):
        # Windows note (from watchdog's own docs): ReadDirectoryChangesW
        # doesn't tell watchdog whether a deleted object was a file or a
        # directory, so a directory delete can arrive shaped as a
        # file-deleted event. remove_path() handles this correctly either
        # way — it deletes the exact-path row AND anything under a
        # matching path prefix, so a deleted directory's contents are
        # cleaned up regardless of which shape the event arrived in.
        self._queue.put(("remove", event.src_path))

    def on_moved(self, event):
        if event.is_directory:
            self._queue.put(("rebuild", event.src_path))
        else:
            self._queue.put(("remove", event.src_path))
            self._queue.put(("upsert", event.dest_path))
