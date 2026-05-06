"""Chunked HTTP download helpers.

The bot streams the user-provided URL in 1 MB chunks so multi-GB log
archives never need to fit in RAM. The same primitives are used both
for plain-text Netscape cookie URLs (``stream_lines``) and for archive
URLs that have to be saved to disk before extraction (``download_to_file``).

Progress callbacks now receive an extra ``speed_bps`` argument
(bytes-per-second over the last sampling window) so the bot can render
human-friendly ``X MB/s, ETA Ys`` strings without having to track
state itself. The older ``(read, total)`` two-argument signature is
still honoured for backwards compatibility.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Iterator, Optional, Union

import requests

log = logging.getLogger(__name__)

CHUNK_SIZE = 1 * 1024 * 1024  # 1 MB — bigger chunks → fewer syscalls → faster
DEFAULT_TIMEOUT = (30, 600)  # (connect, read)
DEFAULT_USER_AGENT = (
    "logs-to-cookie/2.1 (+https://github.com/zyblahblah/logs-to-cookie)"
)


class DownloadError(RuntimeError):
    """Raised when the download fails for any reason.

    ``size`` (when known) carries the server-advertised content length
    and ``cap`` carries the configured ``max_bytes`` so callers can
    render a richer "X GB > cap (Y GB)" error.
    """

    def __init__(
        self,
        msg: str,
        *,
        size: Optional[int] = None,
        cap: Optional[int] = None,
    ):
        super().__init__(msg)
        self.size = size
        self.cap = cap


# ``progress(bytes_read, total_or_None, speed_bps_or_None)`` — the
# third argument is added for richer status renders and may be ``None``
# on the very first emit before a sampling window has elapsed. Callers
# implementing the older two-argument signature still work because we
# fall back to a positional call when a TypeError is raised.
ProgressCallback = Union[
    Callable[[int, Optional[int]], None],
    Callable[[int, Optional[int], Optional[float]], None],
]


def _emit_progress(
    cb: Optional[ProgressCallback],
    read: int,
    total: Optional[int],
    speed: Optional[float],
) -> None:
    if cb is None:
        return
    try:
        cb(read, total, speed)  # type: ignore[call-arg]
    except TypeError:
        # Backwards-compat with the old (read, total) signature.
        cb(read, total)  # type: ignore[call-arg]


def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"})
    return s


def _open_stream(
    url: str,
    *,
    session: Optional[requests.Session] = None,
    timeout: tuple = DEFAULT_TIMEOUT,
) -> requests.Response:
    sess = session or _build_session()
    try:
        resp = sess.get(url, stream=True, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        raise DownloadError(f"network error: {exc}") from exc

    if resp.status_code >= 400:
        resp.close()
        raise DownloadError(f"HTTP {resp.status_code} for {url}")
    return resp


def _check_cap(total: int, cap: Optional[int]) -> None:
    if cap is not None and total > cap:
        raise DownloadError(
            f"file is {total} bytes, larger than max ({cap})",
            size=total,
            cap=cap,
        )


def download_to_file(
    url: str,
    dest: Path,
    *,
    max_bytes: Optional[int] = None,
    on_progress: Optional[ProgressCallback] = None,
    progress_interval: float = 1.0,
    session: Optional[requests.Session] = None,
) -> int:
    """Stream ``url`` to ``dest`` in 1 MB chunks.

    Returns the number of bytes written. Raises :class:`DownloadError`
    on transport failures or if the response exceeds ``max_bytes``.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = _open_stream(url, session=session)

    total: Optional[int] = None
    cl = resp.headers.get("Content-Length")
    if cl and cl.isdigit():
        total = int(cl)
        try:
            _check_cap(total, max_bytes)
        except DownloadError:
            resp.close()
            raise

    written = 0
    started = time.time()
    last_emit = started
    last_emit_bytes = 0
    try:
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue
                f.write(chunk)
                written += len(chunk)
                if max_bytes is not None and written > max_bytes:
                    raise DownloadError(
                        f"download exceeded max_bytes ({max_bytes})",
                        size=total,
                        cap=max_bytes,
                    )
                if on_progress is not None:
                    now = time.time()
                    if now - last_emit >= progress_interval:
                        elapsed = max(now - last_emit, 1e-6)
                        speed = (written - last_emit_bytes) / elapsed
                        _emit_progress(on_progress, written, total, speed)
                        last_emit = now
                        last_emit_bytes = written
    finally:
        resp.close()

    if on_progress is not None:
        # Final emit uses the average speed across the whole download
        # so the last status line shows a stable number.
        elapsed = max(time.time() - started, 1e-6)
        avg_speed = written / elapsed
        _emit_progress(on_progress, written, total, avg_speed)
    return written


def stream_lines(
    url: str,
    *,
    max_bytes: Optional[int] = None,
    on_progress: Optional[ProgressCallback] = None,
    progress_interval: float = 1.0,
    session: Optional[requests.Session] = None,
) -> Iterator[str]:
    """Yield lines from ``url`` without ever buffering the full body.

    Bytes are decoded as UTF-8 with errors replaced. The helper stitches
    partial lines across chunk boundaries so a cookie row split across
    two TCP packets is never lost.
    """
    resp = _open_stream(url, session=session)
    total: Optional[int] = None
    cl = resp.headers.get("Content-Length")
    if cl and cl.isdigit():
        total = int(cl)
        try:
            _check_cap(total, max_bytes)
        except DownloadError:
            resp.close()
            raise

    bytes_read = 0
    started = time.time()
    last_emit = started
    last_emit_bytes = 0
    pending = ""
    try:
        for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
            if not chunk:
                continue
            bytes_read += len(chunk)
            if max_bytes is not None and bytes_read > max_bytes:
                raise DownloadError(
                    f"download exceeded max_bytes ({max_bytes})",
                    size=total,
                    cap=max_bytes,
                )
            if on_progress is not None:
                now = time.time()
                if now - last_emit >= progress_interval:
                    elapsed = max(now - last_emit, 1e-6)
                    speed = (bytes_read - last_emit_bytes) / elapsed
                    _emit_progress(on_progress, bytes_read, total, speed)
                    last_emit = now
                    last_emit_bytes = bytes_read

            text = chunk.decode("utf-8", errors="replace")
            if pending:
                text = pending + text
                pending = ""

            # Hold on to a trailing partial line until the next chunk.
            if not text.endswith("\n"):
                idx = text.rfind("\n")
                if idx == -1:
                    pending = text
                    continue
                pending = text[idx + 1:]
                text = text[: idx + 1]

            for line in text.splitlines():
                yield line

        if pending:
            yield pending
    finally:
        resp.close()

    if on_progress is not None:
        elapsed = max(time.time() - started, 1e-6)
        avg_speed = bytes_read / elapsed
        _emit_progress(on_progress, bytes_read, total, avg_speed)
