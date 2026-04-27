"""Telegram bot worker for logs-to-cookie.

Wraps the ``ulp`` / ``cookies`` / ``sort`` subcommands behind a Telegram chat
flow so you can hand the bot a direct-download URL (or attached file) and get
back a zip of the processed output. Modelled on the worker layout of
``zyblahblah-ulp-to-combo`` so it drops straight into a Railway ``worker``
process.

Configuration (environment variables — never check real values into git):

* ``BOT_TOKEN``          — Telegram bot token from @BotFather (required).
* ``ADMIN_IDS``          — comma-separated Telegram user IDs allowed to use
                           the bot. If unset, the bot rejects everyone.
* ``WORKERS``            — parallel range-split download workers
                           (default 4).
* ``DOC_UPLOAD_LIMIT``   — bytes; results bigger than this are skipped with
                           a friendly message (default 50 MB — the standard
                           Telegram Bot API limit; raise it if you run a
                           self-hosted Bot API server).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Set

# Load a local .env file if python-dotenv is installed. Railway and other
# hosts inject env vars directly so this is a no-op there; for local
# development copy .env.example -> .env and fill in your values.
try:
    from dotenv import load_dotenv  # type: ignore[import-not-found]

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

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

from logs_to_cookie import __version__
from logs_to_cookie.download import is_url

logging.basicConfig(
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("logs-to-cookie-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS: Set[int] = {
    int(x)
    for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "").strip())
    if x.isdigit()
}
WORKERS = int(os.getenv("WORKERS", "4"))
DOC_UPLOAD_LIMIT = int(os.getenv("DOC_UPLOAD_LIMIT", str(50 * 1024 * 1024)))

# Conversation states
ASK_PWD, ASK_KEYWORDS = range(2)

WELCOME = (
    f"*logs-to-cookie* v{__version__}\n\n"
    "Send me a *direct download URL* and I'll run it through the logs "
    "pipeline:\n"
    "  • `/sort <url>`     — bucket per-victim, one Netscape file per "
    "source cookie file\n"
    "  • `/cookies <url>`  — collect cookies (per-victim folders)\n"
    "  • `/ulp <url>`      — extract URL\\:USER\\:PASS lines\n\n"
    "I'll then ask for the archive password and (for `/sort`) the keywords "
    "to filter by, run the job, and send the result back as a zip."
)


# ---------------------------------------------------------------------------
# Auth + command dispatch
# ---------------------------------------------------------------------------


def _is_admin(uid: Optional[int]) -> bool:
    if not ADMIN_IDS:
        return False
    return uid in ADMIN_IDS


async def _reject(update: Update) -> None:
    await update.message.reply_text(
        "You're not authorised to use this bot. Ask the operator to add "
        "your user ID to `ADMIN_IDS`."
    )


async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id):
        await _reject(update)
        return
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)


# ---------------------------------------------------------------------------
# /sort, /cookies, /ulp — conversation entry points
# ---------------------------------------------------------------------------


async def _entry(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    cmd: str,
    needs_keywords: bool,
) -> int:
    if not _is_admin(update.effective_user.id):
        await _reject(update)
        return ConversationHandler.END

    args = ctx.args or []
    if not args:
        await update.message.reply_text(
            f"Usage: `/{cmd} <https://...>`", parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END
    url = args[0]
    if not is_url(url):
        await update.message.reply_text("Please provide a direct http(s) URL.")
        return ConversationHandler.END

    ctx.user_data["job_cmd"] = cmd
    ctx.user_data["job_url"] = url
    ctx.user_data["job_needs_keywords"] = needs_keywords
    await update.message.reply_text(
        "Send the archive password (or `none` if it isn't encrypted):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ASK_PWD


async def cmd_sort(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    return await _entry(update, ctx, cmd="sort", needs_keywords=True)


async def cmd_cookies(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    return await _entry(update, ctx, cmd="cookies", needs_keywords=False)


async def cmd_ulp(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    return await _entry(update, ctx, cmd="ulp", needs_keywords=False)


async def on_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    pwd = (update.message.text or "").strip()
    ctx.user_data["job_passwords"] = (
        [] if pwd.lower() in ("", "none", "n/a", "-") else [pwd]
    )
    if ctx.user_data.get("job_needs_keywords"):
        await update.message.reply_text(
            "Send the keywords to filter by (comma-separated), e.g. "
            "`netflix,spotify,roblox`:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ASK_KEYWORDS
    return await _run_job(update, ctx, keywords=None)


async def on_keywords(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    keywords = (update.message.text or "").strip()
    if not keywords:
        await update.message.reply_text("Keywords are required for `/sort`.")
        return ASK_KEYWORDS
    return await _run_job(update, ctx, keywords=keywords)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Subprocess runner + result packaging
# ---------------------------------------------------------------------------


def _build_argv(
    cmd: str,
    url: str,
    passwords: List[str],
    keywords: Optional[str],
    out_dir: Path,
) -> List[str]:
    argv = [sys.executable, "-m", "logs_to_cookie", cmd, url, "--workers", str(WORKERS)]
    for pw in passwords:
        argv += ["--password", pw]
    if cmd == "sort":
        argv += ["--keywords", keywords or "", "-o", str(out_dir)]
    elif cmd == "cookies":
        argv += ["-o", str(out_dir)]
    elif cmd == "ulp":
        argv += ["-o", str(out_dir / "creds.ulp.txt")]
        out_dir.mkdir(parents=True, exist_ok=True)
    return argv


def _zip_dir(src: Path, dest: Path) -> int:
    """Zip ``src`` recursively into ``dest``. Returns total bytes written."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in src.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(src))
    return dest.stat().st_size


