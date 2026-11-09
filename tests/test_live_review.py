import json
from dataclasses import replace

import pytest
from test_development_controls import development_fixture
from test_responses_adapter import api_server, response

from reviewflow.backend.approval import ApprovalGate, Denied
from reviewflow.backend.development import load_development_config
from reviewflow.backend.live_review import RunAuthorization, make_preview
from reviewflow.quick import main
from reviewflow.review.interactive import SessionDemoBackend, run_interactive_review
from reviewflow.review.report import render_review
from reviewflow.review.workflow_models import ModelRequest, RunLimits


def arguments(config, source, output):
    return [
        "--review",
        "--development",
        "--config",
        str(config),
        "-d",
        str(source.info.root),
        "--source",
        "directory",
        "--output",
        str(output),
        "--format",
        "json",
    ]


def test_cli_preview_does_not_send_or_create_output_and_excludes_key(tmp_path, capsys):
    path, source, inventory = development_fixture(tmp_path)
    output = tmp_path / "new-report"
    assert main(arguments(path, source, output)) == 0
    captured = capsys.readouterr()
    preview = json.loads(captured.out)
    assert preview["operation"] == "live_development_review"
    assert preview["selected_files"] == {"a.py": inventory.snapshot.records[0].sha256}
    assert preview["cost_usd"] is None
    assert preview["network_requests"] == 0
    assert len(preview["approval_digest"]) == 64
    assert "synthetic-secret" not in captured.out + captured.err
    assert not output.exists()


def test_preview_exposes_context_shortfall_and_bounded_batch_suggestions(tmp_path, capsys):
    path, source, _ = development_fixture(tmp_path)
    for name in ("a.py", "b.py", "c.py", "d.py", "e.py", "f.py"):
        (source.info.root / name).write_text("x = 1\n" * 1500)  # 9,000 bytes per file
    output = tmp_path / "new-report"
    assert main(arguments(path, source, output)) == 0
    preview = json.loads(capsys.readouterr().out)
    context = preview["context_preflight"]
    assert context["selected_source_bytes"] == 54000
    assert context["max_context_bytes"] == 32768
    assert context["whole_selection_fits"] is False
    assert {r["path"] for r in context["planner_ranges"]} == {
        "a.py",
        "b.py",
        "c.py",
        "d.py",
        "e.py",
        "f.py",
    }
    assert all(r["start_line"] == 1 for r in context["planner_ranges"])
    assert context["suggested_include_batches"] == [
        ["a.py", "b.py", "c.py"],
        ["d.py", "e.py", "f.py"],
    ]
    assert context["oversized_files"] == []
    assert preview["network_requests"] == 0
    assert not output.exists()


def test_preview_does_not_claim_oversized_file_fits_a_batch(tmp_path, capsys):
    path, source, _ = development_fixture(tmp_path)
    (source.info.root / "a.py").write_text("x = 1\n" * 6000)
    assert main(arguments(path, source, tmp_path / "new-report")) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["context_preflight"]["whole_selection_fits"] is False
    assert preview["context_preflight"]["oversized_files"] == ["a.py"]
    assert preview["context_preflight"]["suggested_include_batches"] == []


def test_batch_suggestions_recover_files_omitted_by_global_index_budget(tmp_path, capsys):
    path, source, _ = development_fixture(tmp_path)
    (source.info.root / "a.py").write_text("\n" * 200000)  # Exhausts the shared 1,000 chunks.
    (source.info.root / "b.py").write_text("x=1\n")
    assert main(arguments(path, source, tmp_path / "new-report")) == 0
    preview = json.loads(capsys.readouterr().out)
    context = preview["context_preflight"]
    assert context["whole_selection_fits"] is False
    assert context["oversized_files"] == ["a.py"]
    assert context["suggested_include_batches"] == [["b.py"]]


