"""Parent authorization unit tests; fixture adapters never establish target acceptance."""

import json

import pytest
from test_repair_candidate import create, plan_for
from test_validation_publication import process

from reviewflow.agents.profiles import load_profiles
from reviewflow.backend.approval import ApprovalGate, Denied, fingerprint
from reviewflow.backend.development import DevelopmentConfig, DevelopmentModel
from reviewflow.backend.transport import Reply, TransportError, Usage
from reviewflow.backend.volume_runner import VolumeDockerRunner
from reviewflow.review.workflow_models import ModelRequest

IMAGE = "sha256:" + "a" * 64


def setup_budget(repo, tmp_path, cap=3, paths=("a.py",)):
    from reviewflow.backend.repair_budget import RepairRunAuthority, make_repair_preview

    plan = plan_for(repo, tmp_path / "storage", paths)
    profiles = load_profiles()
    config = DevelopmentConfig(model=DevelopmentModel(provider="openai", api_type="responses",
        model="fixture", base_url="http://127.0.0.1:9999/v1", api_key_file="secrets.local.toml"),
        privacy={"allowed_project_root": str(repo), "allowed_paths": paths})
    runner = VolumeDockerRunner(IMAGE, execute=lambda *args, **kwargs: process())
    preview = make_repair_preview(plan, config, profiles, runner, tmp_path.parent / "report", cap)
    gate = ApprovalGate()
    authority = RepairRunAuthority(preview, preview["approval_digest"], gate)
    return plan, config, profiles, runner, preview, authority


class Adapter:
    def __init__(self, model, reply=Reply("{}", Usage(12, 3))):
        self.model, self.reply, self.sent = model, reply, []

    def body_size(self, payload):
        return len(payload.encode())

    def __call__(self, payload, **kwargs):
        self.sent.append(payload)
        return self.reply


def request(phase):
    record = next(r for r in phase.inventory.snapshot.records if r.status == "included")
    return ModelRequest("reviewer", "Trusted unit fixture", json.dumps({"files": [{
        "evidence": {"snapshot_id": phase.inventory.snapshot.snapshot_id,
                     "path": "a.py", "content_sha256": record.sha256,
                     "start_line": 1, "end_line": 1},
        "text": "old\n"}]}), {"type": "object"})


def test_parent_budget_is_shared_across_transports(git_repo, tmp_path):
    plan, config, profiles, _, _, authority = setup_budget(git_repo, tmp_path)
    candidate = create(plan)
    adapter = Adapter(config.model)
    for count in (2, 1):
        phase = authority.start_phase(candidate, ("a.py",), adapter, profiles)
        for _ in range(count):
            assert phase.gateway.complete(request(phase)) == "{}"
        authority.record_phase(phase)
    with pytest.raises((Denied, TransportError)):
        authority.start_phase(candidate, ("a.py",), adapter, profiles)
    assert len(adapter.sent) == 3
    assert authority.accounting()["requests_attempted"] == 3
    assert authority.accounting()["input_tokens_reported"] == 36
    assert authority.accounting()["cost_usd"] is None


@pytest.mark.parametrize("change", ["missing", "mismatch", "modified_preview", "zero", "over_cap", "boolean"])
def test_invalid_approval_never_creates_candidate(git_repo, tmp_path, change):
    from reviewflow.backend.repair_budget import RepairRunAuthority

    plan, _, _, _, preview, _ = setup_budget(git_repo, tmp_path)
    approved = preview["approval_digest"]
    if change == "missing":
        approved = None
    elif change == "mismatch":
        approved = "0" * 64
    elif change == "modified_preview":
        preview["output"] += "-changed"
    else:
        preview["max_calls"] = {"zero": 0, "over_cap": 49, "boolean": True}[change]
        preview["approval_digest"] = fingerprint({k: v for k, v in preview.items() if k != "approval_digest"})
        approved = preview["approval_digest"]
    with pytest.raises((Denied, ValueError)):
        RepairRunAuthority(preview, approved, ApprovalGate())
    assert not plan.destination.exists()


@pytest.mark.parametrize("change", ["model", "scope", "profiles", "original", "candidate", "active"])
def test_phase_binding_rejects_changed_inputs(git_repo, tmp_path, change):
    plan, config, profiles, _, _, authority = setup_budget(git_repo, tmp_path)
    candidate = create(plan)
    adapter = Adapter(config.model)
    paths = ("a.py",)
    if change == "model":
        adapter.model = config.model.model_copy(update={"model": "other"})
    elif change == "scope":
        paths = ("src/b.py",)
    elif change == "profiles":
        class ForeignProfiles:
            def manifest(self):
                return {"foreign": True}
        profiles = ForeignProfiles()
    elif change == "original":
        (git_repo / "a.py").write_text("user changed\n")
    elif change == "candidate":
        (candidate.path / "a.py").write_text("external change\n")
    else:
        authority.start_phase(candidate, paths, adapter, profiles)
    with pytest.raises(ValueError):
        authority.start_phase(candidate, paths, adapter, profiles)
    assert not adapter.sent


def test_unknown_usage_closes_run_before_next_send_or_tests(git_repo, tmp_path):
    plan, config, profiles, runner, _, authority = setup_budget(git_repo, tmp_path)
    candidate = create(plan)
    adapter = Adapter(config.model, Reply("{}", None))
    phase = authority.start_phase(candidate, ("a.py",), adapter, profiles)
    with pytest.raises(TransportError):
        phase.gateway.complete(request(phase))
    authority.record_phase(phase)
    with pytest.raises(ValueError):
        authority.start_phase(candidate, ("a.py",), adapter, profiles)
    with pytest.raises(ValueError):
        authority.authorize_tests(candidate, runner)
    assert authority.accounting()["requests_attempted"] == 1
    assert authority.accounting()["usage_complete"] is False
    assert len(adapter.sent) == 1


def test_stale_phase_cannot_issue_another_grant(git_repo, tmp_path):
    plan, config, profiles, _, _, authority = setup_budget(git_repo, tmp_path)
    candidate = create(plan)
    adapter = Adapter(config.model)
    phase = authority.start_phase(candidate, ("a.py",), adapter, profiles)
    phase.gateway.complete(request(phase))
    authority.record_phase(phase)
    with pytest.raises(ValueError):
        phase.gateway.complete(request(phase))
    assert len(adapter.sent) == 1


def test_test_grant_binds_image_timeout_and_candidate(git_repo, tmp_path):
    plan, _, _, runner, _, authority = setup_budget(git_repo, tmp_path)
    candidate = create(plan)
    grant = authority.authorize_tests(candidate, runner)
    result = runner.run(candidate.files(), "repository", authority.gate, grant)
    assert result.status == "passed"
    runner.image = "sha256:" + "b" * 64
    with pytest.raises(ValueError):
        authority.authorize_tests(candidate, runner)
