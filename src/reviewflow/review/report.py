"""Workflow reports never include raw context packets or provider responses."""

import html
import json
from dataclasses import fields, is_dataclass

from pydantic import BaseModel

from reviewflow.report import safe_text
from reviewflow.review.workflow_models import RunReport

NOTICE = "离线模拟流程：未调用真实模型，候选与判定均为演示数据，不构成代码审查结论。"


def _plain(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def render_review(report: RunReport, fmt: str = "text") -> str:
    live = report.execution == "live_development"
    notice = "真实开发审查：金额未知；模型判定不等于测试验证或上线安全保证。" if live else NOTICE
    if fmt == "json":
        return json.dumps(
            {"report_type": "review_workflow", **_plain(report), "notice": notice},
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
    lines = [
        "ReviewFlow · 真实开发审查" if live else "ReviewFlow · 审查流程演示（模拟）",
        "",
        notice,
        "",
        f"模式      {report.mode}",
        f"流程状态  {report.status}（不是上线安全结论）",
        f"快照      {report.snapshot_id}",
        (
            f"工作流尝试 {report.calls} / {report.limits.max_calls}；网络用量见运行统计"
            if live
            else f"模拟调用  {report.calls} / {report.limits.max_calls}；网络模型调用 0"
        ),
        f"文件      纳入 {len(report.included_paths)}；提供上下文 {len(report.supplied_paths)}；"
        + ("模型自报检查 " if live else "模拟自报检查 ")
        + f"{len(report.reported_reviewed_paths)}；未完整覆盖 {len(report.unreviewed_paths)}",
        f"任务      {len(report.tasks)}；候选 {len(report.findings)}",
        "",
    ]
    for item in report.findings:
        f = item.finding
        verdict = item.verification.verdict if item.verification else "未独立验证"
        lines += [
            f"[{f.severity}] {f.title}",
            f"  来源: {item.role} / {f.task_id}；" + ("判定: " if live else "模拟判定: ") + verdict,
            f"  原因: {f.root_cause}",
            f"  触发: {f.trigger}",
            f"  影响: {f.impact}",
        ]
        for ref in f.evidence:
            lines.append(
                f"  证据: {ref.path}:{ref.start_line}-{ref.end_line} SHA256={ref.content_sha256}"
            )
        if item.verification:
            lines.append(f"  复核: {item.verification.rationale}")
            for ref in item.verification.evidence:
                lines.append(f"  复核证据: {ref.path}:{ref.start_line}-{ref.end_line}")
        if item.error:
            lines.append(f"  复核未完成: {item.error}")
        lines.append("")
    if not report.findings:
        lines.append(
            "没有候选问题；这不表示代码没有问题。"
            if live
            else "没有演示候选；这不表示代码没有问题。"
        )
    for outcome in report.tasks:
        lines.append(
            f"任务 {outcome.task.task_id}: {outcome.status}"
            + (f" ({outcome.error})" if outcome.error else "")
        )
    for path, reason in report.omissions:
        lines.append(f"上下文跳过: {path} ({reason})")
    for path in report.unreviewed_paths:
        lines.append(f"未完整覆盖: {path}")
    for error in report.errors:
        lines.append(f"流程错误: {error}")
    for warning in report.inventory_warnings:
        lines.append(f"盘点提示: {warning}")
    lines.append(
        "未执行目标测试，未修改文件；模型静态判定不等于动态复现。"
        if live
        else "未执行目标测试，未修改文件；模拟结果不可用于准确率或修复有效性评估。"
    )
    text = "\n".join(safe_text(line) for line in lines) + "\n"
    if fmt == "markdown":
        title = "# ReviewFlow 真实开发审查报告" if live else "# ReviewFlow 模拟流程报告"
        return title + "\n\n<pre>" + html.escape(text) + "</pre>\n"
    return text
