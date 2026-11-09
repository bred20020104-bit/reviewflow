"""All grouped members validate before writes; rollback owns the whole attempt."""
import json

import pytest
from test_repair_candidate import create, plan_for
from test_repair_workspace import proposal_for

from reviewflow.backend.grouped_patch import GroupEditProposal, validate_group
from reviewflow.backend.repair import PartialApplicationError, RepairError
from reviewflow.review.context import ReviewContext


def prepared(repo, tmp_path):
    candidate = create(plan_for(repo, tmp_path / "storage", ("a.py", "src/b.py")))
    source, inventory = candidate.source_inventory(candidate.plan.selected)
    members, patches = [], []
    for i, path in enumerate(candidate.plan.selected):
        task, finding, _ = proposal_for(source, inventory, path=path)
        finding = finding.model_copy(update={"finding_id": f"issue-{i}"})
        members.append((task, finding))
        ref = finding.evidence[0]
        patches.append({"task_id": task.task_id, "finding_id": finding.finding_id,
            "edits": [{"path": path, "before_sha256": ref.content_sha256,
                       "old_text": (candidate.path / path).read_text(), "new_text": "fixed\n"}]})
    context = ReviewContext(source, inventory)
    proposal = GroupEditProposal.model_validate_json(json.dumps({
        "snapshot_id": inventory.snapshot.snapshot_id, "patches": patches, "explanation": "Fixture"}))
    return candidate, context, tuple(members), validate_group(context, members, proposal)


def test_candidate_applies_and_restores_entire_group(git_repo, tmp_path):
    candidate, context, members, patch = prepared(git_repo, tmp_path)
    before = candidate.files()
    candidate.apply_group(patch, context, members)
    assert all(candidate.files()[p] == b"fixed\n" for p in candidate.plan.selected)
    with pytest.raises(RepairError):
        candidate.apply_group(patch, context, members)
    candidate.restore_attempt()
    assert candidate.files() == before
    assert (git_repo / "a.py").read_text() == "old\n"


@pytest.mark.parametrize("change", ["missing", "evidence", "origin", "candidate"])
def test_revalidation_denies_group_before_any_owned_write(git_repo, tmp_path, change):
    candidate, context, members, patch = prepared(git_repo, tmp_path)
    if change == "missing":
        members = members[:1]
    elif change == "evidence":
        task, finding = members[1]
        ref = finding.evidence[0].model_copy(update={"content_sha256": "0" * 64})
        members = (members[0], (task, finding.model_copy(update={"evidence": (ref,)})))
    elif change == "origin":
        (git_repo / "a.py").write_text("user change\n")
    else:
        (candidate.path / "src/b.py").write_text("user change\n")
    with pytest.raises(RepairError):
        candidate.apply_group(patch, context, members)
    assert (candidate.path / "a.py").read_text() == "old\n"


@pytest.mark.parametrize("race", [False, True])
def test_group_write_failure_rolls_back_only_owned_changes(git_repo, tmp_path, monkeypatch, race):
    from reviewflow.backend import repair_candidate

    candidate, context, members, patch = prepared(git_repo, tmp_path)
    replace = repair_candidate._replace
    calls = 0

    def fail_second(root, path, before, after):
        nonlocal calls
        calls += 1
        if calls == 2:
            if race:
                (candidate.path / "a.py").write_text("user race\n")
            raise RepairError("Synthetic write failure")
        return replace(root, path, before, after)

    monkeypatch.setattr(repair_candidate, "_replace", fail_second)
    with pytest.raises(PartialApplicationError if race else RepairError):
        candidate.apply_group(patch, context, members)
    assert (candidate.path / "a.py").read_text() == ("user race\n" if race else "old\n")
    assert (candidate.path / "src/b.py").read_text() == "b = 1\n"
    assert (git_repo / "a.py").read_text() == "old\n"
