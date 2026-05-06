"""End-to-end pipeline that ties download + extract + cookie parsing.

The flow mirrors the diagram in the README:

    /start
       └─► download link  (chunked stream, never buffered to disk)
             └─► password? (only used for encrypted archives)
                  └─► keywords?  (optional case-insensitive filter)
                       └─► parse + extract cookies
                            └─► one Netscape file per "cookie set"
                                 └─► zip → uploaded by the bot

Two public entrypoints:

* :func:`run_pipeline` — single URL.
* :func:`run_pipeline_multi` — many URLs, downloaded and extracted in
  parallel, all results merged into one zip. Used by the multi-link
  flow in ``bot.py``.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from .archive import (
    ARCHIVE_SUFFIXES,
    detect_archive_kind,
    extract_archive,
)
from .cookies import (
    CookieRow,
    iter_cookies_from_lines,
    parse_cookie_line,
    write_netscape_file,
)
from .download import download_to_file

log = logging.getLogger(__name__)

# A "cookie set" is a single source of cookies — one log victim, one
# domain, one stored cookies.txt. For archive inputs we honour the
# directory layout the stealer left behind; for plain-text URLs we
# treat the whole stream as a single set.
COOKIE_FILENAME_HINTS: Tuple[str, ...] = (
    "cookie",
    "cookies",
    "cookie.txt",
    "cookies.txt",
    "passwords-cookies",
)

# Per-URL extraction worker count. The work is heavily I/O bound
# (parsing big text files) so a small thread pool already saturates
# typical disks; bumping it higher mostly costs RAM.
DEFAULT_PARSE_WORKERS = max(2, min(8, (os.cpu_count() or 2)))

# How many URLs to download + extract in parallel for the multi-link
# flow. Defaults to 4 because Telegram's typical user pastes 2-5 links
# and each one easily saturates a modern home connection.
DEFAULT_DOWNLOAD_WORKERS = 4

StatusCallback = Callable[[str], None]
"""``status(message)`` — called by the pipeline to report progress."""

ProgressCallback = Callable[[int, Optional[int]], None]
"""``progress(bytes_read, total_bytes_or_None)``."""


@dataclass
class PipelineResult:
    zip_path: Path
    output_dir: Path
    cookie_files: List[Path] = field(default_factory=list)
    cookie_count: int = 0
    bytes_read: int = 0


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned or "cookies"


def _find_cookie_files(root: Path) -> List[Path]:
    out: List[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        lname = p.name.lower()
        if any(h in lname for h in COOKIE_FILENAME_HINTS):
            out.append(p)
            continue
        if lname.endswith(".txt"):
            # Heuristic: peek at the first non-comment line and accept
            # files that look like Netscape cookie tables.
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if not line.strip() or line.lstrip().startswith("#"):
                            continue
                        if parse_cookie_line(line) is not None:
                            out.append(p)
                        break
            except OSError:  # pragma: no cover
                continue
    return out


def _extract_set(
    src: Path,
    out_path: Path,
    keywords: Optional[Sequence[str]],
) -> int:
    """Read one source cookies file and write one Netscape file out."""
    rows: List[CookieRow] = []
    with open(src, "r", encoding="utf-8", errors="replace") as f:
        for row in iter_cookies_from_lines(f, keywords=keywords):
            rows.append(row)
    if not rows:
        return 0
    return write_netscape_file(out_path, rows)


def _zip_results(zip_path: Path, files: Sequence[Path], root: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        zip_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=1,
    ) as z:
        for p in files:
            z.write(p, arcname=p.relative_to(root).as_posix())


def _label_for(src: Path, archive_root: Path) -> str:
    """Build a human-readable filename from the source path inside the archive."""
    try:
        rel = src.relative_to(archive_root)
    except ValueError:
        rel = Path(src.name)
    parts = [_safe_name(p) for p in rel.parts if p not in (".", "")]
    if not parts:
        return _safe_name(src.stem)
    label = "__".join(parts)
    if label.lower().endswith(".txt"):
        label = label[:-4]
    return label or _safe_name(src.stem)


def _suffix_for_url(url: str) -> str:
    return next(
        (s for s in ARCHIVE_SUFFIXES if url.lower().endswith(s)),
        "",
    )


# ---------------------------------------------------------------------------
# Per-URL processing — shared by both single- and multi-URL entrypoints.
# ---------------------------------------------------------------------------
@dataclass
class _UrlOutcome:
    url: str
    bytes_read: int = 0
    sources: List[Path] = field(default_factory=list)  # source cookie files inside extracted/
    extracted_root: Optional[Path] = None  # base for _label_for()
    plain_rows: List[CookieRow] = field(default_factory=list)  # for non-archive URLs
    error: Optional[str] = None


def _process_one_url(
    idx: int,
    url: str,
    *,
    workdir: Path,
    password: Optional[str],
    keywords: Optional[Sequence[str]],
    max_bytes: Optional[int],
    on_progress: Optional[ProgressCallback],
) -> _UrlOutcome:
    """Download one URL, extract it (if archive), and locate cookie sources.

    The actual cookie parsing is done by the caller so we can renumber
    output files globally across all URLs in the multi-link flow.
    """
    outcome = _UrlOutcome(url=url)
    try:
        url_dir = workdir / f"url_{idx:04d}"
        url_dir.mkdir(parents=True, exist_ok=True)
        suffix = _suffix_for_url(url)
        download_path = url_dir / f"input{suffix or '.bin'}"
        outcome.bytes_read = download_to_file(
            url,
            download_path,
            max_bytes=max_bytes,
            on_progress=on_progress,
        )

        kind = detect_archive_kind(download_path)
        if kind is not None:
            extracted = url_dir / "extracted"
            extract_archive(download_path, extracted, password=password)
            outcome.extracted_root = extracted
            outcome.sources = _find_cookie_files(extracted)
        else:
            # Plain Netscape file — parse directly. We materialise the
            # rows here (rather than re-opening the file later) because
            # the temp dir for this URL gets nuked once the multi-URL
            # caller is done.
            with open(download_path, "r", encoding="utf-8", errors="replace") as f:
                outcome.plain_rows = [
                    row for row in iter_cookies_from_lines(f, keywords=keywords)
                ]
    except Exception as exc:  # noqa: BLE001 — capture everything for the caller
        outcome.error = str(exc)
        log.warning("url #%d failed: %s — %s", idx, url, exc)
    return outcome


# ---------------------------------------------------------------------------
# Single-URL entrypoint (kept for backwards compatibility + tests).
# ---------------------------------------------------------------------------
def run_pipeline(
    url: str,
    workdir: Path,
    *,
    password: Optional[str] = None,
    keywords: Optional[Sequence[str]] = None,
    max_bytes: Optional[int] = None,
    on_status: Optional[StatusCallback] = None,
    on_progress: Optional[ProgressCallback] = None,
) -> PipelineResult:
    """Run the full download → extract → convert pipeline for one URL."""
    return run_pipeline_multi(
        [url],
        workdir,
        password=password,
        keywords=keywords,
        max_bytes=max_bytes,
        on_status=on_status,
        on_progress=on_progress,
        download_workers=1,
    )


# ---------------------------------------------------------------------------
# Multi-URL entrypoint.
# ---------------------------------------------------------------------------
def run_pipeline_multi(
    urls: Sequence[str],
    workdir: Path,
    *,
    password: Optional[str] = None,
    passwords: Optional[Sequence[Optional[str]]] = None,
    keywords: Optional[Sequence[str]] = None,
    max_bytes: Optional[int] = None,
    on_status: Optional[StatusCallback] = None,
    on_progress: Optional[ProgressCallback] = None,
    download_workers: int = DEFAULT_DOWNLOAD_WORKERS,
    parse_workers: int = DEFAULT_PARSE_WORKERS,
) -> PipelineResult:
    """Process every URL in ``urls`` concurrently and merge the results.

    All cookie sets — across every URL — land in a single
    ``cookies_result.zip``. Each output file's name is prefixed with a
    sequential index plus a ``urlNN`` tag so the source is obvious.

    Passwords:
      * ``password=...`` — single password applied to every URL
        (backwards-compatible default).
      * ``passwords=[...]`` — per-URL passwords. The list **must** be
        the same length as ``urls``; entries may be ``None`` for
        URLs that aren't password-protected. ``passwords`` takes
        precedence over ``password`` when both are given.
    """
    if not urls:
        raise ValueError("run_pipeline_multi requires at least one URL")

    if passwords is not None:
        passwords_list: List[Optional[str]] = list(passwords)
        if len(passwords_list) != len(urls):
            raise ValueError(
                f"passwords has {len(passwords_list)} entries but "
                f"got {len(urls)} URL(s); they must match 1:1"
            )
    else:
        passwords_list = [password] * len(urls)

    workdir.mkdir(parents=True, exist_ok=True)
    output_dir = workdir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    cookies_dir = output_dir / "cookies"
    cookies_dir.mkdir(parents=True, exist_ok=True)

    def status(msg: str) -> None:
        log.info("status: %s", msg)
        if on_status is not None:
            try:
                on_status(msg)
            except Exception:  # noqa: BLE001
                log.exception("status callback raised")

    # ------------------------------------------------------------------
    # Aggregated progress: sum of bytes read across every URL.
    # ------------------------------------------------------------------
    progress_lock = threading.Lock()
    per_url_bytes: List[int] = [0] * len(urls)

    def _emit_progress() -> None:
        if on_progress is None:
            return
        with progress_lock:
            total = sum(per_url_bytes)
        try:
            on_progress(total, None)
        except Exception:  # noqa: BLE001
            log.exception("progress callback raised")

    def _make_per_url_progress(i: int) -> ProgressCallback:
        def _cb(read: int, _total: Optional[int]) -> None:
            with progress_lock:
                per_url_bytes[i] = read
            _emit_progress()
        return _cb

    # ------------------------------------------------------------------
    # 1. Download + extract every URL in parallel.
    # ------------------------------------------------------------------
    n_urls = len(urls)
    if n_urls == 1:
        status("⏳ Downloading...")
    else:
        status(f"⏳ Downloading... ({n_urls} links in parallel)")

    outcomes: List[_UrlOutcome] = [None] * n_urls  # type: ignore[list-item]
    workers = min(max(1, int(download_workers)), n_urls)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _process_one_url,
                i,
                u,
                workdir=workdir,
                password=passwords_list[i],
                keywords=keywords,
                max_bytes=max_bytes,
                on_progress=_make_per_url_progress(i),
            )
            for i, u in enumerate(urls)
        ]
        for i, fut in enumerate(futures):
            outcomes[i] = fut.result()

    total_bytes = sum(o.bytes_read for o in outcomes)
    errors = [(i, o) for i, o in enumerate(outcomes) if o.error is not None]
    if errors and len(errors) == n_urls:
        # Every URL failed — bubble up the first error so the bot can
        # show a single, useful message.
        first_err = errors[0][1].error or "unknown error"
        raise RuntimeError(f"all {n_urls} download(s) failed: {first_err}")

    # ------------------------------------------------------------------
    # 2. Collect every (url_idx, source_path) pair across all URLs.
    # ------------------------------------------------------------------
    work_items: List[Tuple[int, _UrlOutcome, Optional[Path]]] = []
    for i, o in enumerate(outcomes):
        if o.error is not None:
            continue
        if o.sources:
            for src in o.sources:
                work_items.append((i, o, src))
        elif o.plain_rows:
            work_items.append((i, o, None))  # plain text URL

    n_sets = len(work_items)
    if n_sets == 0:
        status("⚙ Processing... (no cookie files found)")
    elif n_urls == 1:
        status(f"🔄 Converting... ({n_sets} cookie set(s))")
    else:
        status(
            f"🔄 Converting... ({n_sets} cookie set(s) from {n_urls} link(s))"
        )

    # ------------------------------------------------------------------
    # 3. Convert each cookie set in parallel. Files are numbered in
    #    the order the work items were collected so the on-disk layout
    #    is deterministic.
    # ------------------------------------------------------------------
    cookie_files: List[Path] = []
    cookie_count = 0
    if work_items:
        per_set_results: List[Tuple[Path, int]] = [None] * len(work_items)  # type: ignore[list-item]
        with ThreadPoolExecutor(max_workers=max(1, parse_workers)) as pool:
            futures = []
            for set_idx, (url_idx, outcome, src) in enumerate(work_items, start=1):
                if src is not None:
                    label = _label_for(src, outcome.extracted_root or src.parent)
                    fname = (
                        f"{set_idx:04d}_url{url_idx + 1:02d}_{label}.txt"
                    )
                else:
                    fname = f"{set_idx:04d}_url{url_idx + 1:02d}_cookies.txt"
                out_path = cookies_dir / fname

                if src is None:
                    # Plain rows we already parsed in the worker thread.
                    rows = outcome.plain_rows
                    futures.append(
                        (
                            set_idx - 1,
                            out_path,
                            pool.submit(write_netscape_file, out_path, rows),
                        )
                    )
                else:
                    futures.append(
                        (
                            set_idx - 1,
                            out_path,
                            pool.submit(_extract_set, src, out_path, keywords),
                        )
                    )

            for slot, out_path, fut in futures:
                n = fut.result()
                per_set_results[slot] = (out_path, n)

        for out_path, n in per_set_results:
            if n:
                cookie_files.append(out_path)
                cookie_count += n
            else:
                try:
                    out_path.unlink()
                except OSError:  # pragma: no cover
                    pass

    # ------------------------------------------------------------------
    # 4. Zip everything up — even an empty result so callers always have
    #    something to look at.
    # ------------------------------------------------------------------
    result = PipelineResult(
        zip_path=output_dir / "cookies_result.zip",
        output_dir=output_dir,
        cookie_files=cookie_files,
        cookie_count=cookie_count,
        bytes_read=total_bytes,
    )
    if cookie_files:
        status(
            f"⚙ Processing... (packaging {len(cookie_files)} file(s))"
        )
    _zip_results(result.zip_path, cookie_files, output_dir)
    return result
