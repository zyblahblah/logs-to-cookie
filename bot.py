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
update with ``/queue``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Sequence
from urllib.parse import urlparse

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from pipeline import AccessStore, Job, JobQueue, run_pipeline_multi
from pipeline.archive import SEVENZIP_BINARIES

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("logs-to-cookie.bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DOC_UPLOAD_LIMIT = int(os.getenv("DOC_UPLOAD_LIMIT", str(50 * 1024 * 1024)))
MAX_DOWNLOAD_BYTES = int(
    os.getenv("MAX_DOWNLOAD_BYTES", str(5 * 1024 * 1024 * 1024))
)
MAX_LINKS_PER_JOB = int(os.getenv("MAX_LINKS_PER_JOB", "10"))
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "1"))
STATE_PATH = Path(os.getenv("STATE_PATH", "state.json")).expanduser()


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


def _human_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.2f} {u}"
        f /= 1024
    return f"{n} B"


def _progress_bar(read: int, total: Optional[int], width: int = 12) -> str:
    if total and total > 0:
        ratio = min(1.0, read / total)
        filled = int(ratio * width)
        bar = "▓" * filled + "░" * (width - filled)
        pct = f"{ratio * 100:.1f}%"
        return f"{bar} {pct}  ({_human_bytes(read)} / {_human_bytes(total)})"
    return f"░░░░░░░░░░░░ — ({_human_bytes(read)} so far)"


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


def _parse_url_lines(text: str) -> List[tuple[str, Optional[str]]]:
    """Pull every ``http(s)`` URL plus optional inline password.

    Users can either paste plain URLs (one per line / comma- or
    whitespace-separated) **or** mix in inline passwords with the
    syntax ``url|password``. Lines without an inline password yield
    ``(url, None)``.

    Dedupes by URL while preserving first-seen order. The first
    inline password seen for a given URL wins; later duplicates are
    silently dropped.
    """
    out: List[tuple[str, Optional[str]]] = []
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

    # Wait for the job to finish so the conversation handler returns
    # cleanly (errors are already surfaced to the user inside the
    # runner; we only re-raise CancelledError).
    try:
        if job.task is not None:
            await job.task
    except asyncio.CancelledError:
        return
    except Exception:  # noqa: BLE001
        # Already logged + reported by the runner.
        return
    finally:
        context.user_data.clear()


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

    async def _edit(text: str) -> None:
        nonlocal last_text
        if text == last_text:
            return
        last_text = text
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg_id,
                text=text,
            )
        except Exception:  # noqa: BLE001
            pass

    def _post_status(line: str) -> None:
        elapsed = int(time.time() - started)
        body = f"{line}\n⏱️ Elapsed: {elapsed}s"
        asyncio.run_coroutine_threadsafe(_edit(body), loop)

    def _post_progress(read: int, total: Optional[int]) -> None:
        elapsed = int(time.time() - started)
        body = (
            "⏳ Downloading...\n"
            f"{_progress_bar(read, total)}\n"
            f"⏱️ Elapsed: {elapsed}s"
        )
        asyncio.run_coroutine_threadsafe(_edit(body), loop)

    workdir = Path(tempfile.mkdtemp(prefix="logs2cookie-"))
    try:
        try:
            result = await loop.run_in_executor(
                None,
                lambda: run_pipeline_multi(
                    list(urls),
                    workdir,
                    passwords=list(passwords),
                    keywords=keywords,
                    max_bytes=MAX_DOWNLOAD_BYTES,
                    on_status=_post_status,
                    on_progress=_post_progress,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("pipeline failed for %s", urls)
            await _edit(f"❌ Error: {exc}")
            return

        elapsed = int(time.time() - started)
        if result.cookie_count == 0:
            await _edit(
                "ℹ️ Done — no matching cookies found.\n"
                f"📡 Read: {_human_bytes(result.bytes_read)} from "
                f"{len(urls)} link(s)\n"
                f"⏱️ Elapsed: {elapsed}s"
            )
            return

        zip_size = result.zip_path.stat().st_size

        if zip_size > DOC_UPLOAD_LIMIT:
            await _edit(
                f"❌ Result zip is too large for Telegram "
                f"({_human_bytes(zip_size)} > "
                f"{_human_bytes(DOC_UPLOAD_LIMIT)}).\n"
                f"📦 {len(result.cookie_files)} cookie set(s) — "
                f"{result.cookie_count} cookies\n"
                "Tip: re-run with a stricter keyword filter to shrink "
                "the result."
            )
            return

        await _edit(
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
        await _edit(
            f"✅ Done! Sent {_human_bytes(zip_size)} "
            f"({len(result.cookie_files)} sets, "
            f"{result.cookie_count} cookies)."
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
    """Warn loudly at startup if the archive extractor isn't on PATH."""

    def _first_on_path(candidates: Sequence[str]) -> Optional[str]:
        for c in candidates:
            p = shutil.which(c)
            if p:
                return p
        return None

    sevenzip = _first_on_path(SEVENZIP_BINARIES)
    if sevenzip is None:
        log.warning(
            "7z binary not found on PATH (looked for %s). "
            "All archive extraction (zip / 7z / rar) will fail at "
            "runtime. Install p7zip-full on your host (Railway: see "
            "railpack.json; Debian/Ubuntu: apt-get install p7zip-full).",
            ", ".join(SEVENZIP_BINARIES),
        )
    else:
        log.info("7z binary OK: %s (handles zip, 7z, rar)", sevenzip)


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
