"""Reviewer correction uses actual controller/authority with synthetic adapters only."""
import json

import pytest
from test_live_repair import RepairFixtureAdapter
from test_repair_budget import setup_budget

from reviewflow.backend.approval import ApprovalGate, Denied, fingerprint
from reviewflow.backend.live_repair import run_repair_session
from reviewflow.backend.repair_budget import RepairRunAuthority
from reviewflow.backend.transport import Reply, Usage


class ReviewerEvidenceAdapter(RepairFixtureAdapter):
    def __init__(self, model, error="hash", corrected="valid", final=False):
        super().__init__(model, "final_finding" if final else "success")
        self.error, self.corrected, self.final = error, corrected, final
        self.corrections = []
        self.corrected_verifications = 0

    def __call__(self, wire, **kwargs):
        reply = super().__call__(wire, **kwargs)
        request = json.loads(wire)
        data, stage = request["payload"], request["stage"]
        if stage == "verifier" and data.get("reviewer_correction"):
            self.corrected_verifications += 1
            value = json.loads(reply.text)
            result = value.get("result", value)
            result["verdict"] = self.corrected if self.corrected in {"rejected", "unresolved"} else "accepted"
            return Reply(json.dumps(value), Usage(10, 5))
        if stage != "reviewer" or data["task"]["role"] != "correctness" or "refresh" in data:
            return reply
        value = json.loads(reply.text)
        correction = data.get("reviewer_correction")
        if correction:
            self.corrections.append(data)
            if self.corrected == "unknown_usage":
                return Reply(reply.text, None)
            if self.corrected == "invalid_json":
                return Reply("PRIVATE invalid JSON", Usage(10, 5))
        active = not self.final or any(p["text"] == "fixed\n" for p in data["files"])
        findings = value["result"]["findings"]
        if active and findings and (not correction or self.corrected == "invalid"):
            ref = findings[0]["evidence"][0]
            if self.error == "hash":
                ref["content_sha256"] = "0" * 64
            elif self.error == "snapshot":
                ref["snapshot_id"] = "0" * 64
            elif self.error == "range":
                ref["end_line"] = 2
            elif self.error == "scope":
                ref["path"] = "outside.py"
            elif self.error == "coverage":
                value["result"]["reviewed_paths"] = []
        if correction and self.corrected == "coverage":
            value["result"]["reviewed_paths"] = []
        return Reply(json.dumps(value), Usage(10, 5))


@pytest.mark.parametrize("error,code", [
    ("hash", "evidence_hash_mismatch"), ("snapshot", "evidence_snapshot_mismatch"),
    ("range", "evidence_line_range_invalid"),
    ("coverage", "finding_evidence_not_reviewed"),
])
def test_reviewer_evidence_is_reissued_then_independently_verified(git_repo, tmp_path, error, code):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)
    adapter = ReviewerEvidenceAdapter(config.model, error)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "applied", result
    assert len(adapter.corrections) == adapter.corrected_verifications == 1
    assert result["initial_reviews"][0]["tasks"][0]["error"] == code
    audit, = result["reviewer_recoveries"]
    assert audit["original_error"] == code and audit["status"] == "recovered"
    assert audit["requests_attempted"] == 2
    assert adapter.corrections[0]["reviewer_correction"]["reason"] == code
    assert result["accounting"]["requests_attempted"] == len(adapter.sent)
    assert (git_repo / "a.py").read_text() == "fixed\n"


@pytest.mark.parametrize("corrected", ["invalid", "unknown_usage", "invalid_json", "coverage", "unresolved"])
def test_failed_reviewer_correction_never_patches_or_retries(git_repo, tmp_path, corrected):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)
    adapter = ReviewerEvidenceAdapter(config.model, corrected=corrected)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert len(adapter.corrections) == 1
    assert not adapter.patch_calls and result["application"] is None
    assert "PRIVATE" not in json.dumps(result)
    assert (git_repo / "a.py").read_text() == "old\n"


def test_scope_violation_never_triggers_reviewer_correction(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)
    adapter = ReviewerEvidenceAdapter(config.model, error="scope")
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert not adapter.corrections and not adapter.patch_calls


@pytest.mark.parametrize("cap,corrections,verifications", [(3, 0, 0), (4, 1, 0)])
def test_reviewer_correction_shares_parent_call_budget(git_repo, tmp_path, cap, corrections, verifications):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=cap)
    adapter = ReviewerEvidenceAdapter(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert len(adapter.corrections) == corrections
    assert adapter.corrected_verifications == verifications
    assert len(adapter.sent) == result["accounting"]["requests_attempted"] == cap
    assert not adapter.patch_calls


@pytest.mark.parametrize("verdict,status", [("rejected", "applied"), ("valid", "blocked")])
def test_final_reviewer_correction_still_needs_rejected_findings(git_repo, tmp_path, verdict, status):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)
    adapter = ReviewerEvidenceAdapter(config.model, corrected=verdict, final=True)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == status, result
    assert len(adapter.corrections) == adapter.corrected_verifications == 1
    assert result["reviewer_recoveries"][0]["stage"] == "final_review"
    assert (git_repo / "a.py").read_text() == ("fixed\n" if status == "applied" else "old\n")


