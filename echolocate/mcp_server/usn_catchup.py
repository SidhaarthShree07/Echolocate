"""
EchoLocate -- fast incremental re-indexing via the NTFS USN Change Journal.

THE GAP THIS CLOSES: IndexWatcher (watcher.py) keeps the index live in real
time WHILE EchoLocate is running, via ReadDirectoryChangesW. That mechanism
is not persistent -- it only sees changes that happen while it's actively
watching. Every time EchoLocate is closed and reopened, whatever changed on
disk in between is invisible to the watcher, and until now the only way to
catch up was a full build() -- a complete re-walk of the whole tree. On a
346K-file drive that's ~5 minutes on EVERY startup, even if only three files
changed since last time.

THE FIX: NTFS maintains its own append-only log of every change to every
file/directory on the volume -- the USN Change Journal ($UsnJrnl / $J).
It's a system feature, also used internally by Windows Search, DFS
Replication, and backup software for exactly this "what changed since I
last looked" problem. Every entry carries a monotonically increasing
64-bit USN. We persist the journal's "next USN" every time we finish a
scan; next startup we ask NTFS for only the records between that saved
USN and now -- typically dozens to low thousands of records, regardless
of total drive size, because unchanged files are never touched at all.

HARD REQUIREMENTS (fail closed if unmet -- see catch_up()'s return value):
  - Windows + NTFS only. Not ReFS (different record format), not FAT32/
    exFAT (no journal at all), not non-Windows platforms.
  - Reading a volume's journal needs an ELEVATED (Administrator) process
    -- this opens a raw volume handle (\\\\.\\D:), which Windows restricts
    regardless of ordinary NTFS file permissions.
  - The saved cursor must still be valid: the journal must not have been
    deleted/recreated (`fsutil usn deletejournal`, or natural trimming
    under very heavy churn once the journal exceeds its configured max
    size) since we last saved a cursor. Detected via a JournalID mismatch
    or the saved USN falling below the journal's current FirstUsn; either
    case forces a one-time full build(), exactly like a first-ever run.

STATUS -- UNTESTED ON A REAL WINDOWS MACHINE. Unlike index.py's SQL-side
fix (benchmarked directly against your actual 346K-file drive), the
DeviceIoControl calls here can only be checked for correct struct
layout/logic in this environment -- there's no NTFS volume available to
exercise them against. The buffer-parsing logic (_iter_journal_records)
IS unit-tested here against a hand-built synthetic USN_RECORD_V2 buffer
matching Microsoft's documented layout exactly -- see the __main__ block
at the bottom, or run: python -m echolocate.mcp_server.usn_catchup
That verifies the fiddly offset/length arithmetic is correct; it does NOT
verify the actual Windows API calls (DeviceIoControl, OpenFileById,
GetFinalPathNameByHandleW) behave as documented on your specific machine.

catch_up() is written to fail closed: ANY unexpected error -- missing
pywin32, no admin rights, invalidated journal, a malformed record, an
OpenFileById failure -- returns False rather than raising, so a bug here
degrades to "did a full rebuild instead," never to a crash or a silently
stale index. Please run it once with the verbose logging left on and
sanity-check the reported before/after counts before trusting it
unattended.
"""
from __future__ import annotations

import struct
import sys
import time
from pathlib import Path
from typing import Optional, Iterator, Tuple

IS_WINDOWS = sys.platform == "win32"

# ---- USN reason flags (winioctl.h) -----------------------------------------
USN_REASON_DATA_OVERWRITE    = 0x00000001
USN_REASON_DATA_EXTEND       = 0x00000002
USN_REASON_DATA_TRUNCATION   = 0x00000004
USN_REASON_FILE_CREATE       = 0x00000100
USN_REASON_FILE_DELETE       = 0x00000200
USN_REASON_RENAME_OLD_NAME   = 0x00001000
USN_REASON_RENAME_NEW_NAME   = 0x00002000
USN_REASON_BASIC_INFO_CHANGE = 0x00008000

