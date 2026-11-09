"""Optional workflow command, imported only after explicit --demo."""

import sys

from reviewflow.errors import CollectionError, InvalidInputError
from reviewflow.models import ScanPolicy
from reviewflow.report import safe_text
from reviewflow.review.demo import DemoBackend
from reviewflow.review.report import render_review
from reviewflow.review.workflow import run_review
from reviewflow.snapshot import build_inventory
from reviewflow.sources.git import open_source


def run_demo(args) -> int:
    try:
        policy = ScanPolicy(args.max_files, args.max_file_bytes, args.max_total_bytes)
        source = open_source(args.path, args.source)
        inventory = build_inventory(source, policy)
        report = run_review(source, inventory, DemoBackend(), mode=args.mode or "multi")
        output = render_review(report, args.format)
    except InvalidInputError as exc:
        print(f"review: {safe_text(exc)}", file=sys.stderr)
        return 2
    except (CollectionError, OSError) as exc:
        print(f"review: {safe_text(exc)}", file=sys.stderr)
        return 1
    print(output, end="" if output.endswith("\n") else "\n")
    return 0 if report.status == "complete" else 1
