# logs-to-cookie

Telegram bot that streams a direct download link **chunk by chunk**,
parses every Netscape cookie line, and exports each match as its own
`cookie_N.txt` inside a single zip — designed to handle multi-GB log
dumps without saving the full file to disk.

```
[Telegram User]
      ↓ /process <url> [filter]
[Python Bot]
      ↓ requests.get(stream=True)
[Chunk → Line Parser]
      ↓ optional keyword filter
[Convert to Netscape]
      ↓ 1 cookie = 1 file
[cookie_1.txt, cookie_2.txt, ...]
      ↓ zip
[cookies_result.zip]
      ↓
[Telegram bot.send_document()]
```

## Commands

| Command | What it does |
|---|---|
| `/start`, `/help` | Welcome card + usage |
| `/process <url> [filter]` | Stream `<url>`, keep cookies whose line contains `[filter]` (case-insensitive). Filter is optional — omit to dump everything. |
| `/cancel` | Abort the current `/process` flow (e.g. while it's asking for a password) |

If `<url>` ends in `.zip`, `.rar`, or `.7z`, the bot will ask for the
archive password after you run `/process`. Send `none` (or just `-`) if
the archive is unencrypted.

### Example

```
/process https://example.com/Black.Logs.zip netflix
→ Archive URL detected. Send the password (or none):
1234
→ 🌀 Status: Downloading archive Black.Logs.zip...
→ 🌀 Status: Extracting Black.Logs.zip...
→ 🌀 Status: Scanning extracted files...
→ 🌀 Status: Packaging 184 cookie file(s)...
→ 📤 Uploading result...
→ ✅ Done — sent 1.34 MB (184 cookies).
```

## How it works

1. **Streaming.** `requests.get(stream=True)` pulls the file 64 KB at a
   time. For plain text URLs nothing is buffered to disk; for archive
   URLs the file is streamed straight into a temp file.
2. **Line buffer.** Chunks are decoded as UTF-8 (errors replaced) and
   split on `\n`. A trailing partial line is stitched onto the next
   chunk so a cookie split across chunk boundaries is never lost.
3. **Filter.** If you passed `[filter]`, the line must contain it
   (case-insensitive) before being parsed.
4. **Parse.** Lines that match the 7-column Netscape cookie format
   (`domain TAB flag TAB path TAB secure TAB expires TAB name TAB
   value`) are kept. Comments and malformed lines are skipped silently.
5. **Per-item output.** Every kept cookie becomes its own
   `cookie_N.txt` containing the standard Netscape header and one
   cookie row.
6. **Package.** All `cookie_N.txt` files are bundled into
   `cookies_result.zip` with `ZIP_DEFLATED` (`compresslevel=1`) and
   sent back via `bot.send_document()`.

## Run locally

```bash
git clone https://github.com/zyblahblah/logs-to-cookie.git
cd logs-to-cookie
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then edit .env with your real BOT_TOKEN
python bot.py
```

## Deploy to Railway

1. Fork or link this repo to a Railway project.
2. Open *Variables* and set:
   - `BOT_TOKEN` — token from [@BotFather](https://t.me/BotFather)
   - `ADMIN_IDS` — your Telegram user ID (comma-separated for multiple)
3. Deploy. Railway runs `worker: python bot.py` (see `Procfile`).
   `nixpacks.toml` installs `p7zip` and `unrar` so encrypted archives
   work out of the box.

> **Rotate your token.** Anyone who has seen your bot token can
> control the bot. If you've ever pasted it in chat, run
> `/revoke` → pick the bot → `/token` in BotFather to issue a fresh
> one before deploying.

## Tests

```bash
pip install -r requirements.txt pytest
pytest
```