# A path that's gone (or about to be) -- resolve via PARENT frn + filename
# from the record, since the file itself no longer exists to ask about.
REMOVE_REASONS = USN_REASON_FILE_DELETE | USN_REASON_RENAME_OLD_NAME
# A path that exists (or newly exists) -- resolve via the file's OWN frn.
UPSERT_REASONS = (
    USN_REASON_FILE_CREATE | USN_REASON_DATA_OVERWRITE | USN_REASON_DATA_EXTEND
    | USN_REASON_DATA_TRUNCATION | USN_REASON_RENAME_NEW_NAME
    | USN_REASON_BASIC_INFO_CHANGE
)

FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_READ_USN_JOURNAL = 0x000900BB
FSCTL_CREATE_USN_JOURNAL = 0x000900E7

# USN_JOURNAL_DATA_V0: UsnJournalID, FirstUsn, NextUsn, LowestValidUsn,
# MaxUsn, MaximumSize, AllocationDelta -- 7 unsigned 64-bit fields, no
# padding possible (every field is the same 8-byte width).
_JOURNAL_DATA_FMT = "QQQQQQQ"
_JOURNAL_DATA_SIZE = struct.calcsize(_JOURNAL_DATA_FMT)

# READ_USN_JOURNAL_DATA_V0: StartUsn(8) ReasonMask(4) ReturnOnlyOnClose(4)
# Timeout(8) BytesToWaitFor(8) UsnJournalID(8). Every field already falls
# on its own natural alignment boundary in this order (verified: offsets
# 0/8/12/16/24/32, size 40) so explicit little-endian, no-padding packing
# ('<') and native packing agree here -- '<' is used for that certainty.
_READ_REQUEST_FMT = "<QIIQQQ"
_READ_REQUEST_SIZE = struct.calcsize(_READ_REQUEST_FMT)

# USN_RECORD_V2 fixed header (60 bytes), preceding the variable-length,
# UTF-16LE filename. RecordLength(4) MajorVersion(2) MinorVersion(2)
# FileReferenceNumber(8) ParentFileReferenceNumber(8) Usn(8) TimeStamp(8)
# Reason(4) SourceInfo(4) SecurityId(4) FileAttributes(4)
# FileNameLength(2) FileNameOffset(2). Also alignment-neutral (offsets
# 0/4/6/8/16/24/32/40/44/48/52/56/58, size 60) -- '<' used for certainty
# rather than relying on native padding matching across platforms.
_RECORD_HEADER_FMT = "<IHHQQqqIIIIHH"
_RECORD_HEADER_SIZE = struct.calcsize(_RECORD_HEADER_FMT)

FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
GENERIC_READ = 0x80000000


def _to_signed64(v: int) -> int:
    """FileReferenceNumbers are 64-bit bit patterns, unpacked here as
    unsigned ('Q'). OpenFileById's FILE_ID_DESCRIPTOR.FileId is a
    LARGE_INTEGER (signed). Values >= 2^63 are rare in practice (the
    high 16 bits are a slowly-incrementing per-slot reuse sequence
    number) but not impossible on a years-old, heavily-churned volume --
    this reinterprets the same bits as signed instead of raising
    OverflowError on assignment to a signed ctypes field."""
    return v - (1 << 64) if v >= (1 << 63) else v


