"""Explicit CLI approval/report boundary for live development repair."""

import json
import os
import sys

from reviewflow.agents.profiles import load_profiles
from reviewflow.backend.approval import ApprovalGate, fingerprint
from reviewflow.backend.candidate_seed import SeedPlan
from reviewflow.backend.credentials import resolve_key_file
from reviewflow.backend.dependency_verification import DEPENDENCY_CODES
from reviewflow.backend.development import DevelopmentLimits, load_development_config
from reviewflow.backend.diagnostics import SAFE_ERROR_CODES
from reviewflow.backend.finding_refresh import REFRESH_CODES
from reviewflow.backend.live_repair import repair_batches, run_repair_session
from reviewflow.backend.live_review import output_location, save_report, selected_inventory
from reviewflow.backend.repair_budget import RepairRunAuthority, make_repair_preview
from reviewflow.backend.repair_candidate import CandidatePlan
from reviewflow.backend.repair_diagnostics import render_test_runs
from reviewflow.backend.responses_adapter import ResponsesAdapter
from reviewflow.backend.volume_runner import VolumeDockerRunner
from reviewflow.backend.worker_progress import safe_progress
from reviewflow.errors import CollectionError, InvalidInputError
from reviewflow.models import ScanPolicy
from reviewflow.review.evidence_errors import EVIDENCE_CODES
from reviewflow.snapshot import build_inventory
from reviewflow.sources.git import open_source


def _markdown(document):
    recoveries = document.get("verification_recoveries", [])
    codes = {f.get("error") for report in
             document.get("initial_reviews", []) + document.get("final_reviews", [])
             for f in report.get("findings", [])}
    reviewer_recoveries = document.get("reviewer_recoveries", [])
    codes.update(t.get("error") for report in
                 document.get("initial_reviews", []) + document.get("final_reviews", [])
                 for t in report.get("tasks", []))
    codes.update(r.get("diagnostic_code") for r in recoveries + reviewer_recoveries)
    safe_codes = EVIDENCE_CODES | {
        "invalid_evidence", "unsupplied_evidence", "invalid_output", "verification_unresolved",
        "recovery_budget_exhausted", "recovery_authorization_stopped",
        "recovery_transport_stopped", "recovery_stopped",
        "reviewer_recovery_authorization_stopped", "reviewer_recovery_transport_stopped",
        "reviewer_recovery_stopped", "finding_evidence_not_reviewed",
    }
    diagnostics = ", ".join(sorted(c for c in codes if c in safe_codes)) or "无"
    recovered = sum(r["status"] == "recovered" for r in recoveries)
    refreshes = document.get("finding_refreshes", [])
    refresh_codes = ", ".join(sorted({r["diagnostic_code"] for r in refreshes
        if r.get("diagnostic_code") in REFRESH_CODES | EVIDENCE_CODES})) or "无"
    resolved = sum(issue.get("status") == "resolved" for r in refreshes for issue in r["issues"])
    dependencies = document.get("dependency_verifications", [])
    dependency_codes = ", ".join(sorted({r["diagnostic_code"] for r in dependencies
        if r.get("diagnostic_code") in DEPENDENCY_CODES | EVIDENCE_CODES})) or "无"
    decided = sum(r.get("status") == "complete" for r in dependencies)
    transport_diagnostics = []
    for record in document["accounting"].get("records", []):
        code = record.get("error_code")
        if type(code) is not str or code not in SAFE_ERROR_CODES:
            continue
        progress = safe_progress(record.get("transport_progress"))
        detail = "阶段诊断不可用"
        if progress is not None:
            event = progress["events"][-1]
            detail = f"最后完成阶段 {event['stage']}（worker 内 {event['elapsed_ms']} ms）"
        recovery = "后续重试已恢复" if record.get("retry_status") == "recovered" else "未恢复"
        transport_diagnostics.append(f"- 传输尝试失败：{code}；{recovery}；{detail}\n")
    seed_accounting = ""
    cumulative = document.get("cumulative_reported_accounting")
    if cumulative is not None:
        seed_accounting = (
            "\n## 显式候选续跑\n"
            "旧候选仅作为未验证源码输入；测试、审查和写回权限均由本轮重新建立。\n"
            "历史用量由旧报告提供，未经认证；不表示旧调用免费或旧权限重新开放。\n"
            f"- 累计请求尝试：{cumulative['requests_attempted']}\n"
            f"- 累计补丁成员尝试：{cumulative['patch_attempts']}\n"
            "- 累计金额：未知。\n"
        )
    records = document["accounting"].get("records", [])
    retry_count = sum(type(r.get("retry_attempt")) is int and r["retry_attempt"] > 0 for r in records)
    retry_limit = (document["preview"].get("transport_retry") or {}).get("max_retries", 0)
    unknown_usage = sum(r.get("usage") is None for r in records)
    cap = document["preview"]["max_calls"]
    cap_label = "不设总模型调用次数上限（其他限制仍保留）" if cap is None else str(cap)
    return (
        "# ReviewFlow 自动修复运行报告\n\n"
        f"- 状态：{document['status']}\n"
        f"- 请求尝试：{document['accounting']['requests_attempted']} / {cap_label}\n"
        "- 金额：未知；没有提交、推送或合并。\n"
        f"- 传输重试：每请求最多 {retry_limit} 次；实际额外尝试 {retry_count} 次，均计入调用次数。\n"
        f"- 用量未知的尝试：{unknown_usage} 次；重试成功也不代表先前请求免费或用量完整。\n"
        f"- 报告目录：`{document['report_directory']}`\n"
        f"- 候选目录：`{document['candidate_path']}`\n"
        f"- 失败阶段：{document.get('failure_stage', '无')}\n"
        f"- 仅测试模式：{document.get('tests_only', False)}\n"
        f"- 证据诊断（含已恢复的原始错误）：{diagnostics}\n"
        f"- 复核纠错：{len(recoveries)} 条记录；恢复成功：{recovered}\n"
        f"- 审查纠错：{len(reviewer_recoveries)} 条记录；恢复成功："
        f"{sum(r.get('status') == 'recovered' for r in reviewer_recoveries)}\n"
        f"- 跨批依赖复核：{len(dependencies)} 次尝试；独立明确结论：{decided}；诊断：{dependency_codes}\n"
        "依赖读取不扩大审查覆盖或修改权限；原始 unresolved 与有效复核结果分别保留于 report.json。\n"
        f"- 补丁后重新审查：{len(refreshes)} 轮；静态确认消除：{resolved} 条；诊断：{refresh_codes}\n"
        "旧发现只有经新证据审查和独立复核后才退出队列；静态消除不代表整轮修复成功。\n\n"
        "验证对象为 HEAD 加候选补丁，不包含原工作区未提交内容。"
        "语义审查只覆盖审批选定源码；测试范围为全部可发现的已提交测试。"
        "不保留单测数量和原始日志，不声称逐项缺陷已有动态复现。\n\n"
        "`report.json` 保存初审、独立复核、补丁尝试、容器退出状态、最终复审和写回结果。"
        "`candidate.diff` 是经所有权检查的候选差异，不代表 blocked 状态已写回原仓库。"
        "出现 partial_application 时保留两处工作区，需要人工检查，不自动清理。\n"
        + "".join(transport_diagnostics)
        + seed_accounting
        + render_test_runs(document["test_runs"])
    )


