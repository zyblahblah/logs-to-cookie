"""Sanity tests for the small text-parsing helpers in ``bot.py``.

We don't import the whole bot (it requires ``BOT_TOKEN``); only the
pure helpers we want to exercise.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def bot_module():
    # Provide a token so ``build_app`` is callable, but we only use
    # the pure helpers.
    os.environ.setdefault("BOT_TOKEN", "test-token")
    if "bot" in sys.modules:
        del sys.modules["bot"]
    return importlib.import_module("bot")


@pytest.fixture
def bot_module_with_workdir(tmp_path, monkeypatch):
    """Reload ``bot`` after pointing it at a per-test workdir root.

    ``LOGS_TO_COOKIE_WORKDIR`` is read at import time, so to test the
    deterministic / persistent-workdir behaviour we have to reload the
    module after setting the env var.
    """
    os.environ.setdefault("BOT_TOKEN", "test-token")
    monkeypatch.setenv("LOGS_TO_COOKIE_WORKDIR", str(tmp_path))
    if "bot" in sys.modules:
        del sys.modules["bot"]
    mod = importlib.import_module("bot")
    yield mod, tmp_path
    if "bot" in sys.modules:
        del sys.modules["bot"]


def test_split_urls_handles_newlines(bot_module) -> None:
    text = "https://a.example.com/x\nhttps://b.example.com/y"
    assert bot_module._split_urls(text) == [
        "https://a.example.com/x",
        "https://b.example.com/y",
    ]


def test_split_urls_handles_mixed_separators(bot_module) -> None:
    text = "https://a.com/1, https://b.com/2; https://c.com/3 https://d.com/4"
    assert bot_module._split_urls(text) == [
        "https://a.com/1",
        "https://b.com/2",
        "https://c.com/3",
        "https://d.com/4",
    ]


def test_split_urls_dedupes_preserving_order(bot_module) -> None:
    text = "https://a.com/1\nhttps://b.com/2\nhttps://a.com/1"
    assert bot_module._split_urls(text) == [
        "https://a.com/1",
        "https://b.com/2",
    ]


def test_split_urls_drops_non_urls(bot_module) -> None:
    text = "ftp://nope.example/x\nplain text\nhttps://good.example/ok"
    assert bot_module._split_urls(text) == ["https://good.example/ok"]


def test_split_urls_drops_slash_commands(bot_module) -> None:
    text = "/skip\nhttps://good.example/ok\n/cancel"
    assert bot_module._split_urls(text) == ["https://good.example/ok"]


def test_split_urls_empty(bot_module) -> None:
    assert bot_module._split_urls("") == []
    assert bot_module._split_urls("   \n\n   ") == []


# ---------------------------------------------------------------------------
# Inline-password parsing (url|password syntax).
# ---------------------------------------------------------------------------
def test_parse_url_lines_no_inline(bot_module) -> None:
    text = "https://a.com/1\nhttps://b.com/2"
    assert bot_module._parse_url_lines(text) == [
        ("https://a.com/1", None),
        ("https://b.com/2", None),
    ]


def test_parse_url_lines_with_inline_passwords(bot_module) -> None:
    text = (
        "https://a.com/1|secret-a\n"
        "https://b.com/2\n"
        "https://c.com/3|p3-with-spaces are fine"
    )
    assert bot_module._parse_url_lines(text) == [
        ("https://a.com/1", "secret-a"),
        ("https://b.com/2", None),
        ("https://c.com/3", "p3-with-spaces are fine"),
    ]


def test_parse_url_lines_empty_inline_password_treated_as_none(bot_module) -> None:
    # ``url|`` (trailing separator with no password) should yield None.
    text = "https://a.com/1|\nhttps://b.com/2"
    assert bot_module._parse_url_lines(text) == [
        ("https://a.com/1", None),
        ("https://b.com/2", None),
    ]


def test_parse_url_lines_dedupes_keeps_first_password(bot_module) -> None:
    text = (
        "https://a.com/1|first-pwd\n"
        "https://a.com/1|second-pwd-ignored\n"
        "https://b.com/2"
    )
    assert bot_module._parse_url_lines(text) == [
        ("https://a.com/1", "first-pwd"),
        ("https://b.com/2", None),
    ]


def test_split_passwords_single(bot_module) -> None:
    assert bot_module._split_passwords("hunter2") == ["hunter2"]


def test_split_passwords_list(bot_module) -> None:
    assert bot_module._split_passwords("p1, p2, p3") == ["p1", "p2", "p3"]
    assert bot_module._split_passwords("p1\np2\np3") == ["p1", "p2", "p3"]


def test_split_passwords_blank_entries_become_none(bot_module) -> None:
    assert bot_module._split_passwords("p1, , p3") == ["p1", None, "p3"]
    assert bot_module._split_passwords("p1,skip,p3") == ["p1", None, "p3"]


def test_split_passwords_empty(bot_module) -> None:
    assert bot_module._split_passwords("") == []


# ---------------------------------------------------------------------------
# Per-job workdir resolution.
#
# When ``LOGS_TO_COOKIE_WORKDIR`` is unset the bot uses ``mkdtemp`` —
# every /start gets a fresh ephemeral directory and a container
# restart loses the partial download. When set, the bot derives a
# deterministic path from the job inputs so a re-submission of the
# same URL lands in the same workdir and the resumable download in
# ``pipeline.download`` finishes from where the previous run died.
# ---------------------------------------------------------------------------
class TestJobWorkdir:
    def test_workdir_name_is_stable_across_calls(self, bot_module) -> None:
        a = bot_module._job_workdir_name(
            user_id=42,
            urls=["https://example.com/a.zip"],
            passwords=[None],
            keywords=[],
        )
        b = bot_module._job_workdir_name(
            user_id=42,
            urls=["https://example.com/a.zip"],
            passwords=[None],
            keywords=[],
        )
        assert a == b
        assert a.startswith("job-")

    def test_workdir_name_differs_per_input(self, bot_module) -> None:
        base_kwargs = dict(
            user_id=42,
            urls=["https://example.com/a.zip"],
            passwords=[None],
            keywords=[],
        )
        base = bot_module._job_workdir_name(**base_kwargs)
        # Different user → different workdir.
        diff_user = bot_module._job_workdir_name(
            **{**base_kwargs, "user_id": 43}
        )
        # Different URL → different workdir.
        diff_url = bot_module._job_workdir_name(
            **{**base_kwargs, "urls": ["https://example.com/b.zip"]}
        )
        # Different password → different workdir.
        diff_pw = bot_module._job_workdir_name(
            **{**base_kwargs, "passwords": ["hunter2"]}
        )
        # Different keyword filter → different workdir.
        diff_kw = bot_module._job_workdir_name(
            **{**base_kwargs, "keywords": ["facebook"]}
        )
        assert len({base, diff_user, diff_url, diff_pw, diff_kw}) == 5

    def test_resolve_workdir_falls_back_to_mkdtemp(
        self, bot_module
    ) -> None:
        """With no ``LOGS_TO_COOKIE_WORKDIR`` env, each call mints a
        fresh ephemeral directory \u2014 historical behaviour."""
        # The module-level fixture above does NOT set the env var, so
        # ``bot_module.WORKDIR_ROOT`` must be ``None``.
        assert bot_module.WORKDIR_ROOT is None
        wd1 = bot_module._resolve_workdir(
            user_id=1, urls=["x"], passwords=[None], keywords=[]
        )
        wd2 = bot_module._resolve_workdir(
            user_id=1, urls=["x"], passwords=[None], keywords=[]
        )
        try:
            assert wd1 != wd2  # ephemeral \u2192 random path
            assert wd1.exists() and wd2.exists()
        finally:
            for p in (wd1, wd2):
                if p.exists():
                    import shutil

                    shutil.rmtree(p, ignore_errors=True)

    def test_resolve_workdir_deterministic_when_root_set(
        self, bot_module_with_workdir
    ) -> None:
        """With ``LOGS_TO_COOKIE_WORKDIR`` pointing at a persistent
        directory, the same job inputs always land in the same path.

        This is the Railway-restart story: the bot dies mid-download,
        the partial file survives the container restart on a mounted
        volume, the next /start with the same URL resolves the same
        workdir and the resumable download in ``pipeline.download``
        finishes the job without restarting from byte 0."""
        bot_module, root = bot_module_with_workdir
        wd1 = bot_module._resolve_workdir(
            user_id=99,
            urls=["https://example.com/x.zip"],
            passwords=[None],
            keywords=[],
        )
        wd2 = bot_module._resolve_workdir(
            user_id=99,
            urls=["https://example.com/x.zip"],
            passwords=[None],
            keywords=[],
        )
        assert wd1 == wd2
        assert wd1.exists()
        assert root in wd1.parents
        # Verify a different user submitting the same URL gets a
        # separate workdir so users never trample on each other.
        wd_other = bot_module._resolve_workdir(
            user_id=100,
            urls=["https://example.com/x.zip"],
            passwords=[None],
            keywords=[],
        )
        assert wd_other != wd1

    def test_resolve_workdir_preserves_existing_partials(
        self, bot_module_with_workdir
    ) -> None:
        """A pre-existing file in the deterministic workdir must
        survive ``_resolve_workdir`` \u2014 that's the whole point."""
        bot_module, _root = bot_module_with_workdir
        wd = bot_module._resolve_workdir(
            user_id=99,
            urls=["https://example.com/x.zip"],
            passwords=[None],
            keywords=[],
        )
        partial: Path = wd / "url_0000" / "input.zip"
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(b"already-downloaded-bytes")
        # Resolving again must return the same path and the partial
        # file must still be there.
        wd_again = bot_module._resolve_workdir(
            user_id=99,
            urls=["https://example.com/x.zip"],
            passwords=[None],
            keywords=[],
        )
        assert wd_again == wd
        assert partial.exists()
        assert partial.read_bytes() == b"already-downloaded-bytes"
