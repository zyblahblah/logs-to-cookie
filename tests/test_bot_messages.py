"""Regression tests for bot.py message formatting.

Telegram returns ``BadRequest: Can't parse entities`` whenever a
Markdown-formatted message contains an unbalanced ``_``, ``*``, ``[``,
or backtick. The hosted-download-link message embeds a URL whose
filename ends in ``cookies_result.zip`` — the underscore there used to
be interpreted as an italic open marker and the message was rejected
the moment the result zip exceeded ``DOC_UPLOAD_LIMIT``.
"""

from __future__ import annotations

import os

# Set a fake BOT_TOKEN so importing ``bot`` doesn't crash on missing env.
os.environ.setdefault("BOT_TOKEN", "0:test")

import bot  # noqa: E402


SAMPLE_URL = (
    "https://files.example.com/files/Ab12cD34Ef56/cookies_result.zip"
)


def test_hosted_link_message_uses_html_not_markdown_underscore() -> None:
    """The hosted-link body must be HTML, with no raw Markdown underscores
    around our generated URL.
    """
    body = bot._format_hosted_link_message(
        download_url=SAMPLE_URL,
        ttl_min=60,
        zip_size=12345,
        cookie_set_count=2,
        cookie_count=5,
    )

    # HTML, not Markdown.
    assert "<b>Done!</b>" in body, body
    assert "*Done!*" not in body, body

    # The user-controlled URL must appear literally (with its underscore).
    assert SAMPLE_URL in body, body

    # Sanity — counts and TTL show up correctly.
    assert "valid ~60 min" in body, body
    assert "2 cookie set(s)" in body, body
    assert "5 cookies" in body, body


def test_hosted_link_message_escapes_html_metacharacters() -> None:
    """A URL containing ``<``/``>``/``&`` must be HTML-escaped so HTML
    parse mode doesn't reject the message.
    """
    nasty_url = "https://x/<token&id=1>/cookies_result.zip"
    body = bot._format_hosted_link_message(
        download_url=nasty_url,
        ttl_min=1,
        zip_size=1,
        cookie_set_count=1,
        cookie_count=1,
    )
    # Must not contain raw ``<token`` or ``&id``.
    assert "<token" not in body, body
    assert "&id=" not in body, body
    assert "&lt;token" in body, body
    assert "&amp;id=" in body, body
