"""Archive extraction helpers (zip / 7z / rar).

Encrypted archives are supported as long as the corresponding
unpacker is on ``$PATH`` (``7z``, ``7za``, ``7zz`` for zip/7z and
``unrar`` for rar archives).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import urlparse

log = logging.getLogger(__name__)

ARCHIVE_SUFFIXES: tuple[str, ...] = (".zip", ".7z", ".rar")

# All 7z-family binaries the bot will accept. ``7z`` is the canonical
# one on Linux nixpkgs; macOS Homebrew installs ``7zz``.
SEVENZIP_BINARIES: tuple[str, ...] = ("7z", "7za", "7zz")
UNRAR_BINARIES: tuple[str, ...] = ("unrar",)


class ArchiveError(RuntimeError):
    """Raised when archive extraction fails."""


def is_archive_url(url: str) -> bool:
    """Return ``True`` when the URL path ends in a known archive ext."""
    name = Path(urlparse(url).path).name.lower()
    return any(name.endswith(suf) for suf in ARCHIVE_SUFFIXES)


def archive_kind(path: Path) -> Optional[str]:
    """Return ``"zip"`` / ``"7z"`` / ``"rar"`` based on extension."""
    name = path.name.lower()
    for suf in ARCHIVE_SUFFIXES:
        if name.endswith(suf):
            return suf.lstrip(".")
    return None


def _which_first(candidates: Sequence[str]) -> Optional[str]:
    for c in candidates:
        path = shutil.which(c)
        if path:
            return path
    return None


def extract_archive(
    archive_path: Path,
    dest_dir: Path,
    *,
    password: Optional[str] = None,
    timeout: int = 1800,
) -> Path:
    """Extract ``archive_path`` into ``dest_dir`` (created if needed).

    Returns ``dest_dir``. Raises :class:`ArchiveError` on any failure.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    kind = archive_kind(archive_path)
    if kind is None:
        raise ArchiveError(f"unsupported archive type: {archive_path.name}")

    if kind in ("zip", "7z"):
        bin_path = _which_first(SEVENZIP_BINARIES)
        if bin_path is None:
            raise ArchiveError(
                "7z binary not found — install p7zip on your host."
            )
        cmd = [
            bin_path,
            "x",
            "-y",
            f"-o{dest_dir}",
            str(archive_path),
        ]
        if password is not None and password != "":
            cmd.insert(2, f"-p{password}")
        else:
            # Use a non-empty placeholder so 7z fails fast on encrypted
            # archives instead of hanging on the interactive prompt.
            cmd.insert(2, "-p-")
    elif kind == "rar":
        bin_path = _which_first(UNRAR_BINARIES)
        if bin_path is None:
            raise ArchiveError(
                "unrar binary not found — install unrar on your host."
            )
        cmd = [bin_path, "x", "-y"]
        if password is not None and password != "":
            cmd.append(f"-p{password}")
        else:
            cmd.append("-p-")
        cmd += [str(archive_path), str(dest_dir) + "/"]
    else:  # pragma: no cover — guarded by archive_kind() above
        raise ArchiveError(f"unsupported archive type: {kind}")

    log.info("extracting %s -> %s (kind=%s)", archive_path, dest_dir, kind)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ArchiveError(f"extraction timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise ArchiveError(f"extractor binary not found: {exc}") from exc

    if proc.returncode != 0:
        # 7z returns 2 for "fatal error" which is what we get on a bad
        # password; surface a friendly message either way.
        stderr = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = stderr[-1] if stderr else f"rc={proc.returncode}"
        raise ArchiveError(f"extraction failed: {tail}")

    return dest_dir