def test_lower_call_cap_binds_preview_and_stops_network(tmp_path, capsys):
    path, source, _ = development_fixture(tmp_path)
    secret = path.parent / "secrets.local.toml"
    with api_server(result=fixture_response) as (url, calls):
        secret.write_text(secret.read_text().replace("http://127.0.0.1:9999/v1", url))
        args = arguments(path, source, tmp_path / "report") + ["--max-calls", "1"]
        assert main(args) == 0
        preview = json.loads(capsys.readouterr().out)
        assert preview["run_limits"]["max_calls"] == 1
        assert main(args + ["--approve", preview["approval_digest"]]) == 1
        document = json.loads(capsys.readouterr().out)
        assert document["accounting"]["requests_attempted"] == len(calls) == 1
        assert "请求尝试: 1 / 1" in (tmp_path / "report" / "report.md").read_text()
        assert document["report"]["status"] != "complete"
        assert main(["--max-calls", "1"]) == 2


@pytest.mark.parametrize("cap", ["0", "9", "-1"])
def test_cli_call_cap_cannot_exceed_development_envelope(tmp_path, capsys, cap):
    path, source, _ = development_fixture(tmp_path)
    assert main(arguments(path, source, tmp_path / "report") + ["--max-calls", cap]) == 2
    assert not (tmp_path / "report").exists()


def test_skill_edit_rejects_old_cli_approval_before_network(tmp_path, capsys, monkeypatch):
    from test_agent_profiles import copied_resources

    root = copied_resources(tmp_path, monkeypatch)
    path, source, _ = development_fixture(tmp_path)
    secret = path.parent / "secrets.local.toml"
    output = tmp_path / "report"
    with api_server(result=fixture_response) as (url, calls):
        secret.write_text(secret.read_text().replace("http://127.0.0.1:9999/v1", url))
        args = arguments(path, source, output)
        assert main(args) == 0
        old = json.loads(capsys.readouterr().out)
        with (root / "skills/security/SKILL.md").open("a") as stream:
            stream.write("\nTRUSTED_SKILL_REVISION\n")
        assert main(args + ["--approve", old["approval_digest"]]) == 2
        assert not calls and not output.exists()
        capsys.readouterr()
        assert main(args) == 0
        new = json.loads(capsys.readouterr().out)
        assert new["prompt_digest"] != old["prompt_digest"]
        assert new["agent_profiles"]["bundle_digest"] != old["agent_profiles"]["bundle_digest"]


def test_live_graph_uses_same_frozen_bundle_loaded_for_approval(tmp_path, capsys, monkeypatch):
    from test_agent_profiles import copied_resources

    from reviewflow.backend import live_review

    root = copied_resources(tmp_path, monkeypatch)
    path, source, _ = development_fixture(tmp_path)
    secret = path.parent / "secrets.local.toml"
    authorization = live_review.RunAuthorization

    def mutate_after_approval(*args, **kwargs):
        result = authorization(*args, **kwargs)
        with (root / "skills/security/SKILL.md").open("a") as stream:
            stream.write("\nPOST_APPROVAL_RESOURCE_EDIT\n")
        return result

    monkeypatch.setattr(live_review, "RunAuthorization", mutate_after_approval)
    with api_server(result=fixture_response) as (url, calls):
        secret.write_text(secret.read_text().replace("http://127.0.0.1:9999/v1", url))
        args = arguments(path, source, tmp_path / "report")
        assert main(args) == 0
        preview = json.loads(capsys.readouterr().out)
        assert main(args + ["--approve", preview["approval_digest"]]) == 0
        document = json.loads(capsys.readouterr().out)
        assert len(calls) == 3
        bodies = [json.loads(body) for _, _, body in calls]
        security = next(
            b for b in bodies if json.loads(b["input"]).get("task", {}).get("role") == "security"
        )
        assert "# Security Reviewer" in security["instructions"]
        assert "Trust boundaries" in security["instructions"]
        assert "POST_APPROVAL_RESOURCE_EDIT" not in security["instructions"]
        assert document["preview"]["agent_profiles"] == preview["agent_profiles"]
        assert main(arguments(path, source, tmp_path / "next-report")) == 0
        next_preview = json.loads(capsys.readouterr().out)
        assert next_preview["prompt_digest"] != preview["prompt_digest"]