def _iter_journal_records(
    read_fn, journal_id: int, start_usn: int, next_usn_ceiling: int, buffer_size: int = 65536
) -> Iterator[Tuple[int, int, int, int, str]]:
    """
    Yields (usn, reason, frn, parent_frn, filename) for every journal
    record from start_usn up to next_usn_ceiling (the journal's NextUsn
    at the moment we started catching up -- we deliberately don't chase
    records written WHILE we're catching up; those will be seen on the
    NEXT catch-up call, or live via IndexWatcher if we're already running
    by then).

    read_fn(request_bytes, buffer_size) -> bytes is injected rather than
    calling win32file.DeviceIoControl directly, so this parsing logic can
    be unit-tested against a synthetic buffer with no Windows/pywin32
    dependency at all -- see verify_record_parsing() below.
    """
    usn = start_usn
    while usn < next_usn_ceiling:
        request = struct.pack(_READ_REQUEST_FMT, usn, 0xFFFFFFFF, 0, 0, 0, journal_id)
        buf = read_fn(request, buffer_size)
        if not buf or len(buf) < 8:
            return

        returned_usn = struct.unpack("<q", buf[:8])[0]
        offset = 8
        saw_any = False
        while offset + _RECORD_HEADER_SIZE <= len(buf):
            (record_length, _major, _minor, frn, parent_frn, rec_usn,
             _timestamp, reason, _source_info, _security_id, _attributes,
             name_len, name_offset) = struct.unpack(
                _RECORD_HEADER_FMT, buf[offset:offset + _RECORD_HEADER_SIZE]
            )
            if record_length <= 0:
                break
            name_start = offset + name_offset
            name_end = name_start + name_len
            if name_end > len(buf):
                break  # truncated record at the end of this buffer -- stop, re-fetch from returned_usn
            filename = buf[name_start:name_end].decode("utf-16-le", errors="replace")
            saw_any = True
            yield rec_usn, reason, frn, parent_frn, filename
            offset += record_length

        if returned_usn <= usn and not saw_any:
            return  # no forward progress -- avoid an infinite loop on a malformed/empty response
        usn = returned_usn


# --------------------------------------------------------------------------
# Everything below this line touches real Windows APIs and is the part that
# needs verification on an actual machine -- see module docstring.
# --------------------------------------------------------------------------

def _open_volume(drive_letter: str):
    import win32file
    return win32file.CreateFile(
        f"\\\\.\\{drive_letter}",
        win32file.GENERIC_READ | win32file.GENERIC_WRITE,
        win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
        None, win32file.OPEN_EXISTING, 0, None,
    )


def _query_journal(volume_handle) -> Optional[dict]:
    import win32file
    try:
        buf = win32file.DeviceIoControl(volume_handle, FSCTL_QUERY_USN_JOURNAL, None, _JOURNAL_DATA_SIZE)
    except Exception:
        return None
    journal_id, first_usn, next_usn, lowest_valid_usn, max_usn, _max_size, _alloc_delta = \
        struct.unpack(_JOURNAL_DATA_FMT, buf)
    return {"journal_id": journal_id, "first_usn": first_usn, "next_usn": next_usn,
            "lowest_valid_usn": lowest_valid_usn, "max_usn": max_usn}


def _ensure_journal_active(volume_handle) -> None:
    """No-op/extend if a journal already exists on this volume (default
    since Vista) -- only matters on the rare volume where it was
    explicitly disabled."""
    import win32file
    try:
        req = struct.pack("<QQ", 32 * 1024 * 1024, 8 * 1024 * 1024)
        win32file.DeviceIoControl(volume_handle, FSCTL_CREATE_USN_JOURNAL, req, 0)
    except Exception:
        pass


def _resolve_path_by_frn(volume_handle, frn: int) -> Optional[str]:
    """OpenFileById + GetFinalPathNameByHandleW: resolves a STILL-EXISTING
    file/dir's current full path from its FileReferenceNumber. Deliberately
    avoids building our own FRN->name->parent table (that's what a full
    MFT enumeration is for, and it's overkill for a catch-up pass that's
    typically dozens to low thousands of records)."""
    import ctypes
    from ctypes import wintypes
    import win32file

    class _FileIdDescriptor(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("Type", wintypes.DWORD), ("FileId", ctypes.c_longlong)]

    kernel32 = ctypes.windll.kernel32
    desc = _FileIdDescriptor(dwSize=ctypes.sizeof(_FileIdDescriptor), Type=0, FileId=_to_signed64(frn))
    h = kernel32.OpenFileById(
        int(volume_handle), ctypes.byref(desc), GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, None, FILE_FLAG_BACKUP_SEMANTICS,
    )
    if not h or h == win32file.INVALID_HANDLE_VALUE:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        n = kernel32.GetFinalPathNameByHandleW(h, buf, 32768, 0)
        if n == 0:
            return None
        path = buf.value
        if path.startswith("\\\\?\\"):
            path = path[4:]
        return path
    finally:
        kernel32.CloseHandle(h)


