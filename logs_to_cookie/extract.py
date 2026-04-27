"""Archive extraction helpers.

Stealer log dumps are usually delivered as password-protected archives
(typically ``.zip``, sometimes ``.rar`` or ``.7z``). This module extracts
them recursively into a working directory before the rest of the tool
walks them.

* ``.zip`` is handled natively via the stdlib ``zipfile`` module.
* ``.rar`` falls back to the ``unrar`` or ``7z``/``7za`` binaries.
* ``.7z`` requires the ``7z``/``7za`` binary.

Multiple passwords can be supplied; each is tried in turn until one
succeeds. Archives that cannot be opened (wrong password, missing
binary, corruption) are reported but do not abort the run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

ARCHIVE_EXTS = {".zip", ".rar", ".7z"}


def is_archive(path: Path) -> bool:
    return path.suffix.lower() in ARCHIVE_EXTS


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _try_zip(archive: Path, dest: Path, passwords: List[Optional[str]]) -> bool:
    try:
        zf = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError):
        return False
    try:
        for pwd in passwords:
            pwd_b = pwd.encode("utf-8") if pwd else None
            try:
                zf.extractall(dest, pwd=pwd_b)
                return True
            except RuntimeError:
                # Wrong password.
                continue
            except zipfile.BadZipFile:
                # BUG FIX: Some Python builds raise BadZipFile for a wrong
                # password instead of RuntimeError — continue to next password
                # rather than giving up on all remaining passwords.
                continue
    finally:
        zf.close()
    return False


def _seven_zip_binary() -> Optional[str]:
    for cand in ("7z", "7za", "7zz"):
        if _have(cand):
            return cand
    return None


def _try_7z(archive: Path, dest: Path, passwords: List[Optional[str]]) -> bool:
    binary = _seven_zip_binary()
    if not binary:
        return False
    for pwd in passwords:
        cmd = [binary, "x", "-y", f"-o{dest}"]
        cmd.append(f"-p{pwd}" if pwd is not None else "-p")
        cmd.append(str(archive))
        try:
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=600,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return True
    return False


def _try_unrar(archive: Path, dest: Path, passwords: List[Optional[str]]) -> bool:
    if not _have("unrar"):
        return False
    for pwd in passwords:
        cmd = ["unrar", "x", "-y", "-inul"]
        cmd.append(f"-p{pwd}" if pwd is not None else "-p-")
        cmd.append(str(archive))
        cmd.append(str(dest) + os.sep)
        try:
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=600,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return True
    return False


def extract_archive(
    archive: Path, dest: Path, passwords: List[Optional[str]]
) -> bool:
    """Extract ``archive`` into ``dest`` using each password in turn.

    Returns ``True`` if extraction succeeded, ``False`` otherwise.
    """
    dest.mkdir(parents=True, exist_ok=True)
    ext = archive.suffix.lower()
    if ext == ".zip":
        if _try_zip(archive, dest, passwords):
            return True
        return _try_7z(archive, dest, passwords)
    if ext == ".rar":
        if _try_unrar(archive, dest, passwords):
            return True
        return _try_7z(archive, dest, passwords)
    if ext == ".7z":
        return _try_7z(archive, dest, passwords)
    return False


def expand_input(
    inputs: Iterable[Path],
    passwords: Iterable[str],
    workdir: Path,
    recurse: bool = True,
) -> Tuple[List[Path], List[Path]]:
    """Resolve a mix of directories and archive files into a list of roots.

    Each archive found at the top level (or, when ``recurse`` is true,
    nested inside an already-extracted archive) is unpacked into a unique
    subdirectory of ``workdir`` and added to the returned roots.

    Returns ``(roots, failures)``.
    """
    pwd_list: List[Optional[str]] = [None]
    for p in passwords:
        if p and p not in pwd_list:
            pwd_list.append(p)

    workdir.mkdir(parents=True, exist_ok=True)
    roots: List[Path] = []
    failures: List[Path] = []
    queue: List[Path] = []
    seen: set = set()

    for inp in inputs:
        inp = inp.resolve()
        if inp.is_file() and is_archive(inp):
            queue.append(inp)
        else:
            roots.append(inp)

    counter = 0
    while queue:
        archive = queue.pop()
        if archive in seen:
            continue
        seen.add(archive)
        counter += 1
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in archive.stem)
        target = workdir / f"x{counter:03d}_{safe or 'arch'}"
        if extract_archive(archive, target, pwd_list):
            roots.append(target)
            if recurse:
                for nested in target.rglob("*"):
                    if nested.is_file() and is_archive(nested) and nested not in seen:
                        queue.append(nested)
        else:
            failures.append(archive)

    return roots, failures
