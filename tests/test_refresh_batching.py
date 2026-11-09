"""Batched current-source verification; synthetic fixtures, not target acceptance."""
import json

import pytest
from test_finding_refresh import RefreshAdapter
from test_repair_budget import setup_budget

from reviewflow.backend.live_repair import run_repair_session
from reviewflow.backend.transport import Reply, Usage


class BatchAdapter(RefreshAdapter):
    def __init__(self, model, mode="valid"):
        super().__init__(model)
        self.batch_mode = mode
        self.batches = []
        self.corrections = []

    def __call__(self, wire, **kwargs):
        req = json.loads(wire)
        data = req["payload"]
        if req["stage"] == "verifier" and "findings" in data:
            self.sent.append(wire)
            self.batches.append(data)
            verdicts = [{"finding_id": f["finding_id"], "verdict": "rejected",
                "rationale": "Independent current-source rejection", "evidence": f["evidence"]}
                for f in data["findings"]]
            if self.batch_mode == "reverse":
                verdicts.reverse()
            elif self.batch_mode == "missing":
                verdicts.pop()
            elif self.batch_mode == "duplicate":
                verdicts[1] = verdicts[0]
            elif self.batch_mode == "foreign":
                verdicts[1]["finding_id"] = "foreign"
            elif self.batch_mode == "empty":
                verdicts = []
            elif self.batch_mode in {"hash", "scope"}:
                verdicts[0]["evidence"][0]["content_sha256"] = "0" * 64
                if self.batch_mode == "scope":
                    verdicts[1]["evidence"][0]["path"] = "outside.py"
            elif self.batch_mode == "unresolved":
                verdicts[0]["verdict"] = "unresolved"
            return Reply(json.dumps({"verdicts": verdicts}),
                         None if self.batch_mode == "unknown_usage" else Usage(10, 5))
        if data.get("evidence_correction"):
            self.corrections.append(data)
        reply = super().__call__(wire, **kwargs)
        if req["stage"] == "reviewer" and "refresh" in data and data["task"]["role"] == "correctness":
            value = json.loads(reply.text)
            old = data["refresh"]["historical_findings"][0]
            value["new_findings"] = [{**old, "finding_id": "third-bug",
                                      "evidence": [data["files"][0]["evidence"]]}]
            return Reply(json.dumps(value), Usage(10, 5))
        return reply


def run_batch(repo, tmp_path, mode="valid"):
    plan, config, profiles, runner, _, authority = setup_budget(repo, tmp_path, cap=48)
    adapter = BatchAdapter(config.model, mode)
    return run_repair_session(plan, config, authority, profiles, runner, adapter), adapter


@pytest.mark.parametrize("mode", ["valid", "reverse", "hash"])
def test_two_pending_findings_share_one_independent_verifier_request(git_repo, tmp_path, mode):
    result, adapter = run_batch(git_repo, tmp_path, mode)
    assert result["status"] == "applied", json.dumps(result)
    assert len(adapter.batches) == 1
    batch = adapter.batches[0]
    assert {f["finding_id"] for f in batch["findings"]} == {"other-bug", "third-bug"}
    assert "assessments" not in batch and "refresh" not in batch
    assert len(adapter.corrections) == (1 if mode == "hash" else 0)
    if adapter.corrections:
        assert adapter.corrections[0]["finding"]["finding_id"] == "other-bug"
    issues = result["finding_refreshes"][0]["issues"]
    assert len(issues) == 2 and all(i["status"] == "resolved" for i in issues)
    assert all(i["verification"]["finding_id"] == i["finding_id"] for i in issues)
    assert result["final_reviews"] and result["application"]
    assert result["accounting"]["requests_attempted"] == len(adapter.sent)


@pytest.mark.parametrize("mode", ["missing", "duplicate", "foreign", "empty", "scope",
                                  "unresolved", "unknown_usage"])
def test_invalid_or_unknown_batch_never_drops_pending_claims(git_repo, tmp_path, mode):
    result, adapter = run_batch(git_repo, tmp_path, mode)
    assert len(adapter.batches) == 1
    assert result["status"] == "blocked"
    assert result["failure_stage"] == "finding_refresh"
    assert result["application"] is None and not adapter.corrections
    assert (git_repo / "a.py").read_text() == "old\n"
    if mode == "scope":
        assert result["finding_refreshes"][0]["diagnostic_code"] == "evidence_path_outside_scope"
    if mode == "unknown_usage":
        assert result["accounting"]["usage_complete"] is False