class _ProgressMessage:
    """Edit a single chat message with the latest tail of the job's output.

    The CLI emits multi-line ``🌀 Status: ...`` blocks. Whenever we see a
    line starting with ``🌀 Status:`` we treat it as the start of a fresh
    block and discard the previous block, so the chat message stays a
    single live tile.
    """

    MIN_INTERVAL = 2.0  # seconds between edits — Telegram rate-limits hard
    BLOCK_MARKER = "🌀 Status:"
    MAX_BLOCK_LINES = 8
    MAX_FALLBACK_LINES = 12

    def __init__(self, message, header: str) -> None:
        self.message = message
        self.header = header
        self._last = 0.0
        self._tail: List[str] = []
        self._in_block = False
        self._lock = asyncio.Lock()

    async def push(self, line: str) -> None:
        line = line.rstrip()
        if not line:
            return
        if line.startswith(self.BLOCK_MARKER):
            # Start a fresh status block, dropping the previous one.
            self._tail = [line]
            self._in_block = True
        elif self._in_block:
            self._tail.append(line)
            self._tail = self._tail[-self.MAX_BLOCK_LINES :]
        else:
            self._tail.append(line)
            self._tail = self._tail[-self.MAX_FALLBACK_LINES :]
        now = time.monotonic()
        if now - self._last < self.MIN_INTERVAL:
            return
        self._last = now
        async with self._lock:
            await self._render()

    async def finish(self, footer: str = "") -> None:
        async with self._lock:
            await self._render(footer)

    async def _render(self, footer: str = "") -> None:
        body = "\n".join(self._tail) or "(starting…)"
        text = f"{self.header}\n{body}"
        if footer:
            text += f"\n\n{footer}"
        # Plain text — emojis and pipe chars in the new progress block
        # don't need markdown, and skipping it sidesteps BadRequest from
        # stray ``_`` / ``*`` / ``[`` characters in URLs.
        try:
            await self.message.edit_text(text[-3500:])
        except Exception as exc:  # noqa: BLE001 — Telegram edits often race
            log.debug("progress edit failed: %s", exc)


# How long we let the subprocess go without producing any output before
# we treat it as hung and kill it. Each download retry / extraction
# stage prints something well within this window when working normally.
SUBPROC_INACTIVITY_TIMEOUT = float(os.getenv("SUBPROC_INACTIVITY_TIMEOUT", "600"))


async def _stream_proc(proc: asyncio.subprocess.Process, prog: _ProgressMessage) -> None:
    assert proc.stdout is not None
    buf = b""
    while True:
        try:
            chunk = await asyncio.wait_for(
                proc.stdout.read(1024), timeout=SUBPROC_INACTIVITY_TIMEOUT
            )
        except asyncio.TimeoutError:
            log.warning(
                "subprocess produced no output for %.0fs; killing it",
                SUBPROC_INACTIVITY_TIMEOUT,
            )
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await prog.push("🌀 Status: Job aborted")
            await prog.push(
                f"⚠️ No output for {int(SUBPROC_INACTIVITY_TIMEOUT)}s — "
                "the worker was killed."
            )
            break
        if not chunk:
            break
        buf += chunk
        # Strip ANSI cursor moves emitted by the TTY-mode progress
        # printer (``\x1b[6A\x1b[J``); they're meaningless here.
        while True:
            esc = buf.find(b"\x1b[")
            if esc < 0:
                break
            # Find the terminating letter (any byte in @-~ range).
            j = esc + 2
            while j < len(buf) and not (0x40 <= buf[j] <= 0x7E):
                j += 1
            if j >= len(buf):
                break  # incomplete sequence; wait for more data
            buf = buf[:esc] + buf[j + 1 :]
        # Split on whichever of \n or \r appears first — the downloader's
        # in-place progress bar writes \r-separated frames, so we need to
        # treat \r as a real line terminator (not a fallback after \n).
        while True:
            idx_n = buf.find(b"\n")
            idx_r = buf.find(b"\r")
            if idx_n < 0 and idx_r < 0:
                break
            if idx_n < 0:
                idx = idx_r
            elif idx_r < 0:
                idx = idx_n
            else:
                idx = min(idx_n, idx_r)
            line, buf = buf[:idx], buf[idx + 1 :]
            text = line.decode("utf-8", "replace")
            if text.strip():
                await prog.push(text)


