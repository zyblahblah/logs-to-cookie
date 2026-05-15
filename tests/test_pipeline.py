"""End-to-end pipeline tests using a local HTTP server.

These tests exercise the chunked-download → parse → convert flow
without ever touching Telegram or external networks.
"""

from __future__ import annotations

import shutil
import socket
import threading
import zipfile
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from pipeline import run_pipeline, run_pipeline_multi


GOOD_LINE_A = (
    ".netflix.com\tTRUE\t/\tTRUE\t1735689600\tNetflixId\txyz"
)
GOOD_LINE_B = (
    ".example.com\tTRUE\t/\tFALSE\t1735689600\tsession_id\tabc123"
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_handler(payload: bytes, content_type: str = "text/plain"):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args, **_kwargs) -> None:  # noqa: D401
            return None

    return Handler


@contextmanager
def serve(payload: bytes, content_type: str = "text/plain") -> Iterator[str]:
    port = _free_port()
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", port), _make_handler(payload, content_type)
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/file.txt"
    finally:
        httpd.shutdown()
        thread.join(timeout=2)


@contextmanager
def serve_zip(
    zip_bytes: bytes, *, url_path: str = "/payload.zip"
) -> Iterator[str]:
    port = _free_port()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(zip_bytes)))
            self.end_headers()
            self.wfile.write(zip_bytes)

        def log_message(self, *_args, **_kwargs) -> None:  # noqa: D401
            return None

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}{url_path}"
    finally:
        httpd.shutdown()
        thread.join(timeout=2)


def test_pipeline_plain_text_url(tmp_path: Path) -> None:
    body = (
        "# Netscape cookie file\n"
        f"{GOOD_LINE_A}\n"
        f"{GOOD_LINE_B}\n"
    ).encode("utf-8")

    with serve(body) as url:
        result = run_pipeline(url, tmp_path)

    assert result.cookie_count == 2
    assert len(result.cookie_files) == 1
    assert result.zip_path.exists()
    with zipfile.ZipFile(result.zip_path) as z:
        names = z.namelist()
    assert any(n.endswith(".txt") for n in names)


def test_pipeline_keyword_filter(tmp_path: Path) -> None:
    body = "\n".join([GOOD_LINE_A, GOOD_LINE_B]).encode("utf-8")
    with serve(body) as url:
        result = run_pipeline(url, tmp_path, keywords=["netflix"])
    assert result.cookie_count == 1
    assert len(result.cookie_files) == 1
    out = result.cookie_files[0].read_text(encoding="utf-8")
    assert "netflix" in out.lower()
    assert "example.com" not in out


def test_pipeline_no_matches_returns_empty_zip(tmp_path: Path) -> None:
    body = "\n".join([GOOD_LINE_A, GOOD_LINE_B]).encode("utf-8")
    with serve(body) as url:
        result = run_pipeline(url, tmp_path, keywords=["nope-no-match"])
    assert result.cookie_count == 0
    assert result.cookie_files == []
    assert result.zip_path.exists()


@pytest.mark.skipif(
    shutil.which("7z") is None
    and shutil.which("7za") is None
    and shutil.which("7zz") is None,
    reason="7z binary not available on this host",
)
def test_pipeline_archive_one_file_per_set(tmp_path: Path) -> None:
    """Archive with two stealer victims → two cookie set files."""
    src_root = tmp_path / "src"
    (src_root / "victim_1").mkdir(parents=True)
    (src_root / "victim_2").mkdir(parents=True)
    (src_root / "victim_1" / "cookies.txt").write_text(
        f"{GOOD_LINE_A}\n", encoding="utf-8"
    )
    (src_root / "victim_2" / "cookies.txt").write_text(
        f"{GOOD_LINE_B}\n", encoding="utf-8"
    )

    archive_path = tmp_path / "logs.zip"
    with zipfile.ZipFile(archive_path, "w") as z:
        for p in src_root.rglob("*"):
            if p.is_file():
                z.write(p, arcname=p.relative_to(src_root).as_posix())
    zip_bytes = archive_path.read_bytes()

    work = tmp_path / "work"
    with serve_zip(zip_bytes) as url:
        result = run_pipeline(url, work)

    assert len(result.cookie_files) == 2
    assert result.cookie_count == 2


