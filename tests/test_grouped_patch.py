"""Grouped edits retain individual provenance against one immutable snapshot."""
import json

import pytest
from test_live_patch import fixture

from reviewflow.backend.repair import RepairError


def group_fixture(tmp_path):
    context, task, first = fixture(tmp_path)
    second = first.model_copy(update={"finding_id": "second"})
    members = ((task, first), (task, second))
    ref = first.evidence[0]
    proposal = {"snapshot_id": task.snapshot_id, "explanation": "Two independent corrections",
        "patches": [{"task_id": task.task_id, "finding_id": finding.finding_id,
            "edits": [{"path": ref.path, "before_sha256": ref.content_sha256,
                       "old_text": old, "new_text": new}]}
            for finding, old, new in [(first, "value = 1", "value = 100"),
                                      (second, "other = 2", "other = 200")]]}
    return context, members, proposal


def validate(context, members, data):
    from reviewflow.backend.grouped_patch import GroupEditProposal, validate_group
    return validate_group(context, members, GroupEditProposal.model_validate_json(json.dumps(data)))


@pytest.mark.parametrize("reverse", [False, True])
def test_group_combines_same_file_edits_using_original_coordinates(tmp_path, reverse):
    context, members, data = group_fixture(tmp_path)
    if reverse:
        data["patches"].reverse()
    checked = validate(context, members, data)
    assert checked.replacements[0].content == "value = 100\nother = 200\n"
    assert len(checked.members) == 2
    assert {p.proposal.finding_id for p in checked.members} == {f.finding_id for _, f in members}
    assert (tmp_path / "app.py").read_text() == "value = 1\nother = 2\n"


@pytest.mark.parametrize("mode", ["missing", "duplicate", "foreign", "task", "hash", "snapshot",
                                  "path", "overlap", "noop"])
def test_invalid_member_rejects_whole_group_without_source_changes(tmp_path, mode):
    context, members, data = group_fixture(tmp_path)
    member = data["patches"][1]
    edit = member["edits"][0]
    if mode == "missing":
        data["patches"].pop()
    elif mode == "duplicate":
        data["patches"][1] = data["patches"][0]
    elif mode == "foreign":
        member["finding_id"] = "unknown"
    elif mode == "task":
        member["task_id"] = "unknown"
    elif mode == "hash":
        edit["before_sha256"] = "0" * 64
    elif mode == "snapshot":
        data["snapshot_id"] = "0" * 64
    elif mode == "path":
        edit["path"] = "outside.py"
    elif mode == "overlap":
        edit.update(old_text="value = 1\nother = 2", new_text="combined")
    elif mode == "noop":
        edit["new_text"] = edit["old_text"]
    with pytest.raises(RepairError):
        validate(context, members, data)
    assert (tmp_path / "app.py").read_text() == "value = 1\nother = 2\n"


def test_identical_exact_edits_retain_both_claim_owners(tmp_path):
    context, members, data = group_fixture(tmp_path)
    data["patches"][1]["edits"] = data["patches"][0]["edits"]
    checked = validate(context, members, data)
    assert len(checked.members) == 2
    assert checked.replacements[0].content == "value = 100\nother = 2\n"


def test_same_finding_id_across_tasks_remains_two_distinct_owners(tmp_path):
    context, members, data = group_fixture(tmp_path)
    task, finding = members[1]
    other = task.model_copy(update={"task_id": "security", "role": "security"})
    finding = finding.model_copy(update={"task_id": "security", "finding_id": members[0][1].finding_id})
    members = (members[0], (other, finding))
    data["patches"][1].update(task_id="security", finding_id=finding.finding_id)
    assert len(validate(context, members, data).members) == 2
