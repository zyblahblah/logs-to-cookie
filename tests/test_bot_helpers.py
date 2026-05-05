"""Sanity tests for the small text-parsing helpers in ``bot.py``.

We don't import the whole bot (it requires ``BOT_TOKEN``); only the
pure helpers we want to exercise.
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest


@pytest.fixture(scope="module")
def bot_module():
    # Provide a token so ``build_app`` is callable, but we only use
    # the pure helpers.
    os.environ.setdefault("BOT_TOKEN", "test-token")
    if "bot" in sys.modules:
        del sys.modules["bot"]
    return importlib.import_module("bot")


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
