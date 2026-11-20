import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from reviewflow.cli import main
from reviewflow.errors import CollectionError

NOTICE = "Inventory only: no semantic code review or target tests have been performed."


def test_json_is_inventory_not_review(tmp_path, capsys):
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    assert main(["inspect", str(tmp_path), "--format", "json"]) == 0
    out = capsys.readouterr()
    data = json.loads(out.out)
    assert out.err == ""
    assert data["coverage"]["semantic_review_performed"] is False
    assert data["coverage"]["target_tests_executed"] is False
    assert data["coverage"]["included_files"] == 1
    assert data["snapshot"]["source"]["git_repair_eligible"] is False
    assert data["notice"] == NOTICE
    assert "print('hello')" not in out.out


def test_help_without_model_credentials(monkeypatch, capsys):
    for key in list(os.environ):
        if "API_KEY" in key:
            monkeypatch.delenv(key)
    assert main(["--help"]) == 0
    assert "inspect" in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["--max-files", "--max-file-bytes", "--max-total-bytes"])
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "bad"])
def test_invalid_limits_exit_2(tmp_path, capsys, flag, value):
    assert main(["inspect", str(tmp_path), flag, value]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err


def test_missing_path_exit_2(tmp_path, capsys):
    assert main(["inspect", str(tmp_path / "missing")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "directory" in captured.err


def test_empty_head_exit_2(tmp_path, capsys):
    from conftest import git

    git(tmp_path, "init")
    assert main(["inspect", str(tmp_path)]) == 2
    assert "HEAD" in capsys.readouterr().err
    assert main(["inspect", str(tmp_path), "--source", "directory"]) == 0
    assert NOTICE in capsys.readouterr().out


def test_collection_error_exit_1_no_report(tmp_path, capsys, monkeypatch):
    def fail(*args, **kwargs):
        raise CollectionError("fixture read failure")

    monkeypatch.setattr("reviewflow.cli.build_inventory", fail)
    assert main(["inspect", str(tmp_path), "--source", "directory"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "fixture read failure" in captured.err


def test_root_resolve_permission_denied_exits_1(tmp_path, capsys, monkeypatch):
    target = tmp_path / "locked"
    target.mkdir()
    original = Path.resolve

    def deny(self, *args, **kwargs):
        if os.path.normcase(os.path.abspath(self)) == os.path.normcase(os.path.abspath(target)):
            raise PermissionError("permission denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", deny)
    assert main(["inspect", str(target), "--source", "directory"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Cannot resolve input directory." in captured.err


@pytest.mark.skipif(os.name != "posix", reason="Windows rejects newline characters in file names")
def test_markdown_escapes_paths(tmp_path, capsys):
    name = "a|`<script>\nnext.py"
    (tmp_path / name).write_bytes(b"x")
    assert main(["inspect", str(tmp_path), "--source", "directory"]) == 0
    out = capsys.readouterr().out
    assert "&#124;" in out and "&#96;" in out and "&lt;script&gt;" in out
    assert "\\nnext.py" in out
    assert "a|`<script>\nnext.py" not in out
    assert NOTICE in out
    assert "Included files" in out and "Skipped entries" in out


def test_json_stable_except_root_metadata(tmp_path, capsys):
    (tmp_path / "a.py").write_bytes(b"a")
    args = ["inspect", str(tmp_path), "--source", "directory", "--format", "json"]
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(args) == 0
    assert first == json.loads(capsys.readouterr().out)


def test_budget_limited_is_success_with_disclosure(tmp_path, capsys):
    (tmp_path / "a.py").write_bytes(b"a")
    (tmp_path / "b.py").write_bytes(b"b")
    assert main(["inspect", str(tmp_path), "--max-files", "1", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["coverage"]["budget_limited"] is True
    assert data["snapshot"]["records"][1]["reason"] == "file_limit"


def test_git_cli_preserves_input(dirty_repo, capsys):
    before = (dirty_repo / ".git/index").read_bytes()
    assert main(["inspect", str(dirty_repo), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["snapshot"]["source"]["mode"] == "head"
    assert data["snapshot"]["source"]["dirty"] is True
    assert data["snapshot"]["source"]["git_repair_eligible"] is True
    assert (dirty_repo / ".git/index").read_bytes() == before
    assert (dirty_repo / "a.py").read_bytes() == b"new\n"


@pytest.mark.parametrize("entrypoint", ["module", "console"])
def test_console_script_and_module_entrypoint(tmp_path, entrypoint):
    marker = tmp_path / "EXECUTED"
    payload = f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
    (tmp_path / "sitecustomize.py").write_text(payload)
    (tmp_path / "json.py").write_text(payload)
    (tmp_path / "main.py").write_bytes(b"print('not run')\n")
    original_files = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    if entrypoint == "module":
        command = [sys.executable, "-I", "-m", "reviewflow"]
    else:
        command = [str(Path(sys.executable).parent / "reviewflow")]
    command += ["inspect", str(tmp_path), "--source", "directory", "--format", "json"]
    result = subprocess.run(
        command, cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    data = json.loads(result.stdout)
    assert data["coverage"]["included_files"] == 3
    assert not marker.exists()
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == original_files
