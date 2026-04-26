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

## Sort: one folder per victim with a hit (default behavior)

This is the most common workflow when the input is a stealer dump containing
many per-victim folders. The `--keywords` are used as a *filter*: every
victim folder that has at least one match (in its passwords or cookies) gets
its own folder under `--output`, containing a single `cookies.txt` (and
`creds.txt` if there were any matching credentials) for that victim. **This
is the default** as of `v0.7.0` — no extra flag needed:

```
python logs_to_cookie.py sort logs.zip --password "$PW" \
    --keywords netflix,claude -o sorted/
```

Produces:
```
sorted/
  ADMIN_@v_d_e_(1)/
    cookies.txt
    creds.txt
  ADMIN_@v_d_e_(3)/
    cookies.txt
    creds.txt
  ...
```

Each `cookies.txt` is fully importable on its own (curl `-b`, browser cookie
extension, etc.) — one ready-to-use session per hit. Folder names are derived
from the victim folder inside the archive (with filesystem-unsafe characters
sanitized to `_`). Victims with no matches are skipped entirely. A victim's
`cookies.txt` contains every keyword-matching cookie they had (across all
keywords) — it is *not* split per keyword.

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
