import json

import pytest

from reviewflow.models import ScanPolicy
from reviewflow.review.interactive import SessionDemoBackend, run_interactive_review
from reviewflow.review.workflow_models import RunLimits
from reviewflow.snapshot import build_inventory
from reviewflow.sources.directory import DirectorySource


def scanned(tmp_path, text):
    (tmp_path / "a.py").write_text(text)
    source = DirectorySource(tmp_path)
    return source, build_inventory(source, ScanPolicy())


def test_empty_search_feedback_and_final_turn_contract(tmp_path):
    source, inventory = scanned(tmp_path, "x = 1\n")
    seen = []

    class Backend:
        def complete(self, request):
            data = json.loads(request.payload)
            seen.append((data, request.response_schema))
            if data["tool_round"] < 2:
                return json.dumps({"requests": [{"kind": "search", "query": "not-present"}]})
            return json.dumps({"result": {"findings": [], "reviewed_paths": ["a.py"]}})

    result = run_interactive_review(source, inventory, Backend(), mode="single")
    assert result.report.status == "complete"
    expected = [{"request": {"kind": "search", "query": "not-present"}, "matches": []}]
    assert seen[1][0].get("tool_results") == expected
    assert seen[2][0].get("tool_results") == expected
    assert [data["tool_rounds_remaining"] for data, _ in seen] == [2, 1, 0]
    assert seen[2][1]["properties"]["requests"]["maxItems"] == 0
    assert "anyOf" not in seen[2][1]["properties"]["result"]
    assert seen[0][1]["properties"]["requests"]["maxItems"] == 3
    # Query feedback is in-memory protocol data, not an expansion of saved sessions.
    assert result.retrievals[0]["evidence"] == []


@pytest.mark.parametrize("live_policy,complete", [(True, True), (False, False)])
def test_source_completeness_is_explicit_but_not_semantic_coverage(tmp_path, live_policy, complete):
    source, inventory = scanned(tmp_path, "x = 1\n" * 210)
    seen = []
    demo = SessionDemoBackend()

    class Backend:
        def complete(self, request):
            data = json.loads(request.payload)
            seen.append((request.stage, data))
            if request.stage == "reviewer":
                return json.dumps({"result": {"findings": [], "reviewed_paths": []}})
            return demo.complete(request)

    result = run_interactive_review(source, inventory, Backend(), live_policy=live_policy,
                                    limits=RunLimits(max_tasks=2, max_context_bytes=32768))
    planner = next(data for stage, data in seen if stage == "planner")
    assert planner["available_files"] == [{"path": "a.py", "line_count": 210}]
    assert planner["reviewer_context_policy"]["preload_complete_files_if_fits"] is live_policy
    for stage, data in seen:
        if stage == "reviewer":
            assert data["source_coverage"]["files"] == [
                {"path": "a.py", "line_count": 210, "fully_supplied": complete}]
    # Supplying all bytes cannot silently turn an empty declaration into reviewed coverage.
    assert result.report.unreviewed_paths == ("a.py",)
    assert result.report.status == "partial"


def test_source_completeness_updates_after_explicit_read(tmp_path):
    source, inventory = scanned(tmp_path, "x = 1\n" * 210)
    seen = []

    class Backend:
        def complete(self, request):
            data = json.loads(request.payload)
            seen.append(data)
            if data["tool_round"] == 0:
                return json.dumps({"result": None, "requests": [
                    {"kind": "read", "path": "a.py", "start_line": 201, "end_line": 210}]})
            return json.dumps({"result": {"findings": [], "reviewed_paths": ["a.py"]}})

    result = run_interactive_review(source, inventory, Backend(), mode="single")
    assert [d["source_coverage"]["files"][0]["fully_supplied"] for d in seen] == [False, True]
    assert result.report.status == "complete"


