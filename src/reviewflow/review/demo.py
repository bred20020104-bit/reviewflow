"""Deterministic fixture responses. This is NOT a code analysis model."""

import json

from reviewflow.review.workflow_models import ModelRequest

MARKER = "# REVIEWFLOW_DEMO_FINDING"


class DemoBackend:
    def complete(self, request: ModelRequest) -> str:
        data = json.loads(request.payload)
        if request.stage == "planner":
            result = {
                "tasks": [
                    {
                        "task_id": role,
                        "snapshot_id": data["snapshot_id"],
                        "role": role,
                        "paths": [f["path"] for f in data["files"]],
                        "objective": "离线模拟任务，不进行真实分析",
                    }
                    for role in ("correctness", "security")
                ]
            }
        elif request.stage == "reviewer":
            task = data["task"]
            findings = []
            if task["role"] != "security":
                for packet in data["files"]:
                    lines = packet["text"].split("\n")
                    # Deliberately recognizes a demo marker, not a vulnerability pattern.
                    line = next(
                        (i for i, value in enumerate(lines, 1) if value.strip() == MARKER), None
                    )
                    if line is not None:
                        line += packet["evidence"]["start_line"] - 1
                        ref = {**packet["evidence"], "start_line": line, "end_line": line}
                        findings.append(
                            {
                                "finding_id": f"{task['task_id']}-demo-{len(findings) + 1}",
                                "task_id": task["task_id"],
                                "title": "演示候选（不是模型发现）",
                                "root_cause": "固定演示标记触发，不代表代码存在缺陷",
                                "trigger": "文件包含 REVIEWFLOW_DEMO_FINDING 标记",
                                "impact": "仅展示候选问题与复核流程，无真实风险结论",
                                "severity": "low",
                                "evidence": [ref],
                            }
                        )
            result = {"findings": findings, "reviewed_paths": task["paths"]}
        else:
            result = {
                "finding_id": data["finding"]["finding_id"],
                "verdict": "accepted",
                "rationale": "固定模拟判定，仅演示状态流；不是独立语义验证",
                "evidence": data["finding"]["evidence"],
            }
        return json.dumps(result, ensure_ascii=True)
