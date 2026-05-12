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
    _all_on_path,
    _build_unrar_cmd,
    _decode_subproc_bytes,
    _dest_has_files,
    _is_password_error,
    _is_retryable,
    _last_useful_line,
    _run,
    _stderr_blob,
    _wipe_dir_contents,
)


# Real ``unrar-free`` 0.0.2 stderr/stdout blob for a RAR4 archive
# (``rar a -m0 ...``) that the GPL fork can't actually decode. The
# tally line at the bottom is THE distinguishing feature — without
# it, the bot would happily report "extraction failed: 485 Failed"
# verbatim to the user.
UNRAR_FREE_FAILED_BLOB = """\
UNRAR-free 0.0.2

Extracting from input.rar

Extracting  src/cookies.txt                                            FAILED
Extracting  src/passwords.txt                                          FAILED
Extracting  src/forms.txt                                              FAILED
2 Failed
"""

# Real ``unrar-free`` blob for a RAR3+ archive that the fork
# rejects at the header level (it can't even start enumerating
# entries). Different code path, same effective failure.
UNRAR_FREE_UNKNOWN_TYPE_BLOB = """\
UNRAR-free 0.0.2

Extracting from input.rar

unknown archive type, only plain RAR 2.0 supported(normal compression),
SFXes, Volumes, Encryption and Comments are not supported either
All OK
"""


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

    def test_unrar_free_failed_tally_is_retryable(self) -> None:
        # Reproduces the exact bug from the screenshot: unrar-free
        # 0.0.2 finishes a RAR4 archive with "<num> Failed". Without
        # this routing the bot raises immediately instead of falling
        # through to ``bsdtar`` (which CAN read RAR4/RAR5).
        assert _is_retryable(UNRAR_FREE_FAILED_BLOB) is True
        assert _is_retryable("485 Failed\n") is True
        assert _is_retryable("  1 Failed\n") is True

    def test_unrar_free_unknown_archive_type_is_retryable(self) -> None:
        # A RAR3+ archive whose header unrar-free can't even parse
        # — must fall through to the next extractor in the chain.
        assert _is_retryable(UNRAR_FREE_UNKNOWN_TYPE_BLOB) is True

    def test_libarchive_unsupported_block_header_is_retryable(self) -> None:
        # Real ``bsdtar`` stderr against the screenshot bug RAR5
        # archive. Without flagging this as retryable the chain
        # raises libarchive's noisy "Error exit delayed" tail
        # verbatim instead of the codec-gap rewrite — i.e. the
        # final user-visible message becomes "extraction failed:
        # bsdtar: Error exit delayed from previous errors." which
        # is just as useless as "485 Failed".
        blob = (
            "AcolyteBases.txt: Unsupported block header size "
            "(was 5, max is 2): No such file or directory\n"
            "bsdtar: Error exit delayed from previous errors.\n"
        )
        assert _is_retryable(blob) is True

    def test_libarchive_error_exit_delayed_alone_is_retryable(self) -> None:
        # Defensive: the trailing ``Error exit delayed`` line on
        # its own (e.g. when the per-entry error gets dropped on
        # capture) must still flag as retryable.
        blob = "bsdtar: Error exit delayed from previous errors.\n"
        assert _is_retryable(blob) is True

    def test_bare_failed_word_is_NOT_retryable(self) -> None:
        # The "<num> Failed" pattern is anchored to a number — a
        # generic "asprintf failed: out of memory" must NOT match,
        # otherwise we'd retry every malloc failure forever.
        assert _is_retryable("asprintf failed: out of memory") is False
        assert _is_retryable("operation failed unexpectedly\n") is False


