"""Stream-download large archives from a direct HTTP(S) URL.

Range-split parallel download with single-stream resume fallback. Stdlib
only.

Inspired by the engine in ``zyblahblah/zyblahblah-ulp-to-combo``: HEAD
probe for ``Accept-Ranges`` + ``Content-Length``, split the byte range
into N parts, run them in a thread pool, each part writes to its own
offset in the pre-allocated destination file. Each part keeps a tight
per-read socket timeout and retries with exponential backoff so flaky
mobile / shared links don't leave the download wedged near completion.
"""

from __future__ import annotations

import concurrent.futures
import os
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional, Tuple

# 1 MiB chunks balance memory and throughput on flaky mobile links.
DEFAULT_CHUNK = 1024 * 1024
DEFAULT_WORKERS = 4
# Per-read socket timeout. If urllib doesn't see *any* bytes for this
# many seconds it raises socket.timeout, which the part loop catches and
# turns into a fresh retry. Keep it tight enough that a dead connection
# gets detected before the whole job looks "stuck".
DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 5
USER_AGENT = "logs-to-cookie/0.10 (+https://github.com/zyblahblah/logs-to-cookie)"

ProgressCallback = Callable[[int, int, float], None]


def is_url(s: str) -> bool:
    return isinstance(s, str) and s.lower().startswith(("http://", "https://"))


def filename_from_url(url: str, fallback: str = "download.bin") -> str:
    parsed = urllib.parse.urlparse(url)
    name = Path(urllib.parse.unquote(parsed.path or "")).name
    return name or fallback


def _open(url: str, headers: Optional[dict] = None, timeout: int = DEFAULT_TIMEOUT):
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    return urllib.request.urlopen(req, timeout=timeout)


