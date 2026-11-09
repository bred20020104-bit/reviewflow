"""Shared offline inspection engine and compatible legacy CLI."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from reviewflow.errors import CollectionError, InvalidInputError
from reviewflow.models import ScanPolicy
from reviewflow.report import render_json, render_markdown, render_text, safe_text
from reviewflow.snapshot import build_inventory
from reviewflow.sources.git import open_source


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def add_scan_options(parser: argparse.ArgumentParser, *, default_format: str) -> None:
    parser.add_argument(
        "--source",
        choices=("auto", "head", "directory"),
        default="auto",
        help="auto: committed Git HEAD or ordinary directory; directory: current files",
    )
    parser.add_argument("--format", choices=("text", "json", "markdown"), default=default_format)
    parser.add_argument("--verbose", action="store_true", help="list all files (text format only)")
    defaults = ScanPolicy()
    parser.add_argument("--max-files", type=_positive_int, default=defaults.max_files)
    parser.add_argument("--max-file-bytes", type=_positive_int, default=defaults.max_file_bytes)
    parser.add_argument("--max-total-bytes", type=_positive_int, default=defaults.max_total_bytes)


def run_inspection(args: argparse.Namespace) -> int:
    if args.verbose and args.format != "text":
        print("reviewflow: --verbose requires --format text", file=sys.stderr)
        return 2
    try:
        policy = ScanPolicy(args.max_files, args.max_file_bytes, args.max_total_bytes)
        source = open_source(args.path, args.source)
        inventory = build_inventory(source, policy)
        if args.format == "json":
            report = render_json(inventory)
        elif args.format == "markdown":
            report = render_markdown(inventory)
        else:
            report = render_text(inventory, verbose=args.verbose)
    except InvalidInputError as exc:
        print(f"reviewflow: {safe_text(exc)}", file=sys.stderr)
        return 2
    except (CollectionError, OSError) as exc:
        print(f"reviewflow: {safe_text(exc)}", file=sys.stderr)
        return 1
    print(report)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline, read-only repository inventory.", allow_abbrev=False
    )
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser(
        "inspect", help="Inventory without reviewing or executing code", allow_abbrev=False
    )
    inspect.add_argument("path", type=Path)
    add_scan_options(inspect, default_format="markdown")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    return run_inspection(args)
