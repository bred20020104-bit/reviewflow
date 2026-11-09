"""Unresolved reviewer recovery on owned fixtures, never target acceptance."""
import json

import pytest
from test_dependency_verification import setup_cross_batch
from test_grouped_repair import GroupAdapter, setup_group

from reviewflow.backend.approval import ApprovalGate, Denied, fingerprint
from reviewflow.backend.live_repair import run_repair_session
from reviewflow.backend.repair_budget import RepairRunAuthority, make_repair_preview
from reviewflow.backend.transport import Reply, Usage


class RecoveringReviewer(GroupAdapter):
    def __init__(self, model, mode="resolved"):
        super().__init__(model)
        self.recovery_mode = mode
        self.reassessments = []

    def __call__(self, wire, **kwargs):
        req = json.loads(wire)
        data = req["payload"]
        if data.get("reviewer_dependency_reassessment"):
            self.sent.append(wire)
            self.reassessments.append(data)
            dependency = any(p["evidence"]["path"] == "src/b.py" for p in data["files"])
            if self.recovery_mode == "read_loop" or (not dependency and self.recovery_mode != "no_read"):
                path = "private.py" if self.recovery_mode == "outside" else "src/b.py"
                value = {"result": None, "requests": [{"kind": "read", "path": path,
                                                      "start_line": 1, "end_line": 1}]}
            else:
                ref = next(p["evidence"] for p in data["files"] if p["evidence"]["path"] == "a.py")
                if self.recovery_mode == "hash":
                    ref["content_sha256"] = "0" * 64
                elif self.recovery_mode == "foreign_evidence":
                    ref = next(p["evidence"] for p in data["files"] if p["evidence"]["path"] == "src/b.py")
                value = {"result": {"finding_id": data["historical_finding"]["finding_id"],
                    "status": ("unresolved" if self.recovery_mode == "unresolved" else
                               "present" if self.recovery_mode == "present" else "resolved"),
                    "evidence": [ref], "rationale": "Synthetic current dependency inspected"}, "requests": []}
            return Reply(json.dumps(value), None if self.recovery_mode == "unknown_usage" else Usage(10, 5))
        if data.get("dependency_verification"):
            self.sent.append(wire)
            dependency = any(p["evidence"]["path"] == "src/b.py" for p in data["files"])
            result, requests = None, [{"kind": "read", "path": "src/b.py", "start_line": 1, "end_line": 1}]
            if dependency:
                result = {"finding_id": data["finding"]["finding_id"],
                    "verdict": "accepted" if self.recovery_mode in {"disagree", "present"} else "rejected",
                    "evidence": [data["files"][0]["evidence"]], "rationale": "Independent fixture inspection"}
                requests = []
            return Reply(json.dumps({"result": result, "requests": requests}), Usage(10, 5))
        reply = super().__call__(wire, **kwargs)
        if req["stage"] == "reviewer" and "refresh" in data:
            value = json.loads(reply.text)
            if value["assessments"]:
                value["assessments"][0].update(status="unresolved", evidence=[],
                    rationale="Need current dependency context")
            return Reply(json.dumps(value), reply.usage)
        return reply