@pytest.mark.skipif(
    shutil.which("7z") is None
    and shutil.which("7za") is None
    and shutil.which("7zz") is None,
    reason="7z binary not available on this host",
)
def test_pipeline_archive_url_without_extension(tmp_path: Path) -> None:
    """Regression: tokenised CDN URLs (LinkForge-style) carry no
    ``.zip``/``.7z``/``.rar`` suffix in their path. The pipeline must
    still detect the archive kind from the response body's magic bytes
    and extract it correctly.
    """
    src_root = tmp_path / "src"
    (src_root / "victim_alpha").mkdir(parents=True)
    (src_root / "victim_alpha" / "cookies.txt").write_text(
        f"{GOOD_LINE_A}\n", encoding="utf-8"
    )
    archive_path = tmp_path / "logs.zip"
    with zipfile.ZipFile(archive_path, "w") as z:
        for p in src_root.rglob("*"):
            if p.is_file():
                z.write(p, arcname=p.relative_to(src_root).as_posix())
    zip_bytes = archive_path.read_bytes()

    work = tmp_path / "work"
    # Simulate a CDN URL: tokenised path, no archive extension.
    with serve_zip(zip_bytes, url_path="/download/AgAD0w22104") as url:
        result = run_pipeline(url, work)

    assert len(result.cookie_files) == 1
    assert result.cookie_count == 1
    with zipfile.ZipFile(result.zip_path) as z:
        names = sorted(z.namelist())
    assert all(n.endswith(".txt") for n in names if not n.endswith("/"))


def test_pipeline_multi_merges_two_plain_text_urls(tmp_path: Path) -> None:
    body_a = (f"{GOOD_LINE_A}\n").encode("utf-8")
    body_b = (f"{GOOD_LINE_B}\n").encode("utf-8")

    with serve(body_a) as url_a, serve(body_b) as url_b:
        result = run_pipeline_multi([url_a, url_b], tmp_path)

    assert result.cookie_count == 2
    assert len(result.cookie_files) == 2
    assert result.zip_path.exists()
    with zipfile.ZipFile(result.zip_path) as z:
        names = sorted(z.namelist())
    # File names embed the source URL index so users can tell which
    # cookie set came from which link.
    assert any("url01" in n for n in names)
    assert any("url02" in n for n in names)


def test_pipeline_multi_keyword_filter_applies_to_all(tmp_path: Path) -> None:
    body_a = (f"{GOOD_LINE_A}\n").encode("utf-8")  # netflix
    body_b = (f"{GOOD_LINE_B}\n").encode("utf-8")  # example.com

    with serve(body_a) as url_a, serve(body_b) as url_b:
        result = run_pipeline_multi(
            [url_a, url_b], tmp_path, keywords=["netflix"]
        )
    assert result.cookie_count == 1
    assert len(result.cookie_files) == 1
    body = result.cookie_files[0].read_text(encoding="utf-8")
    assert "netflix" in body.lower()
    assert "example.com" not in body


def test_pipeline_multi_partial_failure_keeps_good_results(
    tmp_path: Path,
) -> None:
    body_a = (f"{GOOD_LINE_A}\n").encode("utf-8")
    with serve(body_a) as url_a:
        # url_b is intentionally a port that nothing's listening on, so
        # the second download will fail. The first should still succeed.
        bad_url = "http://127.0.0.1:1/never-listens"
        result = run_pipeline_multi([url_a, bad_url], tmp_path)

    assert result.cookie_count == 1
    assert len(result.cookie_files) == 1
    assert result.bytes_read >= len(body_a)


def test_pipeline_multi_passwords_length_mismatch_raises(tmp_path: Path) -> None:
    body = (f"{GOOD_LINE_A}\n").encode("utf-8")
    with serve(body) as url_a, serve(body) as url_b:
        with pytest.raises(ValueError, match="passwords has"):
            run_pipeline_multi(
                [url_a, url_b],
                tmp_path,
                passwords=["only-one"],  # 1 password, 2 URLs
            )


def test_pipeline_multi_passwords_list_overrides_single(tmp_path: Path) -> None:
    """When both ``password=`` and ``passwords=`` are given, the list wins."""
    body_a = (f"{GOOD_LINE_A}\n").encode("utf-8")
    body_b = (f"{GOOD_LINE_B}\n").encode("utf-8")

    # Both URLs are plain text, so the password is unused; we're just
    # verifying that supplying the list doesn't break anything and that
    # both URLs are still processed.
    with serve(body_a) as url_a, serve(body_b) as url_b:
        result = run_pipeline_multi(
            [url_a, url_b],
            tmp_path,
            password="ignored-because-list-wins",
            passwords=["per-url-a", None],
        )

    assert result.cookie_count == 2
    assert len(result.cookie_files) == 2


