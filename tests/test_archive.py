"""Tests for archive helpers (URL detection + extraction)."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from pipeline.archive import (
    ARCHIVE_SUFFIXES,
    ArchiveError,
    archive_kind,
    detect_archive_kind,
    extract_archive,
    is_archive_url,
)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://example.com/foo.zip", True),
        ("https://example.com/foo.7z", True),
        ("https://example.com/foo.rar", True),
        ("https://example.com/foo.txt", False),
        ("https://example.com/foo.zip?token=x", True),
        ("https://example.com/", False),
        ("not-a-url", False),
    ],
)
def test_is_archive_url(url: str, expected: bool) -> None:
    assert is_archive_url(url) is expected


def test_archive_kind_from_path(tmp_path: Path) -> None:
    assert archive_kind(tmp_path / "x.zip") == "zip"
    assert archive_kind(tmp_path / "x.7z") == "7z"
    assert archive_kind(tmp_path / "x.rar") == "rar"
    assert archive_kind(tmp_path / "x.txt") is None


def test_archive_suffixes_are_tuple() -> None:
    assert isinstance(ARCHIVE_SUFFIXES, tuple)
    assert ".zip" in ARCHIVE_SUFFIXES
    assert ".7z" in ARCHIVE_SUFFIXES
    assert ".rar" in ARCHIVE_SUFFIXES


@pytest.mark.skipif(
    shutil.which("7z") is None
    and shutil.which("7za") is None
    and shutil.which("7zz") is None,
    reason="7z binary not available on this host",
)
def test_extract_zip_roundtrip(tmp_path: Path) -> None:
    archive = tmp_path / "input.zip"
    inner_dir = tmp_path / "src"
    inner_dir.mkdir()
    cookies_file = inner_dir / "cookies.txt"
    cookies_file.write_text("hello cookies\n", encoding="utf-8")

    with zipfile.ZipFile(archive, "w") as z:
        z.write(cookies_file, arcname="src/cookies.txt")

    out = tmp_path / "out"
    extract_archive(archive, out, password=None)
    assert (out / "src" / "cookies.txt").read_text(encoding="utf-8") == (
        "hello cookies\n"
    )


def test_extract_unsupported_extension_raises(tmp_path: Path) -> None:
    bogus = tmp_path / "x.tar"
    bogus.write_bytes(b"junk")
    with pytest.raises(ArchiveError):
        extract_archive(bogus, tmp_path / "out")


class TestDetectArchiveKind:
    """Magic-byte sniffing must work even when the URL has no suffix."""

    def test_zip_local_file_header(self, tmp_path: Path) -> None:
        f = tmp_path / "opaque-cdn-path"
        f.write_bytes(b"PK\x03\x04" + b"\x00" * 16)
        assert detect_archive_kind(f) == "zip"

    def test_zip_eocd(self, tmp_path: Path) -> None:
        f = tmp_path / "opaque-cdn-path"
        f.write_bytes(b"PK\x05\x06" + b"\x00" * 16)
        assert detect_archive_kind(f) == "zip"

    def test_seven_zip_signature(self, tmp_path: Path) -> None:
        f = tmp_path / "opaque"
        f.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 8)
        assert detect_archive_kind(f) == "7z"

    def test_rar_signature(self, tmp_path: Path) -> None:
        f = tmp_path / "opaque"
        f.write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 8)
        assert detect_archive_kind(f) == "rar"

    def test_falls_back_to_extension_when_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "x.zip"
        f.write_bytes(b"")
        assert detect_archive_kind(f) == "zip"

    def test_returns_none_for_plain_text(self, tmp_path: Path) -> None:
        f = tmp_path / "opaque-cdn-path"
        f.write_bytes(b"# Netscape cookie file\n.example.com\tTRUE\n")
        assert detect_archive_kind(f) is None

    def test_magic_bytes_beat_extension(self, tmp_path: Path) -> None:
        # A .zip-named file that is actually a 7z archive
        f = tmp_path / "mislabeled.zip"
        f.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 8)
        assert detect_archive_kind(f) == "7z"


@pytest.mark.skipif(
    shutil.which("7z") is None
    and shutil.which("7za") is None
    and shutil.which("7zz") is None,
    reason="7z binary not available on this host",
)
def test_extract_zip_with_no_extension_via_magic_bytes(
    tmp_path: Path,
) -> None:
    """Reproduces the LinkForge-style URL path: archive served as e.g.
    ``/download/AgAD0w22104`` with no ``.zip`` suffix.

    The bot now writes the body to ``input.bin`` and detects the kind
    from the magic bytes alone.
    """
    src_archive = tmp_path / "src.zip"
    inner = tmp_path / "src"
    inner.mkdir()
    (inner / "cookies.txt").write_text("hello\n", encoding="utf-8")
    with zipfile.ZipFile(src_archive, "w") as z:
        z.write(inner / "cookies.txt", arcname="cookies.txt")

    # Simulate "saved with no suffix" — the bot's pipeline writes
    # ``input.bin`` for tokenised CDN URLs.
    no_ext = tmp_path / "input.bin"
    no_ext.write_bytes(src_archive.read_bytes())

    out = tmp_path / "out"
    extract_archive(no_ext, out, password=None)
    assert (out / "cookies.txt").read_text(encoding="utf-8") == "hello\n"


def test_extract_garbage_raises_with_clear_message(tmp_path: Path) -> None:
    bogus = tmp_path / "input.bin"
    bogus.write_bytes(b"this is just plain text, not an archive\n" * 10)
    with pytest.raises(ArchiveError, match="magic bytes"):
        extract_archive(bogus, tmp_path / "out")


# ---------------------------------------------------------------------------
# Pure-string error-classification helpers. We test them directly so we
# don't have to actually invoke an extractor against every possible
# archive variant (RAR4 vs RAR5, header-encrypted .7z, etc.).
# ---------------------------------------------------------------------------
from pipeline.archive import (  # noqa: E402
    _is_password_error,
    _is_retryable,
    _last_useful_line,
)


# Real p7zip 16.02 stderr captured locally. Reproduces the exact
# scenario the user hit in the bug report (".rar / .7z gives `❌ Error:
# extraction failed: Compressed: 0`").
P7ZIP_NOT_AN_ARCHIVE_BLOB = """\

