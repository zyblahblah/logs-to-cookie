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