class TestIsPasswordError:
    def test_p7zip_encrypted_no_password(self) -> None:
        assert _is_password_error(P7ZIP_ENCRYPTED_BLOB) is True

    def test_wrong_password_phrase(self) -> None:
        assert _is_password_error("ERROR: Wrong password?\n") is True

    def test_headers_error_alone_is_NOT_password_error(self) -> None:
        # ``Headers Error`` on its own is too ambiguous to classify
        # as a password failure: p7zip / 7zz emits the same phrase
        # on a TRUNCATED download, a CORRUPT archive, or a CODEC
        # GAP — none of which are password problems.
        #
        # On a real wrong-password failure the extractor always
        # emits the companion phrase ``Can(?:not| not) open
        # encrypted archive. Wrong password?`` (see
        # ``P7ZIP_ENCRYPTED_BLOB`` above and the ``test_p7zip_
        # encrypted_no_password`` case below). Matching that
        # phrase still classifies the case correctly — the bare
        # ``Headers Error`` is just a noisy companion line.
        #
        # Before this assertion was flipped, the bot told users
        # "the password looks wrong" on truncated downloads of
        # un-encrypted RAR archives — a confusing false positive
        # that sent them re-typing a password they never needed.
        assert _is_password_error("ERRORS:\nHeaders Error\n") is False

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

    def test_strips_archive_property_listing(self) -> None:
        # ``Volumes = 1`` / ``Path = ...`` / ``Type = Rar5`` etc. are
        # archive METADATA, not failure reasons. They appear above
        # the real error in every 7zz / p7zip extraction blob. If
        # the only "lines" left after filtering the summary footer
        # are property lines, we'd previously surface
        # ``"extraction failed: Volumes = 1"`` to the user — the
        # exact screenshot bug we're fixing here.
        blob = (
            "7-Zip (z) 26.01 (x64)\n"
            "Scanning the drive for archives:\n"
            "1 file, 200 bytes (1 KiB)\n"
            "\n"
            "Extracting archive: tiny.rar\n"
            "--\n"
            "Path = tiny.rar\n"
            "Type = Rar5\n"
            "Physical Size = 299\n"
            "Encrypted = -\n"
            "Solid = -\n"
            "Blocks = 0\n"
            "Multivolume = -\n"
            "Volumes = 1\n"
            "Comment = \n"
            "Sub items Errors: 1\n"
            "Archives with Errors: 1\n"
            "Open Errors: 1\n"
        )
        line = _last_useful_line(blob)
        low = line.lower()
        assert "volumes" not in low
        assert "path =" not in low
        assert "type =" not in low

    def test_property_only_blob_falls_back_to_generic(self) -> None:
        # If LITERALLY every diagnostic line is an archive property
        # listing (no real error message at all), the user should
        # see a generic ``extraction failed`` rather than a
        # confusing ``Volumes = 1`` / ``Path = ...`` tail.
        blob = (
            "Path = /tmp/foo.rar\n"
            "Type = Rar5\n"
            "Solid = -\n"
            "Volumes = 1\n"
        )
        assert _last_useful_line(blob) == "extraction failed"

    def test_skips_blank_and_bare_error_lines(self) -> None:
        blob = "\n\nERRORS\n\nthe real error\nFiles: 0\nCompressed: 0\n"
        assert _last_useful_line(blob) == "the real error"


class TestDestHasFiles:
    """`_dest_has_files` is the safety net that catches extractors
    (notably ``unrar-free`` 0.0.2) which return rc=0 but produce no
    files — without it, the bot would silently report success on a
    corrupt RAR. See PR #31."""

    def test_empty_dir_is_false(self, tmp_path: Path) -> None:
        d = tmp_path / "empty"
        d.mkdir()
        assert _dest_has_files(d) is False

    def test_dir_with_only_subdirs_is_false(self, tmp_path: Path) -> None:
        # ``rglob('*')`` yields the directory entry too, but
        # ``_dest_has_files`` must only return True for *regular files*.
        d = tmp_path / "nested"
        (d / "subdir" / "deeper").mkdir(parents=True)
        assert _dest_has_files(d) is False

    def test_dir_with_a_file_is_true(self, tmp_path: Path) -> None:
        d = tmp_path / "ok"
        d.mkdir()
        (d / "cookies.txt").write_text("x", encoding="utf-8")
        assert _dest_has_files(d) is True

    def test_nested_file_is_true(self, tmp_path: Path) -> None:
        d = tmp_path / "ok"
        (d / "deep" / "path").mkdir(parents=True)
        (d / "deep" / "path" / "cookies.txt").write_text("x", encoding="utf-8")
        assert _dest_has_files(d) is True

    def test_missing_dir_is_false(self, tmp_path: Path) -> None:
        # Defensive: should not raise, just report "no files".
        assert _dest_has_files(tmp_path / "does-not-exist") is False

    def test_zero_byte_files_are_false(self, tmp_path: Path) -> None:
        # On a per-file-encrypted RAR (the ``@CenturionTXT``
        # stealer-log layout), ``7zz`` creates one 0-byte placeholder
        # per archive entry before bailing with ``ERROR: Wrong
        # password``. Treating those as "files extracted" would skip
        # the password-error classification and hand 0-byte files to
        # the cookie parser instead.
        d = tmp_path / "placeholders"
        d.mkdir()
        (d / "@CenturionTXT.txt").write_bytes(b"")
        (d / "@CenturionTXT.jpg").write_bytes(b"")
        assert _dest_has_files(d) is False

    def test_mixed_real_and_zero_byte_files_is_true(self, tmp_path: Path) -> None:
        # As long as ONE real file made it through, extraction
        # genuinely produced something useful. The bot can still
        # parse cookies from that one file.
        d = tmp_path / "partial"
        d.mkdir()
        (d / "empty.txt").write_bytes(b"")
        (d / "cookies.txt").write_text("x", encoding="utf-8")
        assert _dest_has_files(d) is True