@pytest.mark.skipif(
    shutil.which("7z") is None
    and shutil.which("7za") is None
    and shutil.which("7zz") is None,
    reason="7z binary not available on this host",
)
def test_pipeline_multi_per_url_passwords_decrypt_correctly(
    tmp_path: Path,
) -> None:
    """End-to-end: two encrypted archives with *different* passwords."""
    import subprocess

    sevenzip = (
        shutil.which("7z") or shutil.which("7za") or shutil.which("7zz")
    )
    assert sevenzip is not None  # for type checkers

    archives: list[bytes] = []
    passwords = ["pwd-alpha", "pwd-beta"]
    for label, line, pwd in (
        ("alpha", GOOD_LINE_A, passwords[0]),
        ("beta", GOOD_LINE_B, passwords[1]),
    ):
        src_root = tmp_path / f"src_{label}"
        (src_root / f"victim_{label}").mkdir(parents=True)
        (src_root / f"victim_{label}" / "cookies.txt").write_text(
            f"{line}\n", encoding="utf-8"
        )
        archive_path = tmp_path / f"logs_{label}.zip"
        # ``-mhe=on`` would also encrypt headers but only for 7z; for
        # zip we just rely on per-file encryption with the password.
        subprocess.run(
            [
                sevenzip,
                "a",
                "-tzip",
                f"-p{pwd}",
                str(archive_path),
                str(src_root) + "/.",
            ],
            check=True,
            capture_output=True,
        )
        archives.append(archive_path.read_bytes())

    work = tmp_path / "work"
    with serve_zip(archives[0], url_path="/a.zip") as url_a, serve_zip(
        archives[1], url_path="/b.zip"
    ) as url_b:
        result = run_pipeline_multi(
            [url_a, url_b],
            work,
            passwords=passwords,
        )

    assert result.cookie_count == 2
    assert len(result.cookie_files) == 2


@pytest.mark.skipif(
    shutil.which("7z") is None
    and shutil.which("7za") is None
    and shutil.which("7zz") is None,
    reason="7z binary not available on this host",
)
def test_pipeline_multi_archives_merge_into_one_zip(tmp_path: Path) -> None:
    """Two zip URLs → all victims show up in the merged result zip."""
    archives: list[bytes] = []
    for label, line in (("alpha", GOOD_LINE_A), ("beta", GOOD_LINE_B)):
        src_root = tmp_path / f"src_{label}"
        (src_root / f"victim_{label}").mkdir(parents=True)
        (src_root / f"victim_{label}" / "cookies.txt").write_text(
            f"{line}\n", encoding="utf-8"
        )
        archive_path = tmp_path / f"logs_{label}.zip"
        with zipfile.ZipFile(archive_path, "w") as z:
            for p in src_root.rglob("*"):
                if p.is_file():
                    z.write(p, arcname=p.relative_to(src_root).as_posix())
        archives.append(archive_path.read_bytes())

    work = tmp_path / "work"
    with serve_zip(archives[0], url_path="/a.zip") as url_a, serve_zip(
        archives[1], url_path="/b.zip"
    ) as url_b:
        result = run_pipeline_multi([url_a, url_b], work)

    assert len(result.cookie_files) == 2
    assert result.cookie_count == 2
    with zipfile.ZipFile(result.zip_path) as z:
        names = sorted(z.namelist())
    assert any("alpha" in n for n in names)
    assert any("beta" in n for n in names)
    # And the URL-index tag is present on every output filename.
    assert all("url01" in n or "url02" in n for n in names)


# ---------------------------------------------------------------------------
# Cookie file discovery — see pipeline._find_cookie_files. These tests
# exercise the discovery directly so we don't need a 7z binary.
# ---------------------------------------------------------------------------
from pipeline.pipeline import _find_cookie_files  # noqa: E402


