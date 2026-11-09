"""Explicit live development CLI composition. No target execution or repair authority."""

import json
import os
import re
import sys
from dataclasses import replace
from pathlib import Path

from reviewflow.agents.profiles import load_profiles
from reviewflow.backend.approval import ApprovalGate, Denied, fingerprint
from reviewflow.backend.credentials import resolve_key_file
from reviewflow.backend.development import DevelopmentTransport, load_development_config
from reviewflow.backend.gateway import WorkflowGateway
from reviewflow.backend.responses_adapter import ADAPTER_VERSION, ResponsesAdapter
from reviewflow.errors import CollectionError, InvalidInputError
from reviewflow.models import ScanPolicy
from reviewflow.review.context import ReviewContext
from reviewflow.review.context_selection import context_preflight
from reviewflow.review.interactive import (
    LIVE_REVIEW_POLICY,
    TOOL_INSTRUCTIONS,
    run_interactive_review,
)
from reviewflow.review.report import render_review
from reviewflow.review.workflow_models import RunLimits
from reviewflow.snapshot import build_inventory, compute_snapshot_id
from reviewflow.sources.git import open_source

NOTICE = (
    "真实开发审查：第三方金额未知；HTTP 会明文传输凭据和源码。"
    "不执行目标代码/测试，不修改文件。模型结论仍需核验，不是上线安全保证。"
)


def output_location(path, root):
    path = Path(path).absolute()
    if path.is_symlink():
        raise ValueError("Output symlink")
    result = path.parent.resolve(strict=True) / path.name
    if result.exists() or result == root or root in result.parents:
        raise ValueError("Output must be new and outside target")
    return result


def selected_inventory(inventory, include):
    available = {r.path for r in inventory.snapshot.records if r.status == "included"}
    selected = available if not include else set(include)
    if not selected or not selected <= available or (include and len(selected) != len(include)):
        raise ValueError("Invalid selected scope")
    records = tuple(
        replace(r, status="skipped", reason="not_selected_for_upload")
        if r.status == "included" and r.path not in selected
        else r
        for r in inventory.snapshot.records
    )
    skipped = len(available - selected)
    counts = dict(inventory.coverage.skip_counts)
    if skipped:
        counts["not_selected_for_upload"] = skipped
    snapshot = inventory.snapshot
    return replace(
        inventory,
        snapshot=replace(
            snapshot,
            records=records,
            snapshot_id=compute_snapshot_id(snapshot.source, snapshot.policy, records),
        ),
        coverage=replace(
            inventory.coverage,
            included_files=len(selected),
            included_bytes=sum(r.size_bytes for r in records if r.status == "included"),
            skipped_files=inventory.coverage.skipped_files + skipped,
            skip_counts=counts,
        ),
    )


def make_preview(
    config, source, inventory, output, mode, limits, *, full_snapshot_id=None, profiles=None
):
    profiles = load_profiles() if profiles is None else profiles
    manifest = profiles.manifest()
    output = output_location(output, source.info.root.resolve())
    preview = {
        "operation": "live_development_review",
        "adapter_version": ADAPTER_VERSION,
        "agent_profiles": manifest,
        "prompt_digest": fingerprint(
            {"agent_profiles": manifest, "tool_instructions": TOOL_INSTRUCTIONS}
        ),
        "root": str(source.info.root.resolve()),
        "source_mode": source.info.mode,
        "snapshot": inventory.snapshot.snapshot_id,
        "full_snapshot": full_snapshot_id or inventory.snapshot.snapshot_id,
        "selected_files": {
            r.path: r.sha256 for r in inventory.snapshot.records if r.status == "included"
        },
        "not_selected_or_skipped": [
            {"path": r.path, "reason": r.reason}
            for r in inventory.snapshot.records
            if r.status != "included"
        ],
        "config": config.model_dump(mode="json"),
        "mode": mode,
        "run_limits": limits.model_dump(mode="json"),
        "review_policy": dict(LIVE_REVIEW_POLICY),
        "context_preflight": context_preflight(source, inventory, limits),
        "output": str(output),
        "cost_usd": None,
        "network_requests": 0,
        "notice": NOTICE,
    }
    preview["approval_digest"] = fingerprint(preview)
    return preview


class RunAuthorization:
    """A live in-process bounded policy, not authority reloaded from a saved report."""

    def __init__(self, preview, approved_digest, gate):
        bound = {k: v for k, v in preview.items() if k != "approval_digest"}
        if approved_digest != preview["approval_digest"] or fingerprint(bound) != approved_digest:
            raise Denied("Run approval does not match")
        self.preview = json.loads(json.dumps(preview))
        self.config = json.loads(json.dumps(preview["config"]))
        self.config["privacy"]["allow_code_upload"] = True
        self.gate = gate
        self.issued = 0
        grant = gate.approve(
            {"action": "development_run", "digest": approved_digest}, confirmed=True
        )
        gate.consume(grant, {"action": "development_run", "digest": approved_digest})

    def __call__(self, intent):
        expected_keys = {
            "action",
            "snapshot",
            "files",
            "root",
            "config",
            "payload_sha256",
            "request_bytes",
        }
        files = intent.get("files")
        if (
            set(intent) != expected_keys
            or intent.get("action") != "development_send"
            or intent.get("snapshot") != self.preview["snapshot"]
            or intent.get("root") != self.preview["root"]
            or intent.get("config") != self.config
            or not isinstance(files, dict)
            or not files
            or any(
                self.preview["selected_files"].get(path) != digest for path, digest in files.items()
            )
            or type(intent.get("request_bytes")) is not int
            or not 0 < intent["request_bytes"] <= 65536
            or not isinstance(intent.get("payload_sha256"), str)
            or not re.fullmatch("[0-9a-f]{64}", intent["payload_sha256"])
            or self.issued >= min(8, self.preview["run_limits"]["max_calls"])
        ):
            raise Denied("Request outside approved run")
        token = self.gate.approve(intent, confirmed=True)
        self.issued += 1
        return token


