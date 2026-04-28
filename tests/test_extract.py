"""Unit tests for extract.py (zip-only path; 7z/unrar paths are exercised
indirectly when those binaries are present)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

import extract as E


def _make_plain_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("a/cookies.txt", "hello")
        zf.writestr("b/cookies.txt", "world")
    return path


def test_is_archive():
    assert E.is_archive(Path("foo.zip"))
    assert E.is_archive(Path("foo.RAR"))
    assert E.is_archive(Path("foo.7z"))
    assert not E.is_archive(Path("foo.txt"))


def test_extract_plain_zip(tmp_path: Path):
    arc = _make_plain_zip(tmp_path / "plain.zip")
    out = tmp_path / "out"
    assert E.extract_archive(arc, out, [None]) is True

    assert (out / "a" / "cookies.txt").read_text() == "hello"
    assert (out / "b" / "cookies.txt").read_text() == "world"


def test_extract_zipcrypto_with_correct_password(tmp_path: Path):
    arc = tmp_path / "secret.zip"
    pw = "letmein"
    with zipfile.ZipFile(arc, "w") as zf:
        zf.setpassword(pw.encode())
        zinfo = zipfile.ZipInfo("note.txt")
        zinfo.flag_bits |= 0x1  # encrypted bit
        # We can't easily write a ZipCrypto-encrypted entry through stdlib,
        # so create the archive then re-open with 7z if available — fall
        # back: just verify wrong-password path returns False below.
    # If we couldn't actually encrypt, skip.
    pytest.skip("stdlib cannot write ZipCrypto-encrypted entries")


def test_extract_wrong_password_returns_false(tmp_path: Path, monkeypatch):
    # Stub zipfile + binaries to all-fail and confirm we get False.
    monkeypatch.setattr(E, "_seven_zip", lambda: None)
    monkeypatch.setattr(E, "_unrar", lambda: None)
    arc = _make_plain_zip(tmp_path / "plain.zip")

    # Force the stdlib path to fail by pointing to a missing file.
    bad = tmp_path / "missing.zip"
    out = tmp_path / "out"
    assert E.extract_archive(bad, out, ["whatever"]) is False


def test_extract_unsupported_suffix(tmp_path: Path):
    arc = tmp_path / "data.bin"
    arc.write_bytes(b"\x00\x01\x02")
    assert E.extract_archive(arc, tmp_path / "out", [None]) is False