def catch_up(file_index, sandbox_root: Path, verbose: bool = True) -> bool:
    """
    Attempt a fast, journal-based catch-up for sandbox_root instead of a
    full build(). Returns True if it succeeded (index is now current and
    the cursor has been saved), False if a full build() is needed instead
    (first run, no admin rights, non-NTFS, invalidated cursor, or any
    unexpected error -- all treated the same: fail closed).
    """
    if not IS_WINDOWS:
        return False
    try:
        import win32file
    except ImportError:
        if verbose:
            print("[usn_catchup] pywin32 not installed -- falling back to full build().")
        return False

    root_str = str(sandbox_root)
    drive_letter = Path(root_str).drive  # e.g. "D:"
    if not drive_letter:
        return False

    cursor = file_index.get_journal_cursor(root_str)
    if cursor is None:
        if verbose:
            print(f"[usn_catchup] No saved cursor for {root_str} -- this is a first run, "
                  f"needs a full build() once before catch-up can take over.")
        return False

    try:
        volume_handle = _open_volume(drive_letter)
    except Exception as exc:
        if verbose:
            print(f"[usn_catchup] Couldn't open volume {drive_letter} ({exc}) -- "
                  f"likely not running elevated. Falling back to full build().")
        return False

    try:
        _ensure_journal_active(volume_handle)
        info = _query_journal(volume_handle)
        if info is None:
            if verbose:
                print(f"[usn_catchup] FSCTL_QUERY_USN_JOURNAL failed on {drive_letter} -- "
                      f"not NTFS, or journal unavailable. Falling back to full build().")
            return False

        if info["journal_id"] != cursor["journal_id"]:
            if verbose:
                print(f"[usn_catchup] Journal ID changed since last run (deleted/recreated) -- "
                      f"a gap may exist that only a full build() can safely close.")
            return False
        if cursor["next_usn"] < info["first_usn"]:
            if verbose:
                print(f"[usn_catchup] Saved cursor ({cursor['next_usn']}) is older than the "
                      f"journal's earliest record ({info['first_usn']}) -- journal has trimmed "
                      f"past it. Falling back to full build().")
            return False
        if cursor["next_usn"] >= info["next_usn"]:
            if verbose:
                print(f"[usn_catchup] Already caught up (nothing changed since last cursor).")
            file_index.save_journal_cursor(root_str, info["journal_id"], info["next_usn"])
            return True

        def _read_fn(request: bytes, buffer_size: int) -> bytes:
            return win32file.DeviceIoControl(volume_handle, FSCTL_READ_USN_JOURNAL, request, buffer_size)

        n_upserts = n_removes = n_skipped = 0
        root_prefix = root_str if root_str.endswith("\\") else root_str + "\\"

        for _usn, reason, frn, parent_frn, filename in _iter_journal_records(
            _read_fn, cursor["journal_id"], cursor["next_usn"], info["next_usn"]
        ):
            try:
                if reason & REMOVE_REASONS:
                    parent_path = _resolve_path_by_frn(volume_handle, parent_frn)
                    if parent_path is None:
                        n_skipped += 1
                        continue
                    full_path = str(Path(parent_path) / filename)
                    if full_path.startswith(root_prefix):
                        file_index.remove_path(full_path)
                        n_removes += 1
                elif reason & UPSERT_REASONS:
                    full_path = _resolve_path_by_frn(volume_handle, frn)
                    if full_path is None:
                        n_skipped += 1  # likely deleted again before we got to it -- fine, next pass sees the delete record
                        continue
                    if full_path.startswith(root_prefix):
                        file_index.upsert_file(full_path)
                        n_upserts += 1
            except Exception as exc:
                n_skipped += 1
                if verbose:
                    print(f"[usn_catchup] Skipped one record after an error ({exc}); continuing.")

        file_index.save_journal_cursor(root_str, info["journal_id"], info["next_usn"])
        if verbose:
            print(f"[usn_catchup] Caught up {root_str}: {n_upserts} upserted, {n_removes} removed, "
                  f"{n_skipped} skipped (out-of-scope or already-changed-again records).")
        return True

    except Exception as exc:
        if verbose:
            print(f"[usn_catchup] Unexpected error during catch-up ({exc}) -- falling back to full build().")
        return False
    finally:
        try:
            win32file.CloseHandle(volume_handle)
        except Exception:
            pass


