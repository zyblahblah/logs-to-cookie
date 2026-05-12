"""Telegram bot — Logs to Netscape Cookie Converter.

Implements the interactive flow shown in
``telegram_bot_cookie_converter_flow.svg``::

    /start
       └─► Bot asks for one *or many* direct download URLs
            └─► Bot asks for the archive password (or /skip)
                 └─► Bot asks for keywords to filter on (or /skip)
                      └─► Pipeline runs (concurrent download → parse →
                          convert → 1 file per cookie set → zip)
                           └─► Bot returns the zip directly. If the
                               zip is bigger than Telegram's bot upload
                               limit (50 MB by default), the bot stops
                               with a clear error.

Run as ``worker: python bot.py``. ``BOT_TOKEN`` and ``ADMIN_IDS`` are
read from the environment (or a local ``.env`` file).

Access model
------------
* ``ADMIN_IDS`` env var — full control. Run ``/genkey``, ``/rmkey``,
  ``/addvip``, ``/rmvip``, etc. If unset, the bot is open to everyone
  (backwards-compatible default).
* VIP list — Telegram user IDs in ``state.json`` (``STATE_PATH`` env
  var). Added with ``/addvip <id>`` or by users themselves redeeming
  a key with ``/redeem <key>``.

Concurrency
-----------
Each accepted job is funnelled through a global :class:`JobQueue`.
``MAX_CONCURRENT_JOBS`` (default ``1``) caps the number of pipelines
running at once; users behind the head of the queue see their position
update with ``/queue``. The conversation handler returns the moment a
job is *queued* (rather than blocking until it's done), so a user can
fire off another job back-to-back without losing access to ``/start``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from pipeline import AccessStore, Job, JobQueue, run_pipeline_multi
from pipeline.archive import SEVENZIP_BINARIES, UNRAR_BINARIES, _all_on_path
from pipeline.bootstrap import ensure_bundled_7zz

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("logs-to-cookie.bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DOC_UPLOAD_LIMIT = int(os.getenv("DOC_UPLOAD_LIMIT", str(50 * 1024 * 1024)))
# Bumped from 5 GB → 50 GB. Most stealer logs sit comfortably under
# 5 GB but the occasional bundled dump (the one that produced the
# "22 GB > max 5 GB" rejection in the wild) easily crosses it.
# Operators can still override with the env var.
MAX_DOWNLOAD_BYTES = int(
    os.getenv("MAX_DOWNLOAD_BYTES", str(50 * 1024 * 1024 * 1024))
)
MAX_LINKS_PER_JOB = int(os.getenv("MAX_LINKS_PER_JOB", "10"))
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "1"))
STATE_PATH = Path(os.getenv("STATE_PATH", "state.json")).expanduser()

# Status edits go through Telegram's ``editMessageText`` API which is
# rate-limited at roughly 1 edit / second per chat. We coalesce our
# in-flight edits to avoid hitting ``RetryAfter`` and falling behind.
EDIT_MIN_INTERVAL = float(os.getenv("EDIT_MIN_INTERVAL", "1.2"))


# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------
ASK_URL = 1
ASK_PASSWORD = 2
ASK_KEYWORDS = 3


# ---------------------------------------------------------------------------
# Access store (VIPs + redemption keys)
# ---------------------------------------------------------------------------
ACCESS = AccessStore(STATE_PATH)
QUEUE = JobQueue(concurrency=MAX_CONCURRENT_JOBS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_admins(raw: str) -> set[int]:
    out: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            log.warning("ignoring non-integer ADMIN_IDS entry: %r", part)
    return out


ADMIN_IDS: set[int] = _parse_admins(os.getenv("ADMIN_IDS", ""))


def _is_admin(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    return user.id in ADMIN_IDS


def _is_allowed(update: Update) -> bool:
    """Admin, VIP, or — if no admins are configured — anyone."""
    if not ADMIN_IDS:
        return True
    user = update.effective_user
    if not user:
        return False
    if user.id in ADMIN_IDS:
        return True
    return ACCESS.is_vip(user.id)


def _looks_like_url(s: str) -> bool:
    try:
        u = urlparse(s)
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.netloc)


def _human_bytes(n: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.2f} {u}"
        f /= 1024
    return f"{n} B"


def _human_speed(bps: Optional[float]) -> str:
    if not bps or bps <= 0:
        return "—"
    return f"{_human_bytes(bps)}/s"


def _human_eta(read: int, total: Optional[int], speed: Optional[float]) -> str:
    if not total or not speed or speed <= 0:
        return "—"
    remaining = max(0, total - read)
    secs = int(remaining / speed)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"


def _progress_bar(read: int, total: Optional[int], width: int = 14) -> str:
    if total and total > 0:
        ratio = min(1.0, read / total)
        filled = int(ratio * width)
        bar = "▓" * filled + "░" * (width - filled)
        pct = f"{ratio * 100:.1f}%"
        return f"{bar} {pct}\n📡 {_human_bytes(read)} / {_human_bytes(total)}"
    return f"{'░' * width}\n📡 {_human_bytes(read)} so far"


def _split_keywords(text: str) -> list[str]:
    parts: list[str] = []
    for chunk in text.replace(";", ",").split(","):
        for sub in chunk.split():
            sub = sub.strip()
            if sub:
                parts.append(sub)
    return parts


URL_PASSWORD_SEPARATOR = "|"


def _split_urls(text: str) -> List[str]:
    """Pull every ``http(s)`` URL out of free-form text.

    Thin wrapper around :func:`_parse_url_lines` for callers that
    don't care about inline passwords.
    """
    return [u for u, _ in _parse_url_lines(text)]


def _parse_url_lines(text: str) -> List[Tuple[str, Optional[str]]]:
    """Pull every ``http(s)`` URL plus optional inline password.

    Users can either paste plain URLs (one per line / comma- or
    whitespace-separated) **or** mix in inline passwords with the
    syntax ``url|password``. Lines without an inline password yield
    ``(url, None)``.

    Dedupes by URL while preserving first-seen order. The first
    inline password seen for a given URL wins; later duplicates are
    silently dropped.
    """
    out: List[Tuple[str, Optional[str]]] = []
    seen: set[str] = set()
    # Newlines + commas + semicolons are line separators; passwords
    # may contain spaces so we DON'T split on whitespace at the line
    # level when an inline separator is present.
    for raw in text.replace(";", "\n").replace(",", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("/"):
            continue
        if URL_PASSWORD_SEPARATOR in line:
            url_part, _, pwd_tail = line.partition(URL_PASSWORD_SEPARATOR)
            url_part = url_part.strip()
            pwd_part: Optional[str] = pwd_tail.strip() or None
            if not url_part or not _looks_like_url(url_part):
                continue
            if url_part in seen:
                continue
            seen.add(url_part)
            out.append((url_part, pwd_part))
        else:
            # Plain line — may still contain multiple whitespace-
            # separated URLs, none with inline passwords.
            for tok in line.split():
                tok = tok.strip()
                if not tok or tok.startswith("/"):
                    continue
                if not _looks_like_url(tok):
                    continue
                if tok in seen:
                    continue
                seen.add(tok)
                out.append((tok, None))
    return out


def _split_passwords(text: str) -> List[Optional[str]]:
    """Split a free-form password reply into a list.

    Users may type a single password (the common case) or a column /
    comma-separated list of passwords (one per remaining URL). Empty
    entries become ``None`` so a list like ``"p1, , p3"`` lets users
    skip the middle URL.
    """
    if not text:
        return []
    raw = text.replace(";", "\n").replace(",", "\n")
    out: List[Optional[str]] = []
    for chunk in raw.split("\n"):
        chunk = chunk.strip()
        if chunk.lower() in ("none", "-", "skip", ""):
            out.append(None)
        else:
            out.append(chunk)
    return out


def _friendly_pipeline_error(exc: Exception) -> str:
    """Translate a pipeline RuntimeError into something a user can act on."""
    msg = str(exc)
    low = msg.lower()
    # The "file is X bytes, larger than max (Y)" error from
    # download.DownloadError. Convert raw byte counts to GB so the
    # user can see at a glance what's happening.
    if "larger than max" in low:
        try:
            # Best-effort: pull the two ints out of the message.
            import re

            m = re.search(r"file is (\d+) bytes, larger than max \((\d+)\)", msg)
            if m:
                actual = int(m.group(1))
                cap = int(m.group(2))
                return (
                    f"❌ File is too big: {_human_bytes(actual)} "
                    f"(cap: {_human_bytes(cap)}).\n"
                    "💡 If you trust the source, raise "
                    "`MAX_DOWNLOAD_BYTES` in your `.env` "
                    "and restart the bot."
                )
        except Exception:  # noqa: BLE001
            pass
    if "unsupported method" in low or "unsupported compression" in low:
        return (
            "❌ Archive uses a compression method this server's "
            "extractor can't handle (likely a fresh RAR5 codec).\n"
            "💡 Install a newer `p7zip-full` and the proprietary "
            "`unrar` binary, then retry."
        )
    # ``unrar-free`` 0.0.2 only understands RAR 2.0; on a RAR3+ archive
    # it bails with "<num> Failed" or "unknown archive type, only plain
    # RAR 2.0 supported". The pipeline rewrites that into a sentence
    # the bot can match here so the user gets actionable advice
    # instead of a cryptic "extraction failed: 485 Failed".
    if (
        "unrar-free can only read rar 2.0" in low
        or "only plain rar 2.0 supported" in low
    ):
        return (
            "❌ This server's RAR reader (`unrar-free`) only handles "
            "ancient RAR 2.0 archives — yours uses RAR3 / RAR4 / "
            "RAR5.\n"
            "💡 Install the proprietary `unrar` binary or the "
            "`p7zip-rar` codec (Debian/Ubuntu *multiverse*), or "
            "repackage the logs as `.zip` / `.7z` and retry."
        )
    if "archive is encrypted but no password was supplied" in low:
        return (
            "❌ This archive is encrypted but you sent /skip at the "
            "password prompt. Re-run /start and supply the password "
            "(or paste it inline as `url|password`)."
        )
    if "wrong password" in low or ("data error" in low and "encrypted" in low):
        return (
            "❌ Extraction failed — the password looks wrong for at "
            "least one archive. Re-run /start and double-check it."
        )
    if (
        "p7zip-full alone can't read .rar" in msg
        or ("p7zip-rar" in low and "rar" in low)
        or ("install" in low and "unrar" in low and "rar" in low)
    ):
        return (
            "❌ This server can't read .rar archives — `p7zip-full` "
            "on Debian/Ubuntu doesn't ship the RAR codec.\n"
            "💡 Install `p7zip-rar` (Debian/Ubuntu multiverse) or "
            "the proprietary `unrar` binary, or repackage the logs "
            "as .zip / .7z and retry."
        )
    if "can not open the file as archive" in low or "can't open as archive" in low:
        return (
            "❌ Extraction failed — the file the bot downloaded "
            "isn't a recognisable archive (or the installed "
            "extractor can't read this variant).\n"
            "💡 Double-check the link points at a real archive and "
            "that the host hasn't returned an HTML error page."
        )
    return f"❌ Error: {msg}"


# ---------------------------------------------------------------------------
# Conversation handlers
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Phase 1 — START. Greet and ask for one or more download URLs."""
    if not _is_allowed(update):
        if update.message:
            await update.message.reply_text(
                "🚫 You're not on the allow-list. Ask an admin for a "
                "redemption key and run `/redeem <key>`.",
                parse_mode=ParseMode.MARKDOWN,
            )
        return ConversationHandler.END

    context.user_data.clear()
    text = (
        "👋 *logs-to-cookie* — Netscape cookie converter\n\n"
        "Send me one or more *direct download URLs* to your logs "
        f"(up to {MAX_LINKS_PER_JOB} per job). Paste them on separate "
        "lines, comma-separated, or whitespace-separated — I'll figure "
        "it out. I accept any `http(s)` link — zip, 7z, rar, or "
        "tokenised CDN paths that don't end in `.zip`/`.7z`/`.rar`.\n\n"
        "🔑 *Per-link passwords*: append "
        f"`{URL_PASSWORD_SEPARATOR}password` to a URL to give that "
        "link its own password (e.g. `https://example.com/logs.zip"
        f"{URL_PASSWORD_SEPARATOR}s3cret`). Mix and match — links "
        "without an inline password fall back to whatever you supply "
        "at the next prompt.\n\n"
        "I'll stream every link, extract every Netscape cookie I can "
        "find, and send each cookie set back as its own `.txt` file "
        "inside a single zip.\n\n"
        "At any time you can send /cancel to abort, or /queue to see "
        "where you are in line."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    return ASK_URL


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await cmd_start(update, context)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    cancelled = 0
    if user is not None:
        cancelled = await QUEUE.cancel_user_jobs(user.id)
    context.user_data.clear()
    if update.message:
        if cancelled:
            await update.message.reply_text(
                f"🛑 Cancelled {cancelled} active job(s)."
            )
        else:
            await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def on_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Phase 2a — INPUT. User just sent one or more download URLs."""
    text = (update.message.text or "").strip()
    if text.startswith("/"):
        return await cmd_cancel(update, context)

    pairs = _parse_url_lines(text)
    if not pairs:
        await update.message.reply_text(
            "I couldn't find a valid `http(s)` URL in that. "
            "Send the direct download URL(s) again, or /cancel.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ASK_URL
    if len(pairs) > MAX_LINKS_PER_JOB:
        await update.message.reply_text(
            f"⚠️ That's {len(pairs)} links — the per-job cap is "
            f"{MAX_LINKS_PER_JOB}. Trim the list and try again, or "
            "/cancel.",
        )
        return ASK_URL

    urls = [u for u, _ in pairs]
    inline_passwords = [p for _, p in pairs]
    context.user_data["urls"] = urls
    context.user_data["inline_passwords"] = inline_passwords

    n = len(urls)
    n_inline = sum(1 for p in inline_passwords if p is not None)
    n_remaining = n - n_inline

    if n_remaining == 0:
        # Every URL already has its password from the inline syntax —
        # skip the password prompt entirely.
        context.user_data["passwords"] = list(inline_passwords)
        await update.message.reply_text(
            f"🔐 Got *{n}* link(s) — every one came with an inline "
            "password, so skipping the password prompt.\n\n"
            "🔎 Send the *keywords* you want to filter cookies by "
            "(comma-separated), or send /skip to keep every cookie.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ASK_KEYWORDS

    if n == 1:
        msg = "🔐 Got the link. "
    else:
        if n_inline:
            msg = (
                f"🔐 Got *{n}* links — {n_inline} already have inline "
                f"passwords, {n_remaining} still need one. "
            )
        else:
            msg = f"🔐 Got *{n}* links. "
    msg += (
        "If your archives are encrypted, send the *password*. "
        f"For multi-link jobs you can send a *single* password "
        f"(used for all {n_remaining} link(s) without an inline "
        f"password) or *{n_remaining}* passwords (comma- or "
        "newline-separated, in order). Send /skip if none are "
        "encrypted.\n\n"
        "💡 Tip: paste passwords inline at the URL prompt with "
        f"`url{URL_PASSWORD_SEPARATOR}password` per line."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    return ASK_PASSWORD


async def on_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Phase 2b — INPUT. User just answered the password prompt."""
    text = (update.message.text or "").strip()

    inline_passwords: List[Optional[str]] = (
        context.user_data.get("inline_passwords") or []
    )
    remaining_idx = [i for i, p in enumerate(inline_passwords) if p is None]
    n_remaining = len(remaining_idx)

    if text.startswith("/"):
        if text.lower().startswith("/skip"):
            replies: List[Optional[str]] = [None] * n_remaining
        elif text.lower().startswith("/cancel"):
            return await cmd_cancel(update, context)
        else:
            return await cmd_cancel(update, context)
    elif text.lower() in ("none", "-", "skip", ""):
        replies = [None] * n_remaining
    else:
        parsed = _split_passwords(text)
        # Drop trailing empty entries so a stray newline doesn't trip
        # the count check.
        while parsed and parsed[-1] is None:
            parsed.pop()
        if len(parsed) == 1:
            # Single password — fan it out to every remaining URL.
            replies = [parsed[0]] * n_remaining
        elif len(parsed) == n_remaining:
            replies = parsed
        else:
            await update.message.reply_text(
                f"⚠️ You sent {len(parsed)} password(s) but I need "
                f"either *1* (used for all) or *{n_remaining}* "
                "(one per remaining link, in order). Try again, or "
                "send /skip / /cancel.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return ASK_PASSWORD

    # Merge: keep inline passwords as-is; fill the holes with replies.
    merged: List[Optional[str]] = list(inline_passwords)
    for slot, value in zip(remaining_idx, replies):
        merged[slot] = value
    context.user_data["passwords"] = merged

    await update.message.reply_text(
        "🔎 Send the *keywords* you want to filter cookies by "
        "(comma-separated), or send /skip to keep every cookie.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_KEYWORDS


async def on_keywords(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Phase 2c — INPUT. User just answered the keyword prompt."""
    text = (update.message.text or "").strip()
    if text.startswith("/"):
        if text.lower().startswith("/skip"):
            keywords: list[str] = []
        elif text.lower().startswith("/cancel"):
            return await cmd_cancel(update, context)
        else:
            return await cmd_cancel(update, context)
    elif text.lower() in ("none", "-", "skip", ""):
        keywords = []
    else:
        keywords = _split_keywords(text)

    context.user_data["keywords"] = keywords
    await _submit_job(update, context)
    # The conversation handler is done as soon as the job is queued —
    # the runner takes over the status message and the user can /start
    # another job (or /queue) right away.
    context.user_data.clear()
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Phase 3-5 — PROCESS, OUTPUT, FEEDBACK (run inside the job queue)
# ---------------------------------------------------------------------------
async def _submit_job(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    urls: List[str] = context.user_data.get("urls") or []
    passwords: List[Optional[str]] = (
        context.user_data.get("passwords")
        or [None] * len(urls)
    )
    keywords: Sequence[str] = context.user_data.get("keywords") or []
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id if user else 0

    if not urls:
        await update.message.reply_text("❌ No URLs to process.")
        return

    label = (
        urls[0] if len(urls) == 1 else f"{len(urls)} links (first: {urls[0]})"
    )
    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="📋 Queued...",
    )

    async def runner(job: Job) -> None:
        await _run_pipeline_for_job(
            context=context,
            chat_id=chat_id,
            status_msg_id=status_msg.message_id,
            urls=urls,
            passwords=passwords,
            keywords=keywords,
        )

    job = await QUEUE.submit(
        user_id=user_id,
        chat_id=chat_id,
        label=label,
        runner=runner,
    )

    # Kick off a tiny watcher coroutine that updates the queued
    # message until the job actually starts running. Once running, the
    # pipeline takes over the same status message.
    async def _watch_queue_position() -> None:
        last_text = ""
        while True:
            pos = await QUEUE.position(job.id)
            if pos < 0:
                return
            if pos == 0:
                # The pipeline is about to start emitting its own
                # status updates; bow out.
                return
            text = (
                f"📋 Queued — position #{pos}\n"
                f"⏳ Waiting for {pos} job(s) ahead to finish...\n"
                f"💡 You can /queue any time, /cancel to drop out."
            )
            if text != last_text:
                last_text = text
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=status_msg.message_id,
                        text=text,
                    )
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(2.0)

    asyncio.create_task(_watch_queue_position())

    # NOTE: we deliberately do *not* `await job.task` here. Returning
    # immediately means the conversation handler ends as soon as the
    # job is queued, which lets the user fire off another /start
    # without waiting for the previous job's download to finish.
    # Errors are still surfaced in-band by the runner via the status
    # message edits.


async def _run_pipeline_for_job(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    status_msg_id: int,
    urls: Sequence[str],
    passwords: Sequence[Optional[str]],
    keywords: Sequence[str],
) -> None:
    started = time.time()
    loop = asyncio.get_event_loop()
    last_text = ""
    last_edit_at = 0.0
    edit_lock = asyncio.Lock()
    pending_text: Optional[str] = None

    async def _flush_edit() -> None:
        """Apply the most recent pending text, respecting the rate limit.

        Multiple ``_post_status``/``_post_progress`` calls between
        flushes simply overwrite ``pending_text`` — we never queue up
        a backlog of stale edits.
        """
        nonlocal last_text, last_edit_at, pending_text
        async with edit_lock:
            text = pending_text
            pending_text = None
            if text is None or text == last_text:
                return
            wait = EDIT_MIN_INTERVAL - (time.time() - last_edit_at)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text=text,
                )
                last_text = text
                last_edit_at = time.time()
            except RetryAfter as exc:
                # Telegram is throttling us — back off and try once
                # more on the next tick rather than dropping the edit.
                last_edit_at = time.time() + float(exc.retry_after or 1.0)
            except TimedOut:
                # Transient — let the next flush take over.
                pass
            except Exception as exc:  # noqa: BLE001
                # ``Message is not modified`` is harmless and we don't
                # want to spam the log with it on every duplicate.
                if "not modified" not in str(exc).lower():
                    log.debug("edit failed: %s", exc)

    def _schedule_edit(text: str) -> None:
        nonlocal pending_text
        pending_text = text
        asyncio.run_coroutine_threadsafe(_flush_edit(), loop)

    async def _edit_async(text: str) -> None:
        nonlocal pending_text
        pending_text = text
        await _flush_edit()

    def _post_status(line: str) -> None:
        elapsed = int(time.time() - started)
        body = f"{line}\n⏱️ Elapsed: {elapsed}s"
        _schedule_edit(body)

    def _post_progress(
        read: int,
        total: Optional[int],
        speed: Optional[float] = None,
    ) -> None:
        elapsed = int(time.time() - started)
        eta = _human_eta(read, total, speed)
        body = (
            "⏳ Downloading...\n"
            f"{_progress_bar(read, total)}\n"
            f"🚀 {_human_speed(speed)}    🎯 ETA {eta}\n"
            f"⏱️ Elapsed: {elapsed}s"
        )
        _schedule_edit(body)

    workdir = Path(tempfile.mkdtemp(prefix="logs2cookie-"))
    try:
        try:
            result = await asyncio.to_thread(
                run_pipeline_multi,
                list(urls),
                workdir,
                passwords=list(passwords),
                keywords=keywords,
                max_bytes=MAX_DOWNLOAD_BYTES,
                on_status=_post_status,
                on_progress=_post_progress,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("pipeline failed for %s", urls)
            await _edit_async(_friendly_pipeline_error(exc))
            return

        elapsed = int(time.time() - started)
        partial = ""
        if getattr(result, "errors", None):
            err_lines = "\n".join(
                f"  • link #{idx}: {msg}"
                for idx, msg in result.errors
            )
            partial = f"\n⚠️ {len(result.errors)} link(s) failed:\n{err_lines}"

        if result.cookie_count == 0:
            await _edit_async(
                "ℹ️ Done — no matching cookies found.\n"
                f"📡 Read: {_human_bytes(result.bytes_read)} from "
                f"{len(urls)} link(s)\n"
                f"⏱️ Elapsed: {elapsed}s" + partial
            )
            return

        zip_size = result.zip_path.stat().st_size

        if zip_size > DOC_UPLOAD_LIMIT:
            await _edit_async(
                f"❌ Result zip is too large for Telegram "
                f"({_human_bytes(zip_size)} > "
                f"{_human_bytes(DOC_UPLOAD_LIMIT)}).\n"
                f"📦 {len(result.cookie_files)} cookie set(s) — "
                f"{result.cookie_count} cookies\n"
                "💡 Tip: re-run with a stricter keyword filter to "
                "shrink the result." + partial
            )
            return

        await _edit_async(
            "📤 Uploading result...\n"
            f"📦 {len(result.cookie_files)} cookie set(s) — "
            f"{result.cookie_count} cookies\n"
            f"📡 zip: {_human_bytes(zip_size)}\n"
            f"⏱️ Elapsed: {elapsed}s"
        )
        with open(result.zip_path, "rb") as f:
            await context.bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=result.zip_path.name,
                caption=(
                    f"✅ {len(result.cookie_files)} cookie set(s) — "
                    f"{result.cookie_count} cookies\n"
                    f"📡 read: {_human_bytes(result.bytes_read)} "
                    f"from {len(urls)} link(s)\n"
                    f"⏱️ {elapsed}s"
                ),
            )
        await _edit_async(
            f"✅ Done! Sent {_human_bytes(zip_size)} "
            f"({len(result.cookie_files)} sets, "
            f"{result.cookie_count} cookies)." + partial
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# /queue
# ---------------------------------------------------------------------------
async def cmd_queue(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not _is_allowed(update):
        return
    snap = await QUEUE.snapshot()
    running = snap["running"]
    pending = snap["pending"]

    user = update.effective_user
    user_id = user.id if user else 0
    is_admin = user_id in ADMIN_IDS

    lines: List[str] = []
    lines.append(
        f"📋 Queue: {len(running)} running, {len(pending)} waiting "
        f"(concurrency: {QUEUE.concurrency})"
    )

    def _fmt(job, idx: Optional[int] = None) -> str:
        # Admins see everything; non-admins see only their own labels.
        if is_admin or job.user_id == user_id:
            tag = job.label[:60] + ("…" if len(job.label) > 60 else "")
        else:
            tag = "(other user)"
        prefix = f"#{idx}" if idx is not None else "▶"
        return f"  {prefix} {job.state.value} — uid={job.user_id} — {tag}"

    if running:
        lines.append("Running:")
        for j in running:
            lines.append(_fmt(j))
    if pending:
        lines.append("Pending:")
        for i, j in enumerate(pending, start=1):
            lines.append(_fmt(j, idx=i))

    if user_id:
        # User's own jobs — find their position(s).
        own = [j for j in (running + pending) if j.user_id == user_id]
        if own:
            positions = []
            for j in own:
                pos = await QUEUE.position(j.id)
                positions.append(str(pos) if pos >= 0 else "?")
            lines.append(f"You: position(s) {', '.join(positions)}")

    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Key + VIP commands (admin-only, except /redeem)
# ---------------------------------------------------------------------------
def _admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update):
            if update.message:
                await update.message.reply_text("🚫 Admin only.")
            return
        return await func(update, context)

    wrapper.__name__ = func.__name__
    return wrapper


@_admin_only
async def cmd_genkey(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/genkey [days] [note...]`` — mint a redemption key."""
    args = context.args or []
    valid_days: Optional[int] = None
    note: Optional[str] = None
    if args:
        try:
            valid_days = int(args[0])
            args = args[1:]
        except ValueError:
            valid_days = None
    if args:
        note = " ".join(args).strip() or None

    info = ACCESS.generate_key(valid_days=valid_days, note=note)
    expires = (
        f"expires <t:{info.expires_at}>" if info.expires_at else "no expiry"
    )
    body = (
        "🔑 New key generated.\n"
        f"`{info.key}`\n"
        f"{expires}"
        + (f"\nnote: {info.note}" if info.note else "")
        + "\n\nShare with the recipient. They redeem with "
        "`/redeem <key>`."
    )
    await update.message.reply_text(body, parse_mode=ParseMode.MARKDOWN)


@_admin_only
async def cmd_rmkey(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /rmkey <key>")
        return
    removed = ACCESS.remove_key(args[0])
    await update.message.reply_text(
        "🗑️ Key removed." if removed else "❌ Key not found."
    )


@_admin_only
async def cmd_listkeys(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    keys = ACCESS.list_keys()
    if not keys:
        await update.message.reply_text("No keys yet. /genkey to create one.")
        return
    lines = [f"🔑 {len(keys)} key(s):"]
    for k in keys:
        status = "redeemed" if k.is_redeemed() else (
            "expired" if k.is_expired() else "active"
        )
        who = f" by {k.redeemed_by}" if k.is_redeemed() else ""
        note = f" — {k.note}" if k.note else ""
        lines.append(f"  • `{k.key}` ({status}{who}){note}")
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN
    )


@_admin_only
async def cmd_addvip(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /addvip <user_id>")
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.message.reply_text("user_id must be a number.")
        return
    added = ACCESS.add_vip(uid)
    await update.message.reply_text(
        f"⭐ VIP added: {uid}" if added else f"{uid} is already a VIP."
    )


@_admin_only
async def cmd_rmvip(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /rmvip <user_id>")
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.message.reply_text("user_id must be a number.")
        return
    removed = ACCESS.remove_vip(uid)
    await update.message.reply_text(
        f"💤 VIP removed: {uid}" if removed else f"{uid} wasn't a VIP."
    )


@_admin_only
async def cmd_listvips(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    vips = ACCESS.list_vips()
    if not vips:
        await update.message.reply_text("No VIPs yet.")
        return
    lines = [f"⭐ {len(vips)} VIP(s):"] + [f"  • {v}" for v in vips]
    await update.message.reply_text("\n".join(lines))


async def cmd_redeem(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    if user is None:
        return
    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /redeem <key>")
        return
    key = args[0]
    try:
        ACCESS.redeem_key(key, user.id)
    except KeyError:
        await update.message.reply_text("❌ Unknown key.")
        return
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return
    await update.message.reply_text(
        "✅ Redeemed — you're now a VIP. Send /start to begin."
    )


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
def build_app() -> Application:
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN is not set. Add it to .env or your hosting "
            "provider's environment variables."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("help", cmd_help),
        ],
        states={
            ASK_URL: [
                CommandHandler("cancel", cmd_cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_url),
            ],
            ASK_PASSWORD: [
                CommandHandler("cancel", cmd_cancel),
                CommandHandler("skip", on_password),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_password),
            ],
            ASK_KEYWORDS: [
                CommandHandler("cancel", cmd_cancel),
                CommandHandler("skip", on_keywords),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_keywords),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="logs2cookie_conv",
        persistent=False,
    )

    app.add_handler(conv)

    # Top-level commands available outside the conversation too.
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("redeem", cmd_redeem))
    app.add_handler(CommandHandler("genkey", cmd_genkey))
    app.add_handler(CommandHandler("rmkey", cmd_rmkey))
    app.add_handler(CommandHandler("listkeys", cmd_listkeys))
    app.add_handler(CommandHandler("addvip", cmd_addvip))
    app.add_handler(CommandHandler("rmvip", cmd_rmvip))
    app.add_handler(CommandHandler("listvips", cmd_listvips))
    return app


def _check_extractor_binaries() -> None:
    """Log every extractor the bot will try, in chain order.

    Pre-warms the runtime ``7zz`` bootstrap (see
    :mod:`pipeline.bootstrap`) so the first archive job doesn't pay
    the cold-start download cost. The bootstrap silently no-ops if
    the platform isn't supported or the network is unreachable;
    every diagnostic the operator needs to triage shows up in this
    one log block.
    """
    log.info("PATH=%s", os.environ.get("PATH", ""))

    bundled = ensure_bundled_7zz()
    if bundled:
        log.info(
            "runtime-bundled 7zz at %s (extractor chain will try this first)",
            bundled,
        )
    else:
        log.info(
            "runtime-bundled 7zz unavailable \u2014 falling back to whatever "
            "7z/unrar binaries are already on PATH"
        )

    sevenzips = _all_on_path(SEVENZIP_BINARIES)
    if not sevenzips and not bundled:
        log.warning(
            "7z binary not found on PATH (looked for %s) and the "
            "runtime bundle is also unavailable. All archive "
            "extraction (zip / 7z / rar) will fail at runtime. "
            "Install p7zip-full on your host (Debian/Ubuntu: "
            "apt-get install p7zip-full) or let the bot reach "
            "www.7-zip.org for the runtime bundle.",
            ", ".join(SEVENZIP_BINARIES),
        )
    elif sevenzips:
        log.info("system 7z binaries on PATH: %s", ", ".join(sevenzips))
    else:
        log.info("no system 7z binary on PATH; relying on runtime bundle")

    unrars = _all_on_path(UNRAR_BINARIES)
    if not unrars:
        log.info(
            "unrar binary not on PATH \u2014 fine when the runtime 7zz "
            "bundle is active (it handles RAR3/RAR4/RAR5 natively); "
            "install `unrar` only if you've disabled the bundle."
        )
    else:
        log.info("unrar binaries on PATH: %s", ", ".join(unrars))

    # Heads-up about any legacy build-time bundling. The runtime
    # bootstrap supersedes this entirely \u2014 we only check so
    # operators carrying an older railpack.json don't get confused
    # when the build-time binary doesn't end up first in the chain.
    legacy_bundled = Path("/app/bin/7zz")
    if legacy_bundled.is_file() and bundled and bundled != str(legacy_bundled):
        log.info(
            "legacy build-time 7zz at %s is shadowed by runtime bundle %s",
            legacy_bundled,
            bundled,
        )


def main() -> None:
    app = build_app()
    log.info(
        "logs-to-cookie bot starting (admins=%s, doc_limit=%s, max_dl=%s, "
        "max_links=%s, concurrency=%s, state=%s)",
        ADMIN_IDS or "<everyone>",
        _human_bytes(DOC_UPLOAD_LIMIT),
        _human_bytes(MAX_DOWNLOAD_BYTES),
        MAX_LINKS_PER_JOB,
        MAX_CONCURRENT_JOBS,
        STATE_PATH,
    )
    _check_extractor_binaries()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
