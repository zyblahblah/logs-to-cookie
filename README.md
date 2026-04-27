# logs-to-cookie

A small CLI for triaging credential / cookie log dumps. It walks a folder of
logs, sorts them by domain keyword, and converts them to the two formats
people usually want:

- **ULP** (`URL:USER:PASS`, one per line)
- **Netscape `cookies.txt`** (importable into curl, yt-dlp, browser
  extensions, etc.) — or a normalized JSON array.

> Intended for analyzing data you already own / are authorized to handle (e.g.
> incident response, your own breach exposure checks, account-takeover
> investigations). Don't use it on data you don't have permission to process.

## Telegram bot worker (Railway-ready)

`bot.py` is a Telegram bot that wraps `ulp` / `cookies` / `sort` behind a
chat flow. Send it a direct download URL, answer a couple of prompts
(password, keywords), and it runs the same pipeline and ships the result
back as a zip. A `Procfile` is included so it drops straight into a
Railway *worker* process — same shape as
[`zyblahblah/zyblahblah-ulp-to-combo`](https://github.com/zyblahblah/zyblahblah-ulp-to-combo).

### Deploy on Railway

1. Push this repo (or fork it) and create a new Railway project pointing
   at it.
2. Railway picks up `requirements.txt` (installs `python-telegram-bot`
   plus the local `logs-to-cookie` package) and `Procfile`
   (`worker: python bot.py`).
3. Open the project's *Variables* tab and add:
   * `BOT_TOKEN` — token from `@BotFather` (run `/newbot` if you don't
     have one yet, or `/revoke` → `/token` to rotate an existing one).
   * `ADMIN_IDS` — comma-separated Telegram user IDs allowed to use the
     bot. The bot refuses everyone else. To find your ID, message
     `@userinfobot` once.
   * *(optional)* `WORKERS` — parallel range-split download workers
     (default `4`).
   * *(optional)* `DOC_UPLOAD_LIMIT` — max bytes the bot will upload
     (default `52428800`, i.e. 50 MB — Telegram's standard Bot API
     limit).
4. Hit *Deploy*. Watch the worker logs; you should see
   `logs-to-cookie bot vX.Y.Z starting (admins={...})`.

### Using the bot

In Telegram, message the bot:

```
/start
/sort https://example.com/Black%20Logs.zip
```

It'll prompt for the archive password, then the keywords, then run the
job and reply with `sort-result.zip`.

* `/cookies <url>` — same flow but skips the keyword prompt.
* `/ulp <url>` — same, returns a single `creds.ulp.txt` zipped up.
* Just paste a URL with no command and the bot defaults to `/sort`.
* `/cancel` aborts an in-progress prompt flow.

> ⚠️ Never paste your bot token into a chat or commit it. If you've
> shared the token anywhere, rotate it with `@BotFather` → `/revoke`
> before redeploying.

### Run the bot locally

```bash
pip install -r requirements.txt
export BOT_TOKEN=123456:abcdef...
export ADMIN_IDS=5376199311
python bot.py
```

## Direct download: hand it a URL

Every command (`ulp`, `cookies`, `sort`) accepts an HTTP(S) direct-download
URL in place of a local path or archive. The host downloads the file via a
range-split parallel streamer (and falls back to a single stream if the
server doesn't advertise `Accept-Ranges`), so multi-GB archives can be
processed without first saving them to your phone:

```
python logs_to_cookie.py sort \
    "https://example.com/Black%20Logs.zip" \
    --password "$PW" --keywords netflix,claude --workers 8 -o sorted/
```

In interactive mode, paste the URL at the *"Input path or http(s) URL"*
prompt and you'll be asked how many parallel workers to use (default 4,
set to 1 to force a single stream). The downloaded file lives in a temp
directory that is deleted as soon as processing finishes, so it doesn't
clutter device storage.

The downloader is stdlib-only (`urllib` + a small thread pool), patterned
after the streaming engine in
[`zyblahblah/zyblahblah-ulp-to-combo`](https://github.com/zyblahblah/zyblahblah-ulp-to-combo).

## Sort: per-victim folder, one Netscape file per source cookie file (default)

This is the most common workflow when the input is a stealer dump containing
many per-victim folders, each with several browser-profile cookie files
(`Brave_Default.txt`, `Chrome_Default.txt`, `Brave-Browser_Default_[2F5F].txt`,
…). The `--keywords` are used as a *filter*: every source cookie file with at
least one matching cookie is converted to its own Netscape `cookies.txt`,
written under that victim's output folder. Source files with no matches are
skipped. **This is the default** as of `v0.8.0` — no extra flag needed:

```
python logs_to_cookie.py sort logs.zip --password "$PW" \
    --keywords netflix,claude -o sorted/
```

Produces:
```
sorted/
  ADMIN_@v_d_e_(1)/
    Brave-Browser_Default_[2F5F].txt_82fcdd.txt
    Brave-Browser_Default_[F362].txt_42d2be.txt
    Brave-Default-Cookies.txt_c87543.txt
    Brave_0.txt_954c39.txt
    Brave_Default.txt_78e541.txt
    Chrome_Default.txt_bccb04.txt
    creds.txt
  ADMIN_@v_d_e_(3)/
    Brave_Default.txt_d39f62.txt
    Chrome_Default.txt_5edc7c.txt
    creds.txt
  ...
```

Each generated `*.txt` is a standalone Netscape `cookies.txt` (header +
tab-separated 7-column lines), importable on its own with curl `-b`, yt-dlp,
or any browser cookie-import extension. The trailing 6-character hex hash on
the filename keeps two profiles with the same basename (e.g. multiple
`Cookies.txt` from different browser-profile dirs) from colliding. Folder and
file names are sanitized so that brackets `[]`, parens `()`, dots, hyphens
and underscores are kept; everything else is replaced with `_`. Victims with
no matches at all are skipped entirely.

In interactive mode, pick `3) sort` and just press Enter at *"One folder per
victim with a hit (victim/cookies.txt)? [Y/n]"* — `Y` is the default.

Pass `--no-per-source` if you want the legacy merged
`<keyword>.ulp.txt` + `<keyword>.cookies.txt` layout (everything merged
across victims, grouped by keyword).

## Cookies — one folder per source/victim (default)

By default, the `cookies` command also produces one folder per source. The
`--output` is treated as a directory; each source (victim) gets its own
folder containing a single `cookies.txt` (or `cookies.json`
with `--format json`) for that source only.

```
python logs_to_cookie.py cookies logs.zip --password 1234 -o cookies_out/
```

Produces:
```
cookies_out/
  victim01/
    cookies.txt
  victim02/
    cookies.txt
  victim03/
    cookies.txt
```

In interactive mode, just answer `y` to *"One folder per source/victim?"*.

## Quick start (no CLI flags)

If you don't want to type flags (e.g. running from a mobile launcher, Termux,
or just double-clicking the file), just run it with no arguments and it drops
into an interactive menu:

```
$ python logs_to_cookie.py
====================================================
 logs-to-cookie v0.3.0 — interactive mode
====================================================
 1) ulp      — extract URL:USER:PASS
 2) cookies  — build cookies.txt / JSON
 3) sort     — bucket by keyword (ULP + cookies per keyword)
 q) quit

Choose [1/2/3/q] [3]: 3
Input path (dir, file, or .zip/.rar/.7z): logs.zip
Archive password(s), comma-separated (leave blank if none): 1234
Keywords to sort by (comma-separated, e.g. netflix,spotify,roblox): netflix,spotify
Output directory [sorted]:
  netflix: 42 ulp, 17 cookies
  spotify: 12 ulp,  8 cookies
```

All the same options you'd pass as CLI flags are asked for here; sensible
defaults are offered in `[brackets]` — just press Enter to accept.

## Install

No third-party dependencies — Python 3.9+ stdlib only.

### Single-file (no install)

The whole tool is also packaged as a single self-contained script
[`logs_to_cookie.py`](./logs_to_cookie.py). Just download that one file
and run it:

```bash
curl -sLO https://raw.githubusercontent.com/zyblahblah/logs-to-cookie/main/logs_to_cookie.py
python logs_to_cookie.py --help
```

### As a package

```bash
git clone https://github.com/zyblahblah/logs-to-cookie.git
cd logs-to-cookie
pip install -e .          # installs the `logs-to-cookie` console script
# or
python -m logs_to_cookie --help
```

> All examples below use `python -m logs_to_cookie`. The single-file form is
> identical — just replace it with `python logs_to_cookie.py`.

## Commands

### `ulp` — extract `URL:USER:PASS`

Walks the input directory, reads any password-dump file (`Passwords.txt`,
`All Passwords.txt`, `passwords.log`, etc.) and emits one ULP line per
credential.

```bash
python -m logs_to_cookie ulp /path/to/logs -o creds.txt
python -m logs_to_cookie ulp /path/to/logs --filter netflix,roblox -o creds.txt
```

Supported input shapes:

- Block format (RedLine / Lumma / generic):
  ```
  URL: https://example.com/login
  USER: alice@example.com
  PASS: hunter2
  ```
- `Username:` / `Password:` variant.
- Already-formatted ULP lines (`https://host/path:user:pass`).
- Passwords containing `:` are preserved.

### `cookies` — normalize cookies

Walks the input directory, finds cookie files (Netscape `.txt` or JSON
exports under any folder named `Cookies`) and writes one consolidated file.

```bash
# Netscape cookies.txt (default)
python -m logs_to_cookie cookies /path/to/logs -o cookies.txt
# Filter by domain substring
python -m logs_to_cookie cookies /path/to/logs --filter netflix -o netflix.cookies.txt
# Or JSON
python -m logs_to_cookie cookies /path/to/logs --format json -o cookies.json
```

### `sort` — bucket by keyword

For each keyword, writes `<keyword>.ulp.txt` and `<keyword>.cookies.txt`
into the output directory.

```bash
python -m logs_to_cookie sort /path/to/logs \
    --keywords netflix,spotify,roblox,paypal \
    -o sorted/
```

Output:

```
sorted/
  netflix.ulp.txt
  netflix.cookies.txt
  spotify.ulp.txt
  spotify.cookies.txt
  ...
```

## Password-protected archives

Most logs are shipped as password-protected `.zip` (sometimes `.rar` /
`.7z`) bundles. Pass `--password` and the tool will extract the archive
to a temp dir, recurse into nested archives, and process the result —
no manual unzip step required:

```bash
# single archive
python -m logs_to_cookie sort logs.zip --password "1234" \
    --keywords netflix,spotify -o sorted/

# directory of archives, try multiple passwords in order
python -m logs_to_cookie ulp /downloads/logs/ \
    --password "1234" --password "letmein" -o creds.txt
```

`--password` is repeatable: each is tried until one succeeds. Archives
that none of the passwords unlock are reported on stderr but do not
abort the run.

Format support:

| Format | Requirement |
| ------ | ----------- |
| `.zip` | stdlib (no extra install) |
| `.rar` | `unrar` or `7z` / `7za` on `$PATH` |
| `.7z`  | `7z` / `7za` / `7zz` on `$PATH` |

If a `.rar` / `.7z` is encountered without the matching tool, the
archive is reported in the failure list and skipped.

## Expected log layout

The tool is intentionally lenient. A typical stealer log folder works
out of the box:

```
logs/
  victim_01/
    Passwords.txt
    Cookies/
      Google[Chrome]_Default.txt
      Mozilla[Firefox].json
  victim_02/
    All Passwords.txt
    Cookies/
      ...
```

It also accepts a single file as input (e.g. one big `Passwords.txt`).

## Development

```bash
python -m pytest -q
```
