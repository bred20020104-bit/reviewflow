"""CLI authorization tests: synthetic repositories, never the user's target."""

import json

import pytest
from test_live_repair import RepairFixtureAdapter
from test_responses_adapter import api_server, response
from test_validation_publication import process

from reviewflow.quick import main

IMAGE = "sha256:" + "a" * 64


def cli_fixture(repo, tmp_path):
    storage = tmp_path.parent / (tmp_path.name + "-tool")
    storage.mkdir()
    config = storage / "config.local.toml"
    config.write_text('secrets_file = "secrets.local.toml"\n')
    secret = storage / "secrets.local.toml"
    secret.write_text('provider="openai"\napi_type="responses"\nmodel="fixture"\n'
                      'base_url="http://127.0.0.1:9999/v1"\napi_key="synthetic-secret"\n')
    secret.chmod(0o600)
    output = storage / "report"
    args = ["repair-run", "--development", "--config", str(config), "-d", str(repo),
            "--include", "a.py", "--image", IMAGE, "--output", str(output), "--max-calls", "16"]
    return args, output, secret


def test_repair_cli_preview_has_no_network_workspace_or_execution(git_repo, tmp_path, capsys, monkeypatch):
    from reviewflow.backend.responses_adapter import ResponsesAdapter
    from reviewflow.backend.volume_runner import VolumeDockerRunner

    args, output, _ = cli_fixture(git_repo, tmp_path)
    monkeypatch.setattr(ResponsesAdapter, "__call__", lambda *a, **k: pytest.fail("Unapproved network"))
    monkeypatch.setattr(VolumeDockerRunner, "run", lambda *a, **k: pytest.fail("Unapproved target execution"))
    assert main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["operation"] == "live_development_repair"
    assert preview["max_calls"] == 16 and preview["network_requests"] == 0
    assert preview["semantic_batches"] == [["a.py"]]
    assert not output.exists()
    assert not output.with_name(output.name + "-candidate").exists()
    assert "synthetic-secret" not in json.dumps(preview)
    assert (git_repo / "a.py").read_bytes() == b"old\n"


@pytest.mark.parametrize("change", ["cap", "scope", "image", "output", "head_bytes", "profile"])
def test_cli_approval_is_bound_before_any_side_effect(git_repo, tmp_path, capsys, monkeypatch, change):
    args, output, _ = cli_fixture(git_repo, tmp_path)
    assert main(args) == 0
    digest = json.loads(capsys.readouterr().out)["approval_digest"]
    if change == "cap":
        args[args.index("--max-calls") + 1] = "15"
    elif change == "scope":
        args += ["--include", "src/b.py"]
    elif change == "image":
        args[args.index("--image") + 1] = "sha256:" + "b" * 64
    elif change == "output":
        args[args.index("--output") + 1] += "-other"
    elif change == "head_bytes":
        (git_repo / "a.py").write_text("late user edit\n")
    else:
        from dataclasses import replace

        from reviewflow.agents import profiles
        from reviewflow.backend import live_repair_cli

        bundle = profiles.load_profiles()
        changed = replace(bundle, shared=replace(bundle.shared, text=bundle.shared.text + "\nTrusted change\n"))
        monkeypatch.setattr(live_repair_cli, "load_profiles", lambda: changed)
    assert main(args + ["--approve", digest]) == 2
    assert not output.exists()
    assert not output.with_name(output.name + "-candidate").exists()


@pytest.mark.parametrize("option", ["development", "mutable_image", "existing_output", "inside_output"])
def test_cli_invalid_inputs_are_rejected(git_repo, tmp_path, capsys, option):
    args, output, _ = cli_fixture(git_repo, tmp_path)
    if option == "development":
        args.remove("--development")
    elif option == "mutable_image":
        args[args.index("--image") + 1] = "python:latest"
    elif option == "existing_output":
        output.mkdir()
    else:
        args[args.index("--output") + 1] = str(git_repo / "report")
    assert main(args) == 2
    assert not output.with_name(output.name + "-candidate").exists()


