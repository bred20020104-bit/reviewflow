import json
from pathlib import Path

import pytest

from reviewflow.quick import main


def test_config_check_placeholder_fails_without_env_discovery(monkeypatch, capsys):
    monkeypatch.setenv("REVIEWFLOW_API_KEY", "SECRET")
    assert main(["config-check", "--config", "config.example.toml"]) == 2
    assert "SECRET" not in capsys.readouterr().err


def test_context_cli_chunks_long_file_without_printing_source(tmp_path, capsys):
    (tmp_path / "a.py").write_text("PRIVATE_CONTENT\n" * 450)
    assert main(["context", "-d", str(tmp_path), "--source", "directory"]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["chunks"] == 3
    assert "PRIVATE_CONTENT" not in output
    assert result["semantic_review_performed"] is False


def test_repair_demo_creates_only_new_owned_fixture(tmp_path, capsys):
    destination = tmp_path / "demo"
    assert main(["repair-demo", "--output", str(destination)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["execution"] == "simulated"
    assert not result["commit_ready"]
    assert not result["target_tests_executed"]
    assert result["attempts"] == 1
    assert "return value - 1" in (destination / "project/app.py").read_text()
    assert "return value + 1" in (Path(result["workspace"]) / "app.py").read_text()
    assert main(["repair-demo", "--output", str(destination)]) == 2


def test_backend_commands_help_does_not_scan_or_need_optional_deps(capsys):
    assert main(["repair-apply", "--help"]) == 0
    assert "--approve" in capsys.readouterr().out
    assert main(["prepare", "--help"]) == 0


def test_repair_preview_and_apply_bind_exact_hash(git_repo, tmp_path, capsys):
    from test_repair_workspace import proposal_for

    from reviewflow.models import ScanPolicy
    from reviewflow.snapshot import build_inventory
    from reviewflow.sources.git import GitHeadSource

    source = GitHeadSource(git_repo)
    inventory = build_inventory(source, ScanPolicy())
    task, finding, proposal = proposal_for(source, inventory)
    parent = tmp_path.parent / (tmp_path.name + "-io")
    parent.mkdir()
    for name, value in (("task", task), ("finding", finding), ("proposal", proposal)):
        (parent / (name + ".json")).write_text(value.model_dump_json())
    options = [
        "-d",
        str(git_repo),
        "--task",
        str(parent / "task.json"),
        "--finding",
        str(parent / "finding.json"),
        "--proposal",
        str(parent / "proposal.json"),
        "--workspace-parent",
        str(parent),
        "--run-id",
        "approved",
    ]
    assert main(["repair-preview", *options]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert not (parent / "approved").exists()
    assert main(["repair-apply", *options, "--approve", "wrong"]) == 2
    capsys.readouterr()
    assert not (parent / "approved").exists()
    assert main(["repair-apply", *options, "--approve", preview["approval_digest"]]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["validation"] == "not_run"
    assert result["commit_ready"] is False
    assert (parent / "approved/a.py").read_text() == "fixed\n"
    assert (git_repo / "a.py").read_text() == "old\n"


def test_patch_backend_invalid_output_and_maximum_attempts(git_repo):
    from test_repair_workspace import proposal_for

    from reviewflow.backend.patch_agent import propose_patch
    from reviewflow.backend.repair import RepairError
    from reviewflow.models import ScanPolicy
    from reviewflow.review.context import ReviewContext
    from reviewflow.snapshot import build_inventory
    from reviewflow.sources.git import GitHeadSource

    source = GitHeadSource(git_repo)
    inventory = build_inventory(source, ScanPolicy())
    task, finding, proposal = proposal_for(source, inventory)
    context = ReviewContext(source, inventory)
    requests = []

    class Backend:
        def complete(self, request):
            requests.append(request)
            assert request["stage"] == "patch"
            assert set(request["files"]) == {"a.py"}
            return proposal.model_dump_json()

    patch = propose_patch(context, task, finding, Backend())
    assert "+fixed" in patch.diff
    assert "# Patch Agent" in requests[0]["system"]
    assert "# Minimal patching skill" in requests[0]["system"]
    assert "# Correctness Reviewer" not in requests[0]["system"]

    class BadBackend:
        def complete(self, request):
            raise RuntimeError("SECRET")

    with pytest.raises(RepairError) as exc:
        propose_patch(context, task, finding, BadBackend())
    assert "SECRET" not in str(exc.value)
