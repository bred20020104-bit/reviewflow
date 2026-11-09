"""Test-run correlation and safe, observation-only repair diagnostics."""

from reviewflow.backend.test_diagnostics import CATEGORIES, DIAGNOSTIC_STATES, PHASES, TEST_CODES
from reviewflow.report import _markdown as escape_markdown


def compare_runs(previous, current):
    """Compare complete diagnostic sets, not inferred business correctness or counts."""
    result = {"comparable": False, "new_failures": [], "no_longer_reported": []}
    if (previous is None or any(r.get("diagnostics_status") != "complete"
            or r.get("status") not in {"passed", "failed"} for r in (previous, current))):
        return result
    before, after = previous["test_failures"], current["test_failures"]
    result.update(comparable=True,
                  new_failures=[r for r in after if r not in before],
                  no_longer_reported=[r for r in before if r not in after])
    return result


def render_test_runs(runs):
    lines = ["\n## 逐轮测试诊断\n"]
    for number, run in enumerate(runs, 1):
        stage = run.get("stage")
        stage = stage if stage in {"baseline", "seed", "candidate", "final"} else "unknown"
        code = run.get("diagnostic_code")
        code = code if code in TEST_CODES else "unknown"
        state = run.get("diagnostics_status")
        state = state if state in DIAGNOSTIC_STATES else "invalid"
        lines.append(f"- 第 {number} 轮 / {stage}：{code}；详情状态：{state}")
        if code == "test_assertions_failed":
            lines.append("  - 测试执行失败，可能是断言或运行时异常；不能仅凭退出码归因。")
        for failure in run.get("test_failures", ())[:50]:
            if (failure.get("category") not in CATEGORIES or failure.get("phase") not in PHASES
                    or type(failure.get("path")) is not str
                    or type(failure.get("line")) is not int):
                continue
            path = escape_markdown(failure["path"])
            location = failure.get("test_line")
            location = location if type(location) is int else failure["line"]
            lines.append(f"  - {path}:{location} — {failure['phase']} / {failure['category']}"
                         f"；错误位置：{failure['line']}")
        comparison = run.get("comparison", {})
        if number > 1:
            if comparison.get("comparable") is True:
                lines.append(f"  - 相对上一轮：新增诊断 {len(comparison['new_failures'])} 条；"
                             f"未再报告 {len(comparison['no_longer_reported'])} 条。")
            else:
                lines.append("  - 诊断缺失、不完整或执行受阻，无法可靠比较失败变化。")
    lines.append("\n用例以测试定义位置标识，参数化实例不展开。错误摘要仅保留固定类别和位置，"
                 "不保留原始日志、异常消息或断言值。诊断变化不代表业务缺陷已修复，"
                 "错误类别也不单独证明环境限制是根因。\n")
    return "\n".join(lines)
