"""Sort logs by domain keyword into per-keyword ULP + cookie files."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, Tuple

from .cookies import NETSCAPE_HEADER, collect_cookies, to_netscape_line
from .ulp import collect_credentials
from .utils import domain_of


def _safe_keyword(k: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in k.strip()) or "kw"


def sort_logs(
    root: Path,
    out_dir: Path,
    keywords: Iterable[str],
) -> Dict[str, Tuple[int, int]]:
    """For each keyword, write ``<keyword>.ulp.txt`` and ``<keyword>.cookies.txt``.

    A credential matches a keyword when the keyword appears in either the URL
    or the parsed hostname. A cookie matches when the keyword appears in the
    cookie domain.
    """
    kws = [k.strip() for k in keywords if k and k.strip()]
    if not kws:
        return {}
    out_dir.mkdir(parents=True, exist_ok=True)

    ulp_handles = {}
    cookie_handles = {}
    seen_ulp = {k: set() for k in kws}
    seen_cookie = {k: set() for k in kws}
    counts = {k: [0, 0] for k in kws}

    try:
        for k in kws:
            slug = _safe_keyword(k)
            ulp_handles[k] = open(out_dir / f"{slug}.ulp.txt", "w", encoding="utf-8")
            cookie_handles[k] = open(
                out_dir / f"{slug}.cookies.txt", "w", encoding="utf-8"
            )
            cookie_handles[k].write(NETSCAPE_HEADER)

        for url, user, pwd, _src in collect_credentials(root):
            host = domain_of(url)
            haystack = (url + " " + host).lower()
            line = f"{url}:{user}:{pwd}"
            for k in kws:
                if k.lower() in haystack:
                    if line in seen_ulp[k]:
                        continue
                    seen_ulp[k].add(line)
                    ulp_handles[k].write(line + "\n")
                    counts[k][0] += 1

        for cookie in collect_cookies(root):
            domain = (cookie.get("domain") or "").lower()
            if not domain:
                continue
            line = to_netscape_line(cookie)
            for k in kws:
                if k.lower() in domain:
                    if line in seen_cookie[k]:
                        continue
                    seen_cookie[k].add(line)
                    cookie_handles[k].write(line + "\n")
                    counts[k][1] += 1
    finally:
        for f in ulp_handles.values():
            f.close()
        for f in cookie_handles.values():
            f.close()

    return {k: (counts[k][0], counts[k][1]) for k in kws}
