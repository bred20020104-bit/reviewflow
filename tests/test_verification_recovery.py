"""Real product controller with owned fixtures; no paid model or user target."""
import json

import pytest
from test_live_repair import RepairFixtureAdapter
from test_repair_budget import setup_budget

from reviewflow.backend.approval import ApprovalGate, Denied, fingerprint
from reviewflow.backend.live_repair import run_repair_session
from reviewflow.backend.repair_budget import RepairRunAuthority
from reviewflow.backend.transport import Reply, Usage


class EvidenceAdapter(RepairFixtureAdapter):
    def __init__(self, model, error="hash", corrected="accepted", four=False):
        super().__init__(model)
        self.error, self.corrected, self.four = error, corrected, four
        self.verify_calls = 0
        self.corrections = []

    def __call__(self, wire, **kwargs):
        reply = super().__call__(wire, **kwargs)
        request = json.loads(wire)
        data, stage = request["payload"], request["stage"]
        if self.four and stage == "reviewer":
            # Four distinct candidates, all ultimately rejected: full initial 8-call phase.
            role = data["task"]["role"]
            ref = data["files"][0]["evidence"]
            findings = [{"finding_id": f"{role}-{i}", "task_id": data["task"]["task_id"],
                         "title": "Fixture candidate", "root_cause": f"Distinct fixture {role}-{i}",
                         "trigger": "Fixture", "impact": "Fixture", "severity": "high",
                         "evidence": [ref]} for i in range(2)]
            return Reply(json.dumps({"result": {"findings": findings,
                "reviewed_paths": data["task"]["paths"]}, "requests": []}), Usage(10, 5))
        if stage != "verifier":
            return reply
        self.verify_calls += 1
        recovery = data.get("evidence_correction")
        if recovery:
            self.corrections.append(data)
        value = json.loads(reply.text)
        result = value.get("result", value)
        if self.four:
            result["verdict"] = "rejected"
        if self.verify_calls == 1 or (recovery and self.corrected == "invalid"):
            if self.error == "id":
                result["finding_id"] = "wrong"
            elif self.error == "hash":
                result["evidence"][0]["content_sha256"] = "0" * 64
            elif self.error == "snapshot":
                result["evidence"][0]["snapshot_id"] = "0" * 64
            elif self.error == "range":
                result["evidence"][0]["end_line"] = 2
            elif self.error == "scope":
                result["evidence"][0]["path"] = "outside.py"
        elif recovery:
            result["verdict"] = self.corrected
            if self.corrected == "unresolved":
                result["evidence"] = []
        return Reply(json.dumps(value), Usage(10, 5))


@pytest.mark.parametrize("error,code", [
    ("id", "verification_finding_mismatch"), ("hash", "evidence_hash_mismatch"),
    ("snapshot", "evidence_snapshot_mismatch"), ("range", "evidence_line_range_invalid"),
])
def test_bad_verifier_evidence_is_independently_corrected_once(git_repo, tmp_path, error, code):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)
    adapter = EvidenceAdapter(config.model, error)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "applied", result
    assert adapter.patch_calls == 1
    assert len(adapter.corrections) == 1
    assert adapter.corrections[0]["evidence_correction"]["reason"] == code
    assert result["initial_reviews"][0]["findings"][0]["error"] == code
    assert result["initial_reviews"][0]["findings"][0]["verification"] is None
    recovery, = result["verification_recoveries"]
    assert recovery["original_error"] == code
    assert recovery["status"] == "recovered" and recovery["attempt"] == 1
    assert result["recovered_reviews"][0]["report"]["errors"] == []
    assert result["recovered_reviews"][0]["report"]["findings"][0]["verification"]["verdict"] == "accepted"
    assert result["accounting"]["requests_attempted"] == len(adapter.sent) == 10
    assert (git_repo / "a.py").read_text() == "fixed\n"


@pytest.mark.parametrize("corrected", ["invalid", "unresolved"])
def test_failed_correction_never_retries_again_or_patches(git_repo, tmp_path, corrected):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)
    adapter = EvidenceAdapter(config.model, corrected=corrected)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert len(adapter.corrections) == 1
    assert adapter.verify_calls == 2 and adapter.patch_calls == 0
    assert len(result["verification_recoveries"]) == 1
    assert result["verification_recoveries"][0]["status"] != "recovered"
    assert result["application"] is None
    assert (git_repo / "a.py").read_text() == "old\n"


