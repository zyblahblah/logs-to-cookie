"""Command-line entry point for logs-to-cookie."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import tempfile
from pathlib import Path
from typing import List, Tuple

from .cookies import (
    NETSCAPE_HEADER,
    collect_cookies,
    dedupe as dedupe_cookies,
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
        cookies = []
        for root in roots:
            for cookie in collect_cookies(root):
                domain = (cookie.get("domain") or "").lower()
                if filters and not any(flt in domain for flt in filters):
                    continue
                cookies.append(cookie)
        cookies = dedupe_cookies(cookies)

        out_path = Path(args.output)
        if out_path.parent and str(out_path.parent) not in ("", "."):
            out_path.parent.mkdir(parents=True, exist_ok=True)

        if args.format == "netscape":
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(NETSCAPE_HEADER)
                for c in cookies:
                    f.write(to_netscape_line(c) + "\n")
        else:
            out_path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")

        print(f"wrote {len(cookies)} cookies to {out_path}", file=sys.stderr)
        return 0


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
    pc.add_argument("-o", "--output", required=True, help="output file path")
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
    _add_password_arg(ps)
    ps.set_defaults(func=cmd_sort)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