def test_context_budget_keeps_file_prefixes_instead_of_tail_only_or_starved_paths(tmp_path):
    (tmp_path / "a.py").write_text("a = '" + "x" * 30 + "'\n" + "a = 1\n" * 199)
    (tmp_path / "b.py").write_text("b = 1\n" * 210)
    (tmp_path / "c.py").write_text("c = 1\n" * 100)
    source = DirectorySource(tmp_path)
    inventory = build_inventory(source, ScanPolicy())
    seen = []
    demo = SessionDemoBackend()

    class Backend:
        def complete(self, request):
            data = json.loads(request.payload)
            seen.append((request.stage, data))
            if request.stage == "reviewer":
                # No full-file coverage claim for bounded excerpts.
                return json.dumps({"result": {"findings": [], "reviewed_paths": []}})
            return demo.complete(request)

    result = run_interactive_review(
        source,
        inventory,
        Backend(),
        live_policy=True,
        limits=RunLimits(max_tasks=2, max_context_bytes=700),
    )
    planner = next(data for stage, data in seen if stage == "planner")
    assert {ref["path"] for ref in planner["files"]} == {"a.py", "b.py", "c.py"}
    assert all(ref["start_line"] == 1 for ref in planner["files"])
    reviewers = [data for stage, data in seen if stage == "reviewer"]
    assert len(reviewers) == 2
    for data in reviewers:
        assert set(data["task"]["paths"]) == {"a.py", "b.py", "c.py"}
        assert all(p["evidence"]["start_line"] == 1 for p in data["files"])
        assert sum(len(p["text"].encode()) for p in data["files"]) <= 700
        assert all(not f["fully_supplied"] for f in data["source_coverage"]["files"])
    assert result.report.unreviewed_paths == ("a.py", "b.py", "c.py")
    assert result.report.status == "partial"


@pytest.mark.parametrize("start_line", [1, 100])
def test_retrieving_overlapping_ranges_does_not_charge_lines_twice(tmp_path, start_line):
    (tmp_path / "a.py").write_text("x=1\n" * 200)
    (tmp_path / "b.py").write_text("x=1\n" * 100)
    source = DirectorySource(tmp_path)
    inventory = build_inventory(source, ScanPolicy())

    class Backend:
        def complete(self, request):
            data = json.loads(request.payload)
            if request.stage == "planner":
                return json.dumps(
                    {
                        "tasks": [
                            {
                                "task_id": role,
                                "role": role,
                                "snapshot_id": data["snapshot_id"],
                                "paths": ["a.py"],
                                "objective": "Inspect a only",
                            }
                            for role in ("correctness", "security")
                        ]
                    }
                )
            if data["tool_round"] == 0:
                return json.dumps(
                    {
                        "requests": [
                            {
                                "kind": "read",
                                "path": "a.py",
                                "start_line": start_line,
                                "end_line": 200,
                            }
                        ]
                    }
                )
            assert sum(len(p["text"].encode()) for p in data["files"]) == 800
            return json.dumps({"result": {"findings": [], "reviewed_paths": ["a.py"]}})

    result = run_interactive_review(
        source,
        inventory,
        Backend(),
        limits=RunLimits(max_context_bytes=800),
    )
    assert result.report.errors == ()
    assert all(task.status == "completed" for task in result.report.tasks)
    assert result.report.reported_reviewed_paths == ("a.py",)
    assert result.report.unreviewed_paths == ("b.py",)


def test_context_budget_never_substitutes_a_tail_when_first_line_cannot_fit(tmp_path):
    source, inventory = scanned(tmp_path, "#" * 1000 + "\n" + "x=1\n" * 209)
    result = run_interactive_review(
        source,
        inventory,
        SessionDemoBackend(),
        mode="single",
        limits=RunLimits(max_context_bytes=200),
    )
    assert result.report.supplied_paths == ()
    assert result.report.calls == 0
    assert ("a.py", "context_budget") in result.report.omissions
    assert result.report.unreviewed_paths == ("a.py",)


