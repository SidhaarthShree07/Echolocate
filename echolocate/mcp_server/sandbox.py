"""
EchoLocate — sandbox path resolution and enforcement.

THIS IS THE ONLY SOURCE OF SAFETY TRUTH IN THE SYSTEM.

No LLM output, tool annotation, or classifier confidence score is trusted as
a safety guarantee. Only the deterministic path-resolution check in this
module is. See Architecture Section 4.4 and Section 7.

The design handles two vulnerability classes documented with real CVE
precedent:

1. PATH_MAX symlink truncation (CVE-2025-4517, CVE-2025-4330):
   os.path.realpath(strict=False) can silently stop resolving symlinks once
   the resolved path exceeds PATH_MAX, letting a crafted symlink chain pass
   validation. strict=True raises instead of silently truncating — this is
   the primary defense on ALL platforms.

2. TOCTOU race (CVE-2026-27905, CVE-2026-31979):
   Any design that validates a path and then performs the file operation as a
   separate step leaves a race window. On Unix, O_NOFOLLOW closes this at
   the kernel level for the final path component. On Windows, os.O_NOFOLLOW
   does not exist (confirmed against CPython docs — raises AttributeError on
   win32) so safe_open falls back to canonicalization-only; this residual
   risk is documented and accepted given the single-user desktop threat model.

See safe_open()'s docstring for the full Windows vs. Unix difference.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"


class SandboxViolation(Exception):
    """
    Raised when a requested path resolves outside the sandbox root, or when
    an absolute path or null byte is detected in the input.

    This exception is caught at the node boundary and returned to the user
    as a spoken "I can't access that location" message — never as a raw
    traceback. It is always written to the audit log with outcome "rejected".
    """


def resolve_and_check(
    path: str,
    sandbox_root: Path,
    *,
    must_exist: bool = True,
) -> Path:
    """
    Canonicalize *path* relative to *sandbox_root* and verify it resolves
    inside the sandbox. Raises SandboxViolation if any check fails.

    Returns the validated, canonicalized absolute path as a Path object.

    This function is the PRIMARY safety check — it runs identically on all
    platforms (strict=True realpath resolves NTFS reparse points/junctions
    correctly on Windows as well as symlinks on Unix). safe_open() MUST be
    used to actually open the returned path to avoid leaving a TOCTOU race
    window on Unix (see safe_open() docstring).

    Args:
        path: A user-supplied, sandbox-relative path string. Must be relative
              (not absolute) and must not contain null bytes.
        sandbox_root: The absolute path to the designated sandbox directory.
        must_exist: If True (default), the target path must exist on disk —
                    strict=True will raise FileNotFoundError if it doesn't.
                    Set False for destination paths in move/create operations
                    where the parent directory must exist but the file itself
                    may not yet.
    """
    # --- Pre-checks on the raw string ---
    if not path or path.strip() == "":
        raise SandboxViolation("empty path rejected")

    if os.path.isabs(path):
        raise SandboxViolation(f"absolute paths rejected: {path!r}")

    if "\x00" in path:
        raise SandboxViolation(f"null byte in path rejected: {path!r}")

    # Reject obvious traversal attempts early (belt-and-suspenders;
    # realpath canonicalization below would also catch these, but being
    # explicit here makes tests and audit logs clearer).
    if ".." in Path(path).parts:
        raise SandboxViolation(f"path traversal attempt rejected: {path!r}")

    # --- Resolve the sandbox root itself ---
    # strict=True ensures the sandbox root itself actually exists and is fully
    # canonicalized — a misconfigured sandbox_root should fail loudly at
    # startup, not silently accept or reject everything.
    try:
        sandbox_resolved = os.path.realpath(str(sandbox_root), strict=True)
    except (OSError, ValueError) as exc:
        raise SandboxViolation(
            f"sandbox root cannot be resolved: {sandbox_root!r} — {exc}"
        ) from exc

    # --- Build the candidate absolute path ---
    candidate = os.path.normpath(os.path.join(sandbox_resolved, path))

    # --- Resolve the candidate (strict=True for exist-required paths) ---
    # For must_exist=False (e.g. move destination), we resolve the *parent*
    # with strict=True and then check containment — the parent must exist even
    # if the file itself doesn't. This keeps strict=True as the primary
    # defense while supporting write operations to new filenames.
    try:
        if must_exist:
            resolved = os.path.realpath(candidate, strict=True)
        else:
            parent_resolved = os.path.realpath(
                os.path.dirname(candidate), strict=True
            )
            resolved = os.path.join(
                parent_resolved, os.path.basename(candidate)
            )
    except (OSError, ValueError) as exc:
        raise SandboxViolation(
            f"path cannot be resolved: {path!r} — {exc}"
        ) from exc

    # --- Containment check ---
    try:
        common = os.path.commonpath([resolved, sandbox_resolved])
    except ValueError as exc:
        # commonpath raises ValueError when paths are on different drives
        # (Windows only). Treat as out-of-sandbox.
        raise SandboxViolation(
            f"path is on a different drive from sandbox: {path!r}"
        ) from exc

    if common != sandbox_resolved:
        raise SandboxViolation(
            f"path {path!r} resolves outside sandbox "
            f"({resolved!r} vs sandbox {sandbox_resolved!r})"
        )

    return Path(resolved if must_exist else candidate)


def safe_open(
    path: str,
    sandbox_root: Path,
    mode: str = "rb",
):
    """
    Validate *path* against *sandbox_root* and open the file.

    On Unix (Linux/macOS): uses O_NOFOLLOW so the kernel atomically refuses
    the open if the final path component is a symlink — this closes the TOCTOU
    race window between resolve_and_check() and the actual open() call. Note
    the documented residual scope limit: O_NOFOLLOW guards only the *final*
    path component; a symlink swap on an intermediate directory mid-request
    is not covered. This is an accepted risk given the single-user desktop
    threat model.

    On Windows: os.O_NOFOLLOW does not exist (Unix-only per CPython os module
    docs; absent from Windows constant set). The strict=True canonicalization
    in resolve_and_check() still catches symlinks/junctions at check time, but
    the narrow check-to-open TOCTOU race is not fully closed. This is a
    documented residual risk (Architecture Section 7.1), not a silent gap.
    A stretch-goal hardening for Windows would verify the opened handle's
    canonical path via ctypes GetFinalPathNameByHandleW — not required for MVP.

    Args:
        path: sandbox-relative path string
        sandbox_root: absolute path to the sandbox directory
        mode: file open mode ("rb", "wb", "r", "w", etc.)
    """
    must_exist = "r" in mode
    validated = resolve_and_check(path, sandbox_root, must_exist=must_exist)

    if IS_WINDOWS:
        # Windows: standard open — TOCTOU window exists but is documented
        return open(validated, mode)
    else:
        # Unix: O_NOFOLLOW prevents a symlink being swapped in after
        # resolve_and_check() returned
        base_flags = os.O_RDONLY if "r" in mode else os.O_WRONLY | os.O_CREAT
        flags = base_flags | os.O_NOFOLLOW
        try:
            fd = os.open(str(validated), flags)
        except OSError as exc:
            # ELOOP is raised by O_NOFOLLOW when the final component is a
            # symlink — translate to SandboxViolation for consistent handling
            import errno
            if exc.errno == errno.ELOOP:
                raise SandboxViolation(
                    f"symlink detected at open time (O_NOFOLLOW): {path!r}"
                ) from exc
            raise
        return os.fdopen(fd, mode)
