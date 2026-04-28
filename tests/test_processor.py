"""Unit tests for processor.py."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

import processor as P


# ---------------------------------------------------------------------------
# Netscape parsing
# ---------------------------------------------------------------------------


VALID = (
    ".example.com\tTRUE\t/\tFALSE\t1699999999\tsessionid\tabc123"
)


def test_parse_netscape_line_valid():
    c = P.parse_netscape_line(VALID)
    assert c == {
        "domain": ".example.com",
        "flag": "TRUE",
        "path": "/",
        "secure": "FALSE",
        "expires": "1699999999",
        "name": "sessionid",
        "value": "abc123",
    }


def test_parse_netscape_line_skips_comments_and_blank():
    assert P.parse_netscape_line("# this is a comment") is None
    assert P.parse_netscape_line("") is None
    assert P.parse_netscape_line("   ") is None


def test_parse_netscape_line_rejects_bad_format():
    assert P.parse_netscape_line("not a cookie") is None
    assert P.parse_netscape_line("only one field") is None
    # missing TRUE/FALSE
    assert (
        P.parse_netscape_line(".x.com\tMAYBE\t/\tFALSE\t1\tn\tv") is None
    )


def test_to_netscape_line_roundtrip():
    c = P.parse_netscape_line(VALID)
    assert P.to_netscape_line(c) == VALID


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


def test_matches_filter_none_keyword_keeps_all():
    assert P.matches_filter("anything", None) is True
    assert P.matches_filter("anything", "") is True


def test_matches_filter_case_insensitive():
    assert P.matches_filter(".NETFLIX.com\tTRUE...", "netflix") is True
    assert P.matches_filter(".other.com\t...", "netflix") is False


# ---------------------------------------------------------------------------
# Per-item output
# ---------------------------------------------------------------------------


def test_write_cookie_file(tmp_path: Path):
    c = P.parse_netscape_line(VALID)
    p = P.write_cookie_file(tmp_path, 7, c)

    assert p == tmp_path / "cookie_7.txt"
    body = p.read_text(encoding="utf-8")

    assert body.startswith(P.NETSCAPE_HEADER)
    assert body.rstrip("\n").endswith(VALID)


def test_process_lines_writes_one_file_per_match(tmp_path: Path):
    lines = [
        "# header comment",
        VALID,
        "trash line",
        ".other.com\tTRUE\t/\tTRUE\t1\tx\ty",
    ]
    n = P.process_lines(lines, tmp_path, keyword=None)
    assert n == 2

    files = sorted(tmp_path.iterdir())
    assert [p.name for p in files] == ["cookie_1.txt", "cookie_2.txt"]


def test_process_lines_filter_only_writes_matches(tmp_path: Path):
    lines = [
        ".netflix.com\tTRUE\t/\tTRUE\t1\tNetflixId\tA",
        ".claude.ai\tTRUE\t/\tTRUE\t1\tCookie\tB",
        ".netflix.com\tTRUE\t/\tTRUE\t2\tNetflixSes\tC",
    ]
    n = P.process_lines(lines, tmp_path, keyword="netflix")
    assert n == 2
    assert (tmp_path / "cookie_1.txt").exists()
    assert (tmp_path / "cookie_2.txt").exists()
    assert not (tmp_path / "cookie_3.txt").exists()


# ---------------------------------------------------------------------------
# Streaming (chunk → line)
# ---------------------------------------------------------------------------


class _FakeResp:
    """Minimal stand-in for a `requests.Response` used by stream_lines."""

    def __init__(self, chunks: List[bytes], status: int = 200):
        self._chunks = chunks
        self.status_code = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 1024):
        yield from self._chunks


def test_stream_lines_handles_chunk_split_lines():
    body = (
        ".a.com\tTRUE\t/\tTRUE\t1\tn\tv\n"
        ".b.com\tTRUE\t/\tTRUE\t1\tn\tv\n"
        ".c.com\tTRUE\t/\tTRUE\t1\tn\tv"  # no trailing newline
    )
    # Split mid-line on purpose.
    chunks = [body[:5].encode(), body[5:40].encode(), body[40:].encode()]

    with patch.object(P.requests, "get", return_value=_FakeResp(chunks)):
        out = list(P.stream_lines("http://x"))

    assert out == [
        ".a.com\tTRUE\t/\tTRUE\t1\tn\tv",
        ".b.com\tTRUE\t/\tTRUE\t1\tn\tv",
        ".c.com\tTRUE\t/\tTRUE\t1\tn\tv",
    ]


def test_stream_lines_strips_trailing_carriage_return():
    body = "alpha\r\nbeta\r\ngamma\r\n"
    with patch.object(
        P.requests, "get", return_value=_FakeResp([body.encode()])
    ):
        assert list(P.stream_lines("http://x")) == ["alpha", "beta", "gamma"]


# ---------------------------------------------------------------------------
# End-to-end (text URL via mocked requests)
# ---------------------------------------------------------------------------


def test_process_url_plain_text_end_to_end(tmp_path: Path):
    body = (
        "# Netscape HTTP Cookie File\n"
        f"{VALID}\n"
        ".netflix.com\tTRUE\t/\tTRUE\t2\tNetflixSes\tXYZ\n"
        "garbage line\n"
        ".other.com\tTRUE\t/\tTRUE\t3\tn\tv\n"
    )
    with patch.object(
        P.requests, "get", return_value=_FakeResp([body.encode()])
    ):
        result = P.process_url(
            "http://example.com/log.txt",
            tmp_path,
            keyword="netflix",
        )

    assert result.item_count == 1
    files = sorted(result.output_dir.iterdir())
    assert [p.name for p in files] == ["cookie_1.txt"]

    # Zip was written and contains exactly one entry.
    with zipfile.ZipFile(result.zip_path) as zf:
        assert zf.namelist() == ["cookie_1.txt"]
        body_in_zip = zf.read("cookie_1.txt").decode()
        assert ".netflix.com" in body_in_zip
        assert body_in_zip.startswith(P.NETSCAPE_HEADER)


def test_process_url_no_filter_keeps_all_valid_lines(tmp_path: Path):
    body = (
        f"{VALID}\n"
        ".netflix.com\tTRUE\t/\tTRUE\t2\tNetflixSes\tXYZ\n"
    )
    with patch.object(
        P.requests, "get", return_value=_FakeResp([body.encode()])
    ):
        result = P.process_url("http://example.com/log.txt", tmp_path)
    assert result.item_count == 2
