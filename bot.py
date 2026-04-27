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
    # filters.ALL is used for this state so @mention passwords are delivered.
    # Guard against non-text messages (stickers, photos, etc.).
    if not update.message or not update.message.text:
        await update.message.reply_text(
            "Please send the password as a text message."
        )
        return ASK_PWD
    pwd = update.message.text.strip()
    # Guard: if user accidentally sends a /command here, treat it as cancel.
    if pwd.startswith("/"):
        await update.message.reply_text(
            "Cancelled. Send the command again with the URL."
        )
        ctx.user_data.clear()
        return ConversationHandler.END
    ctx.user_data["job_passwords"] = (
        [] if pwd.lower() in ("", "none", "no", "nil", "n/a", "-") else [pwd]
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


def _zip_dir(
    src: Path,
    dest: Path,
    *,
    on_progress=None,
) -> int:
    """Zip ``src`` recursively into ``dest``. Returns total bytes written.

    Uses ``compresslevel=1`` because the per-file Python overhead
    dominates over compression ratio for many tiny text files (Netscape
    cookies are typically 1-5 KB each and a single big sort job may
    produce tens of thousands of entries). Level 1 is dramatically
    faster than the default level 6 with only a small loss in size.

    ``on_progress(files_done, bytes_in)`` is called after each file so
    the caller can render a heartbeat tile.
    """
    files = [p for p in src.rglob("*") if p.is_file()]
    bytes_in = 0
    with zipfile.ZipFile(
        dest, "w", zipfile.ZIP_DEFLATED, compresslevel=1
    ) as zf:
        for i, p in enumerate(files, 1):
            zf.write(p, p.relative_to(src))
            try:
                bytes_in += p.stat().st_size
            except OSError:
                pass
            if on_progress is not None:
                on_progress(i, len(files), bytes_in)
    return dest.stat().st_size


def _fmt_elapsed(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _fmt_size(n: float) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.2f} MB"
    if n >= 1024:
        return f"{n / 1024:.2f} KB"
    return f"{int(n)} B"


async def _zip_dir_async(src: Path, dest: Path, prog) -> int:
    """Run :func:`_zip_dir` in a thread while emitting periodic
    ``🌀 Status: Packaging result...`` heartbeat blocks to ``prog``.

    The synchronous zip would otherwise block the asyncio event loop,
    keeping the bot from updating the chat tile or responding to other
    users for the entire duration of the zip — which on a job with
    tens of thousands of small Netscape cookie files can run into many
    minutes.
    """
    loop = asyncio.get_running_loop()
    started = time.monotonic()
    state = {"files": 0, "total": 0, "bytes": 0}

    def _on_progress(files_done: int, total_files: int, bytes_in: int) -> None:
        state["files"] = files_done
        state["total"] = total_files
        state["bytes"] = bytes_in

    fut = loop.run_in_executor(
        None, lambda: _zip_dir(src, dest, on_progress=_on_progress)
    )

    async def _heartbeat() -> None:
        # Emit immediately so the tile flips to "Packaging result..."
        # the moment the zip starts, then every 5s until the zip
        # finishes.
        while not fut.done():
            elapsed = time.monotonic() - started
            await prog.push("🌀 Status: Packaging result...")
            if state["total"]:
                await prog.push(
                    f"📦 {state['files']} / {state['total']} files"
                )
            if state["bytes"]:
                await prog.push(f"📡 Read: {_fmt_size(state['bytes'])}")
            await prog.push(f"⏱️ Elapsed: {_fmt_elapsed(elapsed)}")
            try:
                await asyncio.wait_for(asyncio.shield(fut), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except Exception:  # noqa: BLE001
                return

    hb = asyncio.create_task(_heartbeat())
    try:
        size = await fut
    finally:
        try:
            await hb
        except Exception:  # noqa: BLE001
            pass
    return size


class _ProgressMessage:
    """Edit a single chat message with the latest tail of the job's output.

    The CLI emits multi-line ``🌀 Status: ...`` blocks. Whenever we see a
    line starting with ``🌀 Status:`` we treat it as the start of a fresh
    block and discard the previous block, so the chat message stays a
    single live tile.
    """

    # Lower bound between two real edits sent to Telegram. Telegram
    # rate-limits message edits at ~1/s/chat; 2s gives us plenty of
    # headroom.
    MIN_INTERVAL = 2.0
    # Debounce window: we wait this long after the most recent push
    # before sending the edit. The CLI emits a 6-line status block as
    # one ``stream.write`` + ``flush``, but each line lands as a
    # separate ``push()`` here. Without the debounce the very first
    # line of the block would render alone (because ``MIN_INTERVAL``
    # had already elapsed since the last edit) and the remaining 5
    # lines would all be throttled out — exactly the
    # ``🌀 Status: Downloading...`` (and nothing else) symptom.
    DEBOUNCE = 0.4
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
        self._render_task: Optional[asyncio.Task] = None

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
        # Debounce: cancel any in-flight scheduled render and queue a
        # new one. Bursts of pushes (a full block) coalesce into a
        # single Telegram edit.
        if self._render_task and not self._render_task.done():
            self._render_task.cancel()
        self._render_task = asyncio.create_task(self._debounced_render())

    async def _debounced_render(self) -> None:
        try:
            await asyncio.sleep(self.DEBOUNCE)
            now = time.monotonic()
            wait = self.MIN_INTERVAL - (now - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return
        self._last = time.monotonic()
        async with self._lock:
            await self._render()

    async def finish(self, footer: str = "") -> None:
        # Flush any pending debounced render so the final tile reflects
        # the very last block emitted by the worker.
        if self._render_task and not self._render_task.done():
            self._render_task.cancel()
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
SUBPROC_INACTIVITY_TIMEOUT = float(os.getenv("SUBPROC_INACTIVITY_TIMEOUT", "1800"))


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

        if not out_dir.exists() or not any(out_dir.rglob("*")):
            pwd_hint = (
                f" (tried password: `{passwords[0]}`)" if passwords else " (no password)"
            )
            await prog.finish(
                f"❌ No output produced{pwd_hint}.\n\n"
                "Possible causes:\n"
                "• Wrong archive password\n"
                "• Archive is empty or unsupported format\n"
                "• No matching credentials/cookies found\n\n"
                "Start over with /sort, /cookies, or /ulp and supply the correct password."
            )
            return ConversationHandler.END

        zip_path = work / f"{cmd}-result.zip"
        size = await _zip_dir_async(out_dir, zip_path, prog)
        size_mb = size / 1024 / 1024
        if size > DOC_UPLOAD_LIMIT:
            await prog.finish(
                f"Result is {size_mb:.1f} MB which exceeds Telegram's "
                f"{DOC_UPLOAD_LIMIT // 1024 // 1024} MB upload limit. "
                "Re-run with tighter keywords or raise DOC_UPLOAD_LIMIT "
                "behind a self-hosted Bot API server."
            )
            return ConversationHandler.END

        upload_started = time.monotonic()
        upload_done = asyncio.Event()

        async def _upload_heartbeat() -> None:
            # Emit immediately so the tile flips to "Uploading..." the
            # moment we start sending; then refresh every 15s with the
            # elapsed time so a slow Telegram upload doesn't look frozen.
            while not upload_done.is_set():
                elapsed = time.monotonic() - upload_started
                await prog.push("🌀 Status: Uploading...")
                await prog.push(f"📦 {zip_path.name}")
                await prog.push(f"📡 {size_mb:.1f} MB")
                await prog.push(f"⏱️ Elapsed: {_fmt_elapsed(elapsed)}")
                try:
                    await asyncio.wait_for(upload_done.wait(), timeout=15.0)
                except asyncio.TimeoutError:
                    continue

        hb_task = asyncio.create_task(_upload_heartbeat())
        try:
            with open(zip_path, "rb") as fh:
                await update.message.reply_document(
                    document=fh,
                    filename=zip_path.name,
                    caption=f"/{cmd} result ({size_mb:.1f} MB)",
                )
        finally:
            upload_done.set()
            try:
                await hb_task
            except Exception:  # noqa: BLE001
                pass
        await prog.finish(f"Done — sent {size_mb:.1f} MB.")
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
        ],
        states={
            # Use filters.ALL so any text message reaches on_password —
            # including messages with url/mention/hashtag entities which PTB
            # would otherwise not deliver via filters.TEXT.
            ASK_PWD: [MessageHandler(filters.ALL, on_password)],
            ASK_KEYWORDS: [MessageHandler(filters.TEXT, on_keywords)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_chat=True,
        per_user=True,
        allow_reentry=True,
    )
    app.add_handler(sort_conv)
    # on_plain_url is a SEPARATE lower-priority handler registered AFTER the
    # ConversationHandler. PTB processes handlers in registration order and
    # stops at the first match — so while a conversation is active (ASK_PWD /
    # ASK_KEYWORDS), the ConversationHandler consumes the message and
    # on_plain_url never sees it. This was the root cause of the password bug:
    # previously on_plain_url was inside entry_points, which PTB re-evaluates
    # on every message even mid-conversation, hijacking URL/mention passwords
    # back to the start instead of delivering them to on_password.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_plain_url))
    return app


def main() -> None:
    app = build_application()
    log.info("logs-to-cookie bot v%s starting (admins=%s)", __version__, ADMIN_IDS or "<none>")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()