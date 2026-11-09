import json

import pytest
from test_repair_workspace import make_workspace, proposal_for

from reviewflow.backend.repair import RepairError
from reviewflow.backend.repair_loop import run_repair
from reviewflow.models import ScanPolicy
from reviewflow.review.context import ReviewContext
from reviewflow.snapshot import build_inventory
from reviewflow.sources.git import GitHeadSource


def test_failed_verification_retries_twice_and_restores_original(git_repo, tmp_path):
    parent = tmp_path.parent / (tmp_path.name + "-storage")
    parent.mkdir()
    ws, gate, patch = make_workspace(git_repo, parent)
    source = GitHeadSource(git_repo)
    inventory = build_inventory(source, ScanPolicy())
    task, finding, proposal = proposal_for(source, inventory)
    requests = []

    class Backend:
        def complete(self, request):
            requests.append(request)
            return proposal.model_dump_json()

    class Validator:
        def verify(self, workspace, receipt):
            raise RepairError("not validated")

    def validate(workspace):
        assert (workspace.path / "a.py").read_text() == "fixed\n"
        return None

    report = run_repair(
        ws,
        ReviewContext(source, inventory),
        task,
        finding,
        Backend(),
        gate,
        lambda intent: gate.approve(intent, confirmed=True),
        Validator(),
        validate,
    )
    assert report["status"] == "unresolved"
    assert report["attempts"] == 2
    assert len(requests) == 2
    assert requests[1]["feedback"] == "verification_failed"
    assert (ws.path / "a.py").read_text() == "old\n"
    events = [json.loads(line) for line in ws.audit_path.read_text().splitlines()]
    assert [e["attempt"] for e in events if e["event"] == "attempt"] == [1, 2]
    assert all("content" not in event for event in events)
    assert report["commit_ready"] is False


def test_user_edit_blocks_rollback_instead_of_overwriting(git_repo, tmp_path):
    parent = tmp_path.parent / (tmp_path.name + "-storage")
    parent.mkdir()
    ws, gate, patch = make_workspace(git_repo, parent)
    ws.apply(patch, gate, gate.approve(ws.patch_intent(patch), confirmed=True))
    (ws.path / "a.py").write_text("user content\n")
    with pytest.raises(RepairError):
        ws.rollback_attempt()
    assert (ws.path / "a.py").read_text() == "user content\n"


def test_audit_replacement_blocks_before_patch(git_repo, tmp_path):
    parent = tmp_path.parent / (tmp_path.name + "-storage")
    parent.mkdir()
    ws, gate, patch = make_workspace(git_repo, parent)
    ws.audit_path.unlink()
    ws.audit_path.symlink_to(git_repo / "a.py")
    with pytest.raises(RepairError, match="audit"):
        ws.apply(patch, gate, gate.approve(ws.patch_intent(patch), confirmed=True))
    assert (ws.path / "a.py").read_text() == "old\n"
    assert (git_repo / "a.py").read_text() == "old\n"