def test_long_file_retrieval_and_global_line_evidence(tmp_path):
    source, inventory = scanned(tmp_path, "x = 1\n" * 209 + "# REVIEWFLOW_DEMO_FINDING\n")
    result = run_interactive_review(source, inventory, SessionDemoBackend(), mode="single")
    assert result.report.status == "complete"
    assert result.report.findings[0].finding.evidence[0].start_line == 210
    assert len(result.retrievals) == 1
    assert result.report.execution == "simulated" and not result.report.semantic_review_performed


def test_reviewers_are_independent_and_chunks_not_shared(tmp_path):
    source, inventory = scanned(tmp_path, "x = 1\n" * 210)
    seen = []
    demo = SessionDemoBackend()

    class Backend:
        def complete(self, request):
            data = json.loads(request.payload)
            if request.stage == "reviewer":
                seen.append(data)
            return demo.complete(request)

    result = run_interactive_review(source, inventory, Backend())
    initial = [r for r in seen if r.get("tool_round") == 0]
    assert len(initial) == 2
    assert [p["evidence"] for p in initial[0]["files"]] == [
        p["evidence"] for p in initial[1]["files"]
    ]
    assert all("findings" not in r for r in initial)
    assert result.report.status == "complete"


def test_partial_chunk_cannot_claim_full_file_coverage(tmp_path):
    source, inventory = scanned(tmp_path, "x=1\n" * 210)

    class Backend:
        def complete(self, request):
            return json.dumps(
                {"result": {"findings": [], "reviewed_paths": ["a.py"]}, "requests": []}
            )

    result = run_interactive_review(source, inventory, Backend(), mode="single")
    assert result.report.status == "failed"
    assert result.report.unreviewed_paths == ("a.py",)
    assert any("incomplete_chunk_coverage" in error for error in result.report.errors)


@pytest.mark.parametrize("kind", ["escape", "extra_turn", "context", "call"])
def test_retrieval_boundaries_fail_closed(tmp_path, kind):
    source, inventory = scanned(tmp_path, "x=1\n" * 210)

    class Backend:
        def complete(self, request):
            return json.dumps(
                {
                    "result": None,
                    "requests": [
                        {
                            "kind": "read",
                            "path": "../private.py" if kind == "escape" else "a.py",
                            "start_line": 201,
                            "end_line": 210,
                        }
                    ],
                }
            )

    limits = RunLimits(
        max_calls=1 if kind == "call" else 12,
        max_context_bytes=800 if kind == "context" else 131072,
    )
    result = run_interactive_review(source, inventory, Backend(), mode="single", limits=limits)
    assert result.report.status == "failed"
    assert result.report.errors
    assert result.report.calls <= limits.max_calls
    assert len(result.retrievals) <= 2


def test_verifier_can_request_one_supplement_but_not_two(tmp_path):
    source, inventory = scanned(tmp_path, "# REVIEWFLOW_DEMO_FINDING\nx=1\n")
    demo = SessionDemoBackend()
    calls = []

    class Backend:
        def complete(self, request):
            if request.stage == "verifier":
                calls.append(request)
                return json.dumps(
                    {
                        "result": None,
                        "requests": [
                            {
                                "kind": "search",
                                "query": "x=1",
                            }
                        ],
                    }
                )
            return demo.complete(request)

    result = run_interactive_review(source, inventory, Backend())
    assert len(calls) == 2
    assert result.report.status == "partial"
    assert any("retrieval_budget" in error for error in result.report.errors)


def test_duplicate_suggestions_keep_original_findings_and_verify_each(tmp_path):
    source, inventory = scanned(tmp_path, "# REVIEWFLOW_DEMO_FINDING\n")
    demo = SessionDemoBackend()
    verified = []

    class Backend:
        def complete(self, request):
            data = json.loads(request.payload)
            if request.stage == "reviewer" and data["task"]["role"] == "security":
                data["task"]["role"] = "correctness"
                request = type(request)(
                    request.stage, request.system, json.dumps(data), request.response_schema
                )
            if request.stage == "verifier":
                verified.append(data["finding"]["finding_id"])
            return demo.complete(request)

    result = run_interactive_review(source, inventory, Backend())
    assert len(result.report.findings) == 2
    assert len(result.duplicate_groups) == 1
    assert set(result.duplicate_groups[0].member_ids) == set(verified)
    assert all(out.verification.verdict == "accepted" for out in result.report.findings)


