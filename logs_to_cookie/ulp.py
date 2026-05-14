"""Parse credential dumps from stealer logs into URL:USER:PASS triples."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Iterator, Tuple

# Common file-name fragments that mark password dumps.
PASSWORD_NAME_HINTS = (
    "password",
    "passwords",
    "all passwords",
    "all_passwords",
    "passwordsall",
    "logins",
    "credentials",
)

URL_RE = re.compile(
    r"^(?:URL|HOST(?:NAME)?|SITE|SOFT\s*URL|ORIGIN|URL\s*\(form\)|URL\s*Origin)\s*[:=]\s*(.+)$",
    re.IGNORECASE,
)
USER_RE = re.compile(
    r"^(?:USER(?:NAME)?|LOGIN|EMAIL|USER\s*ID|USER\s*NAME)\s*[:=]\s*(.+)$",
    re.IGNORECASE,
)
PASS_RE = re.compile(
    r"^(?:PASS(?:WORD)?|PWD)\s*[:=]\s*(.+)$",
    re.IGNORECASE,
)

# Inline ULP one-liner: url:user:password (password may contain colons).
INLINE_ULP_RE = re.compile(
    r"^(?P<url>(?:https?|android)://[^/:\s]+(?::\d+)?(?:/[^\s:]*)?):"
    r"(?P<user>[^:\s]+):"
    r"(?P<pwd>.+)$"
)


def _emit(cur: dict) -> Iterator[Tuple[str, str, str]]:
    if cur.get("url") and cur.get("user") is not None and cur.get("pwd") is not None:
        yield cur["url"], cur["user"], cur["pwd"]


def parse_credentials_text(text: str) -> Iterator[Tuple[str, str, str]]:
    """Yield (url, user, pwd) tuples parsed from a credential-dump text."""
    cur: dict = {"url": None, "user": None, "pwd": None}

    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line:
            yield from _emit(cur)
            cur = {"url": None, "user": None, "pwd": None}
            continue

        # Already-formatted ULP one-liner: scheme://host[:port]/path:user:pass
        m = INLINE_ULP_RE.match(line)
        if m:
            yield m.group("url"), m.group("user"), m.group("pwd")
            cur = {"url": None, "user": None, "pwd": None}
            continue

        m = URL_RE.match(line)
        if m:
            yield from _emit(cur)
            cur = {"url": m.group(1).strip(), "user": None, "pwd": None}
            continue
        m = USER_RE.match(line)
        if m:
            cur["user"] = m.group(1).strip()
            continue
        m = PASS_RE.match(line)
        if m:
            cur["pwd"] = m.group(1).strip()
            # If we already have url+user, emit immediately so trailing
            # metadata lines (e.g. "Application: ...") don't reset state.
            if cur["url"] and cur["user"] is not None:
                yield cur["url"], cur["user"], cur["pwd"]
                cur = {"url": None, "user": None, "pwd": None}
            continue
        # Unknown lines (e.g. "Application: ...", separators) are ignored.

    yield from _emit(cur)


def is_password_file(path: Path) -> bool:
    name = path.name.lower()
    if not name.endswith((".txt", ".log")):
        return False
    return any(hint in name for hint in PASSWORD_NAME_HINTS)


def iter_password_files(root: Path) -> Iterator[Path]:
    """Walk directory tree efficiently without timeout limits."""
    if root.is_file():
        if is_password_file(root):
            yield root
        return
    
    # Use os.walk for faster traversal than rglob
    for dirpath, dirnames, filenames in os.walk(str(root)):
        # Skip hidden directories to speed up scanning
        dirnames[:] = [d for d in dirnames if not d.startswith('.')]
        
        for filename in filenames:
            filepath = Path(dirpath) / filename
            if is_password_file(filepath):
                yield filepath


def collect_credentials(root: Path) -> Iterator[Tuple[str, str, str, Path]]:
    """Walk ``root`` and yield (url, user, pwd, source_path) tuples."""
    for path in iter_password_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for url, user, pwd in parse_credentials_text(text):
            yield url, user, pwd, path


def dedupe(triples: Iterable[Tuple[str, str, str]]) -> Iterator[Tuple[str, str, str]]:
    seen = set()
    for t in triples:
        if t in seen:
            continue
        seen.add(t)
        yield t
