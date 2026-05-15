# logs-to-cookie

Telegram bot that converts log archives into [Netscape cookie files](https://curl.se/docs/http-cookies.html)
through a guided, conversational `/start` flow. Designed to handle
multi-GB stealer-log dumps without ever buffering the full file to
disk, and to chew through several links in parallel.

```
START      User sends /start
INPUT       └─► Bot asks for one or more direct download URLs
                 └─► Bot asks for archive password (or /skip)
                      └─► Bot asks for keywords filter (or /skip)
QUEUE                └─► Job goes into the FIFO queue
PROCESS                  └─► Concurrent chunked downloads (1 MB chunks)
                              └─► Parse & extract cookies in parallel
                                   └─► Convert to Netscape format
                                        └─► One file per cookie set
OUTPUT                                      └─► Bot uploads cookies_result.zip
                                                  (or stops with an error
                                                   if it exceeds the
                                                   Telegram bot upload limit)
FEEDBACK   📋 Queued #N  ⏳ Downloading... ▓▓░░  ⚙ Processing...  🔄 Converting...
            ✅ Done!  /  ❌ Error
```

## The flow

| Phase | What happens |
|---|---|
| **START** | User sends `/start`. The bot greets them and asks for one or more *direct download URLs* to the logs (paste them on separate lines, comma-separated, or whitespace-separated — up to `MAX_LINKS_PER_JOB`, default 10). Per-link passwords can be appended inline with `url\|password`. |
| **INPUT** | The bot collects three things in sequence: the URL list, password(s) (`/skip`, a single password applied to every link without an inline password, or one password per remaining link in order), and an optional keyword filter (skippable). |
| **QUEUE** | The job lands in a FIFO queue. With `MAX_CONCURRENT_JOBS=1` (default), users behind the head see a `📋 Queued — position #N` message that updates as jobs ahead of them finish. They can `/queue` any time to inspect the queue or `/cancel` to drop out. |
| **PROCESS** | The bot streams every URL in 1 MB chunks to a temp file, then sniffs the first few bytes of each to detect zip/7z/rar (so tokenised CDN URLs without `.zip`/`.7z`/`.rar` in the path also work). All URLs are downloaded + extracted in parallel (default 4 workers). Archives are unpacked with the supplied password; every cookie file inside is parsed. Each detected *cookie set* (one per source file) is emitted as its own Netscape `.txt` file. |
| **OUTPUT** | Every output `.txt` is bundled into a single merged `cookies_result.zip` and uploaded as a Telegram document. If the zip exceeds Telegram's 50 MB bot upload limit, the bot stops with a clear error and asks the user to re-run with a stricter keyword filter. |
| **FEEDBACK** | The bot edits a single status message throughout: `📋 Queued — position #N`, `⏳ Downloading...` with a cumulative progress bar across all links, `⚙ Processing...`, `🔄 Converting... (M cookie sets from N links)`, and finally `✅ Done!` or `❌ Error <reason>`. |

You can send `/cancel` at any prompt to abort the current job.

## Commands

### Conversation
| Command | What it does |
|---|---|
| `/start`, `/help` | Welcome card + start the conversation. |
| `/skip` | Skip the current prompt (password or keywords). |
| `/cancel` | Abort the current job (queued **or** in-flight) and reset the conversation. |
| `/queue` | Show the current queue (running + pending) and your position in it. |

#### Per-link passwords

When you have multiple log archives with **different** passwords, you have two options at the URL prompt:

1. **Inline** — append `|password` to each URL (the password may contain spaces, just no pipe characters):

   ```
   https://link1.example/logs.zip|password-for-link-1
   https://link2.example/logs.zip|p4ss with spaces is fine
   https://link3.example/logs.zip
   ```

   Links without an inline password will fall through to the regular password prompt.

2. **At the password prompt** — paste a list of passwords (newline- or comma-separated, in the same order as the URLs that don't have inline passwords). A single password still works as a fan-out for every remaining link.

### Access management
| Command | Who | What it does |
|---|---|---|
| `/redeem <key>` | Anyone | Redeem a one-shot key to add yourself to the VIP allow-list. |
| `/genkey [days] [note...]` | Admin | Mint a fresh redemption key. With no `days` it never expires; with a number it expires that many days from now. Free-form `note` is for your own records. |
| `/rmkey <key>` | Admin | Revoke a key. |
| `/listkeys` | Admin | List every key (active / redeemed / expired). |
| `/addvip <user_id>` | Admin | Add a Telegram user id to the VIP allow-list directly. |
| `/rmvip <user_id>` | Admin | Remove a Telegram user id from the VIP allow-list. |
| `/listvips` | Admin | List every VIP. |

VIPs and keys are persisted to `STATE_PATH` (default `state.json`). On
Railway, mount a Volume and set `STATE_PATH` to a path inside it so
state survives redeploys.

## Run locally

```bash
git clone https://github.com/zyblahblah/logs-to-cookie.git
cd logs-to-cookie
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then edit .env with your real BOT_TOKEN
python bot.py
```

### Archive extractors

The bot **bootstraps its own `7zz`** at runtime on first use: it
downloads the official upstream 7-Zip 26.01 `7zz` binary (~1.5 MB,
MIT-licensed, ships RAR3 / RAR4 / RAR5 codecs in-tree) into
`~/.cache/logs-to-cookie/bin/7zz` and prepends it to the extractor
chain. This means you no longer need any RAR-capable system package
installed — the bot is self-sufficient on any Linux x64 / arm64 host
that can reach `www.7-zip.org`.

If you want to skip the runtime download (e.g. air-gapped host, or
your network blocks egress to `www.7-zip.org`), set
`LOGS_TO_COOKIE_DISABLE_7ZZ_BOOTSTRAP=1` and install the extractors
yourself:

```bash
# Free RAR readers from main / universe (no multiverse needed):
sudo apt-get install -y p7zip-full unrar-free libarchive-tools
# Or, if you can enable multiverse, the proprietary unrar / p7zip-rar:
sudo apt-get install -y p7zip-full p7zip-rar unrar libarchive-tools
```

The bot tries every extractor in turn — runtime-bundled `7zz` first,
then `7z` / `7za` on `$PATH`, then `unrar`, then `unrar-free`, then
`bsdtar` (libarchive). Each candidate is wiped + retried if the
previous one leaves zero-byte placeholders.

Extra env vars for the bootstrap:

- `LOGS_TO_COOKIE_DISABLE_7ZZ_BOOTSTRAP=1` — opt out of the runtime
  download entirely.
- `LOGS_TO_COOKIE_7ZZ_CACHE_DIR=/path` — pin where the binary lives
  (defaults to `$HOME/.cache/logs-to-cookie/bin`, then `/app/.cache/...`,
  then `/tmp/logs-to-cookie-bin`).
- `LOGS_TO_COOKIE_7ZZ_TARBALL_URL=https://...` — override the upstream
  URL (useful for mirrors or local copies).

## Deploy to Railway

1. Fork or link this repo to a Railway project.
2. Open *Variables* and set:
   - `BOT_TOKEN` — token from [@BotFather](https://t.me/BotFather)
   - `ADMIN_IDS` — comma-separated Telegram user IDs allowed to use the bot. Leave empty to allow everyone (not recommended).
3. Deploy. **No extra configuration is needed for RAR support.** The
   bot downloads the upstream `7zz` binary on first start and caches
   it under `/app/.cache/logs-to-cookie/bin/7zz` for the life of the
   deploy. `railpack.json` additionally pre-bundles the binary at
   build time as an optimization (saves the cold-start download) and
   installs `unrar-free` + `libarchive-tools` as belt-and-braces
   fallbacks.

> **Background.** Earlier releases relied on the bundled binary
> being copied into `/app/bin/7zz` at build time via `railpack.json`'s
> `deployOutputs`. That mechanism was fragile — see PR history #34 /
> #35 / #36 — and silently no-ops if Railpack's custom-step / deploy
> input wiring trips on a schema mismatch. The runtime bootstrap is
> now the source of truth: even if the build-time bundle is missing
> or broken, the bot still produces a working `7zz` on first start.

> **`p7zip-full` 16.02** (the version on Debian / Ubuntu / Railway) and
> `unrar-free` 0.0.2 can *not* read every modern RAR5 codec. The
> runtime-bundled `7zz` 26.01 does. If you've turned off the
> bootstrap, expect "Unsupported Method" / "Only plain RAR 2.0
> supported" failures on fresh stealer-log RAR archives.

> **Rotate your token.** Anyone who has seen your bot token can
> control the bot. If you've ever pasted it in chat, run
> `/revoke` → pick the bot → `/token` in BotFather to issue a fresh
> one before deploying.

## Configuration

All knobs live in environment variables (or `.env` for local runs).
See [`.env.example`](.env.example) for the full list:

- `BOT_TOKEN` *(required)* — from BotFather.
- `ADMIN_IDS` — comma-separated list of Telegram user IDs with full admin powers (`/genkey`, `/rmkey`, `/addvip`, `/rmvip`, `/listkeys`, `/listvips`). If unset, the bot is open to **everyone** (backwards-compatible default).
- `DOC_UPLOAD_LIMIT` *(bytes, default 52428800)* — result zips larger than this make the bot stop with a clear error message instead of uploading.
- `MAX_DOWNLOAD_BYTES` *(bytes, default 5368709120)* — refuses inputs larger than this.
- `MAX_LINKS_PER_JOB` *(default 10)* — caps how many URLs a single `/start` can pull.
- `MAX_CONCURRENT_JOBS` *(default 1)* — number of jobs the queue will run in parallel across all users.
- `STATE_PATH` *(default `state.json`)* — JSON file the bot uses to persist VIPs + redemption keys.
- `LOGS_TO_COOKIE_WORKDIR` *(default unset)* — root directory for per-job temp work (downloads, extractions, intermediate cookie files). When unset the bot uses the OS tempdir (e.g. `/tmp`) which on Railway is **ephemeral**: a container restart wipes any in-flight 30 GB download and the user starts over from byte 0. To survive restarts, mount a Railway Volume at e.g. `/data` and set `LOGS_TO_COOKIE_WORKDIR=/data`. The bot then derives a deterministic per-job path (`<root>/jobs/<hash>`) so re-submitting the same URLs after a restart resumes from the partial file via HTTP `Range:` requests instead of starting over.
- `DOWNLOAD_MAX_ATTEMPTS` *(default 10)* — number of reconnects the resumable download retries on a mid-stream failure (`IncompleteRead` / `ChunkedEncodingError` / connection drop) before giving up.
- `DOWNLOAD_RETRY_BASE_DELAY` *(default 2.0)* and `DOWNLOAD_RETRY_BACKOFF_CAP` *(default 60.0)* — exponential backoff between reconnects: real delay = `min(cap, base * 2 ** (attempt - 1))`.

## How it works

1. **Streaming.** `requests.get(stream=True)` pulls 1 MB at a time straight into a temp file on disk — the body is never buffered in RAM and the file is what we sniff for the archive type. Multi-link jobs run several of these streams in parallel via a thread pool (default 4 workers).
2. **Line buffer.** Chunks are decoded as UTF-8 (errors replaced) and split on `\n`. Trailing partial lines are stitched onto the next chunk so cookie rows split across chunk boundaries are never lost.
3. **Detection + extraction.** The first 8 bytes of each saved file are matched against ZIP (`PK\x03\x04` / `PK\x05\x06` / `PK\x07\x08`), 7Z (`7z\xbc\xaf\x27\x1c`) and RAR (`Rar!\x1a\x07`) signatures, so URLs like `https://cdn2.linkforge.xyz/download/AgAD0w22104` (no extension) work just fine. All three archive types are then unpacked with `7z x` — recent p7zip handles RAR4 and RAR5 natively, including encryption. The bot also tries `unrar x` as a last-resort fallback if 7z is unavailable. Either way it fails fast on a wrong password.
4. **Parse.** Every line that matches the 7-column Netscape cookie format (`domain TAB flag TAB path TAB secure TAB expires TAB name TAB value`) is kept. Comments and malformed lines are silently dropped. The `#HttpOnly_` prefix is preserved on the domain column. Cookie files are converted in parallel via a small thread pool.
5. **Filter.** If keywords were provided, a row is only kept when its raw line contains at least one of them (case-insensitive). The same filter is applied to every URL in a multi-link job.
6. **Per-set output.** For archive inputs, every detected cookie file in the archive becomes its own `NNNN_urlMM_<source-path>.txt` Netscape file (so it's obvious which link a cookie set came from in a multi-link job). For plain-text URLs, the entire stream is one cookie set → one output file.
7. **Package.** All output files — across **every** URL in the job — are bundled into a single `cookies_result.zip` with `ZIP_DEFLATED` (`compresslevel=1`).
8. **Deliver.** If `cookies_result.zip` ≤ `DOC_UPLOAD_LIMIT`, the bot uploads it via `bot.send_document()`. Otherwise the bot stops with a clear `❌ Result zip is too large for Telegram` error and asks the user to re-run with a stricter keyword filter.
9. **Queue.** All accepted jobs are funnelled through a single FIFO queue (`pipeline.JobQueue`). `MAX_CONCURRENT_JOBS` caps how many run at once. While a job is queued the bot edits a `📋 Queued — position #N` status message; once it's running the same message switches to live download/extract/convert progress.

## Tests

```bash
pip install -r requirements.txt
pip install -e '.[dev]'   # pytest, pytest-asyncio, ruff
pytest
ruff check .
```

The test suite spins up tiny local HTTP servers (no external network)
to exercise the full chunked-download → parse → convert → zip path.
