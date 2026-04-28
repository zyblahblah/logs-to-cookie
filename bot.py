"""Telegram bot worker for logs-to-cookie.

Implements the streaming pipeline described in the project README:

    /process <url> [filter]

Optionally asks the user for an archive password when the URL points
at a ``.zip`` / ``.rar`` / ``.7z`` file.

Runs as ``worker: python bot.py`` on Railway. Reads ``BOT_TOKEN`` and
``ADMIN_IDS`` from env vars (or ``./.env``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional, Set
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

from extract import ARCHIVE_SUFFIXES
from processor import process_url

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("logs-to-cookie.bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DOC_UPLOAD_LIMIT = int(os.getenv("DOC_UPLOAD_LIMIT", str(50 * 1024 * 1024)))


def _parse_admins(raw: str) -> Set[int]:
    out: Set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            log.warning("ignoring non-integer ADMIN_IDS entry: %r", part)
    return out


ADMIN_IDS: Set[int] = _parse_admins(os.getenv("ADMIN_IDS", ""))

ASK_PASSWORD = 1


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


def _looks_like_archive_url(url: str) -> bool:
    name = Path(urlparse(url).path).name.lower()
    return any(name.endswith(suf) for suf in ARCHIVE_SUFFIXES)


def _human_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.2f} {u}"
        f /= 1024
    return f"{n} B"


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        return
    text = (
        "👋 *logs-to-cookie* — direct-link streaming bot\n\n"
        "Usage:\n"
        "`/process <url> [filter]`\n\n"
        "Streams the URL chunk-by-chunk, parses every Netscape cookie line "
        "(optionally filtered by `[filter]`), writes each match as its own "
        "`cookie_N.txt`, and sends them back zipped.\n\n"
        "If the URL points at a password-protected archive "
        "(.zip / .rar / .7z), I'll ask for the password after you run "
        "`/process`."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    context.user_data.clear()
    if update.message:
        await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def cmd_process(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not _is_admin(update):
        return ConversationHandler.END

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: `/process <url> [filter]`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    url = args[0]
    keyword = " ".join(args[1:]).strip() or None

    if not _looks_like_url(url):
        await update.message.reply_text(
            "That doesn't look like an http(s) URL."
        )
        return ConversationHandler.END

    context.user_data["url"] = url
    context.user_data["keyword"] = keyword

    if _looks_like_archive_url(url):
        await update.message.reply_text(
            "Archive URL detected. Send the password (or `none`):",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ASK_PASSWORD

    await _run_job(update, context, password=None)
    return ConversationHandler.END


async def on_password(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    text = (update.message.text or "").strip()
    if text.startswith("/"):
        return await cmd_cancel(update, context)
    password: Optional[str] = None if text.lower() in ("none", "-", "") else text
    await _run_job(update, context, password=password)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------


async def _run_job(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    password: Optional[str],
) -> None:
    url: str = context.user_data.get("url", "")
    keyword: Optional[str] = context.user_data.get("keyword")
    chat_id = update.effective_chat.id
    started = time.time()

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="🌀 Status: Starting...",
    )

    last_edit = 0.0

    async def _edit(text: str) -> None:
        nonlocal last_edit
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass
        last_edit = time.time()

    loop = asyncio.get_event_loop()

    def _post_status(s: str) -> None:
        # called from worker thread → schedule on event loop
        elapsed = int(time.time() - started)
        body = f"🌀 Status: {s}\n⏱️ Elapsed: {elapsed}s"
        asyncio.run_coroutine_threadsafe(_edit(body), loop)

    workdir = Path(tempfile.mkdtemp(prefix="logs2cookie-"))
    try:
        try:
            result = await loop.run_in_executor(
                None,
                lambda: process_url(
                    url,
                    workdir,
                    keyword=keyword,
                    password=password,
                    on_status=_post_status,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("process_url failed for %s", url)
            await _edit(f"❌ Failed: {exc}")
            return

        elapsed = int(time.time() - started)
        if result.item_count == 0:
            await _edit(
                "ℹ️ Done — no matching cookies found.\n"
                f"📡 Read: {_human_bytes(result.bytes_read)}\n"
                f"⏱️ Elapsed: {elapsed}s"
            )
            return

        zip_size = result.zip_path.stat().st_size
        if zip_size > DOC_UPLOAD_LIMIT:
            await _edit(
                "⚠️ Result zip too large for Telegram upload.\n"
                f"📦 {result.item_count} cookie file(s)\n"
                f"📡 zip: {_human_bytes(zip_size)} > limit "
                f"{_human_bytes(DOC_UPLOAD_LIMIT)}\n"
                f"⏱️ Elapsed: {elapsed}s"
            )
            return

        await _edit(
            "📤 Uploading result...\n"
            f"📦 {result.item_count} cookie file(s)\n"
            f"📡 zip: {_human_bytes(zip_size)}\n"
            f"⏱️ Elapsed: {elapsed}s"
        )

        with open(result.zip_path, "rb") as f:
            await context.bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=result.zip_path.name,
                caption=(
                    f"✅ {result.item_count} cookie file(s)\n"
                    f"📡 read: {_human_bytes(result.bytes_read)}\n"
                    f"⏱️ {int(time.time() - started)}s"
                ),
            )
        await _edit(
            f"✅ Done — sent {_human_bytes(zip_size)} "
            f"({result.item_count} cookies)."
        )
    finally:
        context.user_data.clear()
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def build_app() -> Application:
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN is not set. Add it to .env or your Railway "
            "environment variables."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("process", cmd_process)],
        states={
            ASK_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_password),
                CommandHandler("cancel", cmd_cancel),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="process_conv",
        persistent=False,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(conv)
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    return app


def main() -> None:
    app = build_app()
    log.info(
        "logs-to-cookie bot starting (admins=%s)",
        ADMIN_IDS or "<everyone>",
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
