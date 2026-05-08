"""Archive extraction helpers (zip / 7z / rar).

All three archive types route through ``7z`` (from ``p7zip-full``) —
recent versions of p7zip support both RAR4 and RAR5 archives in
addition to zip and 7z, including encryption. The proprietary ``unrar``
binary is checked as a fallback for RAR archives when 7z reports
"Unsupported Method" (common for RAR5 archives produced by very recent
WinRAR builds that the bundled p7zip doesn't yet understand).

The bot accepts CDN URLs that don't carry an ``.zip``/``.7z``/``.rar``
suffix in their path (e.g. tokenised LinkForge / file-host URLs), so
:func:`detect_archive_kind` sniffs the file's magic bytes before
falling back to the suffix.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence, Tuple
from urllib.parse import urlparse

log = logging.getLogger(__name__)

ARCHIVE_SUFFIXES: tuple[str, ...] = (".zip", ".7z", ".rar")

# Magic byte signatures, in (kind, prefix) tuples. ZIP has three valid
# leading records (local file header, end-of-central-directory record,
# data descriptor) so we list all three. RAR4 and RAR5 share the
# leading ``Rar!\x1a\x07`` so a single 7-byte prefix covers both.
MAGIC_SIGNATURES: tuple[tuple[str, bytes], ...] = (
    ("zip", b"PK\x03\x04"),
    ("zip", b"PK\x05\x06"),
    ("zip", b"PK\x07\x08"),
    ("7z", b"7z\xbc\xaf\x27\x1c"),
    ("rar", b"Rar!\x1a\x07"),
)

# All 7z-family binaries the bot will accept. ``7z`` is the canonical
# one on Linux nixpkgs; macOS Homebrew installs ``7zz``; older
# installs only have ``7za``. We try them in this order — newer is
# better for "Unsupported Method" failures because the codec set
# tends to grow over time.
SEVENZIP_BINARIES: tuple[str, ...] = ("7zz", "7z", "7za")
# RAR-specific extractors. ``unrar`` is the proprietary binary;
# ``unrar-free`` is the GPLv2 fork shipped in Debian/Ubuntu main
# (handles RAR4 / older RAR5 archives). ``bsdtar`` (libarchive) can
# also crack RAR4 and RAR5 in many cases and is widely preinstalled.
UNRAR_BINARIES: tuple[str, ...] = ("unrar", "unrar-free", "bsdtar")

# Substrings that mean "this binary refused / can't handle this
# archive — try the next candidate". We match on lowercase stderr so
# we don't accidentally swallow real failures (e.g. wrong password).
#
# p7zip phrasing varies between 7-Zip versions and the GNU port:
#   * 7-Zip / 7zz → "Cannot open the file as archive"
#   * p7zip 16.02 (Ubuntu/Debian default) → "Can not open the file as
#     archive" (note the SPACE in "Can not") and a footer line
#     "Can't open as archive: 1". Both phrasings need to match here
#     or the bot ends up reporting the useless "Compressed: 0"
#     summary line as the failure reason.
_RETRYABLE_ERROR_FRAGMENTS: tuple[str, ...] = (
    "unsupported method",
    "unsupported compression method",
    "unsupported feature",
    "cannot open the file as archive",
    "can not open the file as archive",
    "cannot open as archive",
    "can not open as archive",
    "can't open as archive",
    "is not archive",
)

# Substrings that mean "this archive needs a (different) password".
# These are NOT retryable — trying the next extractor will only
# reproduce the same failure. We surface a friendly "wrong / missing
# password" message instead.
_PASSWORD_ERROR_FRAGMENTS: tuple[str, ...] = (
    "wrong password",
    "can not open encrypted archive",
    "cannot open encrypted archive",
    "the password is incorrect",
    "encrypted file. corrupt file or wrong password",
    "headers error",
)

# Footer lines emitted by p7zip / 7-Zip after the real error, e.g.
#   ERROR: foo.rar
#   Can not open the file as archive
#
#   Can't open as archive: 1
#   Files: 0
#   Size:       0
#   Compressed: 0
#
# We strip these out of "last useful line" detection so the user
# sees the real failure ("Can not open the file as archive") instead
# of a meaningless "Compressed: 0".
_NOISE_LINE_PREFIXES: tuple[str, ...] = (
    "files:",
    "size:",
    "compressed:",
    "can't open as archive:",
    "archives with errors:",
    "sub items errors:",
    "errors:",
    "warnings:",
    "open errors:",
)


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


def _read_magic_header(path: Path, n: int = 8) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except OSError:
        return b""


def detect_archive_kind(path: Path) -> Optional[str]:
    """Return the archive kind for ``path`` (``"zip"``/``"7z"``/``"rar"``).

    Magic bytes are checked first so the bot accepts archives served
    behind opaque CDN URLs (no ``.zip``/``.7z``/``.rar`` suffix in the
    URL path). The on-disk filename suffix is consulted as a fallback
    only when the file is empty / unreadable / not-yet-downloaded.
    """
    header = _read_magic_header(path)
    for kind, prefix in MAGIC_SIGNATURES:
        if header.startswith(prefix):
            return kind
    return archive_kind(path)


def _all_on_path(candidates: Sequence[str]) -> List[str]:
    """Return every candidate that resolves to a real binary, in order."""
    out: List[str] = []
    seen: set[str] = set()
    for c in candidates:
        path = shutil.which(c)
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _dest_has_files(dest_dir: Path) -> bool:
    """Return True iff ``dest_dir`` contains at least one regular file
    (recursively). ``unrar-free`` and a few other extractors return
    rc=0 even when they fail to actually extract anything — checking
    the filesystem is the only reliable signal.
    """
    try:
        for entry in dest_dir.rglob("*"):
            if entry.is_file():
                return True
    except OSError:
        pass
    return False


def _which_first(candidates: Sequence[str]) -> Optional[str]:
    found = _all_on_path(candidates)
    return found[0] if found else None


def _build_7z_cmd(
    bin_path: str,
    archive_path: Path,
    dest_dir: Path,
    password: Optional[str],
) -> List[str]:
    pwd_arg = f"-p{password}" if password else "-p-"
    return [
        bin_path,
        "x",
        "-y",
        pwd_arg,
        f"-o{dest_dir}",
        "--",
        str(archive_path),
    ]


def _build_unrar_cmd(
    bin_path: str,
    archive_path: Path,
    dest_dir: Path,
    password: Optional[str],
) -> List[str]:
    name = Path(bin_path).name.lower()
    if name == "bsdtar":
        # libarchive's tar can extract RAR4 / RAR5 with --passphrase.
        cmd = [bin_path, "-x", "-f", str(archive_path), "-C", str(dest_dir)]
        if password:
            cmd += ["--passphrase", password]
        return cmd
    if name == "unrar-free":
        # unrar-free uses a GNU-ish CLI: ``-x`` to extract.
        cmd = [bin_path, "-x"]
        if password:
            cmd += ["-p", password]
        cmd += [str(archive_path), str(dest_dir) + "/"]
        return cmd
    # Proprietary unrar (and most CLI clones).
    cmd = [bin_path, "x", "-y", "-o+"]
    cmd.append(f"-p{password}" if password else "-p-")
    cmd += [str(archive_path), str(dest_dir) + "/"]
    return cmd


def _run(cmd: List[str], timeout: int) -> Tuple[int, str]:
    """Run a subprocess and return (returncode, last_useful_line)."""
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

    stderr_lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    tail = stderr_lines[-1] if stderr_lines else f"rc={proc.returncode}"
    return proc.returncode, tail


def _stderr_blob(cmd: List[str], timeout: int) -> Tuple[int, str]:
    """Same as ``_run`` but returns the full combined stderr+stdout.

    We need the full text (not just the last line) when classifying a
    failure as "retryable" because the meaningful "Unsupported Method"
    line is sometimes followed by a generic "ERROR: ..." footer.
    """
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

    blob = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
    return proc.returncode, blob


def _is_retryable(stderr_blob: str) -> bool:
    low = stderr_blob.lower()
    if any(frag in low for frag in _PASSWORD_ERROR_FRAGMENTS):
        # A wrong-password failure is never "retryable" — the next
        # extractor will hit the same wall. Surface it as-is.
        return False
    return any(frag in low for frag in _RETRYABLE_ERROR_FRAGMENTS)


def _is_password_error(stderr_blob: str) -> bool:
    low = stderr_blob.lower()
    return any(frag in low for frag in _PASSWORD_ERROR_FRAGMENTS)


def _last_useful_line(blob: str) -> str:
    """Return the last non-empty diagnostic line from a 7z/unrar blob.

    7z and p7zip both end their output with a summary footer (``Files:
    0``, ``Size: 0``, ``Compressed: 0``, ``Can't open as archive: 1``,
    ``Archives with Errors: 1``, etc.) that says nothing about *why*
    extraction failed. We skip those noise lines and return the last
    line that actually tells the user something — typically the
    ``Can not open the file as archive`` / ``Wrong password?`` line
    immediately above the footer.
    """
    candidates: List[str] = []
    for line in blob.splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if any(low.startswith(p) for p in _NOISE_LINE_PREFIXES):
            continue
        # Bare ``ERROR: <archive>`` / ``ERROR:`` lines on their own
        # don't help — the line *underneath* them carries the real
        # message. Skip them unless they contain an explanation.
        if low == "errors" or low == "error:" or low == "warnings":
            continue
        if low.startswith("error:") and ":" in s and len(s.split(":", 1)[1].strip()) <= 1:
            continue
        candidates.append(s)
    if candidates:
        return candidates[-1]
    # Fall back to the previous behaviour if every line is noise.
    for line in reversed(blob.splitlines()):
        s = line.strip()
        if s:
            return s
    return "extraction failed"


def extract_archive(
    archive_path: Path,
    dest_dir: Path,
    *,
    password: Optional[str] = None,
    timeout: int = 1800,
) -> Path:
    """Extract ``archive_path`` into ``dest_dir`` (created if needed).

    Returns ``dest_dir``. Raises :class:`ArchiveError` on any failure.
    The archive type is detected from magic bytes first and the file
    extension second — the bot routinely sees archives behind tokenised
    CDN URLs that have no extension at all.

    On a "retryable" failure (e.g. ``Unsupported Method`` from p7zip
    on a fresh RAR5 archive) we walk a chain of fallback extractors:
    every 7z-family binary on PATH first, then ``unrar`` for RAR
    inputs. Only when every candidate has refused do we give up with
    a single, useful error message that names the kind of archive and
    suggests installing a newer extractor.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    kind = detect_archive_kind(archive_path)
    if kind is None:
        raise ArchiveError(
            f"unsupported archive type: {archive_path.name} "
            "(magic bytes don't match zip/7z/rar)"
        )

    sevenzips = _all_on_path(SEVENZIP_BINARIES)
    unrars = _all_on_path(UNRAR_BINARIES) if kind == "rar" else []

    if not sevenzips and not unrars:
        raise ArchiveError(
            "no archive extractor on PATH — install p7zip-full "
            "(handles zip, 7z, rar) and optionally unrar for the "
            "newest RAR5 archives."
        )

    candidates: List[Tuple[str, List[str]]] = []
    for bp in sevenzips:
        candidates.append(
            (bp, _build_7z_cmd(bp, archive_path, dest_dir, password))
        )
    for bp in unrars:
        candidates.append(
            (bp, _build_unrar_cmd(bp, archive_path, dest_dir, password))
        )

    log.info(
        "extracting %s -> %s (kind=%s, candidates=%d)",
        archive_path,
        dest_dir,
        kind,
        len(candidates),
    )

    last_blob = ""
    last_bin = ""
    for bin_path, cmd in candidates:
        rc, blob = _stderr_blob(cmd, timeout)
        if rc == 0 and _dest_has_files(dest_dir):
            log.info(
                "extraction OK with %s",
                Path(bin_path).name,
            )
            return dest_dir
        if rc == 0:
            # Some extractors (notably ``unrar-free`` 0.0.2) return
            # rc=0 even when they fail to actually extract anything
            # — a corrupt RAR or one with an unsupported codec just
            # produces an empty destination directory. Treat that as
            # a retryable failure so the next extractor still gets a
            # chance.
            blob = (
                f"{blob}\nextractor {Path(bin_path).name} reported "
                "success but produced no files (treating as is not archive)"
            )
            log.info(
                "extractor %s rc=0 but dest is empty — "
                "treating as retryable failure",
                Path(bin_path).name,
            )
            last_blob = blob
            last_bin = bin_path
            continue
        last_blob = blob
        last_bin = bin_path
        retryable = _is_retryable(blob)
        log.info(
            "extractor %s rc=%d, retryable=%s",
            Path(bin_path).name,
            rc,
            retryable,
        )
        if _is_password_error(blob):
            # Wrong / missing password is decisive: no other extractor
            # will get any further. Surface a friendly message that
            # the bot's user-facing layer can match on.
            if password:
                raise ArchiveError(
                    "extraction failed: wrong password for archive"
                )
            raise ArchiveError(
                "extraction failed: archive is encrypted but no "
                "password was supplied"
            )
        if not retryable:
            # Real failure (corrupt archive, etc.) — no point trying
            # every other extractor.
            tail = _last_useful_line(blob)
            raise ArchiveError(f"extraction failed: {tail}")

    # Every candidate gave up with a "retryable" complaint. Surface
    # something actionable.
    tail = _last_useful_line(last_blob)
    extra = ""
    if kind == "rar" and not unrars:
        extra = (
            " — install the `p7zip-rar` codec or the `unrar` / "
            "`unrar-free` binary; p7zip-full alone can't read .rar "
            "on Debian/Ubuntu."
        )
    elif kind == "rar":
        extra = (
            " — try a newer extractor (proprietary `unrar`, the "
            "`p7zip-rar` codec, or the official 7-Zip `7zz` build); "
            "the installed extractor doesn't understand this RAR."
        )
    elif kind == "7z":
        extra = (
            " — your p7zip build may be too old for this 7z codec; "
            "install a newer p7zip-full or the standalone `7zz`."
        )
    raise ArchiveError(
        f"extraction failed via {Path(last_bin).name}: {tail}{extra}"
    )
