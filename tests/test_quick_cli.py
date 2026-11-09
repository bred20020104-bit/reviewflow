import json
from hashlib import sha256

import pytest

from reviewflow.quick import main
from reviewflow.report import NOTICE


def test_no_arguments_inventory_current_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "test_file.py").write_bytes(b"value = 1\n")
    assert main([]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert "项目文件盘点" in output.out
    assert str(tmp_path.resolve()) in output.out
    assert "1 个文件" in output.out
    assert "test_file.py" not in output.out
    assert sha256(b"value = 1\n").hexdigest() not in output.out
    assert NOTICE in output.out
    assert "&#" not in output.out


@pytest.mark.parametrize("flag", ["-d", "--directory"])
def test_target_path_and_json(tmp_path, monkeypatch, capsys, flag):
    target = tmp_path / "中文 project"
    target.mkdir()
    (target / "a.py").write_bytes(b"a")
    monkeypatch.chdir(tmp_path)
    assert main([flag, target.name, "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["snapshot"]["source"]["root"] == str(target.resolve())
    assert set(data) == {"snapshot", "coverage", "notice"}


def test_verbose_safe_names_and_skip_details(tmp_path, capsys):
    name = "test_\x1b[31m\nfile.py"
    (tmp_path / name).write_bytes(b"x")
    (tmp_path / "image.png").write_bytes(b"x")
    assert main(["-d", str(tmp_path), "--verbose"]) == 0
    text = capsys.readouterr().out
    assert "test_\\x1b[31m\\nfile.py" in text
    assert "\x1b" not in text
    assert "unsupported_type" in text and "image.png" in text
    assert sha256(b"x").hexdigest() in text
    assert "&#" not in text


@pytest.mark.parametrize(
    "args",
    [
        ["--verbose", "--format", "json"],
        ["--verbose", "--format", "markdown"],
        ["-direcory", "."],
        ["--dire", "."],
        ["--max-files", "0"],
    ],
)
def test_invalid_options_no_report(args, capsys):
    assert main(args) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err


@pytest.mark.parametrize("option", ["--help", "--version"])
def test_help_version_without_scanning(option, capsys, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("must not scan")

    monkeypatch.setattr("reviewflow.cli.open_source", fail)
    assert main([option]) == 0
    assert capsys.readouterr().out


def test_markdown_readable_underscores(tmp_path, capsys):
    (tmp_path / "test_file.py").write_bytes(b"x")
    assert main(["-d", str(tmp_path), "--format", "markdown"]) == 0
    assert "test_file.py" in capsys.readouterr().out


def test_head_warning_in_short_summary(dirty_repo, capsys):
    assert main(["-d", str(dirty_repo / "src")]) == 0
    text = capsys.readouterr().out
    assert "HEAD" in text
    assert "Uncommitted" in text and "expanded" in text