@pytest.mark.parametrize("code,status,exit_code", [
    (0, "tests_passed", 0), (1, "tests_failed", 1), (20, "blocked", 1),
])
def test_tests_only_never_constructs_model_or_changes_source(
        git_repo, tmp_path, capsys, monkeypatch, code, status, exit_code):
    from reviewflow.backend import live_repair_cli
    from reviewflow.backend.volume_runner import VolumeDockerRunner

    args, output, _ = cli_fixture(git_repo, tmp_path)
    args += ["--tests-only"]
    monkeypatch.setattr(live_repair_cli, "ResponsesAdapter",
                        lambda *a, **k: pytest.fail("Tests-only constructed model adapter"))
    original = VolumeDockerRunner.__init__
    def synthetic_docker(self, image, **kwargs):
        return original(self, image, **kwargs,
                        execute=lambda c, **kw: process(code if c[1] == "run" else 0))
    monkeypatch.setattr(VolumeDockerRunner, "__init__", synthetic_docker)
    assert main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["tests_only"] is True and preview["max_calls"] == 0
    assert not output.exists()
    assert main(args + ["--approve", preview["approval_digest"]]) == exit_code
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == status
    assert report["accounting"]["requests_attempted"] == 0
    assert report["application"] is None and report["patch_attempts"] == []
    assert report["initial_reviews"] == [] and report["final_reviews"] == []
    assert (git_repo / "a.py").read_bytes() == b"old\n"
    assert (output / "candidate.diff").read_text() == ""
    assert report["test_runs"][0]["diagnostic_code"] == {
        0: "tests_passed", 1: "test_assertions_failed", 20: "test_collection_error",
    }[code]


@pytest.mark.parametrize("initial_tests_only", [True, False])
def test_tests_only_approval_cannot_be_reused_for_other_mode(
        git_repo, tmp_path, capsys, initial_tests_only):
    args, output, _ = cli_fixture(git_repo, tmp_path)
    initial = args + (["--tests-only"] if initial_tests_only else [])
    assert main(initial) == 0
    preview = json.loads(capsys.readouterr().out)
    changed = args + ([] if initial_tests_only else ["--tests-only"])
    assert main(changed + ["--approve", preview["approval_digest"]]) == 2
    assert not output.exists()
    assert not output.with_name(output.name + "-candidate").exists()


