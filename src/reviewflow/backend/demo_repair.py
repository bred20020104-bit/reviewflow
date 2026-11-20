"""Explicit demo creates its own new fixture; never executes fixture Python."""

import json
from pathlib import Path

from reviewflow.backend.approval import ApprovalGate
from reviewflow.backend.patch_agent import propose_patch
from reviewflow.backend.workspace import Workspace, _git
from reviewflow.models import ScanPolicy
from reviewflow.review.context import ReviewContext
from reviewflow.review.contracts import EvidenceRef, Finding, ReviewTask
from reviewflow.snapshot import build_inventory
from reviewflow.sources.git import GitHeadSource


def run_demo(destination: Path):
    # mkdir is exclusive; existing files, directories and symlinks are never reused.
    destination.mkdir(mode=0o700)
    repo = destination / "project"
    repo.mkdir()
    original = "def increment(value):\n    return value - 1\n"
    (repo / "app.py").write_text(original)
    _git(repo, ["init", "-b", "main"])
    _git(repo, ["add", "--", "app.py"])
    _git(
        repo,
        [
            "-c",
            "user.name=ReviewFlow Demo",
            "-c",
            "user.email=demo@example.invalid",
            "commit",
            "-m",
            "Self-owned offline demo fixture\n\n"
            "Co-Authored-By: Claude Code <noreply@anthropic.com>",
        ],
    )
    source = GitHeadSource(repo)
    inventory = build_inventory(source, ScanPolicy())
    ref = EvidenceRef(
        snapshot_id=inventory.snapshot.snapshot_id,
        path="app.py",
        content_sha256=inventory.snapshot.records[0].sha256,
        start_line=2,
        end_line=2,
    )
    task = ReviewTask(
        task_id="demo-task",
        snapshot_id=ref.snapshot_id,
        role="correctness",
        paths=("app.py",),
        objective="Synthetic fixture demonstration",
    )
    finding = Finding(
        finding_id="demo-finding",
        task_id=task.task_id,
        title="Preset demonstration: increment returns minus one",
        root_cause="Fixed sample, not a model finding",
        trigger="The demonstration requires returning plus one",
        impact="Demonstrates the patch flow only",
        severity="low",
        evidence=(ref,),
    )

    class DemoPatchBackend:
        def complete(self, request):
            return json.dumps(
                {
                    "snapshot_id": ref.snapshot_id,
                    "finding_id": finding.finding_id,
                    "explanation": "Fixed simulated patch; tests were not run",
                    "replacements": [
                        {
                            "path": "app.py",
                            "before_sha256": ref.content_sha256,
                            "content": original.replace(" - 1", " + 1"),
                        }
                    ],
                }
            )

    patch = propose_patch(ReviewContext(source, inventory), task, finding, DemoPatchBackend())
    parent = destination / "workspaces"
    parent.mkdir()
    gate = ApprovalGate()
    creation = Workspace.creation_intent(source, inventory, parent, "demo")
    # The explicit repair-demo command authorizes only this newly created fixture.
    ws = Workspace.create(
        source, inventory, parent, "demo", gate, gate.approve(creation, confirmed=True)
    )
    ws.apply(patch, gate, gate.approve(ws.patch_intent(patch), confirmed=True))
    (destination / "task.json").write_text(task.model_dump_json(indent=2))
    (destination / "finding.json").write_text(finding.model_dump_json(indent=2))
    (destination / "proposal.json").write_text(patch.proposal.model_dump_json(indent=2))
    return {
        "execution": "simulated",
        "workspace": str(ws.path),
        "original": str(repo),
        "attempts": ws.attempts[finding.finding_id],
        "patch_sha256": patch.sha256,
        "diff": patch.diff,
        "validation": "not_run",
        "commit_ready": False,
        "target_tests_executed": False,
        "semantic_review_performed": False,
        "notice": "Offline patch demonstration for the owned sample only. The original file is unchanged. No live review, tests, or push.",
    }