def test_out_of_scope_verifier_does_not_trigger_recovery(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)
    adapter = EvidenceAdapter(config.model, "scope")
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert not adapter.corrections and not adapter.patch_calls
    assert result["initial_reviews"][0]["findings"][0]["error"] == "evidence_path_outside_scope"


@pytest.mark.parametrize("cap,status,total", [(8, "blocked", 8), (9, "no_changes", 9)])
def test_recovery_has_separate_phase_but_no_extra_parent_budget(git_repo, tmp_path, cap, status, total):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=cap)
    adapter = EvidenceAdapter(config.model, corrected="rejected", four=True)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == status, result
    assert result["initial_reviews"][0]["calls"] == 8
    assert len(adapter.sent) == result["accounting"]["requests_attempted"] == total
    assert len(adapter.corrections) == total - 8
    assert not adapter.patch_calls
    if cap == 9:
        assert [r["call"] for r in result["accounting"]["records"]] == list(range(1, 9)) + [1]
    assert (git_repo / "a.py").read_text() == "old\n"


def test_recovery_policy_is_bound_and_fixed(git_repo, tmp_path):
    _, _, _, _, preview, _ = setup_budget(git_repo, tmp_path)
    assert preview.get("verification_recovery", {}).get("max_attempts_per_finding") == 1
    original_digest = preview["approval_digest"]
    preview["verification_recovery"]["max_attempts_per_finding"] = 2
    with pytest.raises(Denied):
        RepairRunAuthority(preview, original_digest, ApprovalGate())
    preview["approval_digest"] = fingerprint({k: v for k, v in preview.items() if k != "approval_digest"})
    with pytest.raises(Denied):
        RepairRunAuthority(preview, preview["approval_digest"], ApprovalGate())


def test_changed_candidate_never_gets_a_recovery_send(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)

    class ChangedCandidate(EvidenceAdapter):
        def __call__(self, wire, **kwargs):
            reply = super().__call__(wire, **kwargs)
            if json.loads(wire)["stage"] == "verifier":
                (plan.destination / "a.py").write_text("external fixture change\n")
            return reply

    adapter = ChangedCandidate(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert adapter.verify_calls == 1 and not adapter.corrections and not adapter.patch_calls
    assert (git_repo / "a.py").read_text() == "old\n"


@pytest.mark.parametrize("mode", ["unknown_usage", "exception", "invalid_json", "scope"])
def test_recovery_transport_or_invalid_response_stops_without_retry(git_repo, tmp_path, mode):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)

    class FailedRecovery(EvidenceAdapter):
        def __call__(self, wire, **kwargs):
            reply = super().__call__(wire, **kwargs)
            if not json.loads(wire)["payload"].get("evidence_correction"):
                return reply
            if mode == "unknown_usage":
                return Reply(reply.text, None)
            if mode == "exception":
                raise RuntimeError("SECRET should never reach report")
            if mode == "invalid_json":
                return Reply("SECRET invalid JSON", Usage(10, 5))
            value = json.loads(reply.text)
            value["evidence"][0]["path"] = "SECRET-outside.py"
            return Reply(json.dumps(value), Usage(10, 5))

    adapter = FailedRecovery(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert len(adapter.corrections) == 1 and adapter.verify_calls == 2
    assert not adapter.patch_calls and result["application"] is None
    assert "SECRET" not in json.dumps(result)
    assert result["verification_recoveries"][0]["status"] == "failed"
    assert (git_repo / "a.py").read_text() == "old\n"


@pytest.mark.parametrize("final_verdict,status", [("rejected", "applied"), ("accepted", "blocked")])
def test_final_recovery_still_requires_no_accepted_findings(git_repo, tmp_path, final_verdict, status):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)

    class FinalRecovery(EvidenceAdapter):
        def __call__(self, wire, **kwargs):
            reply = super().__call__(wire, **kwargs)
            request = json.loads(wire)
            data = request["payload"]
            if request["stage"] != "verifier" or not any(p["text"] == "fixed\n" for p in data["files"]):
                return reply
            value = json.loads(reply.text)
            outcome = value.get("result", value)
            if data.get("evidence_correction"):
                outcome["verdict"] = final_verdict
            else:
                outcome["evidence"][0]["content_sha256"] = "0" * 64
            return Reply(json.dumps(value), Usage(10, 5))

    adapter = FinalRecovery(config.model)
    adapter.behavior = "final_finding"
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == status, result
    assert len(adapter.corrections) == 2
    assert [a["stage"] for a in result["verification_recoveries"]] == ["initial_review", "final_review"]
    assert (git_repo / "a.py").read_text() == ("fixed\n" if status == "applied" else "old\n")
    if status == "blocked":
        assert result["failure_stage"] == "final_review" and result["application"] is None


