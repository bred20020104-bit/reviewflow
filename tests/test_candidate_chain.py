"""Real owned-worktree composition; no target Python execution or publication."""

import os

import pytest
from test_repair_workspace import make_workspace, proposal_for

from reviewflow.backend.approval import Denied
from reviewflow.backend.candidate_chain import CandidateChain
from reviewflow.backend.repair import PartialApplicationError, RepairError


def setup_chain(repo, tmp_path):
    parent = tmp_path.parent / (tmp_path.name + "-chain")
    parent.mkdir()
    ws, gate, _ = make_workspace(repo, parent)
    return ws, gate, CandidateChain(ws)


def proposal(ws, path="a.py", finding_id="first", content="first fix\n"):
    source, inventory = ws.inventory()
    task, finding, patch = proposal_for(source, inventory, path=path, content=content)
    return (
        task,
        finding.model_copy(update={"finding_id": finding_id}),
        patch.model_copy(update={"finding_id": finding_id}),
    )


def apply(chain, gate, args):
    intent = chain.intent(*args)
    chain.apply(*args, gate, gate.approve(intent, confirmed=True))


def test_composition_requires_each_grant_and_rolls_back_one_candidate(git_repo, tmp_path):
    ws, gate, chain = setup_chain(git_repo, tmp_path)
    first = proposal(ws)
    with pytest.raises(Denied):
        chain.apply(*first, gate, "forged")
    assert (ws.path / "a.py").read_text() == "old\n"
    grant = gate.approve(chain.intent(*first), confirmed=True)
    chain.apply(*first, gate, grant)
    second = proposal(ws, "src/b.py", "second", "second fix\n")
    with pytest.raises(Denied):
        chain.apply(*second, gate, grant)
    apply(chain, gate, second)
    assert (ws.path / "a.py").read_text() == "first fix\n"
    assert (ws.path / "src/b.py").read_text() == "second fix\n"
    assert chain.summary()["steps"] == 2
    assert chain.summary()["commit_ready"] is False
    assert chain.summary()["validation"] == "not_run"
    assert (git_repo / "a.py").read_text() == "old\n"
    chain.rollback_last()
    assert (ws.path / "a.py").read_text() == "first fix\n"
    assert (ws.path / "src/b.py").read_bytes() == ws._baseline["src/b.py"]
    chain.rollback_last()
    assert ws.current_digest() == ws.baseline_digest
    assert chain.summary()["steps"] == 0


def test_stale_candidate_and_modified_approval_are_rejected(git_repo, tmp_path):
    ws, gate, chain = setup_chain(git_repo, tmp_path)
    first = proposal(ws)
    old_second = proposal(ws, "src/b.py", "second")
    apply(chain, gate, first)
    with pytest.raises(RepairError):
        chain.intent(*old_second)
    second = proposal(ws, "src/b.py", "second")
    grant = gate.approve(chain.intent(*second), confirmed=True)
    changed = proposal(ws, "src/b.py", "second", "different\n")
    with pytest.raises(Denied):
        chain.apply(*changed, gate, grant)
    assert chain.summary()["steps"] == 1


@pytest.mark.parametrize("user_edit", [False, True])
def test_second_partial_failure_preserves_prior_candidate_or_blocks(
    git_repo, tmp_path, monkeypatch, user_edit
):
    ws, gate, chain = setup_chain(git_repo, tmp_path)
    apply(chain, gate, proposal(ws))
    task, finding, patch = proposal(ws, finding_id="second", content="second a\n")
    _, other, patch_b = proposal(ws, "src/b.py", "second", "second b\n")
    task = task.model_copy(update={"paths": ("a.py", "src/b.py")})
    finding = finding.model_copy(update={"evidence": finding.evidence + other.evidence})
    patch = patch.model_copy(update={"replacements": patch.replacements + patch_b.replacements})
    original = os.replace

    def replace(src, dst, **kwargs):
        if dst == "b.py":
            if user_edit:
                (ws.path / "a.py").write_text("human change\n")
            raise PermissionError("synthetic write failure")
        return original(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(PartialApplicationError if user_edit else RepairError):
        apply(chain, gate, (task, finding, patch))
    if user_edit:
        assert (ws.path / "a.py").read_text() == "human change\n"
        with pytest.raises(RepairError):
            chain.rollback_last()
    else:
        assert (ws.path / "a.py").read_text() == "first fix\n"
        assert chain.summary()["steps"] == 1
        chain.rollback_last()
        assert ws.current_digest() == ws.baseline_digest


def test_user_edit_is_never_overwritten_by_chain_rollback(git_repo, tmp_path):
    ws, gate, chain = setup_chain(git_repo, tmp_path)
    apply(chain, gate, proposal(ws))
    (ws.path / "a.py").write_text("human change\n")
    with pytest.raises(RepairError):
        chain.rollback_last()
    assert (ws.path / "a.py").read_text() == "human change\n"
