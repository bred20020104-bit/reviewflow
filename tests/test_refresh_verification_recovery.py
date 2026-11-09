"""Refresh verifier correction, synthetic controller fixtures only."""
import json

import pytest
from test_finding_refresh import RefreshAdapter
from test_repair_budget import setup_budget

from reviewflow.backend.live_repair import run_repair_session
from reviewflow.backend.transport import Reply, Usage


class RefreshEvidenceAdapter(RefreshAdapter):
    def __init__(self, model, error="hash", corrected="valid"):
        super().__init__(model)
        self.error, self.corrected = error, corrected
        self.corrections = []

    def __call__(self, wire, **kwargs):
        reply = super().__call__(wire, **kwargs)
        request = json.loads(wire)
        data = request["payload"]
        if request["stage"] != "verifier" or not data.get("refresh_verification"):
            return reply
        correction = data.get("evidence_correction")
        if correction:
            self.corrections.append(data)
            if self.corrected == "unknown_usage":
                return Reply(reply.text, None)
            if self.corrected == "invalid_json":
                return Reply("PRIVATE invalid output", Usage(10, 5))
        value = json.loads(reply.text)
        if not correction or self.corrected == "invalid":
            if self.error == "hash":
                value["evidence"][0]["content_sha256"] = "0" * 64
            elif self.error == "snapshot":
                value["evidence"][0]["snapshot_id"] = "0" * 64
            elif self.error == "id":
                value["finding_id"] = "wrong"
            elif self.error == "range":
                value["evidence"][0]["end_line"] = 2
            elif self.error == "mixed_scope":
                value["evidence"][0]["content_sha256"] = "0" * 64
                value["evidence"].append({**value["evidence"][0], "path": "outside.py"})
        elif self.corrected in {"unresolved", "accepted"}:
            value["verdict"] = self.corrected
        return Reply(json.dumps(value), Usage(10, 5))


@pytest.mark.parametrize("error,code", [("hash", "evidence_hash_mismatch"),
    ("snapshot", "evidence_snapshot_mismatch"), ("id", "verification_finding_mismatch"),
    ("range", "evidence_line_range_invalid")])
def test_refresh_metadata_error_is_independently_corrected_once(git_repo, tmp_path, error, code):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=48)
    adapter = RefreshEvidenceAdapter(config.model, error=error)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "applied", result
    assert len(adapter.corrections) == 1
    assert adapter.corrections[0]["evidence_correction"]["reason"] == code
    issue, = result["finding_refreshes"][0]["issues"]
    assert issue["verification_correction"]["original_error"] == code
    assert issue["verification_correction"]["status"] == "recovered"
    assert issue["verification"]["verdict"] == "rejected"
    assert result["accounting"]["requests_attempted"] == len(adapter.sent)
    assert (git_repo / "a.py").read_text() == "fixed\n"


@pytest.mark.parametrize("corrected", ["invalid", "invalid_json", "unknown_usage", "unresolved", "accepted"])
def test_failed_refresh_correction_or_disagreement_never_writes_back(git_repo, tmp_path, corrected):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=48)
    adapter = RefreshEvidenceAdapter(config.model, corrected=corrected)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert result["failure_stage"] == "finding_refresh"
    assert len(adapter.corrections) == 1
    assert adapter.patch_calls == 1 and result["application"] is None
    assert "PRIVATE" not in json.dumps(result)
    assert (git_repo / "a.py").read_text() == "old\n"


def test_scope_violation_dominates_bad_refresh_hash(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=48)
    adapter = RefreshEvidenceAdapter(config.model, error="mixed_scope")
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert not adapter.corrections
    assert result["finding_refreshes"][0]["diagnostic_code"] == "evidence_path_outside_scope"
    assert result["application"] is None


def test_duplicate_refresh_correction_is_denied_before_second_attempt(git_repo, tmp_path, monkeypatch):
    import reviewflow.backend.live_repair as live
    from reviewflow.backend.finding_refresh import refresh_findings

    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=48)
    audit_ref = []

    def capture(candidate, report, outcomes, authority, profiles, adapter, audit, **kwargs):
        audit_ref.append(audit)
        return refresh_findings(candidate, report, outcomes, authority, profiles, adapter, audit, **kwargs)

    class AlreadyCorrected(RefreshEvidenceAdapter):
        def __call__(self, wire, **kwargs):
            reply = super().__call__(wire, **kwargs)
            data = json.loads(wire)["payload"]
            if data.get("refresh_verification") and not data.get("evidence_correction"):
                audit = audit_ref[0]
                audit.insert(0, {"snapshot_id": data["task"]["snapshot_id"],
                    "paths": data["task"]["paths"], "issues": [{
                        "task_id": data["task"]["task_id"], "finding_id": data["finding"]["finding_id"],
                        "verification_correction": {"status": "recovered"}}]})
            return reply

    monkeypatch.setattr(live, "refresh_findings", capture)
    adapter = AlreadyCorrected(config.model)
    result = live.run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert result["finding_refreshes"][-1]["diagnostic_code"] == "refresh_verification_correction_limit"
    assert not adapter.corrections and result["application"] is None


def test_candidate_mutation_stops_before_refresh_correction(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=48)

    class Mutated(RefreshEvidenceAdapter):
        def __call__(self, wire, **kwargs):
            reply = super().__call__(wire, **kwargs)
            if json.loads(wire)["payload"].get("refresh_verification"):
                (plan.destination / "a.py").write_text("external modification\n")
            return reply

    adapter = Mutated(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert not adapter.corrections and result["application"] is None
    assert (git_repo / "a.py").read_text() == "old\n"


def test_refresh_correction_has_no_extra_parent_allowance(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=17)
    adapter = RefreshEvidenceAdapter(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert len(adapter.sent) == result["accounting"]["requests_attempted"] == 9
    assert result["accounting"]["final_review_calls_held"] == 8
    assert not adapter.corrections
    assert result["finding_refreshes"][0]["diagnostic_code"] == "refresh_budget_exhausted"
    assert result["application"] is None
