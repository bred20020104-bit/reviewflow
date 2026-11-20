import json

import pytest

from reviewflow.quick import main
from reviewflow.review.context import MAX_EXCERPT_LINES


def test_demo_rewrites_local_file(tmp_path, capsys):
    app = tmp_path / "app.py"
    app.write_text("def increment(value):\n    return value - 1\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("untouched = 1\n", encoding="utf-8")
    assert main(["local-fix", "-d", str(tmp_path), "--demo"]) == 0
    assert "return value + 1" in app.read_text(encoding="utf-8")
    assert (tmp_path / "other.py").read_text(encoding="utf-8") == "untouched = 1\n"
    report = json.loads(capsys.readouterr().out)
    assert report["changed"] == ["app.py"]
    assert report["worktree"] is False and report["docker"] is False


def test_proposal_writes_only_listed_file(tmp_path, capsys):
    (tmp_path / "pkg").mkdir()
    target = tmp_path / "pkg" / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    proposal = tmp_path / "proposal.json"
    proposal.write_text(
        json.dumps({"files": [{"path": "pkg/app.py", "content": "value = 2\n"}]}),
        encoding="utf-8",
    )
    assert main(["local-fix", "-d", str(tmp_path), "--proposal", str(proposal)]) == 0
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def _private_config(tmp_path):
    secret = tmp_path / "secrets.local.toml"
    secret.write_text(
        "\n".join(
            [
                'provider = "openai"',
                'api_type = "responses"',
                'model = "test-model"',
                'base_url = "https://api.example.invalid/v1"',
                'api_key = "synthetic-key"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    config = tmp_path / "config.local.toml"
    config.write_text('secrets_file = "secrets.local.toml"\n', encoding="utf-8")
    return config


def _isolate_preview(monkeypatch, tmp_path):
    target = tmp_path / "preview.json"
    monkeypatch.setattr("reviewflow.backend.local_fix._preview_file", lambda: target)
    return target


def _scripted_agents(self, request):
    import hashlib

    from reviewflow.review.demo import DemoBackend

    assert self.key == "synthetic-key"
    self.calls += 1
    if isinstance(request, dict):
        path, text = next(iter(request["files"].items()))
        changed = text.replace("return value - 1", "return value + 1").replace("value = 0", "value = 1")
        return json.dumps(
            {
                "snapshot_id": request["finding"]["evidence"][0]["snapshot_id"],
                "finding_id": request["finding"]["finding_id"],
                "replacements": [
                    {
                        "path": path,
                        "before_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "content": changed,
                    }
                ],
                "explanation": "corrected the returned value",
            }
        )
    return DemoBackend().complete(request)


def test_model_call_writes_local_fix(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    app = project / "app.py"
    original = "# REVIEWFLOW_DEMO_FINDING\ndef increment(value):\n    return value - 1\n"
    app.write_text(original, encoding="utf-8")
    monkeypatch.setattr("reviewflow.backend.local_agents._AgentBackend.complete", _scripted_agents)
    preview = _isolate_preview(monkeypatch, tmp_path)
    assert main(["local-fix", "-d", str(project), "--config", str(_private_config(tmp_path))]) == 0
    assert app.read_text(encoding="utf-8") == original
    captured = capsys.readouterr()
    text = captured.out
    saved = json.loads(preview.read_text(encoding="utf-8"))
    assert "1. " in text and "No files have been written" in text
    assert "Preparing to review" in captured.err and "[1/1] start" in captured.err
    assert "waiting for the model" not in text
    assert saved["items"][0]["files"][0]["path"] == "app.py"
    assert saved["stats"]["model_calls"] >= 5
    assert "synthetic-key" not in text
    assert main(["local-fix", "-d", str(project), "--apply"]) == 0
    assert "return value + 1" in app.read_text(encoding="utf-8")


def test_model_reviews_every_file_in_batches(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    for index in range(3):
        (project / f"mod{index}.py").write_text(
            "# REVIEWFLOW_DEMO_FINDING\nvalue = 0\n", encoding="utf-8"
        )
    monkeypatch.setattr("reviewflow.backend.local_agents._AgentBackend.complete", _scripted_agents)
    preview = _isolate_preview(monkeypatch, tmp_path)
    assert main(["local-fix", "-d", str(project), "--config", str(_private_config(tmp_path))]) == 0
    saved = json.loads(preview.read_text(encoding="utf-8"))
    assert saved["stats"]["batches"] == 1
    assert saved["stats"]["model_calls"] >= 5
    assert saved["stats"]["reviewed"] == 3
    assert [row["path"] for item in saved["items"] for row in item["files"]] == [
        "mod0.py",
        "mod1.py",
        "mod2.py",
    ]
    assert (project / "mod0.py").read_text(encoding="utf-8").endswith("value = 0\n")
    assert main(["local-fix", "-d", str(project), "--apply", "1,3"]) == 0
    assert (project / "mod0.py").read_text(encoding="utf-8").endswith("value = 1\n")
    assert (project / "mod1.py").read_text(encoding="utf-8").endswith("value = 0\n")
    assert (project / "mod2.py").read_text(encoding="utf-8").endswith("value = 1\n")


def test_large_file_is_split_and_later_work_continues(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    original = "x = 1\n" * (MAX_EXCERPT_LINES + 1)
    (project / "big.py").write_text(original, encoding="utf-8")
    (project / "small.py").write_text(
        "# REVIEWFLOW_DEMO_FINDING\nreturn value - 1\n", encoding="utf-8"
    )
    monkeypatch.setattr("reviewflow.backend.local_agents._AgentBackend.complete", _scripted_agents)
    preview = _isolate_preview(monkeypatch, tmp_path)
    assert main(["local-fix", "-d", str(project), "--config", str(_private_config(tmp_path))]) == 0
    saved = json.loads(preview.read_text(encoding="utf-8"))
    assert {"files": "big.py", "error": "file exceeds one review excerpt"} in saved["failures"]
    assert [row["path"] for item in saved["items"] for row in item["files"]] == ["small.py"]
    assert (project / "big.py").read_text(encoding="utf-8") == original
    assert main(["local-fix", "-d", str(project), "--apply"]) == 0
    assert (project / "big.py").read_text(encoding="utf-8") == original
    assert "return value + 1" in (project / "small.py").read_text(encoding="utf-8")


def test_apply_can_skip_a_numbered_item_and_refuse_a_blank_file(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    (project / "keep.py").write_text("keep = 1\n", encoding="utf-8")
    (project / "drop.py").write_text("drop = 1\n", encoding="utf-8")
    (project / "empty.py").write_text("empty = 1\n", encoding="utf-8")
    preview = _isolate_preview(monkeypatch, tmp_path)
    preview.write_text(
        json.dumps(
            {
                "version": 2,
                "root": str(project.resolve()),
                "items": [
                    {
                        "id": 1,
                        "title": "保留这项",
                        "explanation": "把 keep 改成 2",
                        "files": [{"path": "keep.py", "content": "keep = 2\n"}],
                    },
                    {
                        "id": 2,
                        "title": "先不改这项",
                        "explanation": "把 drop 改成 2",
                        "files": [{"path": "drop.py", "content": "drop = 2\n"}],
                    },
                    {
                        "id": 3,
                        "title": "会清空文件",
                        "explanation": "新内容是空的",
                        "files": [{"path": "empty.py", "content": ""}],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    assert main(["local-fix", "-d", str(project), "--list"]) == 0
    listed = capsys.readouterr().out
    assert "1. 保留这项" in listed and "would blank the original file" in listed
    assert main(["local-fix", "-d", str(project), "--apply", "except 2"]) == 0
    assert (project / "keep.py").read_text(encoding="utf-8") == "keep = 2\n"
    assert (project / "drop.py").read_text(encoding="utf-8") == "drop = 1\n"
    assert (project / "empty.py").read_text(encoding="utf-8") == "empty = 1\n"
    assert main(["local-fix", "-d", str(project), "--apply", "3"]) == 2
    assert "blank the original file" in capsys.readouterr().err
    assert (project / "empty.py").read_text(encoding="utf-8") == "empty = 1\n"


def test_discover_reads_the_whole_tree(tmp_path):
    from reviewflow.backend.local_fix import _discover_python

    for index in range(9):
        (tmp_path / f"mod{index}.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_hidden.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "App.java").write_text("class App {}\n", encoding="utf-8")
    (tmp_path / "AppTest.java").write_text("class AppTest {}\n", encoding="utf-8")
    found = _discover_python(tmp_path)
    assert len(found) == 10
    assert "App.java" in found
    assert "AppTest.java" not in found
    assert "tests/test_hidden.py" not in found


def test_local_fix_finds_config_without_extra_flag(tmp_path, monkeypatch):
    tool = tmp_path / "tool"
    tool.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    app = project / "app.py"
    app.write_text("value = 1\n", encoding="utf-8")
    _private_config(tool)
    monkeypatch.chdir(tool)

    monkeypatch.setattr("reviewflow.backend.local_agents._AgentBackend.complete", _scripted_agents)
    _isolate_preview(monkeypatch, tmp_path)
    assert main(["local-fix", "-d", str(project)]) == 0
    assert app.read_text(encoding="utf-8") == "value = 1\n"


def test_model_leaves_files_when_no_bug(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    app = project / "app.py"
    original = "def increment(value):\n    return value + 1\n"
    app.write_text(original, encoding="utf-8")

    monkeypatch.setattr("reviewflow.backend.local_agents._AgentBackend.complete", _scripted_agents)
    _isolate_preview(monkeypatch, tmp_path)
    assert main(["local-fix", "-d", str(project), "--config", str(_private_config(tmp_path))]) == 0
    assert app.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("relative", ["../app.py", "/tmp/app.py", "app.txt", ".git/app.py"])
def test_escape_and_non_python_rejected(tmp_path, capsys, relative):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    proposal = tmp_path / "proposal.json"
    proposal.write_text(
        json.dumps({"files": [{"path": relative, "content": "value = 2\n"}]}),
        encoding="utf-8",
    )
    assert main(["local-fix", "-d", str(tmp_path), "--proposal", str(proposal)]) == 2
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    assert "local" in capsys.readouterr().err.lower() or "Local" in capsys.readouterr().err
