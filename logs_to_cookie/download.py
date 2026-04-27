"""Stream-download large archives from a direct HTTP(S) URL.

Range-split parallel download with single-stream fallback. Stdlib only.

Inspired by the engine in ``zyblahblah/zyblahblah-ulp-to-combo``: HEAD probe
for ``Accept-Ranges`` + ``Content-Length``, split the byte range into N
parts, run them in a thread pool, each part writes to its own offset in the
pre-allocated destination file.
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
DEFAULT_TIMEOUT = 60
DEFAULT_RETRIES = 3
USER_AGENT = "logs-to-cookie/0.9 (+https://github.com/zyblahblah/logs-to-cookie)"

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


class _ProgressPrinter:
    def __init__(self, label: str, stream=sys.stderr, interval: float = 0.5):
        self.label = label
        self.stream = stream
        self.interval = interval
        self.start = time.monotonic()
        self.last = 0.0
        self._lock = threading.Lock()

    def __call__(self, done: int, total: int, elapsed: float) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self.last < self.interval and (not total or done < total):
                return
            self.last = now
            mb = done / 1024 / 1024
            speed = (done / elapsed / 1024 / 1024) if elapsed > 0 else 0.0
            if total:
                t_mb = total / 1024 / 1024
                pct = 100.0 * done / total
                eta = ((total - done) / (done / elapsed)) if done else 0.0
                line = (
                    f"\r{self.label}: {mb:7.1f} / {t_mb:7.1f} MB "
                    f"({pct:5.1f}%)  {speed:6.2f} MB/s  ETA {int(eta):4d}s"
                )
            else:
                line = (
                    f"\r{self.label}: {mb:7.1f} MB  {speed:6.2f} MB/s"
                )
            self.stream.write(line)
            self.stream.flush()
            if total and done >= total:
                self.stream.write("\n")
                self.stream.flush()


def _stream_to(
    url: str,
    dest: Path,
    *,
    headers: Optional[dict] = None,
    chunk: int = DEFAULT_CHUNK,
    on_progress: Optional[ProgressCallback] = None,
    append: bool = False,
    bytes_done_offset: int = 0,
    total: Optional[int] = None,
) -> int:
    """Single-stream download to ``dest``. Returns number of bytes written here."""
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
                    on_progress(
                        bytes_done_offset + written,
                        (total or 0) + bytes_done_offset
                        if append
                        else (total or 0),
                        time.monotonic() - start,
                    )
    return written


def _download_part(
    url: str,
    dest: Path,
    start: int,
    end: int,
    *,
    chunk: int,
    progress_cb: Callable[[int], None],
    retries: int,
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
            with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                with open(dest, "rb+") as f:
                    f.seek(req_start)
                    while True:
                        buf = resp.read(chunk)
                        if not buf:
                            break
                        # Respect the part boundary just in case.
                        remaining = end - req_start - written + 1
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
        except Exception as exc:  # noqa: BLE001 — we want to retry any IO error
            last_exc = exc
            time.sleep(min(2 ** attempt, 15))
    raise RuntimeError(f"part {start}-{end} failed after {retries} retries: {last_exc}")


def stream_download(
    url: str,
    dest: Path,
    *,
    workers: int = DEFAULT_WORKERS,
    chunk: int = DEFAULT_CHUNK,
    retries: int = DEFAULT_RETRIES,
    on_progress: Optional[ProgressCallback] = None,
    label: str = "Downloading",
    show_progress: bool = True,
) -> Path:
    """Download ``url`` to ``dest`` (range-split parallel + single-stream fallback)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if on_progress is None and show_progress:
        on_progress = _ProgressPrinter(label)

    total, ranges_ok, final_url = _probe(url)
    use_url = final_url or url

    if not (total and ranges_ok and workers > 1 and total > chunk * 2):
        if show_progress:
            mode_label = "single-stream"
            print(f"  {mode_label}: {use_url}", file=sys.stderr)
        _stream_to(use_url, dest, chunk=chunk, on_progress=on_progress, total=total)
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
                )
                for (s, e) in parts
            ]
            for fut in concurrent.futures.as_completed(futs):
                fut.result()
    except Exception:
        # Fall back to single stream on any range failure.
        if show_progress:
            print(
                "\n  range-split failed → falling back to single stream",
                file=sys.stderr,
            )
        try:
            dest.unlink(missing_ok=True)
        except TypeError:  # py<3.8 unlink has no missing_ok
            if dest.exists():
                dest.unlink()
        _stream_to(use_url, dest, chunk=chunk, on_progress=on_progress, total=total)

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
    label = f"{label_prefix} {name}"
    return stream_download(url, dest, workers=workers, label=label)
