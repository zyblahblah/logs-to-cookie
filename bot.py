"""Telegram bot — Logs to Netscape Cookie Converter.

Implements the interactive flow shown in
``telegram_bot_cookie_converter_flow.svg``::

    /start
       └─► Bot asks for the direct download URL of the logs
            └─► Bot asks for the archive password (or /skip)
                 └─► Bot asks for keywords to filter on (or /skip)
                      └─► Pipeline runs (chunked download → parse →
                          convert → 1 file per cookie set → zip)
                           └─► Bot returns the zip directly, or a
                               hosted download link if the zip is
                               too large for Telegram's bot upload
                               limit.

Run as ``worker: python bot.py``. ``BOT_TOKEN`` and ``ADMIN_IDS`` are
read from the environment (or a local ``.env`` file).
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional, Sequence
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

from pipeline import is_archive_url, run_pipeline
from webserver import FileHost, from_env as webserver_from_env

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


# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------
ASK_URL = 1
ASK_PASSWORD = 2
ASK_KEYWORDS = 3


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
    if not ADMIN_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in ADMIN_IDS)


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


def _format_hosted_link_message(
    *,
    download_url: str,
    ttl_min: int,
    zip_size: int,
    cookie_set_count: int,
    cookie_count: int,
) -> str:
    """Build the *hosted download link* message body.

    HTML parse mode is used (instead of Markdown) because the download
    URL embeds a token + ``cookies_result.zip`` whose underscores would
    otherwise be interpreted as italic markers and rejected by Telegram
    with ``BadRequest: Can't parse entities``.
    """
    safe_url = html.escape(download_url, quote=False)
    return (
        f"✅ <b>Done!</b>\n"
        f"Direct download link (valid ~{ttl_min} min):\n"
        f"{safe_url}\n\n"
        f"📦 {_human_bytes(zip_size)} — "
        f"{cookie_set_count} cookie set(s), "
        f"{cookie_count} cookies"
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Phase 1 — START. Greet and ask for the download URL."""
    if not _is_admin(update):
        return ConversationHandler.END

    context.user_data.clear()
    text = (
        "👋 *logs-to-cookie* — Netscape cookie converter\n\n"
        "Send me a *direct download URL* to your logs (`.txt`, `.zip`, "
        "`.7z`, or `.rar`). I'll stream it, extract every Netscape "
        "cookie I can find, and send each cookie set back as its own "
        "`.txt` file inside a single zip.\n\n"
        "At any time you can send /cancel to abort."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    return ASK_URL


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await cmd_start(update, context)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.message:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def on_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Phase 2a — INPUT. User just sent the download URL."""
    text = (update.message.text or "").strip()
    if text.startswith("/"):
        return await cmd_cancel(update, context)
    if not _looks_like_url(text):
        await update.message.reply_text(
            "That doesn't look like an `http(s)` URL. "
            "Send the direct download URL again, or /cancel.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ASK_URL

    context.user_data["url"] = text
    if is_archive_url(text):
        await update.message.reply_text(
            "🔐 Archive detected. Send the *password* required to "
            "extract it, or send /skip if it's not encrypted.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ASK_PASSWORD

    # Plain text URL — no password needed, jump straight to keywords.
    context.user_data["password"] = None
    await update.message.reply_text(
        "🔎 Send the *keywords* you want to filter cookies by "
        "(comma-separated), or send /skip to keep every cookie.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_KEYWORDS


async def on_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Phase 2b — INPUT. User just answered the password prompt."""
    text = (update.message.text or "").strip()
    if text.startswith("/"):
        if text.lower().startswith("/skip"):
            context.user_data["password"] = None
        elif text.lower().startswith("/cancel"):
            return await cmd_cancel(update, context)
        else:
            return await cmd_cancel(update, context)
    elif text.lower() in ("none", "-", "skip", ""):
        context.user_data["password"] = None
    else:
        context.user_data["password"] = text

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
    await _run_job(update, context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Phase 3-5 — PROCESS, OUTPUT, FEEDBACK
# ---------------------------------------------------------------------------
async def _run_job(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    url: str = context.user_data.get("url", "")
    password: Optional[str] = context.user_data.get("password")
    keywords: Sequence[str] = context.user_data.get("keywords") or []
    chat_id = update.effective_chat.id
    started = time.time()

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="⏳ Downloading...",
    )

    loop = asyncio.get_event_loop()
    last_text = ""

    async def _edit(text: str) -> None:
        nonlocal last_text
        if text == last_text:
            return
        last_text = text
        try:
            await status_msg.edit_text(text)
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
                lambda: run_pipeline(
                    url,
                    workdir,
                    password=password,
                    keywords=keywords,
                    max_bytes=MAX_DOWNLOAD_BYTES,
                    on_status=_post_status,
                    on_progress=_post_progress,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("pipeline failed for %s", url)
            await _edit(f"❌ Error: {exc}")
            return

        elapsed = int(time.time() - started)
        if result.cookie_count == 0:
            await _edit(
                "ℹ️ Done — no matching cookies found.\n"
                f"📡 Read: {_human_bytes(result.bytes_read)}\n"
                f"⏱️ Elapsed: {elapsed}s"
            )
            return

        zip_size = result.zip_path.stat().st_size

        # ---------- decision: send file or hosted link ----------
        host: Optional[FileHost] = context.bot_data.get("file_host")

        if zip_size <= DOC_UPLOAD_LIMIT:
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
                        f"📡 read: {_human_bytes(result.bytes_read)}\n"
                        f"⏱️ {elapsed}s"
                    ),
                )
            await _edit(
                f"✅ Done! Sent {_human_bytes(zip_size)} "
                f"({len(result.cookie_files)} sets, "
                f"{result.cookie_count} cookies)."
            )
            return

        # zip too large — fall back to a hosted download link.
        if host is None:
            await _edit(
                "⚠️ Result zip too large for Telegram upload, and the "
                "built-in file host is disabled.\n"
                f"📦 {len(result.cookie_files)} cookie set(s)\n"
                f"📡 zip: {_human_bytes(zip_size)} > limit "
                f"{_human_bytes(DOC_UPLOAD_LIMIT)}"
            )
            return

        await _edit(
            "📤 Result is too large for Telegram. Hosting it for you...\n"
            f"📦 {_human_bytes(zip_size)} — "
            f"{len(result.cookie_files)} cookie set(s), "
            f"{result.cookie_count} cookies"
        )
        download_url = host.host_file(
            result.zip_path,
            filename="cookies_result.zip",
        )
        ttl_min = max(1, host.ttl_seconds // 60)
        await context.bot.send_message(
            chat_id=chat_id,
            text=_format_hosted_link_message(
                download_url=download_url,
                ttl_min=ttl_min,
                zip_size=zip_size,
                cookie_set_count=len(result.cookie_files),
                cookie_count=result.cookie_count,
            ),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await _edit(
            f"✅ Done! Hosted {_human_bytes(zip_size)} — "
            f"see the download link above."
        )
    finally:
        context.user_data.clear()
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
async def _post_init(app: Application) -> None:
    spool_dir = Path(tempfile.gettempdir()) / "logs2cookie-host"
    server, base = webserver_from_env(spool_dir)
    try:
        await server.start()
        app.bot_data["file_host"] = server
        log.info("hosted-result base URL = %s", base)
    except OSError as exc:
        log.warning(
            "failed to start file host (%s) — oversized results will "
            "not be deliverable until this is fixed.",
            exc,
        )


async def _post_shutdown(app: Application) -> None:
    server: Optional[FileHost] = app.bot_data.get("file_host")
    if server is not None:
        await server.stop()


def build_app() -> Application:
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN is not set. Add it to .env or your hosting "
            "provider's environment variables."
        )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

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
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    return app


def main() -> None:
    app = build_app()
    log.info(
        "logs-to-cookie bot starting (admins=%s, doc_limit=%s, max_dl=%s)",
        ADMIN_IDS or "<everyone>",
        _human_bytes(DOC_UPLOAD_LIMIT),
        _human_bytes(MAX_DOWNLOAD_BYTES),
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
