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
                        "objective": "Offline simulated task; no real analysis",
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
                                "title": "Demonstration candidate (not a model finding)",
                                "root_cause": "Triggered by a fixed demonstration marker; this does not mean the code is defective",
                                "trigger": "The file contains the REVIEWFLOW_DEMO_FINDING marker",
                                "impact": "Shows the candidate and verification flow only; no real risk conclusion",
                                "severity": "low",
                                "evidence": [ref],
                            }
                        )
            result = {"findings": findings, "reviewed_paths": task["paths"]}
        else:
            result = {
                "finding_id": data["finding"]["finding_id"],
                "verdict": "accepted",
                "rationale": "Fixed simulated verdict for the status flow only; not an independent semantic check",
                "evidence": data["finding"]["evidence"],
            }
        return json.dumps(result, ensure_ascii=True)
