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

Resumable downloads
-------------------
``download_to_file`` is **resumable**: on any transient stream failure
(``IncompleteRead`` / ``ChunkedEncodingError`` / connection drop), the
function reopens the URL with a ``Range: bytes={written}-`` header and
appends to the partial file on disk. Up to ``max_attempts`` reconnects
are tried with exponential backoff before giving up. If the existing
partial file already covers the full content length, the helper
returns immediately without any HTTP traffic. This is the single
biggest behaviour change in this module — before it, a flaky CDN that
closed the stream at byte 57 GB of a 62 GB archive forced the user to
restart the whole job from zero.
"""

from __future__ import annotations

import http.client
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Iterator, Mapping, Optional, Union

import requests
import urllib3

log = logging.getLogger(__name__)

CHUNK_SIZE = 1 * 1024 * 1024  # 1 MB — bigger chunks → fewer syscalls → faster
DEFAULT_TIMEOUT = (30, 600)  # (connect, read)
DEFAULT_USER_AGENT = (
    "logs-to-cookie/2.1 (+https://github.com/zyblahblah/logs-to-cookie)"
)

# How many times ``download_to_file`` reconnects after a transient
# stream failure before giving up. The real-world failure that
# motivated this knob was a flaky CDN that dropped the connection at
# byte 57 GB of a 62 GB download — a single reconnect attempt is
# enough to finish the job in that case, but we keep a generous
# default so a brief outage doesn't bubble up to the user.
DEFAULT_DOWNLOAD_MAX_ATTEMPTS: int = int(
    os.getenv("DOWNLOAD_MAX_ATTEMPTS", "10")
)
# Base delay (seconds) for the exponential backoff between reconnect
# attempts. Real delay is ``min(BACKOFF_CAP, base * 2 ** (attempt-1))``.
DEFAULT_DOWNLOAD_RETRY_BASE_DELAY: float = float(
    os.getenv("DOWNLOAD_RETRY_BASE_DELAY", "2.0")
)
DEFAULT_DOWNLOAD_RETRY_BACKOFF_CAP: float = float(
    os.getenv("DOWNLOAD_RETRY_BACKOFF_CAP", "60.0")
)

# Exceptions that mean ``the remote closed the connection mid-stream
# but the request itself was well-formed``. Any of these triggers the
# resume-with-Range retry loop in ``download_to_file``.
_RETRYABLE_STREAM_EXCEPTIONS: tuple = (
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ConnectionError,
    requests.exceptions.ReadTimeout,
    requests.exceptions.StreamConsumedError,
    http.client.IncompleteRead,
    urllib3.exceptions.ProtocolError,
    urllib3.exceptions.ReadTimeoutError,
    ConnectionError,
    TimeoutError,
)

# ``Content-Range: bytes 100-499/500`` — we only care about the total.
_CONTENT_RANGE_TOTAL = re.compile(r"bytes\s+\d+-\d+/(\d+)")


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
    headers: Optional[Mapping[str, str]] = None,
    allowed_statuses: tuple = (200,),
) -> requests.Response:
    """Open a streaming GET for ``url``.

    ``headers`` is merged into the session's defaults (used for
    ``Range:`` on resume attempts). ``allowed_statuses`` controls
    which non-error statuses we treat as success — callers that
    expect 206 Partial Content on a Range request should pass
    ``(200, 206)`` or check the returned status themselves.
    """
    sess = session or _build_session()
    merged = dict(headers) if headers else None
    try:
        resp = sess.get(
            url,
            stream=True,
            timeout=timeout,
            allow_redirects=True,
            headers=merged,
        )
    except requests.RequestException as exc:
        raise DownloadError(f"network error: {exc}") from exc

    if resp.status_code >= 400:
        resp.close()
        raise DownloadError(f"HTTP {resp.status_code} for {url}")
    if resp.status_code not in allowed_statuses:
        resp.close()
        raise DownloadError(
            f"unexpected HTTP {resp.status_code} for {url} "
            f"(expected one of {allowed_statuses})"
        )
    return resp


def _check_cap(total: int, cap: Optional[int]) -> None:
    if cap is not None and total > cap:
        raise DownloadError(
            f"file is {total} bytes, larger than max ({cap})",
            size=total,
            cap=cap,
        )


def _parse_total_from_response(resp: requests.Response) -> Optional[int]:
    """Best-effort: pull the full-resource size out of a response.

    For 200 OK responses we use ``Content-Length`` directly. For
    206 Partial Content we parse ``Content-Range: bytes X-Y/TOTAL``
    — the ``Content-Length`` of a 206 only describes the chunk we
    asked for, not the full resource.
    """
    if resp.status_code == 206:
        cr = resp.headers.get("Content-Range", "")
        m = _CONTENT_RANGE_TOTAL.match(cr)
        if m:
            return int(m.group(1))
        # Fall through: rare but some servers omit Content-Range.
    cl = resp.headers.get("Content-Length")
    if cl and cl.isdigit():
        # For 200 OK this is the full resource; for 206 it's the
        # chunk only — callers handle that case via Content-Range
        # above, so we only get here when the server returned 200.
        return int(cl)
    return None


def download_to_file(
    url: str,
    dest: Path,
    *,
    max_bytes: Optional[int] = None,
    on_progress: Optional[ProgressCallback] = None,
    progress_interval: float = 1.0,
    session: Optional[requests.Session] = None,
    max_attempts: int = DEFAULT_DOWNLOAD_MAX_ATTEMPTS,
    retry_base_delay: float = DEFAULT_DOWNLOAD_RETRY_BASE_DELAY,
    retry_backoff_cap: float = DEFAULT_DOWNLOAD_RETRY_BACKOFF_CAP,
) -> int:
    """Stream ``url`` to ``dest`` in 1 MB chunks, with resume on failure.

    Returns the number of bytes written. Raises :class:`DownloadError`
    on transport failures (after exhausting reconnect attempts) or if
    the response exceeds ``max_bytes``.

    Resume semantics
    ----------------
    * If ``dest`` already exists when this is called, the helper
      assumes it's a previous partial download and resumes from
      ``dest.stat().st_size`` via a ``Range:`` header. The caller is
      responsible for clearing ``dest`` if they explicitly want a
      fresh start.
    * If the stream fails mid-transfer (``IncompleteRead`` /
      ``ChunkedEncodingError`` / connection reset), the function
      reopens with a fresh ``Range:`` header and appends to ``dest``.
    * Each retry waits ``min(retry_backoff_cap, retry_base_delay *
      2 ** (attempt-1))`` seconds before reconnecting.
    * Total attempts are capped at ``max_attempts``; the function
      raises :class:`DownloadError` once the budget is exhausted.
    * If the server returns 200 (full body) on a Range request, the
      helper treats it as "resume not supported" and starts over from
      byte 0.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Resume from an existing partial file, if any. Two contexts:
    # (a) the same process retried the same URL after a network
    #     failure earlier in the run — file is already on disk and we
    #     just want to keep writing.
    # (b) the bot process was restarted (Railway OOM/redeploy) and the
    #     workdir is persistent (e.g. a Volume mount) — the partial
    #     file is still there, and the new run reuses it.
    written = dest.stat().st_size if dest.exists() else 0

    total: Optional[int] = None
    attempt = 0
    last_exc: Optional[BaseException] = None
    started = time.time()
    last_emit = started
    last_emit_bytes = written

    while attempt < max_attempts:
        attempt += 1
        headers: dict = {}
        if written > 0:
            headers["Range"] = f"bytes={written}-"
        try:
            resp = _open_stream(
                url,
                session=session,
                headers=headers or None,
                allowed_statuses=(200, 206) if written > 0 else (200,),
            )
        except DownloadError as exc:
            last_exc = exc
            # 416 Range Not Satisfiable arrives as HTTP 416 — it
            # almost always means we already downloaded the full file
            # earlier and the server is telling us so. Treat it as
            # success and let the size check below confirm.
            if "HTTP 416" in str(exc) and written > 0:
                log.info(
                    "server returned 416 on resume — treating partial "
                    "file at %d bytes as complete",
                    written,
                )
                break
            # Fall through to backoff + retry.
            log.warning(
                "download_to_file attempt %d/%d: open failed at %d bytes: %s",
                attempt,
                max_attempts,
                written,
                exc,
            )
            if attempt >= max_attempts:
                break
            _sleep_backoff(attempt, retry_base_delay, retry_backoff_cap)
            continue

        # Re-discover ``total`` on every reconnect — the server may
        # have advertised it differently on the second response.
        new_total = _parse_total_from_response(resp)
        if new_total is not None:
            total = new_total
        if total is not None:
            try:
                _check_cap(total, max_bytes)
            except DownloadError:
                resp.close()
                raise

        # If we asked for a Range but the server returned 200, the
        # server is ignoring Range — restart from byte 0 by
        # truncating the on-disk file.
        if written > 0 and resp.status_code == 200:
            log.warning(
                "server ignored Range header — restarting download "
                "from byte 0 (was at %d bytes)",
                written,
            )
            resp.close()
            try:
                dest.unlink()
            except OSError:  # pragma: no cover
                pass
            written = 0
            last_emit_bytes = 0
            # Don't count this as a real retry — it's effectively a
            # "resume not supported" branch and we want a fresh budget
            # to actually download the file.
            attempt = max(0, attempt - 1)
            continue

        mode = "ab" if written > 0 else "wb"
        try:
            with open(dest, mode) as f:
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
        except _RETRYABLE_STREAM_EXCEPTIONS as exc:
            last_exc = exc
            log.warning(
                "download_to_file attempt %d/%d: stream broke at %d/%s bytes: %s",
                attempt,
                max_attempts,
                written,
                total if total is not None else "?",
                exc,
            )
            try:
                resp.close()
            except Exception:  # noqa: BLE001  # pragma: no cover
                pass
            if attempt >= max_attempts:
                break
            _sleep_backoff(attempt, retry_base_delay, retry_backoff_cap)
            continue
        else:
            resp.close()
            # No exception, but did we actually get the full file? If
            # ``total`` is known and we're short, treat it like a
            # transient failure and retry with a Range request.
            if total is None or written >= total:
                break
            log.warning(
                "download_to_file attempt %d/%d: stream ended early at "
                "%d/%d bytes — retrying with Range",
                attempt,
                max_attempts,
                written,
                total,
            )
            last_exc = DownloadError(
                f"stream ended early at {written}/{total} bytes"
            )
            if attempt >= max_attempts:
                break
            _sleep_backoff(attempt, retry_base_delay, retry_backoff_cap)
            continue

    if total is not None and written < total:
        raise DownloadError(
            f"download truncated: {written}/{total} bytes after "
            f"{attempt} attempt(s) (last error: {last_exc})",
            size=total,
            cap=max_bytes,
        ) from last_exc

    if on_progress is not None:
        # Final emit uses the average speed across the whole download
        # so the last status line shows a stable number.
        elapsed = max(time.time() - started, 1e-6)
        avg_speed = written / elapsed
        _emit_progress(on_progress, written, total, avg_speed)
    return written


def _sleep_backoff(attempt: int, base: float, cap: float) -> None:
    """Sleep ``min(cap, base * 2 ** (attempt-1))`` seconds.

    Pulled into its own helper so tests can monkeypatch it cheaply.
    """
    delay = min(cap, base * (2 ** max(0, attempt - 1)))
    time.sleep(delay)


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