def test_unresolved_reviewer_identity_survives_without_recovery_scope(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_group(git_repo, tmp_path)
    adapter = RecoveringReviewer(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked" and result["failure_stage"] == "finding_refresh"
    assessment = result["finding_refreshes"][0]["reviewer_assessments"][0]
    assert assessment["task_id"] == "correctness"
    assert assessment["finding_id"] == "synthetic-bug"
    assert assessment["status"] == "unresolved"
    assert assessment["rationale"] == "Need current dependency context"
    assert not adapter.reassessments and result["application"] is None


def run_cross(git_repo, tmp_path, mode="resolved", cap=48):
    plan, config, profiles, runner, _, _ = setup_cross_batch(git_repo, tmp_path)
    preview = make_repair_preview(plan, config, profiles, runner, tmp_path.parent / "report", cap,
                                  patch_scheduling="grouped")
    authority = RepairRunAuthority(preview, preview["approval_digest"], ApprovalGate())
    adapter = RecoveringReviewer(config.model, mode)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    return result, adapter


def test_reviewer_reads_dependencies_then_requires_independent_verification(git_repo, tmp_path):
    result, adapter = run_cross(git_repo, tmp_path)
    assert result["status"] == "applied", json.dumps(result)
    assert len(adapter.reassessments) == 2
    assert all("evidence" not in p["historical_finding"] for p in adapter.reassessments)
    entry = result["finding_refreshes"][0]
    assert entry["reviewer_assessments"][0]["status"] == "unresolved"
    recovery, = entry["reviewer_reassessments"]
    assert recovery["status"] == "complete" and recovery["requests_attempted"] == 2
    assert recovery["assessment"]["status"] == "resolved"
    assert result["dependency_verifications"][0]["verification"]["verdict"] == "rejected"
    assert entry["report"]["included_paths"] == ["a.py"]
    assert (git_repo / "a.py").read_text() == "fixed\n"
    assert len(result["patch_attempts"]) == 2
    assert result["accounting"]["requests_attempted"] == len(adapter.sent)


@pytest.mark.parametrize("mode", ["no_read", "outside", "hash", "foreign_evidence", "unresolved", "disagree", "unknown_usage"])
def test_reviewer_recovery_cannot_bypass_evidence_independence_or_transport(git_repo, tmp_path, mode):
    result, adapter = run_cross(git_repo, tmp_path, mode)
    assert adapter.reassessments, json.dumps(result)
    assert result["status"] == "blocked" and result["application"] is None
    assert result["failure_stage"] == "finding_refresh"
    assert (git_repo / "a.py").read_text() == "old\n"
    if mode == "unknown_usage":
        assert len(adapter.reassessments) == 1 and not result["accounting"]["usage_complete"]
    if mode == "disagree":
        assert result["finding_refreshes"][0]["diagnostic_code"] == "refresh_verdict_disagreement"


def test_reviewer_recovery_policy_cannot_expand_even_with_new_digest(git_repo, tmp_path):
    *_, preview, _ = setup_group(git_repo, tmp_path)
    preview["finding_refresh"]["reviewer_dependency_recovery"]["max_requests_per_attempt"] = 10
    preview["approval_digest"] = fingerprint({k:v for k,v in preview.items() if k != "approval_digest"})
    with pytest.raises(Denied):
        RepairRunAuthority(preview, preview["approval_digest"], ApprovalGate())


def test_recovery_read_loop_stops_after_three_requests(git_repo, tmp_path):
    result, adapter = run_cross(git_repo, tmp_path, "read_loop")
    assert len(adapter.reassessments) == 3
    assert result["finding_refreshes"][0]["diagnostic_code"] == "refresh_reviewer_read_limit"
    assert result["application"] is None


def test_recovery_cannot_spend_held_final_review_calls(git_repo, tmp_path):
    result, adapter = run_cross(git_repo, tmp_path, cap=27)
    assert result["status"] == "blocked" and result["application"] is None
    assert len(adapter.sent) <= 11
    assert result["accounting"]["final_review_calls_held"] == 16
    assert result["finding_refreshes"][0]["diagnostic_code"] == "refresh_budget_exhausted"
    assert not result["accounting"]["final_review_started"]


def test_recovered_present_claim_stays_pending_not_silently_resolved(git_repo, tmp_path):
    result, adapter = run_cross(git_repo, tmp_path, "present")
    entry = result["finding_refreshes"][0]
    assert entry["status"] == "complete", json.dumps(result)
    assert entry["issues"][0]["status"] == "present"
    assert entry["issues"][0]["verification"]["verdict"] == "accepted"
    assert len(adapter.groups) > 1  # Still queued; fixture's next no-op is rejected.
    assert result["status"] == "blocked" and result["application"] is None
    assert (git_repo / "a.py").read_text() == "old\n"

