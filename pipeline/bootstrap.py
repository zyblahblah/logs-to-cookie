"""Runtime bootstrap of a working 7-Zip ``7zz`` binary.

The bot needs a 7z-family extractor that supports RAR3+ (and other
modern codecs). On Debian/Ubuntu hosts — including Railway's Railpack
default image — ``p7zip-full`` is 16.02 from 2016 and can't read every
RAR5 codec that recent WinRAR builds emit. The bundled GPL ``unrar-free``
0.0.2 only handles RAR 2.0. Build-time bundling of a newer ``7zz``
through ``railpack.json`` / ``nixpacks.toml`` has been historically
fragile — see PRs #34/#35/#36.

This module sidesteps that problem entirely by downloading the official
upstream 7-Zip ``7zz`` binary at runtime on first use, caching it under
a writable directory, and exposing the path so the archive extractor
chain can prefer it over every system extractor.

Network and disk failures are logged but **never raised** — if the
bootstrap fails the bot falls back to whatever extractors are on
``PATH``.

The tarball is ~1.5 MB. Bootstrap is idempotent (only the first caller
in a process actually downloads) and thread-safe.

Opt-out: set ``LOGS_TO_COOKIE_DISABLE_7ZZ_BOOTSTRAP=1`` to skip the
runtime download (e.g., for hosts that block egress to www.7-zip.org).
"""

from __future__ import annotations

import io
import logging
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import threading
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Upstream 7-Zip ``7zz`` tarballs, keyed by ``platform.machine()``
# (lowercased). Each tarball is ~1.5 MB and contains ``7zz`` /
# ``7zzs`` plus a few licence / man files. We only extract ``7zz``.
#
# Version 26.01 (2026-04-27) is the current stable release. It
# bundles RAR1 / RAR3 / RAR4 / RAR5 read codecs in-tree.
_TARBALL_URLS: dict[str, str] = {
    "x86_64": "https://www.7-zip.org/a/7z2601-linux-x64.tar.xz",
    "amd64": "https://www.7-zip.org/a/7z2601-linux-x64.tar.xz",
    "i686": "https://www.7-zip.org/a/7z2601-linux-x86.tar.xz",
    "i386": "https://www.7-zip.org/a/7z2601-linux-x86.tar.xz",
    "x86": "https://www.7-zip.org/a/7z2601-linux-x86.tar.xz",
    "aarch64": "https://www.7-zip.org/a/7z2601-linux-arm64.tar.xz",
    "arm64": "https://www.7-zip.org/a/7z2601-linux-arm64.tar.xz",
    "armv7l": "https://www.7-zip.org/a/7z2601-linux-arm.tar.xz",
    "armv6l": "https://www.7-zip.org/a/7z2601-linux-arm.tar.xz",
    "arm": "https://www.7-zip.org/a/7z2601-linux-arm.tar.xz",
}

_DOWNLOAD_TIMEOUT_SECONDS = 60
_VERIFY_TIMEOUT_SECONDS = 10
_DISABLE_ENV_VAR = "LOGS_TO_COOKIE_DISABLE_7ZZ_BOOTSTRAP"
_OVERRIDE_URL_ENV_VAR = "LOGS_TO_COOKIE_7ZZ_TARBALL_URL"
_CACHE_DIR_ENV_VAR = "LOGS_TO_COOKIE_7ZZ_CACHE_DIR"

_BOOTSTRAP_LOCK = threading.Lock()
_CACHED_PATH: Optional[str] = None
_BOOTSTRAP_TRIED = False


def _candidate_cache_dirs() -> list[Path]:
    """Where we'll try to drop the bundled 7zz, in preference order.

    Operators can pin the location with ``LOGS_TO_COOKIE_7ZZ_CACHE_DIR``;
    that value wins outright (no fallback).
    """
    override = os.environ.get(_CACHE_DIR_ENV_VAR, "").strip()
    if override:
        return [Path(override)]

    out: list[Path] = []
    home = os.environ.get("HOME", "").strip()
    if home:
        out.append(Path(home) / ".cache" / "logs-to-cookie" / "bin")
    # Railway / Heroku-style: /app is writable and persists for the
    # life of the deploy. Try it before /tmp so a single warm worker
    # avoids re-downloading on every cold start.
    if Path("/app").is_dir():
        out.append(Path("/app") / ".cache" / "logs-to-cookie" / "bin")
    out.append(Path(tempfile.gettempdir()) / "logs-to-cookie-bin")
    out.append(Path("/tmp") / "logs-to-cookie-bin")

    seen: set[str] = set()
    deduped: list[Path] = []
    for d in out:
        key = str(d)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(d)
    return deduped


def _ensure_writable_cache_dir() -> Optional[Path]:
    for d in _candidate_cache_dirs():
        try:
            d.mkdir(parents=True, exist_ok=True)
            t = d / ".write-test"
            t.write_bytes(b"")
            t.unlink()
            return d
        except OSError:
            continue
    return None