def test_changed_snapshot_rejects_old_approval_before_network(tmp_path, capsys):
    path, source, _ = development_fixture(tmp_path)
    output = tmp_path / "report"
    args = arguments(path, source, output)
    assert main(args) == 0
    digest = json.loads(capsys.readouterr().out)["approval_digest"]
    (source.info.root / "a.py").write_text("x=2\n")
    assert main(args + ["--approve", digest]) == 2
    assert not output.exists()


@pytest.mark.parametrize(
    "case",
    [
        "missing_development",
        "missing_config",
        "inside_target",
        "existing_output",
        "unknown_include",
        "flag_on_inventory",
    ],
)
def test_live_flags_and_output_locations_fail_closed(tmp_path, capsys, case):
    path, source, _ = development_fixture(tmp_path)
    output = tmp_path / "report"
    args = arguments(path, source, output)
    if case == "missing_development":
        args.remove("--development")
    elif case == "missing_config":
        i = args.index("--config")
        del args[i : i + 2]
    elif case == "inside_target":
        args[args.index("--output") + 1] = str(source.info.root / "new-report")
    elif case == "existing_output":
        output.mkdir()
    elif case == "unknown_include":
        args += ["--include", "../secrets.local.toml"]
    else:
        args.remove("--review")
    assert main(args) == 2
    assert "synthetic-secret" not in "".join(capsys.readouterr())


def test_child_grants_bind_config_scope_snapshot_and_full_intent(tmp_path):
    path, source, inventory = development_fixture(tmp_path)
    config = load_development_config(path, source, inventory)
    preview = make_preview(
        config, source, inventory, tmp_path / "report", "multi", RunLimits(max_calls=8)
    )
    gate = ApprovalGate()
    authorization = RunAuthorization(preview, preview["approval_digest"], gate)
    runtime_config = config.model_copy(
        update={"privacy": config.privacy.model_copy(update={"allow_code_upload": True})}
    )
    intent = {
        "action": "development_send",
        "snapshot": inventory.snapshot.snapshot_id,
        "files": preview["selected_files"],
        "root": str(source.info.root),
        "config": runtime_config.model_dump(mode="json"),
        "payload_sha256": "a" * 64,
        "request_bytes": 123,
    }
    for changes in (
        {"snapshot": "other"},
        {"files": {"other.py": "hash"}},
        {"config": {}},
        {"request_bytes": 65537},
        {"extra": "not-bound"},
    ):
        with pytest.raises(Denied):
            authorization({**intent, **changes})
    token = authorization(intent)
    gate.consume(token, intent)
    with pytest.raises(Denied):
        gate.consume(token, intent)
    for _ in range(7):
        gate.consume(authorization(intent), intent)
    with pytest.raises(Denied):
        authorization(intent)


def test_live_renderer_never_claims_simulation_or_zero_network(tmp_path):
    _, source, inventory = development_fixture(tmp_path)
    result = run_interactive_review(source, inventory, SessionDemoBackend(), mode="single")
    live = replace(result.report, execution="live_development", semantic_review_performed=True)
    text = render_review(live, "text")
    assert "模拟" not in text and "网络模型调用 0" not in text
    parsed = json.loads(render_review(live, "json"))
    assert parsed["execution"] == "live_development"
    assert parsed["semantic_review_performed"] is True
    assert not parsed["target_tests_executed"]
    assert "模拟" in render_review(result.report)


def fixture_response(body):
    payload = json.loads(body)
    stage = payload["text"]["format"]["name"].removeprefix("reviewflow_")
    request = ModelRequest(
        stage, payload["instructions"], payload["input"], payload["text"]["format"]["schema"]
    )
    return response(SessionDemoBackend().complete(request))