7-Zip [64] 16.02 : Copyright (c) 1999-2016 Igor Pavlov : 2016-05-21
p7zip Version 16.02 (locale=C.UTF-8,Utf16=on,HugeFiles=on,64 bits,...)

Scanning the drive for archives:
1 file, 14 bytes (1 KiB)

Extracting archive: fake.rar
ERROR: fake.rar
Can not open the file as archive


Can't open as archive: 1
Files: 0
Size:       0
Compressed: 0
"""

# Real p7zip 16.02 stderr for an encrypted .7z opened without a password.
P7ZIP_ENCRYPTED_BLOB = """\

7-Zip [64] 16.02 : Copyright (c) 1999-2016 Igor Pavlov : 2016-05-21
p7zip Version 16.02 (locale=C.UTF-8,Utf16=on,HugeFiles=on,64 bits,...)

Scanning the drive for archives:
1 file, 231 bytes (1 KiB)

Extracting archive: encrypted.7z
ERROR: encrypted.7z
Can not open encrypted archive. Wrong password?

ERRORS:
Headers Error

Can't open as archive: 1
Files: 0
Size:       0
Compressed: 0
"""


class TestIsRetryable:
    """Make sure the bot recognises every variant of "this isn't an archive"
    so it can fall through to the next extractor instead of giving up
    immediately and reporting `Compressed: 0`."""

    def test_p7zip_can_not_open_with_space(self) -> None:
        # p7zip 16.02 (Debian/Ubuntu): "Can not open the file as archive"
        assert _is_retryable(P7ZIP_NOT_AN_ARCHIVE_BLOB) is True

    def test_seven_zip_cannot_open_no_space(self) -> None:
        # Upstream 7-Zip / 7zz: "Cannot open the file as archive"
        assert _is_retryable("ERROR: foo\nCannot open the file as archive\n") is True

    def test_p7zip_summary_canT_open(self) -> None:
        # The summary footer "Can't open as archive: 1" alone should
        # also route through the retryable path.
        assert _is_retryable("Can't open as archive: 1\nFiles: 0\n") is True

    def test_unsupported_method_is_retryable(self) -> None:
        assert _is_retryable("ERROR: Unsupported Method foo.7z") is True

    def test_wrong_password_is_NOT_retryable(self) -> None:
        # A bad password is decisive — the next extractor will hit the
        # same wall, and looping over every binary just spams logs.
        assert _is_retryable(P7ZIP_ENCRYPTED_BLOB) is False
        assert _is_retryable("ERROR: Wrong password? in foo.zip") is False

    def test_clean_run_is_not_retryable(self) -> None:
        assert _is_retryable("Everything is Ok\n") is False


class TestIsPasswordError:
    def test_p7zip_encrypted_no_password(self) -> None:
        assert _is_password_error(P7ZIP_ENCRYPTED_BLOB) is True

    def test_wrong_password_phrase(self) -> None:
        assert _is_password_error("ERROR: Wrong password?\n") is True

    def test_headers_error_alone(self) -> None:
        # Header-encrypted .7z without a password sometimes prints
        # only "Headers Error" — still a password problem.
        assert _is_password_error("ERRORS:\nHeaders Error\n") is True

    def test_plain_failure_is_not_password_error(self) -> None:
        assert _is_password_error(P7ZIP_NOT_AN_ARCHIVE_BLOB) is False
        assert _is_password_error("Everything is Ok") is False


class TestLastUsefulLine:
    """``_last_useful_line`` must skip the 7z summary footer so the
    bot reports the *real* failure to the user instead of `Compressed:
    0`."""

    def test_strips_compressed_zero_footer(self) -> None:
        # This is the exact regression from the bug report: the user
        # saw ``❌ Error: extraction failed: Compressed: 0`` when 7z
        # actually said "Can not open the file as archive". The new
        # behaviour returns the real diagnostic line instead.
        line = _last_useful_line(P7ZIP_NOT_AN_ARCHIVE_BLOB)
        assert "Can not open the file as archive" in line
        assert "compressed" not in line.lower()
        assert "files: 0" not in line.lower()

    def test_strips_summary_for_encrypted(self) -> None:
        line = _last_useful_line(P7ZIP_ENCRYPTED_BLOB)
        # Should land on either the "Wrong password?" line or the
        # bare "Headers Error" line — either way, NOT "Compressed: 0".
        low = line.lower()
        assert "compressed" not in low
        assert "files:" not in low
        assert "size:" not in low
        assert "wrong password" in low or "headers error" in low

    def test_returns_extraction_failed_when_blob_is_empty(self) -> None:
        assert _last_useful_line("") == "extraction failed"

    def test_returns_only_line_when_no_noise(self) -> None:
        assert _last_useful_line("just one diagnostic line\n") == (
            "just one diagnostic line"
        )

    def test_skips_blank_and_bare_error_lines(self) -> None:
        blob = "\n\nERRORS\n\nthe real error\nFiles: 0\nCompressed: 0\n"
        assert _last_useful_line(blob) == "the real error"


class TestExtractArchiveSurfacesPasswordErrors:
    """End-to-end test: when 7z reports a wrong/missing password, the
    bot raises a ``ArchiveError`` with a phrase the friendly-message
    translator can match on (NOT ``Compressed: 0``)."""

    @pytest.mark.skipif(
        shutil.which("7z") is None
        and shutil.which("7za") is None
        and shutil.which("7zz") is None,
        reason="7z binary not available on this host",
    )
    def test_encrypted_7z_without_password(self, tmp_path: Path) -> None:
        """Build a header-encrypted .7z and try to extract it without a
        password. Must surface a password-shaped error, not the
        ``Compressed: 0`` summary footer."""
        import subprocess

        seven = (
            shutil.which("7z") or shutil.which("7za") or shutil.which("7zz")
        )
        assert seven is not None
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "cookies.txt").write_text("hello\n", encoding="utf-8")
        archive = tmp_path / "encrypted.7z"
        # ``-mhe=on`` = header encryption (without it the password
        # prompt only blocks file *contents*, not archive listing).
        result = subprocess.run(
            [
                seven,
                "a",
                "-y",
                "-pcorrect-password",
                "-mhe=on",
                str(archive),
                str(src_dir / "cookies.txt"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

        out = tmp_path / "out"
        with pytest.raises(ArchiveError) as exc:
            extract_archive(archive, out, password=None)
        msg = str(exc.value).lower()
        # The exact phrase the bot's _friendly_pipeline_error matches
        # on — must NOT regress to "Compressed: 0".
        assert "compressed: 0" not in msg
        assert (
            "no password was supplied" in msg
            or "wrong password" in msg
            or "encrypted" in msg
        )