def _probe(url: str, timeout: int = 30) -> Tuple[Optional[int], bool, str]:
    """Return (content_length, supports_ranges, final_url) via HEAD with GET fallback."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT}, method="HEAD"
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            length = r.headers.get("Content-Length")
            ranges = (r.headers.get("Accept-Ranges") or "").lower() == "bytes"
            return (int(length) if length else None, ranges, r.geturl())
    except Exception:
        # Some hosts reject HEAD — peek with a tiny ranged GET.
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                # Content-Range: bytes 0-0/12345 → total = 12345
                cr = r.headers.get("Content-Range") or ""
                total = None
                if "/" in cr:
                    try:
                        total = int(cr.rsplit("/", 1)[1])
                    except ValueError:
                        total = None
                ranges = r.status == 206
                return (total, ranges, r.geturl())
        except Exception:
            return (None, False, url)


# ---------------------------------------------------------------------------
# Pretty progress formatting
# ---------------------------------------------------------------------------


def _fmt_size(n: float) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.2f} MB"
    if n >= 1024:
        return f"{n / 1024:.2f} KB"
    return f"{int(n)} B"


def _fmt_eta(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _bar(pct: float, width: int = 14) -> str:
    pct = max(0.0, min(100.0, pct))
    full = int(pct / 100 * width)
    rem = (pct / 100 * width) - full
    s = "■" * full
    if full < width:
        s += "▧" if rem >= 0.25 else "□"
        s += "□" * (width - full - 1)
    return s


class _ProgressPrinter:
    """Emit a multi-line progress block to ``stream`` on every update.

    Block format::

        🌀 Status: Downloading...
        📦 <filename>
        📊 [■■■▧□□□□□□□□□□] 21.0%
        📡 Progress: 275.00 MB / 1.28 GB
        ⚡ Speed: 19.64 MB/s | ETA: 52s
        ⏱️  Elapsed: 15s

    ``bot.py``'s ``_ProgressMessage`` detects ``🌀 Status:`` as a block
    boundary and replaces the previously-rendered block, so the chat
    message stays a single live tile instead of scrolling. On a real
    terminal we use ANSI cursor moves to overwrite in place.
    """

    BLOCK_LINES = 6

    def __init__(self, label: str, stream=sys.stderr, interval: float = 1.0):
        self.label = label
        self.stream = stream
        self.interval = interval
        self.start = time.monotonic()
        self.last = 0.0
        self._lock = threading.Lock()
        self._isatty = bool(getattr(stream, "isatty", lambda: False)())
        self._printed = False

    def __call__(self, done: int, total: int, elapsed: float) -> None:
        now = time.monotonic()
        with self._lock:
            done_final = bool(total) and done >= total
            if (
                now - self.last < self.interval
                and not done_final
                and self._printed
            ):
                return
            self.last = now
            speed_mb = (done / elapsed / 1024 / 1024) if elapsed > 0 else 0.0
            if total:
                pct = 100.0 * done / total
                eta = (
                    ((total - done) / (done / elapsed))
                    if (done and elapsed > 0)
                    else 0.0
                )
                block = (
                    f"🌀 Status: Downloading...\n"
                    f"📦 {self.label}\n"
                    f"📊 [{_bar(pct)}] {pct:.1f}%\n"
                    f"📡 Progress: {_fmt_size(done)} / {_fmt_size(total)}\n"
                    f"⚡ Speed: {speed_mb:.2f} MB/s | ETA: {_fmt_eta(eta)}\n"
                    f"⏱️ Elapsed: {_fmt_eta(elapsed)}\n"
                )
            else:
                block = (
                    f"🌀 Status: Downloading...\n"
                    f"📦 {self.label}\n"
                    f"📡 Progress: {_fmt_size(done)}\n"
                    f"⚡ Speed: {speed_mb:.2f} MB/s\n"
                    f"⏱️ Elapsed: {_fmt_eta(elapsed)}\n"
                )
            if self._isatty and self._printed:
                # Wipe the previous block so we render in place.
                self.stream.write(f"\x1b[{self.BLOCK_LINES}A\x1b[J")
            self.stream.write(block)
            self.stream.flush()
            self._printed = True


def emit_status(stage: str, detail: str = "", stream=sys.stderr) -> None:
    """Emit a one-shot pretty status block (no progress bar).

    Used by the CLI to announce phases like ``Extracting`` or ``Sorting``
    so the bot's progress message keeps something fresh on screen even
    when the download is finished and post-processing is underway.
    """
    block = f"🌀 Status: {stage}\n"
    if detail:
        block += f"📦 {detail}\n"
    stream.write(block)
    stream.flush()


# ---------------------------------------------------------------------------
# Single-stream download (with resume)
# ---------------------------------------------------------------------------


def _stream_to(
    url: str,
    dest: Path,
    *,
    headers: Optional[dict] = None,
    chunk: int = DEFAULT_CHUNK,
    on_progress: Optional[ProgressCallback] = None,
    bytes_done_offset: int = 0,
    total: Optional[int] = None,
    append: bool = False,
) -> int:
    """Single-stream download to ``dest``. Returns bytes written *here*."""
    mode = "ab" if append else "wb"
    written = 0
    start = time.monotonic()
    with _open(url, headers=headers) as resp:
        if total is None:
            length = resp.headers.get("Content-Length")
            try:
                total = int(length) if length else None
            except ValueError:
                total = None
        with open(dest, mode) as f:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                f.write(buf)
                written += len(buf)
                if on_progress:
                    grand_total = (
                        ((total or 0) + bytes_done_offset) if append else (total or 0)
                    )
                    on_progress(
                        bytes_done_offset + written,
                        grand_total,
                        time.monotonic() - start,
                    )
    return written


def _stream_with_resume(
    url: str,
    dest: Path,
    *,
    chunk: int,
    retries: int,
    on_progress: Optional[ProgressCallback],
    total: Optional[int],
) -> None:
    """Single-stream download that resumes on transient errors."""
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            already = dest.stat().st_size if dest.exists() else 0
            if total is not None and already >= total > 0:
                return
            # Only resume via append when we can actually send a Range
            # request. Without ``total`` (server didn't advertise length)
            # we have no reliable way to ask the server to skip bytes, so
            # falling back to a fresh full-file GET would otherwise be
            # appended on top of the partial bytes — corrupting output.
            headers = (
                {"Range": f"bytes={already}-"} if already and total else None
            )
            if already and not headers:
                # Can't resume safely; truncate and start over.
                try:
                    dest.unlink()
                except FileNotFoundError:
                    pass
                already = 0
            _stream_to(
                url,
                dest,
                headers=headers,
                chunk=chunk,
                on_progress=on_progress,
                bytes_done_offset=already,
                total=(total - already) if (total and headers) else total,
                append=bool(headers),
            )
            return
        except Exception as exc:  # noqa: BLE001 — we want to retry every IO error
            last_exc = exc
            if attempt >= retries:
                break
            backoff = min(2 ** attempt, 30)
            print(
                f"\n  single-stream attempt {attempt + 1} failed ({exc!r}); "
                f"resuming in {backoff}s…",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(backoff)
    raise RuntimeError(f"single-stream download failed: {last_exc}")


# ---------------------------------------------------------------------------
# Range-split parallel download
# ---------------------------------------------------------------------------


def _download_part(
    url: str,
    dest: Path,
    start: int,
    end: int,
    *,
    chunk: int,
    progress_cb: Callable[[int], None],
    retries: int,
    timeout: int,
) -> None:
    last_exc: Optional[Exception] = None
    written = 0
    for attempt in range(retries + 1):
        try:
            req_start = start + written
            if req_start > end:
                return
            headers = {
                "User-Agent": USER_AGENT,
                "Range": f"bytes={req_start}-{end}",
            }
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                with open(dest, "rb+") as f:
                    f.seek(req_start)
                    while True:
                        buf = resp.read(chunk)
                        if not buf:
                            break
                        # Respect the part boundary. ``written`` is the total
                        # bytes successfully written for this part across all
                        # attempts so far, so the formula is relative to
                        # ``start``, not ``req_start``.
                        remaining = end - start - written + 1
                        if len(buf) > remaining:
                            buf = buf[:remaining]
                        if not buf:
                            break
                        f.write(buf)
                        written += len(buf)
                        progress_cb(len(buf))
            if (start + written - 1) >= end:
                return
            # Stream ended early — retry the rest.
            last_exc = RuntimeError(
                f"part {start}-{end}: ended early at {start + written - 1}"
            )
        except Exception as exc:  # noqa: BLE001 — retry every IO error
            last_exc = exc
        if attempt >= retries:
            break
        time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(
        f"part {start}-{end} failed after {retries} retries: {last_exc}"
    )


def stream_download(
    url: str,
    dest: Path,
    *,
    workers: int = DEFAULT_WORKERS,
    chunk: int = DEFAULT_CHUNK,
    retries: int = DEFAULT_RETRIES,
    timeout: int = DEFAULT_TIMEOUT,
    on_progress: Optional[ProgressCallback] = None,
    label: str = "",
    show_progress: bool = True,
) -> Path:
    """Download ``url`` to ``dest`` (range-split parallel + single-stream resume fallback)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if on_progress is None and show_progress:
        on_progress = _ProgressPrinter(label or filename_from_url(url))

    total, ranges_ok, final_url = _probe(url)
    use_url = final_url or url

    if not (total and ranges_ok and workers > 1 and total > chunk * 2):
        if show_progress:
            print(f"  single-stream: {use_url}", file=sys.stderr, flush=True)
        _stream_with_resume(
            use_url,
            dest,
            chunk=chunk,
            retries=retries,
            on_progress=on_progress,
            total=total,
        )
        return dest

    # Pre-allocate the destination file.
    with open(dest, "wb") as f:
        f.truncate(total)

    parts: List[Tuple[int, int]] = []
    part_size = max(chunk, (total + workers - 1) // workers)
    pos = 0
    while pos < total:
        end = min(pos + part_size, total) - 1
        parts.append((pos, end))
        pos = end + 1

    if show_progress:
        print(
            f"  range-split: {len(parts)} parts × {part_size / 1024 / 1024:.1f} MB → {use_url}",
            file=sys.stderr,
            flush=True,
        )

    bytes_done = [0]
    bd_lock = threading.Lock()
    start_t = time.monotonic()

    def _bump(n: int) -> None:
        with bd_lock:
            bytes_done[0] += n
            if on_progress:
                on_progress(bytes_done[0], total, time.monotonic() - start_t)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [
                ex.submit(
                    _download_part,
                    use_url,
                    dest,
                    s,
                    e,
                    chunk=chunk,
                    progress_cb=_bump,
                    retries=retries,
                    timeout=timeout,
                )
                for (s, e) in parts
            ]
            for fut in concurrent.futures.as_completed(futs):
                fut.result()
    except Exception as exc:  # noqa: BLE001 — fall back to single-stream resume
        if show_progress:
            print(
                f"\n  range-split failed ({exc!r}) → falling back to "
                "single stream with resume",
                file=sys.stderr,
                flush=True,
            )
        # The pre-allocated file is full of zeros; truncate so the resume
        # path doesn't think we already have data.
        try:
            dest.unlink(missing_ok=True)
        except TypeError:  # py<3.8 unlink has no missing_ok
            if dest.exists():
                dest.unlink()
        _stream_with_resume(
            use_url,
            dest,
            chunk=chunk,
            retries=retries,
            on_progress=on_progress,
            total=total,
        )

    return dest


def download_to_workdir(
    url: str,
    workdir: Path,
    *,
    workers: int = DEFAULT_WORKERS,
    label_prefix: str = "Downloading",
) -> Path:
    """Convenience wrapper: pick a sensible filename under ``workdir`` and download."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    name = filename_from_url(url)
    dest = workdir / name
    n = 1
    while dest.exists():
        dest = workdir / f"{Path(name).stem}_{n}{Path(name).suffix}"
        n += 1
    return stream_download(url, dest, workers=workers, label=name)
