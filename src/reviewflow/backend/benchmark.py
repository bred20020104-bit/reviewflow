"""Offline experiment harness rehearsal; synthetic output is never an accuracy result."""

import hashlib
import json
import time
from pathlib import Path

from reviewflow.backend.config import read_bounded
from reviewflow.backend.evaluation import Manifest
from reviewflow.models import ScanPolicy
from reviewflow.review.demo import DemoBackend
from reviewflow.review.workflow import run_review
from reviewflow.snapshot import build_inventory
from reviewflow.sources.directory import DirectorySource


def run_benchmark(corpus: Path, *, split="dev", mode="single"):
    if split not in {"dev", "test"} or mode not in {"single", "multi", "multi-no-verifier"}:
        raise ValueError("Invalid experiment mode or split")
    manifest_bytes = read_bounded(corpus / "manifest.json", 2097152)
    manifest = Manifest.model_validate_json(manifest_bytes)
    groups, ids = {}, set()
    for case in manifest.cases:
        if case.case_id in ids or groups.setdefault(case.group, case.split) != case.split:
            raise ValueError("Duplicate case or grouped split leakage")
        ids.add(case.case_id)
    frozen = json.loads(read_bounded(corpus / "frozen-hashes.json", 2097152))
    runs = []
    for case in manifest.cases:
        if case.split != split:
            continue
        folder = corpus / "cases" / case.case_id
        if folder.is_symlink() or folder.resolve().parent != (corpus / "cases").resolve():
            raise ValueError("Invalid case directory")
        source = DirectorySource(folder)
        inventory = build_inventory(source, ScanPolicy())
        for record in inventory.snapshot.records:
            key = "cases/" + case.case_id + "/" + record.path
            if record.status != "included" or frozen.get(key) != record.sha256:
                raise ValueError("Frozen fixture changed")
        expected = {key for key in frozen if key.startswith("cases/" + case.case_id + "/")}
        if expected != {"cases/" + case.case_id + "/" + r.path for r in inventory.snapshot.records}:
            raise ValueError("Frozen fixture missing files")
        started = time.monotonic()
        # Only target files enter the workflow; manifests/labels/reference fixes never do.
        report = run_review(source, inventory, DemoBackend(), mode=mode)
        runs.append(
            {
                "case_id": case.case_id,
                "snapshot": report.snapshot_id,
                "status": report.status,
                "calls": report.calls,
                "candidate_count": len(report.findings),
                "unreviewed_paths": report.unreviewed_paths,
                "seconds": time.monotonic() - started,
            }
        )
    return {
        "execution": "simulated",
        "mode": mode,
        "split": split,
        "runs": runs,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "total_calls": sum(run["calls"] for run in runs),
        "network_calls": 0,
        "semantic_review_performed": False,
        "target_tests_executed": False,
        "notice": "Pipeline rehearsal only; no precision/recall or real token/cost claims.",
    }
