"""Tests for the resumable HTTP download in ``pipeline.download``.

The bot's previous ``download_to_file`` walked off a cliff the moment
the server closed the connection mid-stream — a multi-GB log download
that died at byte 57 GB threw ``IncompleteRead`` and forced the user
to restart from zero. These tests guard the resume-on-failure
behaviour: when ``iter_content`` raises a retryable network exception,
the helper reopens the URL with a ``Range:`` header pointing at the
last byte successfully written to disk.

We drive a tiny in-process HTTP server (``http.server``) so we never
touch the real network. The server honours ``Range`` and can be made
to deliberately truncate a response after N bytes via a
``?fail_after=N`` query parameter.
"""

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

from pipeline.download import DownloadError, download_to_file


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_range_handler(
    payload: bytes,
    *,
    fail_first_n_requests: int = 0,
    truncate_at: int = 0,
):
    """Build a handler that honours Range and can inject failures.

    The handler keeps a class-level call counter so tests can ask the
    *first N* requests to drop the connection after writing
    ``truncate_at`` bytes of the requested range. Subsequent requests
    serve the full range cleanly. ``truncate_at`` of 0 means "send
    everything" — i.e. the failure mode is only triggered when
    ``truncate_at > 0``.
    """

    class _Handler(BaseHTTPRequestHandler):
        call_count = 0

        def log_message(self, *_args, **_kwargs) -> None:  # noqa: D401
            return None

        def do_GET(self) -> None:  # noqa: N802
            type(self).call_count += 1
            range_hdr = self.headers.get("Range")
            start = 0
            if range_hdr:
                # Form: ``bytes=START-`` (no end → up to EOF).
                spec = range_hdr.replace("bytes=", "").strip()
                if "-" in spec:
                    start_str, _ = spec.split("-", 1)
                    if start_str:
                        start = int(start_str)
            body = payload[start:]
            should_fail = (
                type(self).call_count <= fail_first_n_requests
                and truncate_at > 0
            )
            if range_hdr:
                self.send_response(206)
                self.send_header(
                    "Content-Range",
                    f"bytes {start}-{len(payload) - 1}/{len(payload)}",
                )
            else:
                self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            # Always advertise the full body size — when we truncate
            # the response below, the client sees the connection
            # close before reading the promised bytes and raises
            # ``IncompleteRead`` (which is exactly the production
            # failure mode this test is guarding).
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            if should_fail:
                # Write less than we said we would; closing the
                # connection mid-response triggers an IncompleteRead /
                # ProtocolError on the client.
                self.wfile.write(body[: max(0, truncate_at - start)])
                try:
                    self.wfile.flush()
                except Exception:  # noqa: BLE001
                    pass
                # Force-close the underlying TCP connection without
                # writing the rest of the promised payload.
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self.connection.close()
                except OSError:
                    pass
            else:
                self.wfile.write(body)

    return _Handler


@contextmanager
def serve(handler_cls) -> Iterator[str]:
    port = _free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/file.bin"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def fast_backoff():
    """Skip the real ``time.sleep`` backoff so tests stay snappy."""
    with patch("pipeline.download.time.sleep", return_value=None):
        yield


class TestResumableDownload:
    def test_clean_download_no_retries(self, tmp_path: Path) -> None:
        """Sanity: a perfectly behaved server still works."""
        payload = b"hello world" * 1024  # 11 KB
        handler = _make_range_handler(payload)
        with serve(handler) as url:
            n = download_to_file(url, tmp_path / "out.bin")
        assert n == len(payload)
        assert (tmp_path / "out.bin").read_bytes() == payload
        assert handler.call_count == 1

    def test_retries_after_truncated_stream(
        self, tmp_path: Path, fast_backoff
    ) -> None:
        """The first response drops at byte ``truncate_at``; the
        helper must reconnect with ``Range:`` and finish writing the
        rest of the file."""
        payload = b"A" * 5000 + b"B" * 5000  # 10 KB total
        handler = _make_range_handler(
            payload, fail_first_n_requests=1, truncate_at=3000
        )
        with serve(handler) as url:
            n = download_to_file(url, tmp_path / "out.bin")
        assert n == len(payload)
        assert (tmp_path / "out.bin").read_bytes() == payload
        # 1 initial (truncated) + 1 resume = 2 calls.
        assert handler.call_count == 2

    def test_multiple_consecutive_failures_then_success(
        self, tmp_path: Path, fast_backoff
    ) -> None:
        """A flaky CDN that drops on the first three reconnects must
        still produce a complete file by the fourth attempt."""
        payload = bytes(range(256)) * 200  # 51.2 KB
        handler = _make_range_handler(
            payload, fail_first_n_requests=3, truncate_at=10_000
        )
        with serve(handler) as url:
            n = download_to_file(url, tmp_path / "out.bin")
        assert n == len(payload)
        assert (tmp_path / "out.bin").read_bytes() == payload
        assert handler.call_count == 4

    def test_resume_from_existing_partial_file(
        self, tmp_path: Path
    ) -> None:
        """If a previous (crashed) run left a partial file on disk,
        a fresh call to ``download_to_file`` must resume from the
        last byte rather than redownload from zero. This is the
        Railway-restart story: the bot dies mid-download, the
        partial file survives on a Volume mount, the next /start
        with the same URL hits the same workdir and finishes the
        job without redownloading the bytes we already paid for."""
        payload = b"C" * 8000
        # Pre-seed the destination with the first half of the payload
        # as if a previous run had already written it.
        dest = tmp_path / "out.bin"
        dest.write_bytes(payload[:5000])

        handler = _make_range_handler(payload)
        with serve(handler) as url:
            n = download_to_file(url, dest)
        assert n == len(payload)
        assert dest.read_bytes() == payload
        # Only the resume request fires — we already had the first 5000.
        assert handler.call_count == 1

    def test_exhausts_attempts_and_raises(
        self, tmp_path: Path, fast_backoff
    ) -> None:
        """If the server is hopelessly broken (every response truncates
        immediately), the helper raises ``DownloadError`` after the
        configured budget rather than spinning forever."""
        payload = b"D" * 4096
        handler = _make_range_handler(
            payload, fail_first_n_requests=999, truncate_at=10
        )
        with serve(handler) as url:
            with pytest.raises(DownloadError) as ei:
                download_to_file(
                    url, tmp_path / "out.bin", max_attempts=3
                )
        assert "truncated" in str(ei.value).lower()
        # Three reconnects = three calls, one of which was successful
        # by the partial bytes count but never reached EOF.
        assert handler.call_count == 3

    def test_cap_enforced_before_writing(self, tmp_path: Path) -> None:
        """``max_bytes`` is checked against the advertised
        ``Content-Length`` so we reject oversized downloads up-front
        instead of streaming gigabytes only to bail at the end."""
        payload = b"E" * 4096
        handler = _make_range_handler(payload)
        with serve(handler) as url:
            with pytest.raises(DownloadError):
                download_to_file(
                    url, tmp_path / "out.bin", max_bytes=100
                )
        # No bytes should have hit disk.
        assert not (tmp_path / "out.bin").exists() or (
            (tmp_path / "out.bin").stat().st_size == 0
        )