class TestFindCookieFiles:
    """Stealer-log layouts vary wildly — the discovery must catch
    cookies whether they're named ``cookies.txt``, just sit under a
    ``Cookies/`` directory, or look like Netscape but have an
    unrelated filename."""

    def test_filename_hint_matches_anywhere(self, tmp_path: Path) -> None:
        """Direct filename hint wins regardless of folder."""
        (tmp_path / "victim").mkdir()
        f = tmp_path / "victim" / "passwords-cookies.txt"
        f.write_text(f"{GOOD_LINE_A}\n", encoding="utf-8")
        found = _find_cookie_files(tmp_path)
        assert f in found

    def test_parent_dir_named_cookies_promotes_arbitrary_text(
        self, tmp_path: Path
    ) -> None:
        """Stealer logs commonly stash cookies under a ``Cookies/``
        folder with browser-named files like ``Chrome_Default.txt``
        — those have no ``cookie`` in the filename, but their parent
        dir does. They MUST be discovered."""
        cookies_dir = tmp_path / "victim_42" / "Cookies"
        cookies_dir.mkdir(parents=True)
        chrome = cookies_dir / "Chrome_Default.txt"
        chrome.write_text(f"{GOOD_LINE_A}\n", encoding="utf-8")
        firefox = cookies_dir / "Firefox_default-release.txt"
        firefox.write_text(f"{GOOD_LINE_B}\n", encoding="utf-8")

        found = _find_cookie_files(tmp_path)
        assert chrome in found
        assert firefox in found

    def test_parent_dir_match_is_case_insensitive(self, tmp_path: Path) -> None:
        """``COOKIES/`` (uppercase, lowercase, mixed) should all
        match. Stealers come from every locale."""
        for dirname in ("COOKIES", "Cookies", "cookies", "COoKiES"):
            (tmp_path / dirname).mkdir()
            f = tmp_path / dirname / "edge.txt"
            f.write_text(f"{GOOD_LINE_A}\n", encoding="utf-8")
        found = _find_cookie_files(tmp_path)
        names = {p.parent.name for p in found}
        assert names == {"COOKIES", "Cookies", "cookies", "COoKiES"}

    def test_sqlite_under_cookies_dir_is_skipped(
        self, tmp_path: Path
    ) -> None:
        """Chrome's own profile snapshot ``Cookies`` (a SQLite DB)
        often ends up in stealer logs. Feeding it to the Netscape
        parser is a guaranteed 0-row dead end AND wastes IO — so the
        ``_looks_like_text`` filter rejects SQLite blobs in both the
        filename-hint and parent-dir-hint discovery rules."""
        cookies_dir = tmp_path / "Cookies"
        cookies_dir.mkdir()
        sqlite_db = cookies_dir / "Cookies"  # Chrome name, no extension
        sqlite_db.write_bytes(
            b"SQLite format 3\x00" + b"\x00" * 1024
        )

        # And a legit cookie file in the same dir — should still match.
        legit = cookies_dir / "chrome.txt"
        legit.write_text(f"{GOOD_LINE_A}\n", encoding="utf-8")

        found = _find_cookie_files(tmp_path)
        # The legit text file must be discovered, and the SQLite blob
        # must NOT be — even though its filename matches the
        # ``cookies`` hint. Reading multi-GB SQLite databases through
        # the Netscape parser only inflates the "X cookie set(s)"
        # status while producing 0 cookies; skipping them outright
        # makes the count honest and the conversion phase faster on
        # raw browser-profile dumps.
        assert legit in found
        assert sqlite_db not in found

    def test_filename_hint_rejects_binary_blob(self, tmp_path: Path) -> None:
        """A file outside a ``Cookies/`` directory whose name matches
        a filename hint (e.g. literally named ``cookies`` with no
        extension, like Chrome's profile snapshot) must STILL be
        filtered out when its contents are obviously binary. Without
        this, raw stealer dumps that include the full Chrome profile
        directory blow up the candidate count with SQLite blobs that
        always parse to 0 rows."""
        sqlite_db = tmp_path / "victim_42" / "cookies"
        sqlite_db.parent.mkdir(parents=True)
        sqlite_db.write_bytes(
            b"SQLite format 3\x00" + b"\x00" * 1024
        )

        # Same dir but a real Netscape file — still picked up.
        good = tmp_path / "victim_42" / "passwords-cookies.txt"
        good.write_text(f"{GOOD_LINE_A}\n", encoding="utf-8")

        found = _find_cookie_files(tmp_path)
        assert good in found
        assert sqlite_db not in found

    def test_files_with_null_bytes_skipped_under_cookies_dir(
        self, tmp_path: Path
    ) -> None:
        """Generic binary cruft in a Cookies/ dir shouldn't be added.

        Without this, every random ``.bin`` / ``.dll`` ended up
        being parsed as text, costing CPU on multi-GB stealer dumps."""
        cookies_dir = tmp_path / "Cookies"
        cookies_dir.mkdir()
        binary = cookies_dir / "thumb.bin"
        binary.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096)
        text = cookies_dir / "ok.txt"
        text.write_text(f"{GOOD_LINE_A}\n", encoding="utf-8")

        found = _find_cookie_files(tmp_path)
        assert text in found
        assert binary not in found

    def test_netscape_txt_outside_cookies_dir_still_discovered(
        self, tmp_path: Path
    ) -> None:
        """The ``.txt`` first-line peek still catches Netscape
        tables that have neither a cookie-y filename nor a
        cookie-y parent dir — e.g. ``victim_123/dump.txt``."""
        (tmp_path / "victim_123").mkdir()
        dump = tmp_path / "victim_123" / "dump.txt"
        dump.write_text(f"{GOOD_LINE_A}\n", encoding="utf-8")
        passwords = tmp_path / "victim_123" / "passwords.txt"
        passwords.write_text("not a cookie line\n", encoding="utf-8")

        found = _find_cookie_files(tmp_path)
        assert dump in found
        assert passwords not in found

    def test_no_duplicates_across_hints(self, tmp_path: Path) -> None:
        """A file matched by multiple hints (filename AND parent dir
        AND .txt peek) appears exactly once."""
        cookies_dir = tmp_path / "Cookies"
        cookies_dir.mkdir()
        f = cookies_dir / "cookies.txt"
        f.write_text(f"{GOOD_LINE_A}\n", encoding="utf-8")

        found = _find_cookie_files(tmp_path)
        assert found.count(f) == 1