def run_live_repair(args):
    directory_fd = None
    try:
        if not args.development:
            raise ValueError("Explicit development mode required")
        source = open_source(args.path, "head")
        root = source.info.root.resolve()
        output = output_location(args.output, root)
        destination = output.with_name(output.name + "-candidate")
        plan = CandidatePlan.from_head(source, tuple(args.include), destination)
        if args.no_call_limit and (args.tests_only or args.seed_report is not None):
            raise ValueError("Uncapped calls require fresh repair mode without seed import")
        if args.seed_report is not None and args.tests_only:
            raise ValueError("Tests-only cannot import a candidate seed")
        seed = SeedPlan.load(plan, args.seed_report) if args.seed_report is not None else None
        inventory = selected_inventory(build_inventory(source, ScanPolicy()), tuple(args.include))
        config = load_development_config(args.config, source, inventory)
        config = config.model_copy(update={"limits": DevelopmentLimits(
            max_retries=0 if args.tests_only else args.transport_retries)})
        profiles = load_profiles()
        runner = VolumeDockerRunner(args.image, timeout_seconds=args.test_timeout,
            sandbox_profile=args.test_sandbox,
            executable_paths=tuple(path for path, mode in plan.modes if mode == 0o755))
        preview = make_repair_preview(plan, config, profiles, runner, output, args.max_calls,
                                      tests_only=args.tests_only,
                                      patch_scheduling=args.patch_scheduling, seed=seed,
                                      no_call_limit=args.no_call_limit)
        preview.pop("approval_digest")
        preview["semantic_batches"] = ([] if args.tests_only else
                                       [list(batch) for batch in repair_batches(plan)])
        preview["approval_digest"] = fingerprint(preview)
        if not args.approve:
            print(json.dumps(preview, ensure_ascii=True, indent=2, sort_keys=True))
            return 0
        gate = ApprovalGate(ttl_seconds=3600)
        authority = RepairRunAuthority(preview, args.approve, gate)
        plan.check_origin()
        parent = output.parent.stat()
        if (parent.st_dev, parent.st_ino) != plan.storage_identity:
            raise ValueError("Report parent changed")
        output.mkdir(mode=0o700)
        directory_fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        adapter = (None if args.tests_only else
                   ResponsesAdapter(config.model, lambda: resolve_key_file(args.config, config.model)))
        document = run_repair_session(plan, config, authority, profiles, runner, adapter, seed=seed)
        document["preview"] = preview
        document["report_directory"] = str(output)
        document["report_files"] = {name: str(output / name) for name in
                                    ("report.json", "report.md", "candidate.diff")}
        encoded = json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
        save_report(directory_fd, "report.json", encoded)
        save_report(directory_fd, "report.md", _markdown(document))
        save_report(directory_fd, "candidate.diff", document.get("candidate_diff") or "")
        print(encoded, end="")
        return 0 if document["status"] in {"applied", "no_changes", "tests_passed"} else 1
    except (ValueError, OSError, CollectionError, InvalidInputError):
        print("review: repair input/configuration/approval/output rejected; no automatic retry "
              "or cleanup. Preserve existing report and candidate directories.", file=sys.stderr)
        return 2
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
