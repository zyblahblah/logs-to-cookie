"""Archive extraction with optional password.

Supports ``.zip`` (stdlib for plain + ZipCrypto, ``7z`` binary for
AES variants), ``.7z`` (``7z`` binary), and ``.rar`` (``unrar`` /
``7z`` binary).
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import List, Optional

ARCHIVE_SUFFIXES = (".zip", ".rar", ".7z")


def is_archive(path: Path) -> bool:
    return path.suffix.lower() in ARCHIVE_SUFFIXES


def _seven_zip() -> Optional[str]:
    return shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")


def _unrar() -> Optional[str]:
    return shutil.which("unrar")


def _try_zipfile(
    archive: Path, dest: Path, passwords: List[Optional[str]]
) -> bool:
    """Best-effort extract using stdlib ``zipfile`` (handles plain +
    ZipCrypto). Returns ``True`` on success.
    """
    try:
        with zipfile.ZipFile(archive) as zf:
            for pw in passwords:
                try:
                    zf.extractall(
                        dest,
                        pwd=pw.encode("utf-8") if pw else None,
                    )
                    return True
                except (RuntimeError, zipfile.BadZipFile):
                    continue
                except NotImplementedError:
                    return False
    except (zipfile.BadZipFile, OSError):
        return False
    return False


def _try_7z(
    archive: Path, dest: Path, passwords: List[Optional[str]]
) -> bool:
    binary = _seven_zip()
    if not binary:
        return False
    for pw in passwords:
        cmd = [binary, "x", str(archive), f"-o{dest}", "-y"]
        if pw:
            cmd.insert(2, f"-p{pw}")
        try:
            r = subprocess.run(
                cmd,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if r.returncode == 0:
            return True
    return False


def _try_unrar(
    archive: Path, dest: Path, passwords: List[Optional[str]]
) -> bool:
    binary = _unrar()
    if not binary:
        return False
    for pw in passwords:
        cmd = [binary, "x", "-y", "-idq"]
        if pw:
            cmd.append(f"-p{pw}")
        cmd += [str(archive), str(dest) + "/"]
        try:
            r = subprocess.run(
                cmd,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if r.returncode == 0:
            return True
    return False


def extract_archive(
    archive: Path,
    dest: Path,
    passwords: Optional[List[Optional[str]]] = None,
) -> bool:
    """Extract ``archive`` into ``dest`` trying every password in turn.

    ``passwords`` should normally start with ``None`` so unencrypted
    archives extract on the first attempt. Returns ``True`` if any
    password succeeded; ``False`` otherwise.
    """
    dest.mkdir(parents=True, exist_ok=True)
    pwds: List[Optional[str]] = list(passwords) if passwords else [None]
    if None not in pwds:
        pwds.insert(0, None)

    suffix = archive.suffix.lower()
    if suffix == ".zip":
        if _try_zipfile(archive, dest, pwds):
            return True
        return _try_7z(archive, dest, pwds)
    if suffix == ".rar":
        if _try_unrar(archive, dest, pwds):
            return True
        return _try_7z(archive, dest, pwds)
    if suffix == ".7z":
        return _try_7z(archive, dest, pwds)
    return False


__all__ = [
    "ARCHIVE_SUFFIXES",
    "is_archive",
    "extract_archive",
]