class TestDecodeSubprocBytes:
    """Pin the regression that caused the user-visible
    ``'utf-8' codec can't decode byte 0xa0 …`` error: 7z prints
    archive entry names verbatim and Windows-origin stealer logs
    routinely use cp1252 / cp866 / cp936 filenames. We must NOT
    strict-decode."""

    def test_returns_empty_for_none(self) -> None:
        assert _decode_subproc_bytes(None) == ""

    def test_returns_empty_for_empty_bytes(self) -> None:
        assert _decode_subproc_bytes(b"") == ""

    def test_decodes_clean_utf8(self) -> None:
        assert _decode_subproc_bytes(b"hello\n") == "hello\n"

    def test_replaces_invalid_bytes_instead_of_raising(self) -> None:
        # 0xa0 alone is never a valid UTF-8 start byte; cp1252
        # interprets it as a non-breaking space, which is exactly
        # what the user's archive contained.
        out = _decode_subproc_bytes(b"Extracting\xa0file.txt\n")
        # Must not raise UnicodeDecodeError; the offending byte is
        # replaced with U+FFFD so the surrounding diagnostic
        # ("Extracting", "file.txt") is preserved.
        assert "Extracting" in out
        assert "file.txt" in out
        assert "\ufffd" in out


class TestSubprocByteToleration:
    """End-to-end: when 7z (or any extractor) emits non-UTF-8
    bytes, the helpers in archive.py must capture them as bytes
    and decode tolerantly. Before this fix, ``subprocess.run`` was
    called with ``text=True`` which strict-decoded the bytes and
    crashed the entire pipeline with the exact message the user
    reported in the bug screenshot."""

    @pytest.fixture
    def emit_invalid_utf8(self) -> list[str]:
        # Reproduce the user-visible error exactly: emit a 0xa0
        # byte at position 147 of the subprocess output, the same
        # offset Python reported in their crash.
        return [
            "bash",
            "-c",
            r'printf "%-147s\xa0invalid_filename.txt\n" "Extracting:"',
        ]

    def test_run_does_not_raise_unicode_decode_error(
        self, emit_invalid_utf8: list[str]
    ) -> None:
        rc, line = _run(emit_invalid_utf8, timeout=10)
        assert rc == 0
        # Replacement char preserves the surrounding context.
        assert "Extracting" in line
        assert "invalid_filename.txt" in line

    def test_stderr_blob_does_not_raise_unicode_decode_error(
        self, emit_invalid_utf8: list[str]
    ) -> None:
        rc, blob = _stderr_blob(emit_invalid_utf8, timeout=10)
        assert rc == 0
        assert "Extracting" in blob
        assert "invalid_filename.txt" in blob


class TestAllOnPathDedupe:
    """``_all_on_path`` must dedupe by realpath, not just by the
    on-PATH lookup. Debian/Ubuntu's ``unrar`` package is provided by
    ``unrar-free`` via update-alternatives, so ``which unrar`` and
    ``which unrar-free`` resolve to the same physical binary. Running
    it twice in a row just doubles the failure log."""

    def test_dedupes_symlinked_binaries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        # The "real" binary.
        real = bin_dir / "unrar-free"
        real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        real.chmod(0o755)
        # An update-alternatives-style symlink.
        link = bin_dir / "unrar"
        link.symlink_to(real)

        monkeypatch.setenv("PATH", str(bin_dir))
        out = _all_on_path(["unrar", "unrar-free"])
        # Either of the two paths is acceptable; what matters is that
        # we get exactly ONE entry, not two.
        assert len(out) == 1
        assert Path(out[0]).name in {"unrar", "unrar-free"}

    def test_keeps_distinct_binaries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for name in ("7z", "7za", "7zz"):
            p = bin_dir / name
            p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            p.chmod(0o755)

        monkeypatch.setenv("PATH", str(bin_dir))
        out = _all_on_path(["7z", "7za", "7zz"])
        assert len(out) == 3


