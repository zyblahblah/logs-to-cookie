from __future__ import annotations

from urllib.parse import urlparse


def domain_of(url: str) -> str:
    """Return the lowercase hostname of a URL, or empty string."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        host = ""
    return host.lower()


def matches_any(haystack: str, needles) -> bool:
    h = haystack.lower()
    return any(n.lower() in h for n in needles)