def test_same_failed_finding_cannot_request_another_correction(git_repo, tmp_path, monkeypatch):
    import reviewflow.backend.live_repair as live
    from reviewflow.backend.verification_recovery import recover_verification

    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)
    adapter = EvidenceAdapter(config.model, corrected="rejected")

    def twice(*args, **kwargs):
        recover_verification(*args, **kwargs)
        return recover_verification(*args, **kwargs)

    monkeypatch.setattr(live, "recover_verification", twice)
    result = live.run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert len(adapter.corrections) == 1 and not adapter.patch_calls


def test_expired_authority_blocks_recovery_before_send(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)

    class Expired(EvidenceAdapter):
        def __call__(self, wire, **kwargs):
            reply = super().__call__(wire, **kwargs)
            if json.loads(wire)["stage"] == "verifier":
                authority._deadline = 0
            return reply

    adapter = Expired(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked" and not adapter.corrections
    assert not adapter.patch_calls


def test_markdown_explains_original_error_and_recovery(git_repo, tmp_path):
    from reviewflow.backend.live_repair_cli import _markdown

    plan, config, profiles, runner, preview, authority = setup_budget(git_repo, tmp_path, cap=32)
    result = run_repair_session(plan, config, authority, profiles, runner, EvidenceAdapter(config.model))
    result.update(preview=preview, report_directory="fixture-report")
    text = _markdown(result)
    assert "evidence_hash_mismatch" in text
    assert "Verification recovery: 1" in text and "recovered: 1" in text
    result["initial_reviews"][0]["findings"][0]["error"] = "SECRET unknown diagnostic"
    result["verification_recoveries"][0]["diagnostic_code"] = "SECRET unknown diagnostic"
    assert "SECRET" not in _markdown(result)


@pytest.mark.parametrize("scope_first", [False, True])
def test_scope_violation_dominates_bad_hash_in_any_reference_order(git_repo, tmp_path, scope_first):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)

    class MixedEvidence(EvidenceAdapter):
        def __call__(self, wire, **kwargs):
            reply = super().__call__(wire, **kwargs)
            request = json.loads(wire)
            if request["stage"] != "verifier" or request["payload"].get("evidence_correction"):
                return reply
            value = json.loads(reply.text)
            refs = value["result"]["evidence"]
            forbidden = {**refs[0], "path": "outside.py"}
            value["result"]["evidence"] = [forbidden, *refs] if scope_first else [*refs, forbidden]
            return Reply(json.dumps(value), Usage(10, 5))

    adapter = MixedEvidence(config.model)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked"
    assert result["initial_reviews"][0]["findings"][0]["error"] == "evidence_path_outside_scope"
    assert not adapter.corrections and not adapter.patch_calls
    assert result["verification_recoveries"] == []
    assert (git_repo / "a.py").read_text() == "old\n"


def test_earlier_corrected_evidence_survives_later_correction_failure(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path, cap=32)

    class TwoCorrections(EvidenceAdapter):
        def __call__(self, wire, **kwargs):
            reply = super().__call__(wire, **kwargs)
            request = json.loads(wire)
            if request["stage"] != "verifier":
                return reply
            if request["payload"].get("evidence_correction"):
                if len(self.corrections) == 2:
                    return Reply("SECRET malformed second correction", Usage(10, 5))
            elif self.verify_calls == 2:
                value = json.loads(reply.text)
                value["result"]["evidence"][0]["content_sha256"] = "0" * 64
                return Reply(json.dumps(value), Usage(10, 5))
            return reply

    adapter = TwoCorrections(config.model, four=True)
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert result["status"] == "blocked" and result["application"] is None
    assert len(adapter.corrections) == 2 and not adapter.patch_calls
    first, second = result["verification_recoveries"]
    assert first["status"] == "recovered" and second["status"] == "failed"
    assert first.get("verification") is not None
    original = result["initial_reviews"][0]["findings"][0]["finding"]
    assert first["verification"]["finding_id"] == original["finding_id"]
    assert first["verification"]["evidence"] == original["evidence"]
    assert first["verification"]["rationale"] == "Synthetic fixture verification"
    assert first["verification"]["verdict"] == "accepted"
    assert result["recovered_reviews"] == []
    assert "SECRET" not in json.dumps(result)
    assert result["accounting"]["requests_attempted"] == len(adapter.sent) == 10
    assert (git_repo / "a.py").read_text() == "old\n"