@pytest.mark.parametrize("mixed", ["coverage", "read_scope"])
def test_scope_violation_dominates_bad_metadata_before_recovery(git_repo, tmp_path, mixed):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)

    class MixedScopeAdapter(ReviewerEvidenceAdapter):
        def __call__(self, wire, **kwargs):
            reply = super().__call__(wire, **kwargs)
            request = json.loads(wire)
            data = request["payload"]
            if (request["stage"] == "reviewer" and data["task"]["role"] == "correctness"
                    and not data.get("reviewer_correction")):
                value = json.loads(reply.text)
                if mixed == "coverage":
                    value["result"]["reviewed_paths"].append("outside.py")
                else:
                    value = {"result": None, "requests": [
                        {"kind": "read", "path": "a.py", "start_line": 1, "end_line": 999},
                        {"kind": "read", "path": "outside.py", "start_line": 1, "end_line": 1}]}
                return Reply(json.dumps(value), Usage(10, 5))
            return reply

    adapter = MixedScopeAdapter(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert not adapter.corrections and not adapter.patch_calls
    assert not result["reviewer_recoveries"]
    assert result["initial_reviews"][0]["tasks"][0]["error"] in {
        "evidence_path_outside_scope", "tool_scope"}
    assert (git_repo / "a.py").read_text() == "old\n"


def test_changed_candidate_stops_before_reviewer_correction_send(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)

    class MutatingAdapter(ReviewerEvidenceAdapter):
        def __call__(self, wire, **kwargs):
            reply = super().__call__(wire, **kwargs)
            request = json.loads(wire)
            if request["stage"] == "reviewer" and request["payload"]["task"]["role"] == "security":
                (plan.destination / "a.py").write_text("external change\n")
            return reply

    adapter = MutatingAdapter(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert not adapter.corrections and not adapter.patch_calls
    assert (git_repo / "a.py").read_text() == "old\n"


def test_same_task_cannot_be_reissued_twice(git_repo, tmp_path, monkeypatch):
    import reviewflow.backend.live_repair as live
    from reviewflow.backend.reviewer_recovery import recover_reviewers

    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)
    adapter = ReviewerEvidenceAdapter(config.model)

    def twice(*args, **kwargs):
        recover_reviewers(*args, **kwargs)
        return recover_reviewers(*args, **kwargs)

    monkeypatch.setattr(live, "recover_reviewers", twice)
    result = live.run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert len(adapter.corrections) == 1 and not adapter.patch_calls


def test_reviewer_correction_attempts_are_globally_bounded(git_repo, tmp_path, monkeypatch):
    import reviewflow.backend.live_repair as live
    from reviewflow.backend.reviewer_recovery import recover_reviewers

    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)
    adapter = ReviewerEvidenceAdapter(config.model)

    def exhausted(candidate, report, authority, profiles, adapter, audit, **kwargs):
        audit.extend({"status": "failed"} for _ in range(8))
        return recover_reviewers(candidate, report, authority, profiles, adapter, audit, **kwargs)

    monkeypatch.setattr(live, "recover_reviewers", exhausted)
    result = live.run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert not adapter.corrections and not adapter.patch_calls


def test_reviewer_coverage_and_other_verifier_hash_can_both_recover(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)

    class MixedErrors(ReviewerEvidenceAdapter):
        def __call__(self, wire, **kwargs):
            reply = super().__call__(wire, **kwargs)
            request = json.loads(wire)
            data = request["payload"]
            value = json.loads(reply.text)
            if (request["stage"] == "reviewer" and data["task"]["role"] == "security"
                    and "refresh" not in data and any(p["text"] == "old\n" for p in data["files"])):
                value["result"]["findings"] = [{
                    "finding_id": "security-bug", "task_id": data["task"]["task_id"],
                    "title": "Synthetic independent issue", "root_cause": "Synthetic stale value",
                    "trigger": "Fixture", "impact": "Fixture", "severity": "high",
                    "evidence": [data["files"][0]["evidence"]]}]
            if (request["stage"] == "verifier" and data["finding"]["finding_id"] == "security-bug"
                    and not data.get("evidence_correction") and not data.get("refresh_verification")):
                value.get("result", value)["evidence"][0]["content_sha256"] = "0" * 64
            return Reply(json.dumps(value), Usage(10, 5))

    adapter = MixedErrors(config.model, error="coverage")
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "applied", json.dumps(result)
    assert len(result["reviewer_recoveries"]) == len(result["verification_recoveries"]) == 1
    assert len(result["initial_reviews"][0]["errors"]) == 2
    assert (git_repo / "a.py").read_text() == "fixed\n"


def test_reviewer_diagnostics_include_task_errors_without_arbitrary_text():
    from reviewflow.backend.live_repair_cli import _markdown

    document = {"status": "blocked", "accounting": {"requests_attempted": 3},
        "preview": {"max_calls": 48}, "report_directory": "/report", "candidate_path": "/candidate",
        "test_runs": [], "initial_reviews": [{"tasks": [
            {"error": "evidence_hash_mismatch"}, {"error": "PRIVATE arbitrary diagnostic"}]}],
        "reviewer_recoveries": [{"status": "recovered", "diagnostic_code": None}]}
    text = _markdown(document)
    assert "evidence_hash_mismatch" in text
    assert "审查纠错：1 条记录；恢复成功：1" in text
    assert "PRIVATE" not in text


def test_reviewer_recovery_policy_is_fixed_and_approval_bound(git_repo, tmp_path):
    _, _, _, _, preview, _ = setup_budget(git_repo, tmp_path)
    assert preview.get("reviewer_recovery", {}).get("max_attempts_per_task") == 1
    original = preview["approval_digest"]
    preview["reviewer_recovery"]["max_attempts_per_task"] = 2
    with pytest.raises(Denied):
        RepairRunAuthority(preview, original, ApprovalGate())
    preview["approval_digest"] = fingerprint({k: v for k, v in preview.items() if k != "approval_digest"})
    with pytest.raises(Denied):
        RepairRunAuthority(preview, preview["approval_digest"], ApprovalGate())
