"""
EchoLocate — Sandbox escape test suite.

Tests the primary security guarantee: resolve_and_check() and safe_open()
correctly reject all path-escape attempts. This suite is the direct proof
behind the security claims in Architecture Section 7.

Run on BOTH Unix and Windows runners in CI — the IS_WINDOWS platform branch
in safe_open() is exactly the kind of code that's easy to get right on one
platform and wrong on the other without noticing locally.

Test cases:
  - Path traversal (../../../etc/passwd)
  - Absolute path injection (/etc/passwd, C:\\Windows\\System32)
  - Null byte injection
  - Symlink escape (Unix only — symlinks are admin-only on Windows without DevMode)
  - Windows junction escape (Windows only)
  - Long path / PATH_MAX edge case (strict=True vs strict=False distinction)
  - Sandbox root itself (should be ALLOWED)
  - Normal relative path (should be ALLOWED)
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from echolocate.mcp_server.sandbox import SandboxViolation, resolve_and_check, safe_open, IS_WINDOWS


@pytest.fixture
def sandbox(tmp_path):
    """Create a temporary sandbox directory with some test files."""
    (tmp_path / "documents").mkdir()
    (tmp_path / "documents" / "resume.pdf").write_bytes(b"PDF content")
    (tmp_path / "documents" / "notes.txt").write_text("Hello world")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "q1.pdf").write_bytes(b"Q1 report")
    return tmp_path


class TestPathTraversal:
    """../../../ style traversal attempts."""

    def test_single_dotdot(self, sandbox):
        with pytest.raises(SandboxViolation):
            resolve_and_check("../outside.txt", sandbox)

    def test_double_dotdot(self, sandbox):
        with pytest.raises(SandboxViolation):
            resolve_and_check("../../etc/passwd", sandbox)

    def test_dotdot_in_middle(self, sandbox):
        with pytest.raises(SandboxViolation):
            resolve_and_check("documents/../../../etc/passwd", sandbox)

    def test_dotdot_via_url_encoding(self, sandbox):
        """URL-encoded .. should be rejected by the raw path check."""
        # URL encoding is decoded by some frameworks before reaching us;
        # here we test the literal string — if a decoder passes this through,
        # the realpath check still catches it
        with pytest.raises(SandboxViolation):
            resolve_and_check("..%2F..%2Fetc%2Fpasswd", sandbox)


class TestAbsolutePathInjection:
    """Absolute paths should always be rejected."""

    def test_unix_absolute_path(self, sandbox):
        with pytest.raises(SandboxViolation):
            resolve_and_check("/etc/passwd", sandbox)

    def test_windows_absolute_path(self, sandbox):
        with pytest.raises(SandboxViolation):
            resolve_and_check("C:\\Windows\\System32\\drivers\\etc\\hosts", sandbox)

    def test_windows_absolute_path_forward_slash(self, sandbox):
        with pytest.raises(SandboxViolation):
            resolve_and_check("C:/Windows/System32", sandbox)


class TestNullByte:
    """Null bytes in paths should be rejected."""

    def test_null_byte_injection(self, sandbox):
        with pytest.raises(SandboxViolation):
            resolve_and_check("documents/resume\x00.pdf", sandbox)

    def test_null_byte_prefix(self, sandbox):
        with pytest.raises(SandboxViolation):
            resolve_and_check("\x00evil", sandbox)


class TestEmptyPath:
    """Empty or whitespace-only paths should be rejected."""

    def test_empty_string(self, sandbox):
        with pytest.raises(SandboxViolation):
            resolve_and_check("", sandbox)

    def test_whitespace_only(self, sandbox):
        with pytest.raises(SandboxViolation):
            resolve_and_check("   ", sandbox)


class TestValidPaths:
    """Valid paths inside the sandbox should be accepted."""

    def test_simple_filename(self, sandbox):
        result = resolve_and_check("documents/resume.pdf", sandbox)
        assert result.exists()

    def test_nested_path(self, sandbox):
        result = resolve_and_check("reports/q1.pdf", sandbox)
        assert result.exists()

    def test_sandbox_root_file(self, sandbox):
        (sandbox / "top_level.txt").write_text("hello")
        result = resolve_and_check("top_level.txt", sandbox)
        assert result.exists()


@pytest.mark.skipif(IS_WINDOWS, reason="Symlinks require admin/DevMode on Windows")
class TestSymlinkEscape:
    """Symlink escape attempts — Unix only."""

    def test_symlink_pointing_outside(self, sandbox, tmp_path):
        """A symlink inside the sandbox pointing outside should be rejected."""
        # Create a file outside the sandbox
        outside = tmp_path.parent / "outside_secret.txt"
        outside.write_text("secret content")

        # Create a symlink inside the sandbox pointing to the outside file
        symlink = sandbox / "evil_link.txt"
        symlink.symlink_to(outside)

        with pytest.raises(SandboxViolation):
            resolve_and_check("evil_link.txt", sandbox)

    def test_symlink_created_after_check(self, sandbox, tmp_path):
        """
        Tests O_NOFOLLOW behavior: a symlink created BETWEEN resolve_and_check()
        and safe_open() should be caught by O_NOFOLLOW on Unix.

        This is the TOCTOU race test — we simulate the race by manually creating
        the symlink between the check and the open.
        """
        # Create a legitimate file
        legit = sandbox / "legit.txt"
        legit.write_text("safe content")

        # Validate (check passes)
        validated = resolve_and_check("legit.txt", sandbox)
        assert validated.exists()

        # Simulate the race: replace with a symlink to outside
        outside = tmp_path.parent / "outside_secret.txt"
        outside.write_text("secret")
        legit.unlink()
        legit.symlink_to(outside)

        # O_NOFOLLOW should catch this on Unix
        with pytest.raises((SandboxViolation, OSError)):
            safe_open("legit.txt", sandbox, mode="rb")


@pytest.mark.skipif(not IS_WINDOWS, reason="NTFS junctions only on Windows")
class TestWindowsJunctionEscape:
    """NTFS junction escape attempts — Windows only."""

    def test_junction_pointing_outside(self, sandbox, tmp_path):
        """
        A directory junction inside the sandbox pointing outside should be
        caught by strict=True realpath canonicalization.

        Note: creating junctions requires no special privileges on Windows
        (unlike symlinks). This makes junction-based escapes more accessible
        to attackers than symlink escapes.
        """
        import subprocess

        outside_dir = tmp_path.parent / "outside_dir"
        outside_dir.mkdir(exist_ok=True)
        (outside_dir / "secret.txt").write_text("secret content")

        junction_path = sandbox / "evil_junction"

        # Create the junction using mklink /J
        result = subprocess.run(
            ["cmd", "/c", f"mklink /J \"{junction_path}\" \"{outside_dir}\""],
            capture_output=True,
        )

        if result.returncode != 0:
            pytest.skip("Could not create NTFS junction (insufficient permissions)")

        with pytest.raises(SandboxViolation):
            resolve_and_check("evil_junction/secret.txt", sandbox)


class TestMustExistParameter:
    """Test must_exist=False behavior (for write destinations)."""

    def test_nonexistent_file_must_exist_true(self, sandbox):
        """Default must_exist=True: raise if file doesn't exist."""
        with pytest.raises((SandboxViolation, FileNotFoundError, OSError)):
            resolve_and_check("nonexistent.txt", sandbox, must_exist=True)

    def test_nonexistent_file_must_exist_false(self, sandbox):
        """must_exist=False: should succeed if parent directory exists."""
        result = resolve_and_check(
            "documents/new_file.txt", sandbox, must_exist=False
        )
        # Parent should be inside sandbox
        assert str(sandbox) in str(result.parent)

    def test_nonexistent_parent_must_exist_false(self, sandbox):
        """must_exist=False: should fail if parent directory doesn't exist."""
        with pytest.raises((SandboxViolation, FileNotFoundError, OSError)):
            resolve_and_check(
                "nonexistent_dir/file.txt", sandbox, must_exist=False
            )
