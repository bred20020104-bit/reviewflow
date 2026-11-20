"""Synthetic product fixtures test patch provenance, not target defect detection."""

import hashlib
import json

import pytest
from test_repair_workspace import proposal_for

from reviewflow.backend.repair import RepairError
from reviewflow.models import ScanPolicy
from reviewflow.review.context import ReviewContext
from reviewflow.snapshot import build_inventory
from reviewflow.sources.directory import DirectorySource


def fixture(tmp_path, text="value = 1\nother = 2\n", path="app.py"):
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    source = DirectorySource(tmp_path)
    inventory = build_inventory(source, ScanPolicy())
    task, finding, _ = proposal_for(source, inventory, path=path)
    context = ReviewContext(source, inventory)
    return context, task, finding


def edit_proposal(task, finding, edits=None):
    from reviewflow.backend.live_patch import EditProposal

    ref = finding.evidence[0]
    return EditProposal.model_validate_json(json.dumps({
        "snapshot_id": task.snapshot_id, "finding_id": finding.finding_id,
        "edits": edits if edits is not None else [
            {"path": ref.path, "before_sha256": ref.content_sha256,
             "old_text": "value = 1", "new_text": "value = 3"}],
        "explanation": "Correct the synthetic value",
    }))


def test_responses_adapter_accepts_patch_stage_with_existing_limits():
    from test_responses_adapter import model

    from reviewflow.backend.responses_adapter import build_body

    wire = json.dumps({"stage": "patch", "system": "Patch instruction", "payload": {},
                       "response_schema": {"type": "object", "properties": {}}})
    body = json.loads(build_body(model("http://127.0.0.1/v1"), wire, 2048))
    assert body["text"]["format"]["name"] == "reviewflow_patch"
    assert body["max_output_tokens"] == 2048
    assert body["store"] is False and body["stream"] is False


def test_exact_edits_translate_to_checked_full_replacement_without_writing(tmp_path):
    from reviewflow.backend.live_patch import translate_edits

    context, task, finding = fixture(tmp_path)
    patch = translate_edits(context, task, finding, edit_proposal(task, finding))
    assert patch.proposal.replacements[0].content == "value = 3\nother = 2\n"
    assert "+value = 3" in patch.diff
    assert (tmp_path / "app.py").read_text() == "value = 1\nother = 2\n"


@pytest.mark.parametrize("text,changes", [
    ("value = 1\nvalue = 1\n", [("value = 1", "value = 3")]),
    ("aaa\n", [("aa", "b")]),
    ("abcdef\n", [("abc", "x"), ("bcd", "y")]),
    ("abc\n", [("missing", "x")]),
    ("abc\n", [("abc", "abc")]),
    ("abc\n", [("abc", "x\u0000")]),
    ("abc\n", [("abc", "x"), ("x", "y")]),
    ("abc\n", [("abc", "x"), ("abc", "y")]),
])
def test_ambiguous_overlapping_absent_or_noop_edits_are_rejected(tmp_path, text, changes):
    from reviewflow.backend.live_patch import translate_edits

    context, task, finding = fixture(tmp_path, text)
    ref = finding.evidence[0]
    edits = [{"path": ref.path, "before_sha256": ref.content_sha256,
              "old_text": old, "new_text": new} for old, new in changes]
    with pytest.raises(RepairError):
        translate_edits(context, task, finding, edit_proposal(task, finding, edits))
    assert (tmp_path / "app.py").read_text() == text


def test_nonoverlapping_edits_use_original_coordinates(tmp_path):
    from reviewflow.backend.live_patch import translate_edits

    context, task, finding = fixture(tmp_path, "one two three\n")
    ref = finding.evidence[0]
    edits = [{"path": ref.path, "before_sha256": ref.content_sha256,
              "old_text": old, "new_text": new}
             for old, new in [("three", "3"), ("one", "first-value")]]
    patch = translate_edits(context, task, finding, edit_proposal(task, finding, edits))
    assert patch.proposal.replacements[0].content == "first-value two 3\n"


@pytest.mark.parametrize("field", ["snapshot_id", "finding_id", "before_sha256", "path"])
def test_foreign_snapshot_finding_hash_and_path_cannot_be_applied(tmp_path, field):
    from reviewflow.backend.live_patch import EditProposal, translate_edits

    context, task, finding = fixture(tmp_path)
    value = edit_proposal(task, finding).model_dump(mode="json")
    if field == "snapshot_id":
        value[field] = "0" * 64
    elif field == "finding_id":
        value[field] = "different"
    else:
        value["edits"][0][field] = "0" * 64 if field == "before_sha256" else "elsewhere.py"
    with pytest.raises(RepairError):
        translate_edits(context, task, finding, EditProposal.model_validate_json(json.dumps(value)))


@pytest.mark.parametrize("path", ["tests/a.py", "test_a.py", "config.py", "pkg/relay_config.py",
    "settings.py", "wsgi.py", "setup.py", "sitecustomize.py", "migrations/a.py"])
def test_live_patch_preserves_tests_configuration_and_startup_files(tmp_path, path):
    from reviewflow.backend.live_patch import translate_edits

    context, task, finding = fixture(tmp_path, path=path)
    with pytest.raises(RepairError):
        translate_edits(context, task, finding, edit_proposal(task, finding))


