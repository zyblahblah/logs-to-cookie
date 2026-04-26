"""Tests for `sort --per-source` (per-keyword, per-victim folder layout)."""

from __future__ import annotations

import argparse
from pathlib import Path
from textwrap import dedent

import pytest

from logs_to_cookie.cli import cmd_sort


def _make_victim(
    root: Path,
    name: str,
    *,
    creds: list,
    cookies: list,
) -> None:
    """Create one victim folder with Passwords.txt + Cookies/*.txt."""
    base = root / name
    base.mkdir(parents=True, exist_ok=True)
    if creds:
        body = "\n\n".join(
            f"URL: {url}\nUSER: {u}\nPASS: {p}" for url, u, p in creds
        )
        (base / "Passwords.txt").write_text(body + "\n", encoding="utf-8")
    if cookies:
        cdir = base / "Cookies"
        cdir.mkdir(parents=True, exist_ok=True)
        for i, (domain, ck_name, value) in enumerate(cookies):
            (cdir / f"{i}_{domain}.txt").write_text(
                "# Netscape HTTP Cookie File\n"
                f".{domain}\tTRUE\t/\tTRUE\t1900000000\t{ck_name}\t{value}\n",
                encoding="utf-8",
            )


@pytest.fixture()
def logs_dir(tmp_path: Path) -> Path:
    root = tmp_path / "logs"
    _make_victim(
        root,
        "ADMIN @v_d_e (1)",
        creds=[("https://www.netflix.com/", "alice", "pw_a")],
        cookies=[("netflix.com", "Sess1", "tok_a")],
    )
    _make_victim(
        root,
        "ADMIN @v_d_e (2)",
        creds=[("https://www.spotify.com/login", "bob", "pw_b")],
        cookies=[("spotify.com", "Sess2", "tok_b")],
    )
    _make_victim(
        root,
        "ADMIN @v_d_e (3)",
        creds=[("https://claude.ai/login", "carol", "pw_c")],
        cookies=[
            ("netflix.com", "Sess3", "tok_c"),
            ("claude.ai", "Sess3", "tok_c"),
        ],
    )
    _make_victim(
        root,
        "ADMIN @v_d_e (4)",
        creds=[("https://example.com/", "dave", "pw_d")],
        cookies=[("example.com", "Sess4", "tok_d")],
    )
    return root


def _args(**kw):
    ns = argparse.Namespace(
        input=None, output=None, keywords=None, per_source=True, password=[]
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_sort_per_source_layout(logs_dir: Path, tmp_path: Path):
    out = tmp_path / "sorted"
    rc = cmd_sort(
        _args(
            input=str(logs_dir),
            output=str(out),
            keywords="netflix,spotify,claude",
        )
    )
    assert rc == 0

    # Folder names get sanitized: @ and () become _.
    v1_dir = "ADMIN__v_d_e__1_"
    v3_dir = "ADMIN__v_d_e__3_"

    # netflix should match victims (1) and (3) via cookies (and (1) via creds).
    netflix_dirs = sorted(p.name for p in (out / "netflix").iterdir())
    assert netflix_dirs == [v1_dir, v3_dir], netflix_dirs

    # Each victim's folder should hold cookies.txt for that victim only.
    v1_cookies = (out / "netflix" / v1_dir / "cookies.txt").read_text(
        encoding="utf-8"
    )
    assert "tok_a" in v1_cookies
    assert "tok_c" not in v1_cookies

    v3_cookies = (out / "netflix" / v3_dir / "cookies.txt").read_text(
        encoding="utf-8"
    )
    assert "tok_c" in v3_cookies
    assert "tok_a" not in v3_cookies

    # Victim (1) had a netflix credential, so creds.txt should also exist.
    v1_creds = (out / "netflix" / v1_dir / "creds.txt").read_text(encoding="utf-8")
    assert "alice:pw_a" in v1_creds

    # claude bucket: only victim (3).
    claude_dirs = sorted(p.name for p in (out / "claude").iterdir())
    assert claude_dirs == [v3_dir]

    # Victim (4) (example.com) must NOT appear under any keyword bucket.
    for kw in ("netflix", "spotify", "claude"):
        if (out / kw).exists():
            for p in (out / kw).iterdir():
                assert "_4_" not in p.name


def test_sort_per_source_skips_unmatched_victims(logs_dir: Path, tmp_path: Path):
    out = tmp_path / "sorted2"
    rc = cmd_sort(
        _args(input=str(logs_dir), output=str(out), keywords="claude")
    )
    assert rc == 0
    # Only the claude bucket exists, with one victim.
    assert (out / "claude").is_dir()
    dirs = sorted(p.name for p in (out / "claude").iterdir())
    assert dirs == ["ADMIN__v_d_e__3_"]


def test_sort_default_layout_unchanged(logs_dir: Path, tmp_path: Path):
    out = tmp_path / "sorted_legacy"
    rc = cmd_sort(
        _args(
            input=str(logs_dir),
            output=str(out),
            keywords="netflix",
            per_source=False,
        )
    )
    assert rc == 0
    assert (out / "netflix.ulp.txt").is_file()
    assert (out / "netflix.cookies.txt").is_file()
    text = (out / "netflix.cookies.txt").read_text(encoding="utf-8")
    # Legacy mode merges cookies from multiple victims.
    assert "tok_a" in text and "tok_c" in text
