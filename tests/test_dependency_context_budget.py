"""Dependency reads must not spend the source budget twice on the same lines."""
import json

import pytest
from test_dependency_verification import DependencyAdapter, setup_cross_batch

from reviewflow.backend.live_repair import run_repair_session
from reviewflow.backend.transport import Reply, Usage


class OverlapAdapter(DependencyAdapter):
    def __init__(self, model, ranges):
        super().__init__(model)
        self.ranges = ranges
        self.turns = []

    def __call__(self, wire, **kwargs):
        data = json.loads(wire)["payload"]
        if not data.get("dependency_verification"):
            return super().__call__(wire, **kwargs)
        self.sent.append(wire)
        self.turns.append(data)
        turn = len(self.turns) - 1
        if turn < len(self.ranges):
            result = None
            requests = [{"kind": "read", "path": "src/b.py", "start_line": start, "end_line": end}
                        for start, end in self.ranges[turn]]
        else:
            refs = [p["evidence"] for p in data["files"] if p["evidence"]["path"] == "src/b.py"]
            result = {"finding_id": data["finding"]["finding_id"], "verdict": "rejected",
                      "rationale": "Synthetic dependency excludes issue",
                      "evidence": [{**refs[0], "start_line": min(r["start_line"] for r in refs),
                                    "end_line": max(r["end_line"] for r in refs)}]}
            requests = []
        return Reply(json.dumps({"result": result, "requests": requests}), Usage(10, 5))


def run_overlap(repo, tmp_path, ranges, source):
    plan, config, profiles, runner, _, authority = setup_cross_batch(
        repo, tmp_path, dependency_source=source)
    adapter = OverlapAdapter(config.model, ranges)
    return run_repair_session(plan, config, authority, profiles, runner, adapter), adapter


def test_overlapping_reads_fit_unique_source_budget_without_skipping_evidence(git_repo, tmp_path):
    source = "#" + "x" * 14998 + "\n" + "#" + "y" * 14998 + "\n" + "#" * 2766 + "\n"
    result, adapter = run_overlap(git_repo, tmp_path, [[(1, 1)], [(1, 2)]], source)
    assert result["status"] == "no_changes", result
    assert len(adapter.turns) == 3
    final = adapter.turns[-1]
    assert sum(len(p["text"].encode()) for p in final["files"]) == 30004
    assert final["context_budget"] == {"max_bytes": 32768, "supplied_bytes": 30004,
                                       "remaining_bytes": 2764}
    assert next(f for f in final["available"] if f["path"] == "src/b.py")["byte_count"] == 32767
    attempt = result["dependency_verifications"][0]
    assert attempt["status"] == "complete" and attempt["verification"]["verdict"] == "rejected"
    assert result["initial_reviews"][0]["findings"][0]["verification"]["verdict"] == "unresolved"


@pytest.mark.parametrize("ranges", [
    [[(1, 1), (3, 3)], [(1, 3)]],
    [[(1, 3)], [(2, 2)]],
    [[(2, 2)], [(1, 3)]],
    [[(1, 2)], [(2, 3)]],
])
def test_supplied_packets_cover_requested_union_without_duplicate_lines(git_repo, tmp_path, ranges):
    source = "a\nb\nc\n" + "#" * 32760 + "\n"
    result, adapter = run_overlap(git_repo, tmp_path, ranges, source)
    assert result["status"] == "no_changes", result
    seen = set()
    for packet in adapter.turns[-1]["files"]:
        ref = packet["evidence"]
        for line in range(ref["start_line"], ref["end_line"] + 1):
            identity = (ref["path"], line)
            assert identity not in seen
            seen.add(identity)
    assert seen == {("a.py", 1), ("src/b.py", 1), ("src/b.py", 2), ("src/b.py", 3)}


def test_final_turn_preserves_completed_dependency_read_state(git_repo, tmp_path):
    # Removing history from the stateless request must fail this boundary contract:
    # zero remaining reads does not mean previously supplied dependencies vanished.
    source = "a\nb\nc\n" + "#" * 32760 + "\n"
    result, adapter = run_overlap(git_repo, tmp_path, [[(1, 1)], [(1, 2)]], source)
    assert result["status"] == "no_changes", result
    first, second, final = adapter.turns
    assert first["dependency_read_state"] == {
        "review_paths": ["a.py"], "supplied_dependency_paths": [], "satisfied": False}
    for payload in (second, final):
        assert payload["dependency_read_state"] == {
            "review_paths": ["a.py"], "supplied_dependency_paths": ["src/b.py"], "satisfied": True}
    assert final["tool_rounds_remaining"] == 0
    assert result["dependency_verifications"][0]["requests_attempted"] == 3


def test_completed_dependency_reads_do_not_override_unresolved_verdict(git_repo, tmp_path):
    class Uncertain(OverlapAdapter):
        def __call__(self, wire, **kwargs):
            reply = super().__call__(wire, **kwargs)
            if json.loads(wire)["payload"].get("dependency_verification"):
                output = json.loads(reply.text)
                if output["result"] is not None:
                    output["result"]["verdict"] = "unresolved"
                    output["result"]["rationale"] = "Remote implementation is not available"
                    return Reply(json.dumps(output), reply.usage)
            return reply

    plan, config, profiles, runner, _, authority = setup_cross_batch(
        git_repo, tmp_path, dependency_source="a\nb\nc\n" + "#" * 32760 + "\n")
    adapter = Uncertain(config.model, [[(1, 1)], [(1, 2)]])
    result = run_repair_session(plan, config, authority, profiles, runner, adapter)
    assert adapter.turns[-1]["dependency_read_state"]["satisfied"] is True
    assert result["status"] == "blocked" and result["application"] is None
    assert result["dependency_verifications"][0]["diagnostic_code"] == "dependency_unresolved"
    assert not result["patch_attempts"]
    assert (git_repo / "a.py").read_text() == "old\n"


def test_genuinely_oversized_read_stops_with_bounded_metadata(git_repo, tmp_path):
    source = "#" + "x" * 14998 + "\n" + "#" + "y" * 14998 + "\n" + "#" * 2766 + "\n"
    result, adapter = run_overlap(git_repo, tmp_path, [[(1, 1)], [(1, 3)]], source)
    assert result["status"] == "blocked" and result["application"] is None
    assert len(adapter.turns) == 2
    entry = result["dependency_verifications"][0]
    assert entry["diagnostic_code"] == "dependency_context_exhausted"
    rejected = entry["context_rejection"]
    assert rejected["buffered_bytes"] == 15004
    assert rejected["additional_bytes"] == 17767
    assert rejected["limit_bytes"] == 32768
    assert rejected["evidence"]["path"] == "src/b.py"
    assert rejected["evidence"]["start_line"] == 1 and rejected["evidence"]["end_line"] == 3
    assert set(rejected) == {"buffered_bytes", "additional_bytes", "limit_bytes", "evidence"}
    assert (git_repo / "a.py").read_text() == "old\n"