def _tarball_url_for_platform() -> Optional[str]:
    override = os.environ.get(_OVERRIDE_URL_ENV_VAR, "").strip()
    if override:
        return override
    if platform.system().lower() != "linux":
        # We only ship Linux binaries for now. macOS / Windows hosts
        # are expected to install ``7zz`` via Homebrew / winget / etc.
        return None
    machine = platform.machine().lower()
    return _TARBALL_URLS.get(machine)


def _verify_7zz(binary: Path) -> bool:
    """Run ``binary`` and check it prints a 7-Zip banner.

    Invoked with no args, ``7zz`` exits non-zero but prints its banner
    on stdout. We only care that the banner appears — that's enough
    to prove the file is the genuine binary and not a partial
    download / wrong-arch / corrupted blob.
    """
    try:
        proc = subprocess.run(
            [str(binary)],
            capture_output=True,
            text=False,
            timeout=_VERIFY_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    blob = (proc.stdout or b"") + (proc.stderr or b"")
    return b"7-Zip" in blob


def _download_and_extract_7zz(url: str, dest: Path) -> bool:
    """Download the tar.xz at ``url`` and write its ``7zz`` to ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        log.info("downloading bundled 7zz from %s", url)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "logs-to-cookie-bot/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as resp:
            data = resp.read()
        log.info("downloaded %d bytes from %s", len(data), url)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as tf:
            try:
                member = tf.getmember("7zz")
            except KeyError:
                log.warning(
                    "tarball %s has no '7zz' entry — refusing to use it",
                    url,
                )
                return False
            src = tf.extractfile(member)
            if src is None:
                return False
            tmp = dest.with_suffix(dest.suffix + ".part")
            try:
                with open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.chmod(tmp, 0o755)
                os.replace(tmp, dest)
            finally:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
        return True
    except Exception:  # noqa: BLE001 — best-effort bootstrap
        log.warning(
            "failed to bootstrap bundled 7zz from %s (%s)",
            url,
            "see traceback",
            exc_info=True,
        )
        return False


def _disabled_by_env() -> bool:
    val = os.environ.get(_DISABLE_ENV_VAR, "")
    return val.strip().lower() in ("1", "true", "yes", "on")


def ensure_bundled_7zz() -> Optional[str]:
    """Return the path to a working bundled ``7zz`` binary, or ``None``.

    Idempotent across threads and across calls. The first call in a
    process performs the download + verification; subsequent calls
    return the cached result (or ``None`` if the first attempt failed).
    Restart the bot to retry after a failed bootstrap.

    Returns ``None`` when:
      * the platform isn't one we ship a binary for (non-Linux, or an
        architecture we don't have a tarball URL for);
      * the bootstrap is disabled via ``LOGS_TO_COOKIE_DISABLE_7ZZ_BOOTSTRAP``;
      * the download / extraction / verification failed.

    Callers MUST handle ``None`` and fall back to whatever extractors
    they find on ``PATH``.
    """
    global _CACHED_PATH, _BOOTSTRAP_TRIED
    with _BOOTSTRAP_LOCK:
        if _CACHED_PATH is not None:
            return _CACHED_PATH
        if _BOOTSTRAP_TRIED:
            return None
        _BOOTSTRAP_TRIED = True

        if _disabled_by_env():
            log.info(
                "bundled 7zz bootstrap disabled via %s env var",
                _DISABLE_ENV_VAR,
            )
            return None

        url = _tarball_url_for_platform()
        if url is None:
            log.info(
                "no upstream 7zz tarball known for platform "
                "system=%s machine=%s — skipping bundled 7zz bootstrap",
                platform.system(),
                platform.machine(),
            )
            return None

        cache = _ensure_writable_cache_dir()
        if cache is None:
            log.warning(
                "no writable cache dir for bundled 7zz "
                "(checked %s) — skipping bootstrap",
                ", ".join(str(d) for d in _candidate_cache_dirs()),
            )
            return None

        dest = cache / "7zz"
        if dest.is_file() and _verify_7zz(dest):
            log.info("using already-cached bundled 7zz at %s", dest)
            _CACHED_PATH = str(dest)
            return _CACHED_PATH

        ok = _download_and_extract_7zz(url, dest)
        if not ok:
            return None
        if not _verify_7zz(dest):
            log.warning(
                "bundled 7zz at %s failed verification — discarding",
                dest,
            )
            try:
                dest.unlink()
            except OSError:
                pass
            return None

        log.info("bundled 7zz ready at %s", dest)
        _CACHED_PATH = str(dest)
        return _CACHED_PATH


def reset_for_tests() -> None:
    """Reset cached state. Tests only — do not use in production."""
    global _CACHED_PATH, _BOOTSTRAP_TRIED
    with _BOOTSTRAP_LOCK:
        _CACHED_PATH = None
        _BOOTSTRAP_TRIED = False


__all__ = ["ensure_bundled_7zz", "reset_for_tests"]
