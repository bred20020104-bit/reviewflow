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
        description="ReviewFlow 项目文件盘点（尚不执行 AI 审查或目标测试）",
        epilog="示例: review | review -d /path/to/project | review --source directory --verbose",
    )
    parser.add_argument(
        "-d",
        "--directory",
        dest="path",
        type=Path,
        default=Path("."),
        help="项目目录，默认当前目录；不是版本选择",
    )
    parser.add_argument("--version", action="version", version=f"ReviewFlow {__version__}")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--demo", action="store_true", help="离线模拟审查流程，不调用模型")
    operation.add_argument(
        "--review", action="store_true", help="显式真实只读开发审查；先预览再授权"
    )
    parser.add_argument(
        "--development", action="store_true", help="允许有界开发测试，HTTP/金额未知"
    )
    parser.add_argument("--config", type=Path, help="可信工具配置，不从目标自动发现")
    parser.add_argument("--include", action="append", help="只发送指定纳入文件，可重复")
    parser.add_argument("--output", type=Path, help="目标之外的全新报告目录")
    parser.add_argument("--approve", help="批准本次预览摘要绑定的有界运行")
    parser.add_argument(
        "--max-calls",
        type=int,
        choices=range(1, 9),
        help="仅真实开发审查：降低本次调用上限（1–8），默认8；计入预览授权",
    )
    parser.add_argument(
        "--mode",
        choices=("single", "multi", "multi-no-verifier"),
        help="用于 --demo 或 --review；默认 multi；multi-no-verifier 为无复核消融",
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
                    "review: 请在工具环境安装可选依赖：pip install -e '.[review]'", file=sys.stderr
                )
                return 2
            raise
        return run_demo(args)
    return run_inspection(args)


if __name__ == "__main__":
    raise SystemExit(main())
