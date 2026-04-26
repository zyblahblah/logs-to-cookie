"""Command-line entry point for logs-to-cookie."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import __version__

from .cookies import (
    NETSCAPE_HEADER,
    collect_cookies,
    collect_cookies_with_source,
    dedupe as dedupe_cookies,
    safe_source_name,
    source_name_for,
    to_netscape_line,
)
from .extract import expand_input, is_archive
from .sorter import sort_logs
from .ulp import collect_credentials
from .utils import domain_of


def _open_out(path: str):
    if path == "-":
        return sys.stdout, False
    p = Path(path)
    if p.parent and str(p.parent) not in ("", "."):
        p.parent.mkdir(parents=True, exist_ok=True)
    return open(p, "w", encoding="utf-8"), True


def _resolve_roots(
    stack: contextlib.ExitStack,
    input_path: str,
    passwords: List[str],
) -> Tuple[List[Path], List[Path]]:
    """Resolve ``input_path`` into a list of roots, extracting archives.

    Extraction is triggered if any password is supplied or the input itself
    is an archive file. Archives encountered (top level and nested) are
    extracted into a temporary working directory using each password in
    turn. Returns ``(roots, failures)``.
    """
    inp = Path(input_path)
    if not inp.exists():
        return [], []

    do_extract = bool(passwords) or (inp.is_file() and is_archive(inp))
    if not do_extract:
        return [inp], []

    workdir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="l2c-")))

    inputs: List[Path] = []
    if inp.is_file():
        inputs.append(inp)
    else:
        # Keep the directory itself as a root for any already-extracted files,
        # plus all archives under it for extraction.
        inputs.append(inp)
        for child in inp.rglob("*"):
            if child.is_file() and is_archive(child):
                inputs.append(child)

    return expand_input(inputs, passwords, workdir)


def _report_failures(failures: List[Path]) -> None:
    if not failures:
        return
    print(
        f"warning: {len(failures)} archive(s) could not be extracted "
        f"(wrong password or missing tool):",
        file=sys.stderr,
    )
    for f in failures[:10]:
        print(f"  - {f}", file=sys.stderr)
    if len(failures) > 10:
        print(f"  ... and {len(failures) - 10} more", file=sys.stderr)


def cmd_ulp(args: argparse.Namespace) -> int:
    with contextlib.ExitStack() as stack:
        roots, failures = _resolve_roots(stack, args.input, args.password)
        if not roots and not failures:
            print(f"input not found: {args.input}", file=sys.stderr)
            return 2
        _report_failures(failures)
        filters = [
            f.strip().lower() for f in (args.filter or "").split(",") if f.strip()
        ]
        out, owned = _open_out(args.output)
        seen = set()
        count = 0
        try:
            for root in roots:
                for url, user, pwd, _ in collect_credentials(root):
                    if filters:
                        hay = (url + " " + domain_of(url)).lower()
                        if not any(flt in hay for flt in filters):
                            continue
                    line = f"{url}:{user}:{pwd}"
                    if not args.no_dedupe:
                        if line in seen:
                            continue
                        seen.add(line)
                    out.write(line + "\n")
                    count += 1
        finally:
            if owned:
                out.close()
        print(f"wrote {count} ULP entries to {args.output}", file=sys.stderr)
        return 0


def _write_cookies_file(path: Path, cookies: List[dict], fmt: str) -> None:
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "netscape":
        with open(path, "w", encoding="utf-8") as f:
            f.write(NETSCAPE_HEADER)
            for c in cookies:
                f.write(to_netscape_line(c) + "\n")
    else:
        path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")


def cmd_cookies(args: argparse.Namespace) -> int:
    with contextlib.ExitStack() as stack:
        roots, failures = _resolve_roots(stack, args.input, args.password)
        if not roots and not failures:
            print(f"input not found: {args.input}", file=sys.stderr)
            return 2
        _report_failures(failures)
        filters = [
            f.strip().lower() for f in (args.filter or "").split(",") if f.strip()
        ]
        per_source = getattr(args, "per_source", False)

        if per_source:
            # One sub-folder per source (victim): <out>/<source>/cookies.[txt|json]
            out_dir = Path(args.output)
            out_dir.mkdir(parents=True, exist_ok=True)
            filename = "cookies.txt" if args.format == "netscape" else "cookies.json"

            # Group (src_name -> list of cookies), then dedupe per source.
            groups: dict = {}
            for root in roots:
                for cookie, src_path in collect_cookies_with_source(root):
                    domain = (cookie.get("domain") or "").lower()
                    if filters and not any(flt in domain for flt in filters):
                        continue
                    name = safe_source_name(source_name_for(src_path, root))
                    groups.setdefault(name, []).append(cookie)

            total = 0
            for name in sorted(groups):
                deduped = dedupe_cookies(groups[name])
                if not deduped:
                    continue
                _write_cookies_file(out_dir / name / filename, deduped, args.format)
                total += len(deduped)
                print(
                    f"  {name}: {len(deduped)} cookies -> {out_dir / name / filename}",
                    file=sys.stderr,
                )
            print(
                f"wrote {total} cookies across {len(groups)} source(s) to {out_dir}",
                file=sys.stderr,
            )
            return 0

        cookies: List[dict] = []
        for root in roots:
            for cookie in collect_cookies(root):
                domain = (cookie.get("domain") or "").lower()
                if filters and not any(flt in domain for flt in filters):
                    continue
                cookies.append(cookie)
        cookies = dedupe_cookies(cookies)

        out_path = Path(args.output)
        _write_cookies_file(out_path, cookies, args.format)
        print(f"wrote {len(cookies)} cookies to {out_path}", file=sys.stderr)
        return 0


def _sort_per_source(
    roots: List[Path], out_dir: Path, keywords: List[str]
) -> dict:
    """Per-victim folder, one Netscape file per source cookie file::

        out_dir/
          <victim>/
            <orig_cookie_filename>_<hash6>.txt   # Netscape, one per source
            <orig_cookie_filename>_<hash6>.txt
            creds.txt                            # merged keyword-matching creds

    A source cookie file is included only if at least one cookie in it matches
    a keyword. The hash suffix prevents collisions when multiple source files
    share the same basename (e.g. several ``Cookies.txt`` from different
    browser profiles in the same victim's logs).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    kws_lower = [k.lower() for k in keywords]

    by_file: Dict[Tuple[str, Path], List[dict]] = {}
    by_file_root: Dict[Tuple[str, Path], Path] = {}
    for r in roots:
        for cookie, src_path in collect_cookies_with_source(r):
            domain = (cookie.get("domain") or "").lower()
            if not domain:
                continue
            if not any(k in domain for k in kws_lower):
                continue
            victim = safe_source_name(source_name_for(src_path, r))
            key = (victim, src_path)
            by_file.setdefault(key, []).append(cookie)
            by_file_root.setdefault(key, r)

    cookie_files_written = 0
    cookies_total = 0
    for (victim, src_path), cookies in by_file.items():
        deduped = dedupe_cookies(cookies)
        if not deduped:
            continue
        r = by_file_root[(victim, src_path)]
        try:
            rel = src_path.resolve().relative_to(r.resolve())
            hash_input = str(rel)
        except ValueError:
            hash_input = str(src_path)
        h = hashlib.sha1(hash_input.encode("utf-8")).hexdigest()[:6]
        fname = safe_source_name(f"{src_path.name}_{h}") + ".txt"
        target_dir = out_dir / victim
        target_dir.mkdir(parents=True, exist_ok=True)
        with open(target_dir / fname, "w", encoding="utf-8") as f:
            f.write(NETSCAPE_HEADER)
            for c in deduped:
                f.write(to_netscape_line(c) + "\n")
        cookie_files_written += 1
        cookies_total += len(deduped)

    ulp_buf: Dict[str, List[str]] = {}
    seen_ulp: Dict[str, set] = {}
    for r in roots:
        for url, user, pwd, src_path in collect_credentials(r):
            host = domain_of(url)
            haystack = (url + " " + host).lower()
            if not any(k in haystack for k in kws_lower):
                continue
            victim = safe_source_name(source_name_for(src_path, r))
            line = f"{url}:{user}:{pwd}"
            s = seen_ulp.setdefault(victim, set())
            if line in s:
                continue
            s.add(line)
            ulp_buf.setdefault(victim, []).append(line)

    for victim, lines in ulp_buf.items():
        d = out_dir / victim
        d.mkdir(parents=True, exist_ok=True)
        (d / "creds.txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    victims = set(ulp_buf) | {v for v, _ in by_file}
    return {
        "victims": len(victims),
        "ulp": sum(len(v) for v in ulp_buf.values()),
        "cookie_files": cookie_files_written,
        "cookies": cookies_total,
    }


def cmd_sort(args: argparse.Namespace) -> int:
    with contextlib.ExitStack() as stack:
        roots, failures = _resolve_roots(stack, args.input, args.password)
        if not roots and not failures:
            print(f"input not found: {args.input}", file=sys.stderr)
            return 2
        _report_failures(failures)
        keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
        if not keywords:
            print("provide --keywords", file=sys.stderr)
            return 2

        per_source = getattr(args, "per_source", False)
        if per_source:
            s = _sort_per_source(roots, Path(args.output), keywords)
            print(
                f"  {s['victims']} hit(s), {s['ulp']} ulp, "
                f"{s['cookie_files']} cookie file(s) "
                f"({s['cookies']} cookies)",
                file=sys.stderr,
            )
            return 0

        stats = sort_logs(roots, Path(args.output), keywords)
        for k, (u, c) in stats.items():
            print(f"  {k}: {u} ulp, {c} cookies", file=sys.stderr)
        return 0


def _add_password_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--password",
        action="append",
        default=[],
        help=(
            "Password for archived logs (.zip/.rar/.7z). May be repeated to "
            "try several. Without --password, archives in the input are left "
            "untouched."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="logs-to-cookie",
        description="Sort and convert log dumps into ULP and cookie formats.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pu = sub.add_parser("ulp", help="extract URL:USER:PASS lines from log dumps")
    pu.add_argument("input", help="path to log directory, file, or archive")
    pu.add_argument("-o", "--output", default="-", help="output file (- for stdout)")
    pu.add_argument(
        "--filter",
        help="comma-separated keywords matched against URL/host (substring)",
    )
    pu.add_argument(
        "--no-dedupe",
        action="store_true",
        help="keep duplicate lines (default: dedupe)",
    )
    _add_password_arg(pu)
    pu.set_defaults(func=cmd_ulp)

    pc = sub.add_parser("cookies", help="collect and normalize cookies")
    pc.add_argument("input", help="path to log directory, file, or archive")
    pc.add_argument(
        "-o",
        "--output",
        required=True,
        help=(
            "output file path (default), or output directory when "
            "--per-source is set"
        ),
    )
    pc.add_argument(
        "--format",
        choices=["netscape", "json"],
        default="netscape",
        help="output format (default: netscape cookies.txt)",
    )
    pc.add_argument(
        "--filter",
        help="comma-separated keywords matched against cookie domain (substring)",
    )
    pc.add_argument(
        "--per-source",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "write one folder per source (victim) under --output, each "
            "containing a single cookies.txt/.json for that source. "
            "Default is on; pass --no-per-source for a single merged file."
        ),
    )
    _add_password_arg(pc)
    pc.set_defaults(func=cmd_cookies)

    ps = sub.add_parser(
        "sort",
        help="bucket logs into per-keyword ULP + cookies files",
    )
    ps.add_argument("input", help="path to log directory or archive")
    ps.add_argument("-o", "--output", required=True, help="output directory")
    ps.add_argument(
        "--keywords",
        required=True,
        help="comma-separated keywords (e.g. netflix,spotify,roblox)",
    )
    ps.add_argument(
        "--per-source",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "write one folder per victim with a hit "
            "(<output>/<victim>/cookies.txt + creds.txt) — keywords act "
            "as a filter, not as folders. Default is on; pass "
            "--no-per-source for the legacy merged <keyword>.ulp.txt + "
            "<keyword>.cookies.txt layout."
        ),
    )
    _add_password_arg(ps)
    ps.set_defaults(func=cmd_sort)

    return p


def _ask(prompt: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default or ""
    if not answer and default is not None:
        return default
    return answer


def _ask_passwords() -> List[str]:
    raw = _ask(
        "Archive password(s), comma-separated (leave blank if none)", default=""
    )
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _ask_nonempty(prompt: str, default: Optional[str] = None) -> str:
    while True:
        val = _ask(prompt, default=default)
        if val:
            return val
        print("  (required)")


def _build_args(**kwargs) -> argparse.Namespace:
    ns = argparse.Namespace()
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


def run_interactive() -> int:
    """Menu-driven flow used when the CLI is run with no subcommand."""
    print("=" * 52)
    print(f" logs-to-cookie v{__version__} — interactive mode")
    print("=" * 52)
    print(" 1) ulp      — extract URL:USER:PASS")
    print(" 2) cookies  — build cookies.txt / JSON")
    print(" 3) sort     — bucket by keyword (ULP + cookies per keyword)")
    print(" q) quit")
    print()

    try:
        choice = _ask("Choose [1/2/3/q]", default="3").lower()
    except KeyboardInterrupt:
        print()
        return 0

    if choice in ("q", "quit", "exit"):
        return 0

    try:
        if choice in ("1", "ulp"):
            inp = _ask_nonempty("Input path (dir, file, or .zip/.rar/.7z)")
            passwords = _ask_passwords()
            filt = _ask(
                "Filter keywords (substring, comma-separated; blank=all)",
                default="",
            )
            out = _ask("Output file (- for stdout)", default="creds.ulp.txt")
            return cmd_ulp(
                _build_args(
                    input=inp,
                    output=out,
                    filter=filt or None,
                    no_dedupe=False,
                    password=passwords,
                )
            )

        if choice in ("2", "cookies"):
            inp = _ask_nonempty("Input path (dir, file, or .zip/.rar/.7z)")
            passwords = _ask_passwords()
            filt = _ask(
                "Filter keywords (cookie domain substring; blank=all)", default=""
            )
            fmt_choice = _ask("Format [1=netscape, 2=json]", default="1")
            fmt = "json" if fmt_choice.strip() in ("2", "json") else "netscape"
            per_src_ans = _ask(
                "One folder per source/victim? [Y/n]", default="y"
            ).lower()
            per_source = per_src_ans not in ("n", "no", "0", "false")
            if per_source:
                out = _ask("Output directory", default="cookies_out")
            else:
                default_out = "cookies.txt" if fmt == "netscape" else "cookies.json"
                out = _ask("Output file", default=default_out)
            return cmd_cookies(
                _build_args(
                    input=inp,
                    output=out,
                    format=fmt,
                    filter=filt or None,
                    per_source=per_source,
                    password=passwords,
                )
            )

        if choice in ("3", "sort"):
            inp = _ask_nonempty("Input path (dir, file, or .zip/.rar/.7z)")
            passwords = _ask_passwords()
            keywords = _ask_nonempty(
                "Keywords to sort by (comma-separated, e.g. netflix,spotify,roblox)"
            )
            per_src_ans = _ask(
                "One folder per victim with a hit (victim/cookies.txt)? [Y/n]",
                default="y",
            ).lower()
            per_source = per_src_ans not in ("n", "no", "0", "false")
            out = _ask("Output directory", default="sorted")
            return cmd_sort(
                _build_args(
                    input=inp,
                    output=out,
                    keywords=keywords,
                    per_source=per_source,
                    password=passwords,
                )
            )
    except KeyboardInterrupt:
        print("\naborted")
        return 130

    print(f"unknown choice: {choice!r}")
    return 2


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        return run_interactive()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