class TestWipeDirContents:
    """``_wipe_dir_contents`` is the safety net that prevents the
    silent-success bug where a previous extractor's leftovers (e.g.
    ``7z`` 16.02's zero-byte placeholder files for entries it
    couldn't decode) get misread as proof that the next extractor
    succeeded."""

    def test_removes_files(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("x")
        _wipe_dir_contents(tmp_path)
        assert tmp_path.exists()
        assert list(tmp_path.iterdir()) == []

    def test_removes_zero_byte_placeholders(self, tmp_path: Path) -> None:
        # Recreate exactly what 7z 16.02 leaves behind on
        # ``ERROR: Unsupported Method`` against a RAR5 archive.
        (tmp_path / "AcolyteBases.txt").write_bytes(b"")
        (tmp_path / "@AcolyteBases - Telegram.jpg").write_bytes(b"")
        _wipe_dir_contents(tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_removes_nested_directories(self, tmp_path: Path) -> None:
        nested = tmp_path / "sub" / "deep"
        nested.mkdir(parents=True)
        (nested / "leaf").write_text("y")
        _wipe_dir_contents(tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_keeps_dest_dir_itself(self, tmp_path: Path) -> None:
        (tmp_path / "a").write_text("x")
        _wipe_dir_contents(tmp_path)
        # Directory itself MUST survive — extract_archive expects to
        # invoke the next extractor right into it.
        assert tmp_path.is_dir()

    def test_no_op_on_already_empty_dir(self, tmp_path: Path) -> None:
        _wipe_dir_contents(tmp_path)
        assert tmp_path.is_dir()
        assert list(tmp_path.iterdir()) == []

    def test_no_op_on_missing_dir(self, tmp_path: Path) -> None:
        gone = tmp_path / "does-not-exist"
        # MUST NOT raise — extract_archive is tolerant of races where
        # dest_dir hasn't been created yet.
        _wipe_dir_contents(gone)


class TestExtractArchiveResetsDestBetweenAttempts:
    """End-to-end-ish: stub out the extractor chain and prove that a
    later candidate's ``rc=0`` does NOT count as success when only
    leftovers from a *previous* failed candidate are on disk.

    This is the exact failure mode that bit the user in the screenshot
    bug: ``7z`` 16.02 leaves zero-byte placeholders for every RAR5
    entry it can't decode, and ``unrar-free`` 0.0.2 then prints
    "unknown archive type" + ``rc=0`` without writing anything. Without
    the wipe between attempts, ``rc==0 and _dest_has_files`` would
    return the leftover placeholders to the cookie parser and the bot
    would silently report success on a broken extraction.
    """

    def test_seven_zip_placeholders_do_not_fool_later_unrar(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        archive = tmp_path / "input.rar"
        # Real RAR5 magic so detect_archive_kind returns "rar".
        archive.write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\x00" * 32)
        dest = tmp_path / "out"

        from pipeline import archive as A

        # Pretend both 7z and unrar are on PATH, distinct binaries.
        monkeypatch.setattr(
            A,
            "_all_on_path",
            lambda cands: ["/fake/7z"] if "7z" in cands else ["/fake/unrar-free"],
        )

        calls: list[str] = []

        def fake_stderr_blob(cmd: list[str], timeout: int) -> tuple[int, str]:
            calls.append(Path(cmd[0]).name)
            if Path(cmd[0]).name == "7z":
                # Simulate 7z 16.02's behaviour exactly: leaves
                # 0-byte placeholders for the entries it couldn't
                # decode, exits rc=2 with "Unsupported Method".
                dest.mkdir(parents=True, exist_ok=True)
                (dest / "AcolyteBases.txt").write_bytes(b"")
                (dest / "@AcolyteBases - Telegram.jpg").write_bytes(b"")
                return 2, "ERROR: Unsupported Method : AcolyteBases.txt\n"
            # unrar-free returns rc=0 with "unknown archive type" and
            # writes nothing.
            return 0, (
                "unknown archive type, only plain RAR 2.0 supported"
                "(normal and solid archives), SFX and Volumes are NOT "
                "supported!\nAll OK\n"
            )

        monkeypatch.setattr(A, "_stderr_blob", fake_stderr_blob)

        with pytest.raises(A.ArchiveError) as exc:
            A.extract_archive(archive, dest, password="@AcolyteBases", timeout=5)
        msg = str(exc.value).lower()
        # The whole point: we MUST NOT silently succeed.
        # And the wipe MUST have removed the 0-byte placeholders
        # before the unrar-free attempt, so they don't survive the
        # whole chain either.
        leftover = list(dest.rglob("*")) if dest.exists() else []
        leftover_files = [p for p in leftover if p.is_file()]
        assert leftover_files == [], (
            f"placeholders should have been wiped, found: {leftover_files}"
        )
        # And the user-visible error MUST name the codec gap, not
        # bare "All OK" or empty output.
        assert "rar 2.0" in msg or "unsupported method" in msg, msg
        # Both extractors must have actually been tried (proves we
        # didn't short-circuit on the first one's leftovers).
        assert calls == ["7z", "unrar-free"], calls


class TestBuildUnrarCmd:
    """``_build_unrar_cmd`` must use the proprietary-syntax command
    line for both ``unrar`` and ``unrar-free``.

    The motivation is the screenshot bug: the GPL fork's native argp
    parser treats ``-p`` as a no-arg toggle that just enables
    interactive password prompting on the tty, so a password supplied
    as ``-p PASSWORD`` (with a space) hangs at ``Password:`` waiting
    on stdin until the bot's timeout fires. ``--password=PASSWORD``
    is rejected outright with ``option '--password' doesn't allow an
    argument``. The attached form ``-p<password>`` works in BOTH
    proprietary unrar AND unrar-free's ``compat_parse_opts`` path —
    so we always invoke that syntax."""

    def test_unrar_free_uses_proprietary_syntax(self, tmp_path: Path) -> None:
        cmd = _build_unrar_cmd(
            "/usr/bin/unrar-free",
            tmp_path / "input.rar",
            tmp_path / "out",
            "@AcolyteBases",
        )
        # MUST be the attached form ``-p<pwd>`` (no space) — anything
        # else hangs unrar-free at an interactive password prompt.
        assert "-p@AcolyteBases" in cmd
        # Must NOT be the GNU detached form that argp rejects /
        # silently triggers the password prompt.
        assert "-p" not in [arg for arg in cmd if arg == "-p"]
        assert "--password=@AcolyteBases" not in cmd
        # Proprietary extract switches.
        assert cmd[1:4] == ["x", "-y", "-o+"]

    def test_unrar_uses_proprietary_syntax(self, tmp_path: Path) -> None:
        cmd = _build_unrar_cmd(
            "/usr/bin/unrar",
            tmp_path / "input.rar",
            tmp_path / "out",
            "secret",
        )
        assert "-psecret" in cmd
        assert cmd[1:4] == ["x", "-y", "-o+"]

    def test_no_password_uses_dash_marker(self, tmp_path: Path) -> None:
        cmd = _build_unrar_cmd(
            "/usr/bin/unrar-free",
            tmp_path / "input.rar",
            tmp_path / "out",
            None,
        )
        # ``-p-`` tells proprietary unrar / unrar-free's compat
        # parser "no password" without ever prompting.
        assert "-p-" in cmd

    def test_bsdtar_uses_passphrase_flag(self, tmp_path: Path) -> None:
        cmd = _build_unrar_cmd(
            "/usr/bin/bsdtar",
            tmp_path / "input.rar",
            tmp_path / "out",
            "secret",
        )
        # libarchive uses ``--passphrase`` (not ``-p``).
        assert "--passphrase" in cmd
        idx = cmd.index("--passphrase")
        assert cmd[idx + 1] == "secret"
        assert "-x" in cmd
        assert "-f" in cmd


class TestExtractArchiveSurfacesUnrarFreeFailure:
    """Make sure ``extract_archive`` rewrites the cryptic
    "<num> Failed" tally into something a user can act on, instead
    of the screenshot's ``❌ Error: extraction failed: 485 Failed``."""

    def test_rewrites_failed_tally_into_codec_advice(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Plant a pretend RAR file (just the magic bytes) and stub
        # ``_all_on_path`` to report a single fake "unrar" candidate
        # whose stderr matches unrar-free's "<num> Failed" pattern.
        archive = tmp_path / "input.rar"
        archive.write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 64)

        from pipeline import archive as archive_mod

        def fake_all_on_path(candidates: list[str]) -> list[str]:
            if "7z" in candidates:
                return []
            if "unrar-free" in candidates or "unrar" in candidates:
                return ["/usr/bin/unrar-free"]
            return []

        def fake_stderr_blob(
            cmd: list[str], timeout: int
        ) -> tuple[int, str]:
            return 1, UNRAR_FREE_FAILED_BLOB

        monkeypatch.setattr(archive_mod, "_all_on_path", fake_all_on_path)
        monkeypatch.setattr(archive_mod, "_stderr_blob", fake_stderr_blob)

        out = tmp_path / "out"
        with pytest.raises(ArchiveError) as exc:
            extract_archive(archive, out, password=None)
        msg = str(exc.value).lower()
        # The user must NOT see the raw "485 Failed" tally —
        # that's what triggered the bug report.
        assert "failed" not in msg.split(":")[-1].split()[:2]
        # The friendly message names the underlying codec gap.
        assert "rar 2.0" in msg
        assert "rar3" in msg or "rar4" in msg or "rar5" in msg


class TestExtractArchiveAggregatesBlobsForCodecGap:
    """Regression for the codec-gap-message-suppression bug: when
    the chain is 7z → unrar-free → bsdtar and the LAST extractor
    fails with libarchive's noisy "Error exit delayed" tail, the
    final user message must STILL surface the codec-gap rewrite
    (named after the earlier ``unrar-free`` "<num> Failed" signal)
    instead of leaking ``bsdtar``'s message verbatim. ``extract_archive``
    achieves this by aggregating every extractor's blob and scanning
    the union for the codec-gap signal."""

    def test_bsdtar_last_does_not_hide_unrar_free_codec_gap(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        archive = tmp_path / "input.rar"
        archive.write_bytes(b"Rar!\x1a\x07\x01" + b"\x00" * 64)

        from pipeline import archive as archive_mod

        def fake_all_on_path(candidates: list[str]) -> list[str]:
            # Order matters: 7z first, then unrar-free, then bsdtar
            # — exactly what extract_archive computes for a RAR.
            if "7z" in candidates:
                return ["/usr/bin/7z"]
            if "unrar-free" in candidates or "unrar" in candidates:
                return ["/usr/bin/unrar-free"]
            if "bsdtar" in candidates:
                return ["/usr/bin/bsdtar"]
            return []

        # Three blobs, each from a different extractor. The LAST
        # one is bsdtar's libarchive noise; only the MIDDLE one
        # carries the codec-gap signal.
        blobs = iter(
            [
                "ERROR: Unsupported Method\n",  # 7z
                UNRAR_FREE_FAILED_BLOB,  # unrar-free (the signal)
                (
                    "AcolyteBases.txt: Unsupported block header size "
                    "(was 5, max is 2): No such file or directory\n"
                    "bsdtar: Error exit delayed from previous errors.\n"
                ),  # bsdtar (noise — what last_blob alone would surface)
            ]
        )

        def fake_stderr_blob(
            cmd: list[str], timeout: int
        ) -> tuple[int, str]:
            return 1, next(blobs)

        monkeypatch.setattr(archive_mod, "_all_on_path", fake_all_on_path)
        monkeypatch.setattr(archive_mod, "_stderr_blob", fake_stderr_blob)

        out = tmp_path / "out"
        with pytest.raises(ArchiveError) as exc:
            extract_archive(archive, out, password=None)
        msg = str(exc.value).lower()
        # The user MUST see the codec-gap rewrite, not bsdtar's
        # "Error exit delayed" tail.
        assert "rar 2.0" in msg
        assert "rar3" in msg or "rar4" in msg or "rar5" in msg
        assert "error exit delayed" not in msg
        assert "unsupported block header size" not in msg


class TestFriendlyPipelineErrorForUnrarFree:
    """The bot's user-facing translator must catch the rewritten
    unrar-free message and turn it into actionable installation
    advice, not pass through the raw exception text."""

    def test_unrar_free_message_translates_to_install_advice(self) -> None:
        from bot import _friendly_pipeline_error

        raw = (
            "extraction failed via unrar-free: unrar-free can only "
            "read RAR 2.0 archives — this one uses a newer RAR3 / "
            "RAR4 / RAR5 codec it doesn't understand"
        )
        out = _friendly_pipeline_error(raw)
        low = out.lower()
        assert "rar 2.0" in low
        # Must surface installation advice, not the raw "Failed" tally.
        assert "unrar" in low
        assert "485 failed" not in low


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