class TestConversionStatusUpdates:
    """``\U0001F504 Converting...`` used to stay frozen for the whole
    parse phase \u2014 a 300k cookie-set job would sit on the same line
    for many minutes with no feedback. The conversion loop now emits
    periodic ``X / N done`` status updates so the user can tell the
    job is still progressing."""

    def test_conversion_progress_is_emitted(self, tmp_path: Path) -> None:
        """Build a 12-set fake extracted tree and check that the
        per-file conversion progress fires multiple status updates
        between the initial ``\U0001F504 Converting...`` line and the
        terminal ``\u2699 Processing... packaging\u2026`` one.

        We force ``CONVERT_PROGRESS_INTERVAL`` down to 0 so every
        completed future emits a fresh status line \u2014 the production
        default is throttled to one emit / 2 s to keep the Telegram
        edit queue happy."""
        import pipeline.pipeline as pp

        # 12 victim dirs, each with a one-cookie Netscape file. That
        # gives us 12 cookie sets \u2014 enough to observe progress
        # without the test taking forever.
        for i in range(12):
            d = tmp_path / "extracted" / f"victim_{i:02d}" / "Cookies"
            d.mkdir(parents=True)
            (d / "Chrome.txt").write_text(
                f"{GOOD_LINE_A}\n", encoding="utf-8"
            )

        statuses: list[str] = []

        def _capture(line: str) -> None:
            statuses.append(line)

        # Force every completed future to emit a progress line.
        orig_interval = pp.CONVERT_PROGRESS_INTERVAL
        pp.CONVERT_PROGRESS_INTERVAL = 0.0
        try:
            # Re-use the pipeline's _find_cookie_files + low-level
            # conversion path by running the full pipeline against a
            # served zip of the tree above.
            archive_path = tmp_path / "logs.zip"
            with zipfile.ZipFile(archive_path, "w") as z:
                for p in (tmp_path / "extracted").rglob("*"):
                    if p.is_file():
                        z.write(
                            p,
                            arcname=p.relative_to(
                                tmp_path / "extracted"
                            ).as_posix(),
                        )
            zip_bytes = archive_path.read_bytes()

            work = tmp_path / "work"
            if (
                shutil.which("7z") is None
                and shutil.which("7za") is None
                and shutil.which("7zz") is None
            ):
                pytest.skip("7z binary not available on this host")
            with serve_zip(zip_bytes) as url:
                result = run_pipeline(url, work, on_status=_capture)
        finally:
            pp.CONVERT_PROGRESS_INTERVAL = orig_interval

        assert result.cookie_count == 12

        # First a single ``\U0001F504 Converting...`` initial line is emitted
        # with the candidate count, then one or more progress updates,
        # then the terminal ``\u2699 Processing... packaging\u2026`` line.
        converting_lines = [
            s for s in statuses if s.startswith("\U0001F504 Converting...")
        ]
        assert len(converting_lines) >= 2, (
            "expected at least one progress emit after the initial "
            f"Converting line, got: {converting_lines}"
        )
        # Progress lines look like ``\U0001F504 Converting... (X / N done)``.
        # The final progress line must reference the same total.
        done_lines = [s for s in converting_lines if "/" in s]
        assert done_lines, (
            f"expected at least one 'X / N done' progress line, got: "
            f"{converting_lines}"
        )

    def test_extracting_status_emitted_after_download(
        self, tmp_path: Path
    ) -> None:
        """After the download finishes but before extraction returns,
        a ``\U0001F4C2 Extracting...`` status line must be emitted so the
        chat doesn't sit on ``\u23F3 Downloading\u2026`` for multi-GB archives."""
        if (
            shutil.which("7z") is None
            and shutil.which("7za") is None
            and shutil.which("7zz") is None
        ):
            pytest.skip("7z binary not available on this host")

        src_root = tmp_path / "src"
        cookies_dir = src_root / "Victim" / "Cookies"
        cookies_dir.mkdir(parents=True)
        (cookies_dir / "Chrome.txt").write_text(
            f"{GOOD_LINE_A}\n", encoding="utf-8"
        )
        archive_path = tmp_path / "logs.zip"
        with zipfile.ZipFile(archive_path, "w") as z:
            for p in src_root.rglob("*"):
                if p.is_file():
                    z.write(
                        p,
                        arcname=p.relative_to(src_root).as_posix(),
                    )
        zip_bytes = archive_path.read_bytes()

        statuses: list[str] = []

        def _capture(line: str) -> None:
            statuses.append(line)

        work = tmp_path / "work"
        with serve_zip(zip_bytes) as url:
            run_pipeline(url, work, on_status=_capture)

        # The flow MUST traverse Downloading \u2192 Extracting \u2192 Converting.
        assert any(
            s.startswith("\u23F3 Downloading") for s in statuses
        ), statuses
        assert any(
            s.startswith("\U0001F4C2 Extracting") for s in statuses
        ), statuses
        assert any(
            s.startswith("\U0001F504 Converting") for s in statuses
        ), statuses


