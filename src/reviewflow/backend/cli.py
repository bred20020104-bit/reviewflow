"""Explicit backend commands. Parsing/help remain stdlib-only and read nothing."""

import argparse
import json
import sys
from pathlib import Path

COMMANDS = {
    "config-check",
    "credentials-check",
    "session-start",
    "session-show",
    "session-resume",
    "session-export",
    "prepare",
    "context",
    "repair-preview",
    "repair-apply",
    "repair-demo",
    "repair-run",
    "evaluate",
    "package",
    "benchmark-demo",
}


def main(argv):
    parser = argparse.ArgumentParser(prog="review", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in sorted(COMMANDS):
        command = commands.add_parser(name, allow_abbrev=False)
        if name.startswith("session-"):
            command.add_argument("-d", "--directory", dest="path", type=Path, required=True)
            command.add_argument("--source", choices=("auto", "head", "directory"), default="auto")
            if name in {"session-start", "session-resume"}:
                command.add_argument(
                    "--demo",
                    action="store_true",
                    help="Required: in-process simulated adapter only",
                )
            if name != "session-start":
                command.add_argument("--session", type=Path, required=True)
            if name in {"session-start", "session-export"}:
                command.add_argument(
                    "--output", type=Path, required=True, help="New directory outside target"
                )
            if name == "session-start":
                command.add_argument(
                    "--mode", choices=("single", "multi", "multi-no-verifier"), default="multi"
                )
            if name == "session-export":
                command.add_argument("--finding", required=True)
        if name == "repair-run":
            command.add_argument("--development", action="store_true")
            command.add_argument("--config", type=Path, required=True)
            command.add_argument("-d", "--directory", dest="path", type=Path, required=True)
            command.add_argument("--include", action="append", required=True)
            command.add_argument("--image", required=True, help="Immutable local image ID or repository digest")
            command.add_argument("--output", type=Path, required=True, help="New directory outside target")
            call_limit = command.add_mutually_exclusive_group()
            call_limit.add_argument("--max-calls", type=int, choices=range(1, 49), default=48)
            call_limit.add_argument("--no-call-limit", action="store_true",
                                    help="Explicitly remove this run's total model-call cap; cost unknown, other limits remain")
            command.add_argument("--transport-retries", type=int, choices=(0, 1, 2), default=2,
                                 help="Retries per request for HTTP408 or explicit timeouts (default: 2; 0 disables)")
            command.add_argument("--approve", help="Exact current preview digest; includes possible writeback")
            command.add_argument("--test-timeout", type=int, choices=range(1, 301), default=60,
                                 help="Approval-bound total container test deadline in seconds (1..300)")
            command.add_argument("--test-sandbox", choices=("strict", "exec-scratch"),
                                 default="strict",
                                 help="Approval-bound sandbox; exec-scratch permits container-only temporary executables")
            command.add_argument("--patch-scheduling", choices=("serial", "grouped"),
                                 default="serial", help="Approval-bound patch scheduling policy")
            command.add_argument("--seed-report", type=Path,
                                 help="Import unverified selected candidate bytes under a fresh approval")
            command.add_argument("--tests-only", action="store_true",
                                 help="Run HEAD tests only; zero model requests and no source writeback")
        if name == "credentials-check":
            command.add_argument(
                "--file", type=Path, required=True, help="Explicit private key TOML"
            )
        if name in {"config-check", "prepare"}:
            command.add_argument(
                "--config",
                type=Path,
                required=True,
                help="Explicit trusted tool TOML; never discovered in target",
            )
        if name in {"prepare", "context", "repair-preview", "repair-apply"}:
            command.add_argument("-d", "--directory", dest="path", type=Path, default=Path("."))
        if name in {"prepare", "context"}:
            command.add_argument("--source", choices=("auto", "head", "directory"), default="auto")
        if name == "context":
            command.add_argument("--query", help="Literal text search; report evidence only")
        if name in {"repair-preview", "repair-apply"}:
            for option in ("task", "finding", "proposal", "workspace-parent"):
                command.add_argument("--" + option, type=Path, required=True)
            command.add_argument("--run-id", required=True)
            if name == "repair-apply":
                command.add_argument(
                    "--approve",
                    required=True,
                    help="Exact digest from reviewed repair-preview output",
                )
        if name in {"repair-demo", "package"}:
            command.add_argument("--output", type=Path, required=True, help="New destination only")
        if name == "package":
            command.add_argument(
                "--project", type=Path, required=True, help="Trusted ReviewFlow root"
            )
        if name == "benchmark-demo":
            command.add_argument("--corpus", type=Path, required=True)
            command.add_argument("--split", choices=("dev", "test"), default="dev")
            command.add_argument(
                "--mode", choices=("single", "multi", "multi-no-verifier"), default="single"
            )
        if name == "evaluate":
            command.add_argument("--manifest", type=Path, required=True)
            command.add_argument("--results", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        if args.command == "repair-run":
            from reviewflow.backend.live_repair_cli import run_live_repair

            return run_live_repair(args)
        result = execute(args)
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".")[0] in {"pydantic", "langgraph", "langsmith"}:
            print(
                "review: install optional dependencies in tool environment: .[review]",
                file=sys.stderr,
            )
            return 2
        raise
    except (ValueError, OSError):
        print(
            "review: configuration/input/approval rejected; check explicit paths, schema, "
            "preview digest and existing workspace. No automatic overwrite or retry.",
            file=sys.stderr,
        )
        return 2
    from reviewflow.errors import CollectionError

    # Collection failures are converted inside execute; no untrusted validation values are printed.
    if isinstance(result, CollectionError):
        print("review: collection failed; snapshot or files changed.", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
    if args.command in {"session-start", "session-resume"}:
        return 0 if result["report"]["status"] == "complete" else 1
    return 0


def execute(args):
    from reviewflow.errors import CollectionError

    try:
        return _execute(args)
    except CollectionError as exc:
        return exc


def _execute(args):
    from reviewflow.backend.approval import ApprovalGate, fingerprint
    from reviewflow.backend.config import LocalSettings, load_config, read_bounded
    from reviewflow.models import ScanPolicy
    from reviewflow.snapshot import build_inventory
    from reviewflow.sources.git import open_source

    if args.command.startswith("session-"):
        from reviewflow.backend.sessions import (
            export_finding,
            load_session,
            resume_session,
            start_session,
        )

        if args.command in {"session-start", "session-resume"} and not args.demo:
            raise ValueError("Explicit demo required; live adapter unavailable")
        source = open_source(args.path, args.source)
        inventory = build_inventory(source, ScanPolicy())
        if args.command == "session-export":
            return export_finding(args.session, source, inventory, args.finding, args.output)
        if args.command == "session-start":
            document = start_session(args.output, source, inventory, mode=args.mode)
        elif args.command == "session-resume":
            document = resume_session(args.session, source, inventory)
        else:
            document = load_session(args.session, source, inventory)
        return document.model_dump(mode="json")
    if args.command == "credentials-check":
        from reviewflow.backend.credentials import read_key_file

        read_key_file(args.file)
        return {
            "status": "configured",
            "network_calls": 0,
            "key_validated_with_provider": False,
            "notice": "Local format check only; Key is never printed or sent.",
        }
    if args.command == "config-check":
        config = load_config(args.config)
        return {
            "status": "valid",
            "provider": config.model.provider,
            "model": config.model.model,
            "upload_enabled_in_config": (
                False if isinstance(config, LocalSettings) else config.privacy.allow_code_upload
            ),
            "credentials_read": isinstance(config, LocalSettings),
            "budget": config.budget.model_dump(mode="json"),
            "pricing_configured": (
                config.budget.input_price_per_million_usd is not None
                and config.budget.output_price_per_million_usd is not None
            ),
            "scope": "selected_directory_included_files"
            if isinstance(config, LocalSettings)
            else "explicit_config",
            "live_adapter_available": False,
            "notice": "Configuration validity is not source-upload approval.",
        }
    if args.command == "repair-demo":
        from reviewflow.backend.demo_repair import run_demo

        return run_demo(args.output)
    if args.command == "benchmark-demo":
        from reviewflow.backend.benchmark import run_benchmark

        return run_benchmark(args.corpus, split=args.split, mode=args.mode)
    if args.command == "evaluate":
        from reviewflow.backend.evaluation import score

        return score(
            json.loads(read_bounded(args.manifest, 2097152)),
            json.loads(read_bounded(args.results, 2097152)),
        )
    if args.command == "package":
        from reviewflow.backend.delivery import package_source

        return package_source(args.project, args.output)
    if args.command in {"repair-preview", "repair-apply"}:
        from reviewflow.backend.repair import PatchProposal, validate_patch
        from reviewflow.backend.workspace import Workspace
        from reviewflow.review.context import ReviewContext
        from reviewflow.review.contracts import Finding, ReviewTask

        source = open_source(args.path, "head")
        inventory = build_inventory(source, ScanPolicy())
        task = ReviewTask.model_validate_json(read_bounded(args.task))
        finding = Finding.model_validate_json(read_bounded(args.finding))
        proposal = PatchProposal.model_validate_json(read_bounded(args.proposal))
        patch = validate_patch(ReviewContext(source, inventory), task, finding, proposal)
        creation = Workspace.creation_intent(source, inventory, args.workspace_parent, args.run_id)
        combined = {
            "creation": creation,
            "patch_sha256": patch.sha256,
            "finding_id": finding.finding_id,
            "validation": "not_run",
        }
        digest = fingerprint(combined)
        if args.command == "repair-preview":
            return {
                **combined,
                "approval_digest": digest,
                "diff": patch.diff,
                "notice": "Review the exact diff. Apply creates a worktree, not a verified fix.",
            }
        if args.approve != digest:
            raise ValueError("Approval digest mismatch")
        gate = ApprovalGate()
        ws = Workspace.create(
            source,
            inventory,
            args.workspace_parent,
            args.run_id,
            gate,
            gate.approve(creation, confirmed=True),
        )
        ws.apply(patch, gate, gate.approve(ws.patch_intent(patch), confirmed=True))
        return {
            "workspace": str(ws.path),
            "patch_sha256": patch.sha256,
            "validation": "not_run",
            "commit_ready": False,
            "target_tests_executed": False,
        }
    config = load_config(args.config) if args.command == "prepare" else None
    source = open_source(args.path, args.source)
    inventory = build_inventory(source, ScanPolicy())
    if config:
        credentials_read = isinstance(config, LocalSettings)
        if credentials_read:
            config = config.bind(source, inventory)
        if source.info.root.resolve() != Path(config.privacy.allowed_project_root).resolve():
            raise ValueError("Configured project mismatch")
        included = {r.path: r.sha256 for r in inventory.snapshot.records if r.status == "included"}
        if not set(config.privacy.allowed_paths) <= included.keys():
            raise ValueError("Configured scope not included")
        return {
            "status": "preview_only",
            "snapshot": inventory.snapshot.snapshot_id,
            "root": str(source.info.root),
            "provider": config.model.provider,
            "model": config.model.model,
            "endpoint": config.model.base_url,
            "allowed_files": {p: included[p] for p in config.privacy.allowed_paths},
            "budget": config.budget.model_dump(mode="json"),
            "network_calls": 0,
            "credentials_read": credentials_read,
            "upload_enabled_in_config": config.privacy.allow_code_upload,
            "pricing_configured": (
                config.budget.input_price_per_million_usd is not None
                and config.budget.output_price_per_million_usd is not None
            ),
            "notice": "Not an authorization token; complete serialized requests need fresh approval.",
        }
    from reviewflow.backend.context_index import ContextIndex

    index = ContextIndex(source, inventory)
    packets = index.search(args.query) if args.query else index.packets
    return {
        "snapshot": inventory.snapshot.snapshot_id,
        "chunks": len(packets),
        "evidence": [p.evidence.model_dump(mode="json") for p in packets],
        "omissions": index.omissions,
        "semantic_review_performed": False,
        "notice": "Local chunk index only; not yet an automatic model retrieval loop.",
    }
