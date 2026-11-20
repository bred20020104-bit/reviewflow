"""Short CLI, launched by the isolated `review` shell wrapper."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from reviewflow import __version__
from reviewflow.cli import add_scan_options, run_inspection


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from reviewflow.backend.cli import COMMANDS
    from reviewflow.backend.cli import main as backend_main

    if argv and argv[0] in COMMANDS:
        return backend_main(argv)
    parser = argparse.ArgumentParser(
        prog="review",
        allow_abbrev=False,
        description="ReviewFlow repository inventory (no AI review or target tests yet)",
        epilog="Examples: review | review -d /path/to/project | review --source directory --verbose",
    )
    parser.add_argument(
        "-d",
        "--directory",
        dest="path",
        type=Path,
        default=Path("."),
        help="Project directory. Defaults to the current directory; this is not a version selector",
    )
    parser.add_argument("--version", action="version", version=f"ReviewFlow {__version__}")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--demo", action="store_true", help="Offline simulated review; no model call")
    operation.add_argument(
        "--review", action="store_true", help="Explicit live read-only development review; preview, then approve"
    )
    parser.add_argument(
        "--development", action="store_true", help="Allow bounded development testing; HTTP and cost are unknown"
    )
    parser.add_argument("--config", type=Path, help="Trusted tool config. Never discovered inside the target")
    parser.add_argument("--include", action="append", help="Send only this included file; repeat for more")
    parser.add_argument("--output", type=Path, help="A new report directory outside the target")
    parser.add_argument("--approve", help="Approve the bounded run bound to this preview digest")
    parser.add_argument(
        "--max-calls",
        type=int,
        choices=range(1, 9),
        help="Live review only: lower this run's call cap (1-8). Default 8. Included in the preview grant",
    )
    parser.add_argument(
        "--mode",
        choices=("single", "multi", "multi-no-verifier"),
        help="For --demo or --review. Default multi. multi-no-verifier omits verification",
    )
    add_scan_options(parser, default_format="text")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if args.review:
        try:
            from reviewflow.backend.live_review import run_live
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.split(".")[0] in {"pydantic", "langgraph", "langsmith"}:
                print(
                    "review: install optional dependencies in tool environment: .[review]",
                    file=sys.stderr,
                )
                return 2
            raise
        return run_live(args)
    if (
        args.development
        or args.config
        or args.include
        or args.output
        or args.approve
        or args.max_calls is not None
    ):
        print("review: live flags require --review", file=sys.stderr)
        return 2
    if args.mode and not args.demo:
        print("review: --mode requires --demo", file=sys.stderr)
        return 2
    if args.demo:
        if args.verbose:
            print("review: --verbose is only supported for inventory", file=sys.stderr)
            return 2
        try:
            from reviewflow.review.cli import run_demo
        except ModuleNotFoundError as exc:
            if exc.name and exc.name.split(".")[0] in {"pydantic", "langgraph", "langsmith"}:
                print(
                    "review: install optional dependencies in the tool environment: "
                    "pip install -e '.[review]'",
                    file=sys.stderr,
                )
                return 2
            raise
        return run_demo(args)
    return run_inspection(args)


if __name__ == "__main__":
    raise SystemExit(main())
