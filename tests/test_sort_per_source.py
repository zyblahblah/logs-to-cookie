"""Tests for `sort --per-source` (per-victim folder, one Netscape file per source)."""

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
    """Create one victim folder with Passwords.txt + Cookies/*.txt.

    Each entry in ``cookies`` becomes its own file under ``Cookies/`` so the
    per-source-file output layout has something to split on.
    """
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
            ("claude.ai", "Sess3c", "tok_cc"),
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


def _list_files(d: Path) -> list:
    return sorted(p.name for p in d.iterdir() if p.is_file())


def test_per_source_file_layout(logs_dir: Path, tmp_path: Path):
    """Each matching source cookie file becomes its own Netscape file."""
    out = tmp_path / "sorted"
    rc = cmd_sort(
        _args(
            input=str(logs_dir),
            output=str(out),
            keywords="netflix,claude",
        )
    )
    assert rc == 0

    # No per-keyword folders.
    assert not (out / "netflix").exists()
    assert not (out / "claude").exists()

    dirs = sorted(p.name for p in out.iterdir() if p.is_dir())
    assert dirs == ["victim_one", "victim_three"], dirs

    # victim_one: 1 source cookie file (netflix) + creds.txt
    v1_files = _list_files(out / "victim_one")
    cookie_files_v1 = [f for f in v1_files if f != "creds.txt"]
    assert len(cookie_files_v1) == 1, v1_files
    fname = cookie_files_v1[0]
    # Should preserve original cookie source filename + hash + .txt
    assert "0_netflix.com.txt" in fname and fname.endswith(".txt")
    text = (out / "victim_one" / fname).read_text(encoding="utf-8")
    assert text.startswith("# Netscape HTTP Cookie File")
    assert ".netflix.com" in text
    assert "tok_a" in text
    assert "creds.txt" in v1_files

    # victim_three: 2 separate source files (netflix + claude), each its own output
    v3_files = _list_files(out / "victim_three")
    cookie_files_v3 = [f for f in v3_files if f != "creds.txt"]
    assert len(cookie_files_v3) == 2, v3_files

    # Each output file contains exactly the cookies from its source file.
    netflix_out = next(f for f in cookie_files_v3 if "netflix.com" in f)
    claude_out = next(f for f in cookie_files_v3 if "claude.ai" in f)
    nf_text = (out / "victim_three" / netflix_out).read_text(encoding="utf-8")
    cl_text = (out / "victim_three" / claude_out).read_text(encoding="utf-8")
    # Each file is independently Netscape-formatted with its own header.
    assert nf_text.startswith("# Netscape HTTP Cookie File")
    assert cl_text.startswith("# Netscape HTTP Cookie File")
    # Domains are NOT cross-contaminated between source files.
    assert ".netflix.com" in nf_text and ".claude.ai" not in nf_text
    assert ".claude.ai" in cl_text and ".netflix.com" not in cl_text


def test_skips_victims_and_files_without_match(logs_dir: Path, tmp_path: Path):
    out = tmp_path / "sorted2"
    rc = cmd_sort(
        _args(input=str(logs_dir), output=str(out), keywords="claude")
    )
    assert rc == 0
    dirs = sorted(p.name for p in out.iterdir() if p.is_dir())
    assert dirs == ["victim_three"]

    # Only the claude.ai source file should produce output, not the netflix one.
    files = _list_files(out / "victim_three")
    cookie_files = [f for f in files if f != "creds.txt"]
    assert len(cookie_files) == 1, files
    assert "claude.ai" in cookie_files[0]
    text = (out / "victim_three" / cookie_files[0]).read_text(encoding="utf-8")
    assert ".claude.ai" in text
    assert ".netflix.com" not in text


def test_hash_disambiguates_same_basename(tmp_path: Path):
    """Two source files with the same basename get unique outputs."""
    root = tmp_path / "logs"
    base = root / "victim_x"
    (base / "Profile1" / "Cookies").mkdir(parents=True, exist_ok=True)
    (base / "Profile2" / "Cookies").mkdir(parents=True, exist_ok=True)
    (base / "Profile1" / "Cookies" / "Cookies.txt").write_text(
        "# Netscape\n.netflix.com\tTRUE\t/\tTRUE\t1900000000\tA\t1\n",
        encoding="utf-8",
    )
    (base / "Profile2" / "Cookies" / "Cookies.txt").write_text(
        "# Netscape\n.netflix.com\tTRUE\t/\tTRUE\t1900000000\tB\t2\n",
        encoding="utf-8",
    )

    out = tmp_path / "sorted"
    rc = cmd_sort(
        _args(input=str(root), output=str(out), keywords="netflix")
    )
    assert rc == 0
    files = _list_files(out / "victim_x")
    cookie_files = [f for f in files if f != "creds.txt"]
    assert len(cookie_files) == 2, cookie_files
    assert len(set(cookie_files)) == 2  # unique


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