def test_tests_only_runs_with_source_larger_than_model_context(
        git_repo, tmp_path, capsys, monkeypatch):
    from conftest import git

    from reviewflow.backend.volume_runner import VolumeDockerRunner

    content = b"#" + b"x" * 32767 + b"\n"
    (git_repo / "a.py").write_bytes(content)
    git(git_repo, "add", "a.py")
    git(git_repo, "commit", "-qm", "Owned large source fixture")
    args, output, _ = cli_fixture(git_repo, tmp_path)
    args += ["--tests-only"]
    original = VolumeDockerRunner.__init__
    monkeypatch.setattr(VolumeDockerRunner, "__init__", lambda self, image, **kw:
                        original(self, image, **kw, execute=lambda *a, **k: process()))
    assert main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["semantic_batches"] == []
    assert main(args + ["--approve", preview["approval_digest"]]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "tests_passed"
    assert len(report["test_runs"]) == 1
    assert report["accounting"]["requests_attempted"] == 0
    assert (git_repo / "a.py").read_bytes() == content
    assert (output / "candidate.diff").read_text() == ""


def test_test_timeout_is_preview_bound_and_cannot_be_silently_raised(git_repo, tmp_path, capsys):
    args, output, _ = cli_fixture(git_repo, tmp_path)
    args += ["--tests-only", "--test-timeout", "180"]
    assert main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["test_policy"]["timeout_seconds"] == 180
    changed = list(args)
    changed[-1] = "181"
    assert main(changed + ["--approve", preview["approval_digest"]]) == 2
    assert not output.exists()


@pytest.mark.parametrize("timeout", ["0", "301"])
def test_invalid_test_timeout_rejects_without_creating_output(git_repo, tmp_path, timeout):
    args, output, _ = cli_fixture(git_repo, tmp_path)
    assert main(args + ["--test-timeout", timeout]) == 2
    assert not output.exists()


@pytest.mark.parametrize("status", [400, 413, 422])
def test_http_status_reaches_cli_reports_without_body_retry_or_writeback(
        git_repo, tmp_path, capsys, monkeypatch, status):
    from reviewflow.backend.volume_runner import VolumeDockerRunner

    args, output, secret = cli_fixture(git_repo, tmp_path)
    original = VolumeDockerRunner.__init__
    monkeypatch.setattr(VolumeDockerRunner, "__init__", lambda self, image, **kw:
                        original(self, image, **kw, execute=lambda *a, **k: process()))
    with api_server(status=status, raw=b"PRIVATE_PROVIDER_BODY synthetic-secret") as (url, calls):
        secret.write_text(secret.read_text().replace("http://127.0.0.1:9999/v1", url))
        assert main(args) == 0
        preview = json.loads(capsys.readouterr().out)
        assert not calls
        assert main(args + ["--approve", preview["approval_digest"]]) == 1
        document = json.loads(capsys.readouterr().out)
        assert document["status"] == "blocked" and document["application"] is None
        assert document["accounting"]["requests_attempted"] == len(calls) == 1
        assert document["accounting"]["usage_complete"] is False
        assert document["accounting"]["records"][0]["error_code"] == f"http_status_{status}"
        assert not document["patch_attempts"] and not document["final_reviews"]
        for name in ("report.json", "report.md"):
            text = (output / name).read_text()
            assert f"http_status_{status}" in text
            assert "PRIVATE_PROVIDER_BODY" not in text and "synthetic-secret" not in text
        assert (git_repo / "a.py").read_bytes() == b"old\n"


def test_cli_uses_real_responses_http_and_writes_reports(git_repo, tmp_path, capsys, monkeypatch):
    from reviewflow.backend.development import DevelopmentModel
    from reviewflow.backend.volume_runner import VolumeDockerRunner

    args, output, secret = cli_fixture(git_repo, tmp_path)
    fixture = RepairFixtureAdapter(DevelopmentModel(provider="openai", api_type="responses",
        model="fixture", base_url="http://127.0.0.1:9999/v1", api_key_file="secrets.local.toml"))

    def serve(body):
        body = json.loads(body)
        wire = json.dumps({"stage": body["text"]["format"]["name"].removeprefix("reviewflow_"),
            "system": body["instructions"], "payload": json.loads(body["input"]),
            "response_schema": body["text"]["format"]["schema"]})
        return response(fixture(wire).text)

    original = VolumeDockerRunner.__init__
    def synthetic_docker(self, image, **kwargs):
        return original(self, image, **kwargs, execute=lambda *args, **kwargs: process())
    monkeypatch.setattr(VolumeDockerRunner, "__init__", synthetic_docker)
    with api_server(result=serve) as (url, calls):
        secret.write_text(secret.read_text().replace("http://127.0.0.1:9999/v1", url))
        assert main(args) == 0
        digest = json.loads(capsys.readouterr().out)["approval_digest"]
        assert not calls
        assert main(args + ["--approve", digest]) == 0
        document = json.loads(capsys.readouterr().out)
        assert document["status"] == "applied"
        assert document["accounting"]["requests_attempted"] == len(calls) == 9
        assert document["report_directory"] == str(output)
        assert (output / "report.json").exists() and (output / "report.md").exists()
        assert "-old\n+fixed\n" in (output / "candidate.diff").read_text()
        assert (git_repo / "a.py").read_bytes() == b"fixed\n"
        assert "synthetic-secret" not in (output / "report.json").read_text()
        assert main(args + ["--approve", digest]) == 2
        assert len(calls) == 9