def test_real_cli_http_graph_preserves_target_and_writes_truthful_report(tmp_path, capsys):
    path, source, _ = development_fixture(tmp_path)
    secret = path.parent / "secrets.local.toml"
    original = (source.info.root / "a.py").read_bytes()
    with api_server(result=fixture_response) as (url, calls):
        secret.write_text(secret.read_text().replace("http://127.0.0.1:9999/v1", url))
        args = arguments(path, source, tmp_path / "report")
        assert main(args) == 0
        digest = json.loads(capsys.readouterr().out)["approval_digest"]
        assert not calls
        assert main(args + ["--approve", digest]) == 0
        document = json.loads(capsys.readouterr().out)
        assert document["report"]["execution"] == "live_development"
        assert document["report"]["status"] == "complete"
        assert document["report"]["semantic_review_performed"]
        assert document["accounting"]["requests_attempted"] == len(calls) == 3
        assert document["accounting"]["cost_usd"] is None
        assert document["source_snapshot_unchanged"]
        assert (tmp_path / "report" / "report.json").is_file()
        assert (tmp_path / "report" / "report.md").is_file()
        assert (source.info.root / "a.py").read_bytes() == original
        assert "synthetic-secret" not in (tmp_path / "report" / "report.json").read_text()
        assert main(args + ["--approve", digest]) == 2
        assert len(calls) == 3


def test_live_preloads_complete_files_and_bounds_reviewer_output(tmp_path, capsys):
    path, source, _ = development_fixture(tmp_path)
    (source.info.root / "a.py").write_text("x = 1\n" * 250)
    secret = path.parent / "secrets.local.toml"
    with api_server(result=fixture_response) as (url, calls):
        secret.write_text(secret.read_text().replace("http://127.0.0.1:9999/v1", url))
        args = arguments(path, source, tmp_path / "report")
        assert main(args) == 0
        preview = json.loads(capsys.readouterr().out)
        assert main(args + ["--approve", preview["approval_digest"]]) == 0
        document = json.loads(capsys.readouterr().out)
        assert document["report"]["status"] == "complete"
        assert len(calls) == 3  # planner plus two reviews, no redundant chunk round trips
        reviewer_bodies = [json.loads(body) for _, _, body in calls[1:]]
        for body in reviewer_bodies:
            payload = json.loads(body["input"])
            assert [p["evidence"]["end_line"] for p in payload["files"]] == [200, 250]
            assert payload["max_findings"] == 2
            assert (
                body["text"]["format"]["schema"]["$defs"]["ReviewOutput"]["properties"]["findings"][
                    "maxItems"
                ]
                == 2
            )
        assert preview["review_policy"]["max_findings_per_task"] == 2


def test_selected_subset_runs_and_binds_unselected_source(tmp_path, capsys):
    path, source, _ = development_fixture(tmp_path)
    other = source.info.root / "b.py"
    other.write_text("UNSELECTED_SENTINEL = 2\n")
    secret = path.parent / "secrets.local.toml"
    with api_server(result=fixture_response) as (url, calls):
        secret.write_text(secret.read_text().replace("http://127.0.0.1:9999/v1", url))
        args = arguments(path, source, tmp_path / "report") + ["--include", "a.py"]
        assert main(args) == 0
        preview = json.loads(capsys.readouterr().out)
        # A scoped run must pass internal snapshot checks, while reporting omitted coverage.
        assert main(args + ["--approve", preview["approval_digest"]]) == 1
        document = json.loads(capsys.readouterr().out)
        assert document["report"]["status"] == "partial"
        assert document["report"]["semantic_review_performed"]
        assert document["source_snapshot_unchanged"]
        assert len(calls) == 3
        assert all(b"UNSELECTED_SENTINEL" not in body for _, _, body in calls)
        assert document["preview"]["full_snapshot"] != document["preview"]["snapshot"]
        assert other.read_text() == "UNSELECTED_SENTINEL = 2\n"

        args = arguments(path, source, tmp_path / "second-report") + ["--include", "a.py"]
        assert main(args) == 0
        preview = json.loads(capsys.readouterr().out)
        other.write_text("UNSELECTED_SENTINEL = 3\n")
        assert main(args + ["--approve", preview["approval_digest"]]) == 2
        assert len(calls) == 3
        assert not (tmp_path / "second-report").exists()