@pytest.mark.parametrize("stage", ["reviewer", "verifier"])
def test_blank_search_returns_report_instead_of_escaping_workflow(tmp_path, stage):
    source, inventory = scanned(tmp_path, "# REVIEWFLOW_DEMO_FINDING\nx=1\n")
    demo = SessionDemoBackend()

    class Backend:
        def complete(self, request):
            if request.stage == stage:
                return json.dumps({"requests": [{"kind": "search", "query": " \t\n "}]})
            return demo.complete(request)

    result = run_interactive_review(
        source, inventory, Backend(), mode="single" if stage == "reviewer" else "multi"
    )
    assert result.report.status == ("failed" if stage == "reviewer" else "partial")
    assert result.report.errors
    assert not result.retrievals


def test_task_search_is_not_starved_by_other_files_matching_first(tmp_path):
    (tmp_path / "a.py").write_text("needle\n" * 20200)  # 101 earlier matching chunks
    (tmp_path / "b.py").write_text("x=1\n" * 200 + "needle\n")
    source = DirectorySource(tmp_path)
    inventory = build_inventory(source, ScanPolicy())

    class Backend:
        def complete(self, request):
            data = json.loads(request.payload)
            if request.stage == "planner":
                return json.dumps(
                    {
                        "tasks": [
                            {
                                "task_id": role,
                                "role": role,
                                "snapshot_id": data["snapshot_id"],
                                "paths": ["b.py"],
                                "objective": "Inspect b",
                            }
                            for role in ("correctness", "security")
                        ]
                    }
                )
            if data["tool_round"] == 0:
                return json.dumps({"requests": [{"kind": "search", "query": "needle"}]})
            return json.dumps({"result": {"findings": [], "reviewed_paths": ["b.py"]}})

    result = run_interactive_review(source, inventory, Backend(), mode="multi-no-verifier")
    assert result.report.reported_reviewed_paths == ("b.py",)
    assert result.report.unreviewed_paths == ("a.py",)
    assert len(result.retrievals) == 2
    for event in result.retrievals:
        assert [(ref["path"], ref["start_line"]) for ref in event["evidence"]] == [("b.py", 201)]


def test_scoped_index_search_limits_only_allowed_matches(tmp_path):
    from reviewflow.backend.context_index import ContextIndex

    (tmp_path / "a.py").write_text("needle\n" * 20200)
    (tmp_path / "b.py").write_text("needle\n" * 201)
    source = DirectorySource(tmp_path)
    inventory = build_inventory(source, ScanPolicy())
    index = ContextIndex(source, inventory, max_chunks=1000, max_bytes=8388608)
    packets = index.search("needle", limit=1, allowed_paths={"b.py"})
    assert [(p.evidence.path, p.evidence.start_line) for p in packets] == [("b.py", 1)]
    assert index.search("needle", allowed_paths=set()) == ()
    with pytest.raises(ValueError):
        index.search("needle", allowed_paths={"unknown.py"})


def test_invalid_duplicate_ids_cannot_remove_findings(tmp_path):
    source, inventory = scanned(tmp_path, "# REVIEWFLOW_DEMO_FINDING\n")
    demo = SessionDemoBackend()

    class Backend:
        def complete(self, request):
            data = json.loads(request.payload)
            if data.get("operation") == "deduplicate":
                return json.dumps(
                    {"groups": [{"member_ids": ["fake", "fake2"], "rationale": "merge"}]}
                )
            return demo.complete(request)

    result = run_interactive_review(source, inventory, Backend())
    assert len(result.report.findings) == 1
    assert result.duplicate_groups == ()
    assert result.report.status == "partial"
