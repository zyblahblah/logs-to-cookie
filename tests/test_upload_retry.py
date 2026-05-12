"""Tests for ``_send_zip_with_retry`` in ``bot.py``.

The bug this guards against: an unhandled transient error inside
``send_document`` used to leave the chat stuck on
``📤 Uploading result...`` for 30+ minutes because PTB has no
default error_handler and the bot wasn't catching the exception.

These tests stub the ``Bot`` and ``asyncio.sleep`` so they're fast
and deterministic.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path
from typing import Callable, List, Optional
from unittest.mock import patch

import pytest

from telegram.error import (
    BadRequest,
    NetworkError,
    RetryAfter,
    TimedOut,
)


@pytest.fixture(scope="module")
def bot_module():
    os.environ.setdefault("BOT_TOKEN", "test-token")
    if "bot" in sys.modules:
        del sys.modules["bot"]
    return importlib.import_module("bot")


class _FakeBot:
    """Minimal stand-in for ``telegram.Bot`` used in retry tests.

    ``responses`` is a list of either ``None`` (success) or an
    ``Exception`` to raise. Each call pops one entry.
    """

    def __init__(self, responses: List[Optional[BaseException]]) -> None:
        self._responses = list(responses)
        self.calls: List[dict] = []

    async def send_document(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        if not self._responses:
            return None
        outcome = self._responses.pop(0)
        if outcome is None:
            return None
        raise outcome


async def _noop_edit(_text: str) -> None:
    return None


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def _patch_sleep() -> Callable:
    """Replace ``asyncio.sleep`` inside bot module with an instant
    coroutine so retry backoffs don't slow the suite down."""

    async def _instant_sleep(_seconds: float) -> None:
        return None

    return patch("bot.asyncio.sleep", _instant_sleep)


def test_send_succeeds_on_first_attempt(bot_module, tmp_path: Path) -> None:
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"PK\x03\x04dummy")
    fake = _FakeBot([None])
    with _patch_sleep():
        _run(
            bot_module._send_zip_with_retry(
                fake,
                chat_id=42,
                zip_path=zip_path,
                caption="ok",
                edit_status=_noop_edit,
            )
        )
    assert len(fake.calls) == 1
    call = fake.calls[0]
    # We pass the path as a string so PTB streams the file itself —
    # that's intentional, see _send_zip_with_retry's docstring.
    assert call["document"] == str(zip_path)
    assert call["chat_id"] == 42
    assert call["filename"] == zip_path.name
    # Make sure the explicit timeouts are forwarded so PTB doesn't
    # fall back to its 5 s defaults.
    assert call["read_timeout"] == bot_module.UPLOAD_READ_TIMEOUT
    assert call["write_timeout"] == bot_module.UPLOAD_WRITE_TIMEOUT
    assert call["connect_timeout"] == bot_module.UPLOAD_CONNECT_TIMEOUT
    assert call["pool_timeout"] == bot_module.UPLOAD_CONNECT_TIMEOUT


def test_retries_then_succeeds_on_timed_out(bot_module, tmp_path: Path) -> None:
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"PK\x03\x04dummy")
    fake = _FakeBot(
        [TimedOut("write timeout"), NetworkError("conn reset"), None]
    )
    with _patch_sleep():
        _run(
            bot_module._send_zip_with_retry(
                fake,
                chat_id=1,
                zip_path=zip_path,
                caption="ok",
                edit_status=_noop_edit,
            )
        )
    assert len(fake.calls) == 3


def test_raises_after_exhausting_retries(bot_module, tmp_path: Path) -> None:
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"PK\x03\x04dummy")
    fake = _FakeBot([TimedOut("t1"), TimedOut("t2"), TimedOut("t3"), TimedOut("t4")])
    with _patch_sleep():
        with pytest.raises(bot_module._UploadFailed) as ei:
            _run(
                bot_module._send_zip_with_retry(
                    fake,
                    chat_id=1,
                    zip_path=zip_path,
                    caption="ok",
                    edit_status=_noop_edit,
                )
            )
    assert ei.value.attempts == bot_module.UPLOAD_MAX_ATTEMPTS
    assert "TimedOut" in ei.value.last_error
    # Confirm the call count matches the configured attempt budget.
    assert len(fake.calls) == bot_module.UPLOAD_MAX_ATTEMPTS


