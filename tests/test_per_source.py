"""Tests for --per-source cookies output layout."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from logs_to_cookie.cli import cmd_cookies


def _make_victim(root: Path, name: str, domain: str, token: str) -> None:
    cookies_dir = root / name / "Cookies"
    cookies_dir.mkdir(parents=True, exist_ok=True)
    # Vary the cookie name per victim so dedupe (by domain/path/name) keeps
    # each victim's session even when the domain matches.
    cookie_name = f"Session_{name}"
    (cookies_dir / f"{domain}.txt").write_text(
        dedent(
            f"""\
            # Netscape HTTP Cookie File
            .{domain}\tTRUE\t/\tTRUE\t1900000000\t{cookie_name}\t{token}
            """
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def logs_dir(tmp_path: Path) -> Path:
    root = tmp_path / "logs"
    _make_victim(root, "victim01", "netflix.com", "alice-session")
    _make_victim(root, "victim02", "spotify.com", "bob-session")
    _make_victim(root, "victim03", "netflix.com", "carol-session")
    return root


def _args(**kw):
    import argparse

    ns = argparse.Namespace(
        input=None,
        output=None,
        format="netscape",
        filter=None,
        per_source=False,
        password=[],
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_per_source_creates_one_folder_per_victim(logs_dir: Path, tmp_path: Path):
    out = tmp_path / "cookies_out"
    rc = cmd_cookies(_args(input=str(logs_dir), output=str(out), per_source=True))
    assert rc == 0
    assert {p.name for p in out.iterdir()} == {"victim01", "victim02", "victim03"}
    for victim in ("victim01", "victim02", "victim03"):
        f = out / victim / "cookies.txt"
        assert f.is_file(), f"missing {f}"
        content = f.read_text(encoding="utf-8")
        assert content.startswith("# Netscape HTTP Cookie File"), content[:80]
        # each victim should contain exactly one cookie line
        lines = [
            ln for ln in content.splitlines() if ln and not ln.startswith("#")
        ]
        assert len(lines) == 1, f"{victim}: {content}"


def test_per_source_json_format(logs_dir: Path, tmp_path: Path):
    out = tmp_path / "cookies_out_json"
    rc = cmd_cookies(
        _args(input=str(logs_dir), output=str(out), per_source=True, format="json")
    )
    assert rc == 0
    v1 = out / "victim01" / "cookies.json"
    assert v1.is_file()
    data = json.loads(v1.read_text(encoding="utf-8"))
    assert isinstance(data, list) and len(data) == 1
    assert data[0]["domain"].endswith("netflix.com")


def test_per_source_respects_filter(logs_dir: Path, tmp_path: Path):
    out = tmp_path / "cookies_filtered"
    rc = cmd_cookies(
        _args(
            input=str(logs_dir),
            output=str(out),
            per_source=True,
            filter="netflix",
        )
    )
    assert rc == 0
    # Only victims with matching cookies should have a folder.
    dirs = {p.name for p in out.iterdir()}
    assert dirs == {"victim01", "victim03"}, dirs


def test_default_behaviour_still_single_file(logs_dir: Path, tmp_path: Path):
    out = tmp_path / "all.cookies.txt"
    rc = cmd_cookies(_args(input=str(logs_dir), output=str(out)))
    assert rc == 0
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    assert "alice-session" in text
    assert "bob-session" in text
    assert "carol-session" in text
