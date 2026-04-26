"""Tests for `sort --per-source` (flat per-victim layout)."""

from __future__ import annotations

import argparse
from pathlib import Path

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
        "victim_one",
        creds=[("https://www.netflix.com/", "alice", "pw_a")],
        cookies=[("netflix.com", "Sess1", "tok_a")],
    )
    _make_victim(
        root,
        "victim_two",
        creds=[("https://www.spotify.com/login", "bob", "pw_b")],
        cookies=[("spotify.com", "Sess2", "tok_b")],
    )
    _make_victim(
        root,
        "victim_three",
        creds=[("https://claude.ai/login", "carol", "pw_c")],
        cookies=[
            ("netflix.com", "Sess3", "tok_c"),
            ("claude.ai", "Sess3", "tok_c"),
        ],
    )
    _make_victim(
        root,
        "victim_four",
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


def test_flat_per_victim_layout(logs_dir: Path, tmp_path: Path):
    """Each victim with any keyword hit gets a flat folder under <output>/."""
    out = tmp_path / "sorted"
    rc = cmd_sort(
        _args(
            input=str(logs_dir),
            output=str(out),
            keywords="netflix,claude",
        )
    )
    assert rc == 0

    # Output should be FLAT: <out>/<victim>/cookies.txt — no keyword sub-tree.
    assert not (out / "netflix").exists()
    assert not (out / "claude").exists()

    dirs = sorted(p.name for p in out.iterdir() if p.is_dir())
    # victim_one (netflix), victim_three (netflix + claude). NOT victim_two/four.
    assert dirs == ["victim_one", "victim_three"], dirs

    # victim_one carries netflix cookies + creds.
    v1_cookies = (out / "victim_one" / "cookies.txt").read_text(encoding="utf-8")
    v1_creds = (out / "victim_one" / "creds.txt").read_text(encoding="utf-8")
    assert "tok_a" in v1_cookies and "tok_c" not in v1_cookies
    assert "alice:pw_a" in v1_creds

    # victim_three carries cookies for BOTH matching keywords in one cookies.txt.
    v3_cookies = (out / "victim_three" / "cookies.txt").read_text(encoding="utf-8")
    assert "tok_c" in v3_cookies
    # Both netflix.com and claude.ai cookies live in the same file.
    assert ".netflix.com" in v3_cookies
    assert ".claude.ai" in v3_cookies

    # victim_three had a claude credential, so creds.txt also exists.
    v3_creds = (out / "victim_three" / "creds.txt").read_text(encoding="utf-8")
    assert "carol:pw_c" in v3_creds


def test_flat_skips_victims_without_match(logs_dir: Path, tmp_path: Path):
    out = tmp_path / "sorted2"
    rc = cmd_sort(
        _args(input=str(logs_dir), output=str(out), keywords="claude")
    )
    assert rc == 0
    dirs = sorted(p.name for p in out.iterdir() if p.is_dir())
    assert dirs == ["victim_three"]
    # Only the matching cookies are written for that victim.
    text = (out / "victim_three" / "cookies.txt").read_text(encoding="utf-8")
    assert ".claude.ai" in text
    # netflix.com cookie shouldn't be there since it wasn't a claude match.
    assert ".netflix.com" not in text


def test_legacy_merged_via_no_per_source(logs_dir: Path, tmp_path: Path):
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
    # Legacy mode merges netflix cookies from victim_one + victim_three.
    text = (out / "netflix.cookies.txt").read_text(encoding="utf-8")
    assert "tok_a" in text and "tok_c" in text


def test_argparse_per_source_defaults_true():
    """Both `sort` and `cookies` must default to per-source organization."""
    from logs_to_cookie.cli import build_parser

    p = build_parser()
    sort_ns = p.parse_args(
        ["sort", "/tmp/x", "-o", "/tmp/y", "--keywords", "netflix"]
    )
    cookies_ns = p.parse_args(["cookies", "/tmp/x", "-o", "/tmp/y"])
    assert sort_ns.per_source is True
    assert cookies_ns.per_source is True
    sort_off = p.parse_args(
        [
            "sort",
            "/tmp/x",
            "-o",
            "/tmp/y",
            "--keywords",
            "netflix",
            "--no-per-source",
        ]
    )
    assert sort_off.per_source is False