def test_live_patch_uses_frozen_profile_and_exact_checked_packets(tmp_path):
    from reviewflow.agents.profiles import load_profiles
    from reviewflow.backend.live_patch import propose_live_patch

    context, task, finding = fixture(tmp_path)
    profiles = load_profiles()
    requests = []

    class Backend:
        def complete(self, request):
            requests.append(request)
            return edit_proposal(task, finding).model_dump_json()

    patch = propose_live_patch(context, task, finding, Backend(), profiles)
    request = requests[0]
    payload = json.loads(request.payload)
    assert request.stage == "patch"
    assert request.system == profiles.system_for("patch")
    assert "old_text" in request.system and "new_text" in request.system
    assert "edits" in request.response_schema["properties"]
    assert payload["files"][0]["text"] == "value = 1\nother = 2\n"
    ref = payload["files"][0]["evidence"]
    assert ref["content_sha256"] == hashlib.sha256(b"value = 1\nother = 2\n").hexdigest()
    assert (ref["start_line"], ref["end_line"]) == (1, 2)
    assert payload["feedback"] is None
    assert patch.proposal.replacements[0].content == "value = 3\nother = 2\n"


def test_patch_context_budget_rejects_before_backend_send(tmp_path):
    from reviewflow.agents.profiles import load_profiles
    from reviewflow.backend.live_patch import propose_live_patch

    context, task, finding = fixture(tmp_path, "x" * 32769 + "\n")

    class Backend:
        def complete(self, request):
            pytest.fail("Oversized patch context sent")

    with pytest.raises(RepairError):
        propose_live_patch(context, task, finding, Backend(), load_profiles())


def test_live_patch_crosses_real_gateway_transport_and_loopback_http(tmp_path):
    from pydantic import SecretStr
    from test_development_controls import development_fixture
    from test_responses_adapter import api_server, response

    from reviewflow.agents.profiles import load_profiles
    from reviewflow.backend.approval import ApprovalGate
    from reviewflow.backend.development import DevelopmentTransport, load_development_config
    from reviewflow.backend.gateway import WorkflowGateway
    from reviewflow.backend.live_patch import propose_live_patch
    from reviewflow.backend.responses_adapter import ResponsesAdapter

    path, source, inventory = development_fixture(tmp_path)
    task, finding, _ = proposal_for(source, inventory)
    ref = finding.evidence[0]
    proposal = edit_proposal(task, finding, [{"path": "a.py", "before_sha256": ref.content_sha256,
                                            "old_text": "x = 1", "new_text": "x = 2"}])
    with api_server(result=response(proposal.model_dump_json())) as (url, calls):
        config = load_development_config(path, source, inventory)
        config = config.model_copy(update={
            "model": config.model.model_copy(update={"base_url": url}),
            "privacy": config.privacy.model_copy(update={"allow_code_upload": True}),
        })
        adapter = ResponsesAdapter(config.model, lambda: SecretStr("synthetic-secret"))
        gate = ApprovalGate()
        transport = DevelopmentTransport(config, adapter, gate)
        gateway = WorkflowGateway(source, inventory, transport, adapter.body_size,
                                  lambda intent: gate.approve(intent, confirmed=True))
        patch = propose_live_patch(ReviewContext(source, inventory), task, finding,
                                   gateway, load_profiles())
        assert len(calls) == transport.calls == 1
        body = json.loads(calls[0][2])
        assert body["text"]["format"]["name"] == "reviewflow_patch"
        assert json.loads(body["input"])["files"][0]["text"] == "x = 1\n"
        assert patch.proposal.replacements[0].content == "x = 2\n"
        assert transport.records[0]["status"] == "complete"
        assert (source.info.root / "a.py").read_text() == "x = 1\n"


def test_patch_chunks_all_current_lines_without_oversized_evidence_ranges(tmp_path):
    from reviewflow.agents.profiles import load_profiles
    from reviewflow.backend.live_patch import propose_live_patch
    from reviewflow.review.context import MAX_EXCERPT_LINES

    extra = 51
    text = "value = 1\n" + "x\n" * (MAX_EXCERPT_LINES + extra - 1)
    context, task, finding = fixture(tmp_path, text)
    seen = []

    class Backend:
        def complete(self, request):
            seen.extend(json.loads(request.payload)["files"])
            return edit_proposal(task, finding).model_dump_json()

    propose_live_patch(context, task, finding, Backend(), load_profiles())
    assert [(p["evidence"]["start_line"], p["evidence"]["end_line"]) for p in seen] == [
        (1, MAX_EXCERPT_LINES),
        (MAX_EXCERPT_LINES + 1, MAX_EXCERPT_LINES + extra),
    ]
    assert "".join(p["text"] for p in seen) == (tmp_path / "app.py").read_bytes().decode("utf-8")


def test_stale_source_rejects_patch_request_before_backend_send(tmp_path):
    from reviewflow.agents.profiles import load_profiles
    from reviewflow.backend.live_patch import propose_live_patch

    context, task, finding = fixture(tmp_path)
    (tmp_path / "app.py").write_text("user changed this\n")

    class Backend:
        def complete(self, request):
            pytest.fail("Stale context reached backend")

    with pytest.raises(RepairError):
        propose_live_patch(context, task, finding, Backend(), load_profiles())


def test_exact_edit_cannot_produce_oversized_replacement(tmp_path):
    from reviewflow.backend.live_patch import translate_edits

    context, task, finding = fixture(tmp_path, "value = 1\n" + "# comment\n" * 7300)
    with pytest.raises(RepairError):
        translate_edits(context, task, finding, edit_proposal(task, finding))


def test_invalid_patch_output_never_leaks_backend_exception(tmp_path):
    from reviewflow.agents.profiles import load_profiles
    from reviewflow.backend.live_patch import propose_live_patch

    context, task, finding = fixture(tmp_path)

    class Backend:
        def complete(self, request):
            raise RuntimeError("PRIVATE_SENTINEL")

    with pytest.raises(RepairError) as error:
        propose_live_patch(context, task, finding, Backend(), load_profiles())
    assert "PRIVATE_SENTINEL" not in str(error.value)
