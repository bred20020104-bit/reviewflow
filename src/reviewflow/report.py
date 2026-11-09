"""Render metadata only; filenames and repository metadata are untrusted text."""

import html
import json
import unicodedata
from dataclasses import asdict

from reviewflow.models import Inventory

NOTICE = "Inventory only: no semantic code review or target tests have been performed."


def safe_text(value: object) -> str:
    """Make control and formatting characters visible rather than executable."""
    text = str(value)
    return "".join(
        char.encode("unicode_escape").decode("ascii")
        if unicodedata.category(char).startswith("C") or char in "  "
        else char
        for char in text
    )


def _markdown(value: object) -> str:
    text = "".join(
        f"&#{ord(char)};" if char in "\\`|*[]{}()!#~" else html.escape(char, quote=True)
        for char in safe_text(value)
    )
    # Visible newline escapes are ordinary text, never actual table row breaks.
    return text.replace("&#92;n", "\\n").replace("&#92;r", "\\r").replace("&#92;t", "\\t")


def render_json(inventory: Inventory) -> str:
    data = asdict(inventory)
    source = data["snapshot"]["source"]
    source["root"] = str(inventory.snapshot.source.root)
    source["git_repair_eligible"] = inventory.snapshot.source.git_repair_eligible
    data["notice"] = NOTICE
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True)


def _size(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f} KiB"
    return f"{count / (1024 * 1024):.1f} MiB"


def render_text(inventory: Inventory, *, verbose: bool = False) -> str:
    snapshot, coverage = inventory.snapshot, inventory.coverage
    source = snapshot.source
    origin = (
        f"已提交 HEAD ({source.commit})；未提交修改不纳入"
        if source.mode == "head"
        else "当前文件（包括未提交修改；无 Git 修复资格）"
    )
    lines = [
        "ReviewFlow · 项目文件盘点",
        "",
        f"项目      {safe_text(source.root)}",
        f"来源      {origin}",
        "结果      盘点完成，不代表代码审查通过",
        "",
        f"纳入      {coverage.included_files} 个文件 · {_size(coverage.included_bytes)}",
        f"跳过      {coverage.skipped_files} 个文件/条目 · {coverage.excluded_directories} 个目录（内部文件数未知）",
        f"预算      {'已达到限制，覆盖不完整' if coverage.budget_limited else '未达到限制'}",
        f"上限      {snapshot.policy.max_files} 个文件 · 单文件 {_size(snapshot.policy.max_file_bytes)} · 总计 {_size(snapshot.policy.max_total_bytes)}",
        f"快照      {snapshot.snapshot_id}",
        "",
        "注意",
        "  · 尚未进行代码逻辑审查或执行目标测试",
    ]
    lines.extend(f"  · {safe_text(warning)}" for warning in snapshot.warnings)
    if verbose:
        for title, status in (("纳入文件", "included"), ("跳过条目", "skipped")):
            lines += ["", title]
            for record in snapshot.records:
                if record.status == status:
                    lines.append(f"  {safe_text(record.path)}")
                    lines.append(
                        f"    类型={record.kind} 字节={record.size_bytes} 原因={record.reason or '—'}"
                    )
                    if record.sha256:
                        lines.append(f"    SHA-256={record.sha256}")
    else:
        lines += ["", "使用 --verbose 查看完整文件清单。"]
    lines += ["", NOTICE]
    return "\n".join(lines)


def render_markdown(inventory: Inventory) -> str:
    snapshot, coverage = inventory.snapshot, inventory.coverage
    source = snapshot.source
    lines = ["# Repository inventory", "", NOTICE, "", "## Source", ""]
    for label, value in (
        ("Mode", source.mode),
        ("Root", source.root),
        ("Commit", source.commit),
        ("Branch", source.branch),
        ("Dirty", source.dirty),
        ("Git repair eligible (source only; repair not implemented)", source.git_repair_eligible),
        ("Snapshot ID", snapshot.snapshot_id),
        ("Schema version", snapshot.schema_version),
    ):
        lines.append(
            f"- {label}: {_markdown(value if value is not None else 'unknown / not applicable')}"
        )
    lines += ["", "## Coverage and limits", ""]
    for label, value in (
        ("Included files", coverage.included_files),
        ("Included bytes", coverage.included_bytes),
        ("Skipped files / non-directory entries", coverage.skipped_files),
        ("Excluded directories (inner file counts unknown)", coverage.excluded_directories),
        ("Budget limited", coverage.budget_limited),
        ("Semantic review performed", coverage.semantic_review_performed),
        ("Target tests executed", coverage.target_tests_executed),
        ("Maximum included files", snapshot.policy.max_files),
        ("Maximum bytes per file", snapshot.policy.max_file_bytes),
        ("Maximum included bytes", snapshot.policy.max_total_bytes),
    ):
        lines.append(f"- {label}: {value}")
    for reason, count in sorted(coverage.skip_counts.items()):
        lines.append(f"- Skipped ({_markdown(reason)}): {count}")
    lines += ["", "## Warnings", ""]
    lines.extend(f"- {_markdown(warning)}" for warning in snapshot.warnings)
    if not snapshot.warnings:
        lines.append("- None. This does not imply the code is safe.")
    for heading, status in (("Included files", "included"), ("Skipped entries", "skipped")):
        lines += [
            "",
            f"## {heading}",
            "",
            "| Path | Kind | Bytes | Reason | SHA-256 |",
            "| --- | --- | ---: | --- | --- |",
        ]
        for record in snapshot.records:
            if record.status == status:
                values = (record.path, record.kind, record.size_bytes, record.reason, record.sha256)
                lines.append(
                    "| " + " | ".join(_markdown(v) if v is not None else "—" for v in values) + " |"
                )
    return "\n".join(lines)