def save_report(directory_fd, name, text):
    fd = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd
    )
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def document_text(document, fmt):
    # The workflow renderer handles model text escaping; accounting is trusted metadata.
    prefix = "# ReviewFlow 真实开发审查\n\n" if fmt == "markdown" else ""
    prefix += (
        f"请求尝试: {document['accounting']['requests_attempted']} / "
        f"{document['preview']['run_limits']['max_calls']}；"
        f"费用: 未知；源码快照未变: {document['source_snapshot_unchanged']}\n\n"
    )
    return prefix + document["rendered_report"]


def run_live(args):
    directory_fd = None
    try:
        if not args.development or args.config is None or args.output is None or args.verbose:
            raise ValueError("Explicit development/config/output required")
        policy = ScanPolicy(args.max_files, args.max_file_bytes, args.max_total_bytes)
        source = open_source(args.path, args.source)
        full_inventory = build_inventory(source, policy)
        inventory = selected_inventory(full_inventory, args.include)
        config = load_development_config(args.config, source, inventory)
        call_cap = args.max_calls if args.max_calls is not None else 8
        if type(call_cap) is not int or not 1 <= call_cap <= 8:
            raise ValueError("Invalid call cap")
        limits = RunLimits(max_calls=call_cap, max_tasks=2, max_findings=8, max_context_bytes=32768)
        mode = args.mode or "multi"
        profiles = load_profiles()
        preview = make_preview(
            config,
            source,
            inventory,
            args.output,
            mode,
            limits,
            full_snapshot_id=full_inventory.snapshot.snapshot_id,
            profiles=profiles,
        )
        if not args.approve:
            print(json.dumps(preview, ensure_ascii=True, indent=2, sort_keys=True))
            return 0
        gate = ApprovalGate(ttl_seconds=600)
        authorize = RunAuthorization(preview, args.approve, gate)
        context = ReviewContext(source, inventory)
        for name in preview["selected_files"]:
            context.read_file(name)
        config = config.model_copy(
            update={"privacy": config.privacy.model_copy(update={"allow_code_upload": True})}
        )
        adapter = ResponsesAdapter(
            config.model, lambda: resolve_key_file(args.config, config.model)
        )
        transport = DevelopmentTransport(config, adapter, gate)
        gateway = WorkflowGateway(source, inventory, transport, adapter.body_size, authorize)
        destination = Path(preview["output"])
        destination.mkdir(mode=0o700)  # Exclusive: approval replay cannot overwrite an old run.
        directory_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        result = run_interactive_review(
            source,
            inventory,
            gateway,
            mode=mode,
            limits=limits,
            live_policy=True,
            profiles=profiles,
        )
        report = replace(
            result.report,
            execution="live_development",
            semantic_review_performed=any(t.status == "completed" for t in result.report.tasks),
        )
        try:
            after = build_inventory(open_source(args.path, args.source), policy)
            unchanged = after.snapshot.snapshot_id == full_inventory.snapshot.snapshot_id
        except (CollectionError, InvalidInputError, OSError, ValueError):
            unchanged = False
        if not unchanged:
            report = replace(
                report,
                status="partial" if report.semantic_review_performed else "failed",
                errors=report.errors + ("final_snapshot_changed_or_unavailable",),
            )
        known_usage = [r["usage"] for r in transport.records if r["usage"] is not None]
        document = {
            "report_type": "live_development_review",
            "preview": preview,
            "report": json.loads(render_review(report, "json")),
            "accounting": {
                "requests_attempted": transport.calls,
                "attempt_count_notice": "Conservative adapter attempts; failed attempts may have sent or billed.",
                "input_tokens_reported": sum(u["input_tokens"] for u in known_usage),
                "output_tokens_reported": sum(u["output_tokens"] for u in known_usage),
                "usage_complete": all(r["usage"] is not None for r in transport.records),
                "cost_usd": None,
                "records": transport.records,
            },
            "source_snapshot_unchanged": unchanged,
            "unchanged_scope": "inventory included files and records; excluded trees not enumerated",
            "target_tests_executed": False,
            "patches_applied": False,
            "retrievals": list(result.retrievals),
            "duplicate_groups": [g.model_dump(mode="json") for g in result.duplicate_groups],
            "notice": NOTICE,
        }
        encoded = json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        save_report(directory_fd, "report.json", encoded)
        markdown = document_text(
            {**document, "rendered_report": render_review(report, "markdown")}, "markdown"
        )
        save_report(directory_fd, "report.md", markdown)
        if args.format == "json":
            print(encoded, end="")
        else:
            print(
                document_text(
                    {**document, "rendered_report": render_review(report, args.format)}, args.format
                )
            )
            print(f"报告目录: {destination}")
        return 0 if report.status == "complete" else 1
    except (ValueError, OSError, CollectionError, InvalidInputError):
        print(
            "review: live development input/configuration/approval/output rejected; "
            "no automatic retry or overwrite. Normal priced mode is not enabled. "
            "See docs/model-setup.md.",
            file=sys.stderr,
        )
        return 2
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