@pytest.mark.skipif(
    shutil.which("7z") is None
    and shutil.which("7za") is None
    and shutil.which("7zz") is None,
    reason="7z binary not available on this host",
)
def test_pipeline_extracts_cookies_under_browser_named_files(
    tmp_path: Path,
) -> None:
    """End-to-end: a zip that mimics the stealer-log layout (files
    under ``Cookies/`` named after browsers, no ``cookie`` in the
    filename) must produce non-empty output. This is the
    regression that surfaced as 'no matching cookies found' on
    real-world logs where another extractor tool succeeded."""
    src_root = tmp_path / "src"
    cookies_dir = src_root / "VictimXYZ" / "Cookies"
    cookies_dir.mkdir(parents=True)
    (cookies_dir / "Chrome_Default.txt").write_text(
        f"{GOOD_LINE_A}\n", encoding="utf-8"
    )
    (cookies_dir / "Firefox_default-release.txt").write_text(
        f"{GOOD_LINE_B}\n", encoding="utf-8"
    )

    archive_path = tmp_path / "logs.zip"
    with zipfile.ZipFile(archive_path, "w") as z:
        for p in src_root.rglob("*"):
            if p.is_file():
                z.write(p, arcname=p.relative_to(src_root).as_posix())
    zip_bytes = archive_path.read_bytes()

    work = tmp_path / "work"
    with serve_zip(zip_bytes) as url:
        result = run_pipeline(url, work)

    assert len(result.cookie_files) == 2, (
        f"expected 2 cookie sets, got {len(result.cookie_files)}: "
        f"{result.cookie_files}"
    )
    assert result.cookie_count == 2