def test_non_retryable_telegram_error_bails_immediately(
    bot_module, tmp_path: Path
) -> None:
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"PK\x03\x04dummy")
    fake = _FakeBot([BadRequest("file too big")])
    with _patch_sleep():
        with pytest.raises(bot_module._UploadFailed) as ei:
            _run(
                bot_module._send_zip_with_retry(
                    fake,
                    chat_id=1,
                    zip_path=zip_path,
                    caption="ok",
                    edit_status=_noop_edit,
                )
            )
    # No retries, single attempt.
    assert ei.value.attempts == 1
    assert "BadRequest" in ei.value.last_error
    assert len(fake.calls) == 1


def test_honors_short_retry_after(bot_module, tmp_path: Path) -> None:
    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"PK\x03\x04dummy")
    sleeps: List[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    fake = _FakeBot([RetryAfter(retry_after=3), None])
    with patch("bot.asyncio.sleep", _record_sleep):
        _run(
            bot_module._send_zip_with_retry(
                fake,
                chat_id=1,
                zip_path=zip_path,
                caption="ok",
                edit_status=_noop_edit,
            )
        )
    assert len(fake.calls) == 2
    # We slept for the retry_after Telegram requested.
    assert 3.0 in sleeps


def test_huge_retry_after_bails_without_sleeping(
    bot_module, tmp_path: Path
) -> None:
    """If Telegram asks us to wait longer than ``UPLOAD_MAX_RETRY_AFTER``,
    we surface the error instead of holding the chat hostage."""

    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"PK\x03\x04dummy")
    sleeps: List[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    fake = _FakeBot(
        [RetryAfter(retry_after=int(bot_module.UPLOAD_MAX_RETRY_AFTER + 60))]
    )
    with patch("bot.asyncio.sleep", _record_sleep):
        with pytest.raises(bot_module._UploadFailed) as ei:
            _run(
                bot_module._send_zip_with_retry(
                    fake,
                    chat_id=1,
                    zip_path=zip_path,
                    caption="ok",
                    edit_status=_noop_edit,
                )
            )
    assert len(fake.calls) == 1
    assert sleeps == []
    assert "retry_after" in ei.value.last_error


def test_per_attempt_deadline_caught_as_transient(
    bot_module, tmp_path: Path
) -> None:
    """A stuck ``send_document`` must not park the coroutine forever.

    We simulate a hang by making the underlying call never return,
    rely on ``asyncio.wait_for`` to raise ``TimeoutError``, and
    expect the retry loop to count it as a transient failure.
    """

    zip_path = tmp_path / "result.zip"
    zip_path.write_bytes(b"PK\x03\x04dummy")

    class _HangingBot:
        async def send_document(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            # Block on a never-set Event so ``asyncio.wait_for``
            # actually times out — using ``asyncio.sleep`` here would
            # be a no-op since the test patches sleep to be instant.
            await asyncio.Event().wait()

    # Make wait_for time out immediately by patching the per-attempt
    # deadline to ~0 and using a real (instant-patched) sleep.
    original_deadline = bot_module.UPLOAD_ATTEMPT_DEADLINE
    bot_module.UPLOAD_ATTEMPT_DEADLINE = 0.01
    try:
        with _patch_sleep():
            with pytest.raises(bot_module._UploadFailed) as ei:
                _run(
                    bot_module._send_zip_with_retry(
                        _HangingBot(),
                        chat_id=1,
                        zip_path=zip_path,
                        caption="ok",
                        edit_status=_noop_edit,
                    )
                )
        assert ei.value.attempts == bot_module.UPLOAD_MAX_ATTEMPTS
        # Either TimeoutError (Py3.11+) or TimedOut may show in the
        # rendered last error — both are acceptable.
        assert (
            "TimeoutError" in ei.value.last_error
            or "TimedOut" in ei.value.last_error
        )
    finally:
        bot_module.UPLOAD_ATTEMPT_DEADLINE = original_deadline


def test_summarize_upload_error_redacts_bot_token(bot_module) -> None:
    token = bot_module.BOT_TOKEN
    if not token:
        pytest.skip("BOT_TOKEN not set in this test process")
    err = NetworkError(f"connect failed to https://api.telegram.org/bot{token}/sendDocument")
    rendered = bot_module._summarize_upload_error(err)
    assert token not in rendered
    assert "<redacted>" in rendered
