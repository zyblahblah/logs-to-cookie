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

## Install

No third-party dependencies — Python 3.9+ stdlib only.

```bash
git clone https://github.com/zyblahblah/logs-to-cookie.git
cd logs-to-cookie
pip install -e .
```

Or run directly without installing:

```bash
python -m logs_to_cookie --help
```

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
