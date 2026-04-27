"""Smoke tests for `logs_to_cookie.download` against a local HTTP server."""

from __future__ import annotations

import contextlib
import http.server
import socketserver
import threading
from pathlib import Path

import pytest

from logs_to_cookie.download import (
    download_to_workdir,
    filename_from_url,
    is_url,
    stream_download,
)


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    """Tiny HTTP handler that supports HEAD + GET (with Range)."""

    server_version = "RangeTest/1.0"

    def _resolve(self) -> Path:
        path = self.path.lstrip("/")
        return Path(self.server.directory) / path  # type: ignore[attr-defined]

    def do_HEAD(self):  # noqa: N802
        f = self._resolve()
        if not f.is_file():
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(f.stat().st_size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        f = self._resolve()
        if not f.is_file():
            self.send_error(404)
            return
        size = f.stat().st_size
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                start_s, end_s = rng[len("bytes="):].split("-", 1)
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else size - 1
            except ValueError:
                self.send_error(416)
                return
            end = min(end, size - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            with open(f, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    buf = fh.read(min(64 * 1024, remaining))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    remaining -= len(buf)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        with open(f, "rb") as fh:
            while True:
                buf = fh.read(64 * 1024)
                if not buf:
                    break
                self.wfile.write(buf)

    def log_message(self, *_a, **_kw):
        return


class _ThreadedServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


@contextlib.contextmanager
def _serve(directory: Path):
    srv = _ThreadedServer(("127.0.0.1", 0), _RangeHandler)
    srv.directory = str(directory)  # type: ignore[attr-defined]
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def big_blob(tmp_path: Path) -> Path:
    """Make a 5 MB blob so range-split actually splits across workers."""
    blob = tmp_path / "data.bin"
    blob.write_bytes(b"A" * (5 * 1024 * 1024) + b"B" * 7)
    return blob


def test_is_url():
    assert is_url("https://example.com/foo.zip")
    assert is_url("HTTP://example.com/")
    assert not is_url("/local/path.zip")
    assert not is_url("example.com/foo.zip")
    assert not is_url(None)  # type: ignore[arg-type]


def test_filename_from_url():
    assert filename_from_url("https://x.com/a/b/Black%20Logs.zip") == "Black Logs.zip"
    assert filename_from_url("https://x.com/?q=1") == "download.bin"


def test_stream_download_range_split(big_blob: Path, tmp_path: Path):
    serve_dir = big_blob.parent
    out = tmp_path / "downloaded.bin"
    with _serve(serve_dir) as base:
        url = f"{base}/{big_blob.name}"
        stream_download(url, out, workers=4, show_progress=False)
    assert out.read_bytes() == big_blob.read_bytes()


def test_stream_download_single_worker(big_blob: Path, tmp_path: Path):
    out = tmp_path / "downloaded2.bin"
    with _serve(big_blob.parent) as base:
        url = f"{base}/{big_blob.name}"
        stream_download(url, out, workers=1, show_progress=False)
    assert out.read_bytes() == big_blob.read_bytes()


def test_download_to_workdir_picks_filename(big_blob: Path, tmp_path: Path):
    work = tmp_path / "work"
    with _serve(big_blob.parent) as base:
        dest = download_to_workdir(
            f"{base}/{big_blob.name}", work, workers=2
        )
    assert dest.parent == work
    assert dest.name == big_blob.name
    assert dest.read_bytes() == big_blob.read_bytes()


def test_part_retry_resumes_correctly(tmp_path: Path):
    """Regression: retry path must not double-count `written`.

    First attempt for the second range-part returns *short* (truncates the
    response mid-part). The downloader should retry, ask for the remainder,
    and end up writing the full byte range — without the `remaining` boundary
    check zeroing out the buffer. With the old formula the retry would write
    nothing and the part would fail.
    """
    blob = tmp_path / "blob.bin"
    # > 2 × DEFAULT_CHUNK so range-split actually triggers (3 MiB).
    payload = (bytes(range(256)) * (4 * 1024)) * 3
    blob.write_bytes(payload)
    out = tmp_path / "out.bin"

    # State shared between threads: each part is identified by its `end`
    # (which stays constant across retries even though `start` shifts forward
    # as bytes are received). Sabotage only the first attempt for each part.
    sabotaged_ends: set = set()
    sabotage_lock = threading.Lock()

    class _FlakyHandler(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", str(blob.stat().st_size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

        def do_GET(self):  # noqa: N802
            size = blob.stat().st_size
            rng = self.headers.get("Range") or ""
            if not rng.startswith("bytes="):
                self.send_error(500)
                return
            s, e = rng[6:].split("-", 1)
            start = int(s) if s else 0
            end = int(e) if e else size - 1
            end = min(end, size - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with sabotage_lock:
                sabotage_first = end not in sabotaged_ends
                sabotaged_ends.add(end)
            with open(blob, "rb") as fh:
                fh.seek(start)
                if sabotage_first and length > 1024:
                    # Send only the first 256 bytes then drop the connection.
                    self.wfile.write(fh.read(256))
                    try:
                        self.wfile.flush()
                    finally:
                        self.connection.close()
                    return
                remaining = length
                while remaining > 0:
                    buf = fh.read(min(64 * 1024, remaining))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    remaining -= len(buf)

        def log_message(self, *_a, **_kw):
            return

    srv = _ThreadedServer(("127.0.0.1", 0), _FlakyHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{port}/blob.bin"
        stream_download(url, out, workers=4, show_progress=False)
    finally:
        srv.shutdown()
        srv.server_close()
    assert out.read_bytes() == payload


def test_resolve_roots_downloads_url(big_blob: Path, tmp_path: Path):
    """`_resolve_roots` should transparently download URL inputs."""
    import zipfile
    archive = tmp_path / "logs.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(
            "victim/Cookies/0.txt",
            "# Netscape\n.netflix.com\tTRUE\t/\tTRUE\t1900000000\tA\t1\n",
        )

    from logs_to_cookie.cli import _resolve_roots

    with _serve(archive.parent) as base, contextlib.ExitStack() as stack:
        url = f"{base}/{archive.name}"
        roots, failures = _resolve_roots(stack, url, [], workers=2)
        assert not failures
        assert roots
        # The downloaded zip should have been auto-extracted because zip is an archive.
        cookie_files = list(roots[0].rglob("*.txt"))
        assert any("netflix.com" in p.read_text() for p in cookie_files)


def test_resume_does_not_corrupt_when_total_unknown(tmp_path, monkeypatch):
    """If a download attempt partially writes data and then raises while
    ``total`` is unknown, the retry must NOT append a fresh full-file
    GET on top of the partial bytes (which would corrupt the output as
    ``[partial][full_file]``).

    Exercises the ``_stream_with_resume`` retry path directly because
    most servers signal end-of-body via connection close, which urllib's
    chunked ``read(n)`` swallows silently — so producing the bug via a
    real socket is unreliable.
    """
    from logs_to_cookie import download as dl

    payload = b"Y" * 4096
    calls = {"n": 0}

    def fake_stream_to(url, dest, *, headers=None, chunk=0, on_progress=None,
                      bytes_done_offset=0, total=None, append=False):
        # First attempt: write half of payload, then raise.
        # Second attempt: must start fresh (append=False) because total is
        # unknown — write the whole payload.
        calls["n"] += 1
        mode = "ab" if append else "wb"
        if calls["n"] == 1:
            assert append is False  # first attempt, file is empty
            with open(dest, mode) as f:
                f.write(payload[: len(payload) // 2])
            raise OSError("simulated network drop")
        # Second attempt — bug would have set append=True here.
        assert append is False, (
            "retry must NOT append when total is unknown — would corrupt"
        )
        with open(dest, mode) as f:
            f.write(payload)

    monkeypatch.setattr(dl, "_stream_to", fake_stream_to)

    out = tmp_path / "blob.bin"
    dl._stream_with_resume(
        "http://example/x",
        out,
        chunk=1024,
        retries=3,
        on_progress=None,
        total=None,
    )
    assert out.read_bytes() == payload, (
        "output corrupted — got partial bytes prepended to a fresh full "
        "download"
    )