def test_unicode_escaped_key_cannot_reach_cli_or_persisted_reports(tmp_path, capsys):
    path, source, _ = development_fixture(tmp_path)
    (source.info.root / "a.py").write_text("# REVIEWFLOW_DEMO_FINDING\nx = 1\n")
    key = "synthetic-secret"
    escaped = "".join(f"\\u{ord(c):04x}" for c in key)
    secret = path.parent / "secrets.local.toml"

    def echo_encoded_key(body):
        envelope = fixture_response(body)
        text = envelope["output"][0]["content"][0]["text"]
        data = json.loads(text)
        if "result" in data and data["result"].get("findings"):
            data["result"]["findings"][0]["root_cause"] = key
            text = json.dumps(data).replace(key, escaped)
            assert key not in text and key in json.dumps(json.loads(text))
            envelope["output"][0]["content"][0]["text"] = text
        return envelope

    with api_server(result=echo_encoded_key) as (url, calls):
        secret.write_text(secret.read_text().replace("http://127.0.0.1:9999/v1", url))
        args = arguments(path, source, tmp_path / "report") + ["--mode", "single"]
        assert main(args) == 0
        digest = json.loads(capsys.readouterr().out)["approval_digest"]
        code = main(args + ["--approve", digest])
        captured = capsys.readouterr()
        persisted = "".join(
            (tmp_path / "report" / name).read_text() for name in ("report.json", "report.md")
        )
        assert key not in captured.out + captured.err + persisted
        assert escaped not in captured.out + captured.err + persisted
        assert code == 1
        document = json.loads(captured.out)
        assert document["report"]["status"] == "failed"
        assert not document["report"]["semantic_review_performed"]
        assert len(calls) == 1


def test_reviewer_incomplete_reason_survives_cli_and_blocks_further_sends(tmp_path, capsys):
    path, source, _ = development_fixture(tmp_path)
    secret = path.parent / "secrets.local.toml"

    def incomplete_reviewer(body):
        data = json.loads(body)
        if data["text"]["format"]["name"] == "reviewflow_reviewer":
            return response(
                status="incomplete",
                incomplete_details={"reason": "max_output_tokens", "message": "synthetic-secret"},
            )
        return fixture_response(body)

    with api_server(result=incomplete_reviewer) as (url, calls):
        secret.write_text(secret.read_text().replace("http://127.0.0.1:9999/v1", url))
        args = arguments(path, source, tmp_path / "report")
        assert main(args) == 0
        digest = json.loads(capsys.readouterr().out)["approval_digest"]
        assert main(args + ["--approve", digest]) == 1
        captured = capsys.readouterr()
        document = json.loads(captured.out)
        assert len(calls) == document["accounting"]["requests_attempted"] == 2
        assert document["report"]["status"] == "failed"
        assert [task["error"] for task in document["report"]["tasks"]] == [
            "response_incomplete_max_output_tokens",
            "run_blocked",
        ]
        assert document["accounting"]["records"][1]["error_code"] == (
            "response_incomplete_max_output_tokens"
        )
        markdown = (tmp_path / "report" / "report.md").read_text()
        assert "response_incomplete_max_output_tokens" in markdown
        assert "synthetic-secret" not in captured.out + captured.err + markdown
        assert document["target_tests_executed"] is False
        assert document["patches_applied"] is False


def test_transport_failure_retains_failed_report_and_never_retries(tmp_path, capsys):
    path, source, _ = development_fixture(tmp_path)
    secret = path.parent / "secrets.local.toml"
    with api_server(status=429) as (url, calls):
        secret.write_text(secret.read_text().replace("http://127.0.0.1:9999/v1", url))
        args = arguments(path, source, tmp_path / "report")
        assert main(args) == 0
        digest = json.loads(capsys.readouterr().out)["approval_digest"]
        assert main(args + ["--approve", digest]) == 1
        report = json.loads(capsys.readouterr().out)
        assert report["report"]["status"] == "failed"
        assert not report["report"]["semantic_review_performed"]
        assert len(calls) == 1
        assert (tmp_path / "report" / "report.json").exists()