async def _run_job(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    keywords: Optional[str],
) -> int:
    # Snapshot the job state but DON'T clear ``user_data`` until the
    # conversation has actually terminated. If we cleared eagerly and a later
    # call raised (Telegram BadRequest, network blip, etc.) the function
    # would never return ``ConversationHandler.END``, leaving the user stuck
    # in ``ASK_PWD`` / ``ASK_KEYWORDS`` with empty user_data — every
    # subsequent message would then ``KeyError`` here.
    cmd = ctx.user_data.get("job_cmd")
    url = ctx.user_data.get("job_url")
    passwords = list(ctx.user_data.get("job_passwords", []))
    if not cmd or not url:
        ctx.user_data.clear()
        await update.message.reply_text(
            "Sorry — job state was lost. Please start over with /sort, "
            "/cookies, or /ulp."
        )
        return ConversationHandler.END

    work = Path(tempfile.mkdtemp(prefix="bot-job-"))
    out_dir = work / "out"
    try:
        progress_msg = await update.message.reply_text(
            f"Job queued: /{cmd} <- {url}"
        )
        prog = _ProgressMessage(
            progress_msg, header=f"Running /{cmd}  (workers={WORKERS})"
        )

        argv = _build_argv(cmd, url, passwords, keywords, out_dir)
        log.info("running: %s", " ".join(argv))
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await _stream_proc(proc, prog)
        rc = await proc.wait()
        if rc != 0:
            await prog.finish(f"Job failed (exit {rc}).")
            return ConversationHandler.END

        if not out_dir.exists():
            await prog.finish("No output produced.")
            return ConversationHandler.END

        zip_path = work / f"{cmd}-result.zip"
        size = _zip_dir(out_dir, zip_path)
        size_mb = size / 1024 / 1024
        if size > DOC_UPLOAD_LIMIT:
            await prog.finish(
                f"Result is {size_mb:.1f} MB which exceeds Telegram's "
                f"{DOC_UPLOAD_LIMIT // 1024 // 1024} MB upload limit. "
                "Re-run with tighter keywords or raise DOC_UPLOAD_LIMIT "
                "behind a self-hosted Bot API server."
            )
            return ConversationHandler.END

        await prog.finish(f"Done — uploading {size_mb:.1f} MB...")
        with open(zip_path, "rb") as fh:
            await update.message.reply_document(
                document=fh,
                filename=zip_path.name,
                caption=f"/{cmd} result ({size_mb:.1f} MB)",
            )
    except Exception:
        # Make absolutely sure we never leave the conversation hanging.
        log.exception("job failed for /%s %s", cmd, url)
        try:
            await update.message.reply_text(
                "Job failed unexpectedly — see worker logs. State has been "
                "reset; start over with /sort, /cookies, or /ulp."
            )
        except Exception:  # noqa: BLE001 — best-effort
            pass
    finally:
        shutil.rmtree(work, ignore_errors=True)
        # Only clear once everything (success or handled error) is done so
        # that any future /cancel still works as expected.
        ctx.user_data.clear()
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Convenience: a plain URL message starts /sort flow
# ---------------------------------------------------------------------------


async def on_plain_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_admin(update.effective_user.id):
        await _reject(update)
        return ConversationHandler.END
    text = (update.message.text or "").strip().split()
    if not text or not is_url(text[0]):
        return ConversationHandler.END
    ctx.args = [text[0]]
    return await cmd_sort(update, ctx)


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------


def build_application() -> Application:
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN is not set. Set it in your Railway env vars (or "
            "locally with `export BOT_TOKEN=...`)."
        )
    if not ADMIN_IDS:
        log.warning(
            "ADMIN_IDS is empty — bot will refuse every user. Set "
            "ADMIN_IDS=<your_user_id> to enable yourself."
        )

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))

    sort_conv = ConversationHandler(
        entry_points=[
            CommandHandler("sort", cmd_sort),
            CommandHandler("cookies", cmd_cookies),
            CommandHandler("ulp", cmd_ulp),
            MessageHandler(filters.TEXT & ~filters.COMMAND, on_plain_url),
        ],
        states={
            ASK_PWD: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_password)],
            ASK_KEYWORDS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_keywords)
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_chat=True,
        per_user=True,
    )
    app.add_handler(sort_conv)
    return app


def main() -> None:
    app = build_application()
    log.info("logs-to-cookie bot v%s starting (admins=%s)", __version__, ADMIN_IDS or "<none>")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
