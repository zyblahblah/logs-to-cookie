# logs-to-cookie

Telegram bot that converts log archives into [Netscape cookie files](https://curl.se/docs/http-cookies.html)
through a guided, conversational `/start` flow. Designed to handle
multi-GB stealer-log dumps without ever buffering the full file to
disk.

```
START      User sends /start
INPUT       └─► Bot asks for direct download URL
                 └─► Bot asks for archive password (or /skip)
                      └─► Bot asks for keywords filter (or /skip)
PROCESS              └─► Chunked download (64 KB at a time)
                          └─► Parse & extract cookies
                               └─► Convert to Netscape format
                                    └─► One file per cookie set
OUTPUT                                  └─► Bot uploads cookies_result.zip
                                              (or stops with an error
                                               if it exceeds the
                                               Telegram bot upload limit)
FEEDBACK   ⏳ Downloading... ▓▓░░  ⚙ Processing...  🔄 Converting...
            ✅ Done!  /  ❌ Error
```

## The flow

| Phase | What happens |
|---|---|
| **START** | User sends `/start`. The bot greets them and asks for a *direct download URL* to the logs. |
| **INPUT** | The bot collects three things in sequence: the URL, an archive password (skippable), and an optional keyword filter (skippable). |
| **PROCESS** | The bot streams the URL in 64 KB chunks. Archive URLs (`.zip`, `.7z`, `.rar`) are extracted with the supplied password; every cookie file inside is parsed. Each detected *cookie set* (one per source file) is emitted as its own Netscape `.txt` file. |
| **OUTPUT** | Every output `.txt` is bundled into `cookies_result.zip` and uploaded as a Telegram document. If the zip exceeds Telegram's 50 MB bot upload limit, the bot stops with a clear error and asks the user to re-run with a stricter keyword filter. |
| **FEEDBACK** | The bot edits a single status message throughout: `⏳ Downloading...` with a progress bar, `⚙ Processing...`, `🔄 Converting...`, and finally `✅ Done!` or `❌ Error <reason>`. |

You can send `/cancel` at any prompt to abort the current job.

## Commands

| Command | What it does |
|---|---|
| `/start`, `/help` | Welcome card + start the conversation. |
| `/skip` | Skip the current prompt (password or keywords). |
| `/cancel` | Abort the current job and reset the conversation. |

## Run locally

```bash
git clone https://github.com/zyblahblah/logs-to-cookie.git
cd logs-to-cookie
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then edit .env with your real BOT_TOKEN
python bot.py
```

You'll need `7z` (from `p7zip`) and `unrar` on your `$PATH` for
encrypted archive extraction. On Debian/Ubuntu:

```bash
sudo apt-get install -y p7zip-full unrar
```

## Deploy to Railway

1. Fork or link this repo to a Railway project.
2. Open *Variables* and set:
   - `BOT_TOKEN` — token from [@BotFather](https://t.me/BotFather)
   - `ADMIN_IDS` — comma-separated Telegram user IDs allowed to use the bot. Leave empty to allow everyone (not recommended).
3. Deploy. Railway runs `worker: python bot.py` (see `Procfile`). `nixpacks.toml` installs `p7zip` and `unrar` so encrypted archives work out of the box.

> **Rotate your token.** Anyone who has seen your bot token can
> control the bot. If you've ever pasted it in chat, run
> `/revoke` → pick the bot → `/token` in BotFather to issue a fresh
> one before deploying.

## Configuration

All knobs live in environment variables (or `.env` for local runs).
See [`.env.example`](.env.example) for the full list:

- `BOT_TOKEN` *(required)* — from BotFather.
- `ADMIN_IDS` — comma-separated allow-list of Telegram user IDs.
- `DOC_UPLOAD_LIMIT` *(bytes, default 52428800)* — result zips larger than this make the bot stop with a clear error message instead of uploading.
- `MAX_DOWNLOAD_BYTES` *(bytes, default 5368709120)* — refuses inputs larger than this.

## How it works

1. **Streaming.** `requests.get(stream=True)` pulls 64 KB at a time. Plain `.txt` URLs are decoded and line-buffered straight into the parser; archive URLs are streamed straight into a temp file before extraction.
2. **Line buffer.** Chunks are decoded as UTF-8 (errors replaced) and split on `\n`. Trailing partial lines are stitched onto the next chunk so cookie rows split across chunk boundaries are never lost.
3. **Extraction.** `.zip`/`.7z` archives are unpacked with `7z x` (handles encryption); `.rar` archives with `unrar x` (handles encryption). The bot fails fast on a wrong password.
4. **Parse.** Every line that matches the 7-column Netscape cookie format (`domain TAB flag TAB path TAB secure TAB expires TAB name TAB value`) is kept. Comments and malformed lines are silently dropped. The `#HttpOnly_` prefix is preserved on the domain column.
5. **Filter.** If keywords were provided, a row is only kept when its raw line contains at least one of them (case-insensitive).
6. **Per-set output.** For archive inputs, every detected cookie file in the archive becomes its own `NNNN_<source-path>.txt` Netscape file. For plain-text URLs, the entire stream is one cookie set → one output file.
7. **Package.** All output files are bundled into `cookies_result.zip` with `ZIP_DEFLATED` (`compresslevel=1`).
8. **Deliver.** If `cookies_result.zip` ≤ `DOC_UPLOAD_LIMIT`, the bot uploads it via `bot.send_document()`. Otherwise the zip is copied into the built-in file host's spool directory and the user gets a tokenised direct download URL valid for `RESULT_TTL_SECONDS`.

## Tests

```bash
pip install -r requirements.txt pytest
pytest
```

The test suite spins up tiny local HTTP servers (no external network)
to exercise the full chunked-download → parse → convert → zip path.