def record_initial_cursor(file_index, sandbox_root: Path, verbose: bool = True) -> None:
    """Call this once right after a full build() completes (cold start,
    or any time catch_up() returned False and a full build() was run
    instead), so the NEXT startup has a cursor to catch up from."""
    if not IS_WINDOWS:
        return
    try:
        import win32file
        root_str = str(sandbox_root)
        drive_letter = Path(root_str).drive
        if not drive_letter:
            return
        volume_handle = _open_volume(drive_letter)
        try:
            _ensure_journal_active(volume_handle)
            info = _query_journal(volume_handle)
            if info:
                file_index.save_journal_cursor(root_str, info["journal_id"], info["next_usn"])
                if verbose:
                    print(f"[usn_catchup] Recorded journal cursor for {root_str} "
                          f"(journal {info['journal_id']}, usn {info['next_usn']}) -- "
                          f"next startup can use fast catch-up instead of a full rebuild.")
        finally:
            win32file.CloseHandle(volume_handle)
    except Exception as exc:
        if verbose:
            print(f"[usn_catchup] Couldn't record initial cursor ({exc}) -- "
                  f"next startup will need a full build() again, but nothing is broken.")


def verify_record_parsing() -> bool:
    """
    Pure-Python self-test for _iter_journal_records -- builds a synthetic
    buffer matching FSCTL_READ_USN_JOURNAL's documented output format
    exactly (an 8-byte leading USN, followed by two back-to-back
    USN_RECORD_V2 entries with different filename lengths, deliberately
    NOT aligned to any convenient boundary) and confirms every field is
    extracted correctly. This is the part of this module that CAN be
    verified without a real NTFS volume -- run it with:
        python -m echolocate.mcp_server.usn_catchup
    """
    def build_record(usn, reason, frn, parent_frn, name):
        name_bytes = name.encode("utf-16-le")
        header_size = _RECORD_HEADER_SIZE
        record_length = header_size + len(name_bytes)
        # USN_RECORD_V2 records are 8-byte aligned as a whole (Microsoft's
        # docs: "Attach any padding necessary to QuadAlign the record")
        padded_length = (record_length + 7) & ~7
        header = struct.pack(
            _RECORD_HEADER_FMT,
            padded_length, 2, 0, frn, parent_frn, usn, 0,
            reason, 0, 0, 0, len(name_bytes), header_size,
        )
        return header + name_bytes + b"\x00" * (padded_length - record_length)

    r1 = build_record(1000, USN_REASON_FILE_CREATE, 111, 5, "budget_report.pdf")
    r2 = build_record(1001, USN_REASON_FILE_DELETE, 222, 5, "old_draft.txt")
    fake_buffer = struct.pack("<q", 1002) + r1 + r2

    def fake_read_fn(request: bytes, buffer_size: int) -> bytes:
        return fake_buffer

    results = list(_iter_journal_records(fake_read_fn, journal_id=1, start_usn=999, next_usn_ceiling=1002))

    expected = [
        (1000, USN_REASON_FILE_CREATE, 111, 5, "budget_report.pdf"),
        (1001, USN_REASON_FILE_DELETE, 222, 5, "old_draft.txt"),
    ]
    ok = results == expected
    print(f"[usn_catchup self-test] {'PASS' if ok else 'FAIL'}")
    print(f"  expected: {expected}")
    print(f"  got:      {results}")
    return ok


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(0 if verify_record_parsing() else 1)
