import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from reviewflow.quick import main
from reviewflow.review.context import MAX_EXCERPT_LINES


@pytest.mark.parametrize("mode,calls", [("single", 1), ("multi", 4)])
def test_demo_report_modes(tmp_path, capsys, mode, calls):
    (tmp_path / "example.py").write_text("# REVIEWFLOW_DEMO_FINDING\nanswer = 42\n")
    assert main(["-d", str(tmp_path), "--demo", "--mode", mode, "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["report_type"] == "review_workflow"
    assert report["execution"] == "simulated"
    assert report["semantic_review_performed"] is False
    assert report["target_tests_executed"] is False
    assert report["calls"] == calls
    assert len(report["findings"]) == 1
    assert "answer = 42" not in json.dumps(report)
    decision = report["findings"][0]["verification"]
    assert (decision is None) == (mode == "single")
    assert "simulated" in report["notice"]


@pytest.mark.parametrize("fmt", ["text", "markdown"])
@pytest.mark.skipif(os.name != "posix", reason="Windows rejects ESC characters in file names")
def test_safe_demo_rendering(tmp_path, capsys, fmt):
    (tmp_path / "bad\x1b[31m.py").write_text("# REVIEWFLOW_DEMO_FINDING\n")
    assert main(["-d", str(tmp_path), "--demo", "--format", fmt]) == 0
    out = capsys.readouterr().out
    assert "simulated" in out and "Demonstration candidate" in out
    assert "\x1b" not in out
    assert "bad\\x1b[31m.py" in out


def test_live_placeholder_never_scans_or_connects(monkeypatch, capsys):
    monkeypatch.setenv("REVIEWFLOW_API_KEY", "SECRET")
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("network"))
    monkeypatch.setattr("reviewflow.cli.open_source", lambda *a: pytest.fail("scan"))
    assert main(["--review"]) == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert "docs/model-setup.md" in captured.err
    assert "SECRET" not in captured.err


@pytest.mark.parametrize(
    "args", [["--demo", "--review"], ["--mode", "single"], ["--demo", "--verbose"]]
)
def test_invalid_workflow_combinations(args, capsys):
    assert main(args) == 2
    assert capsys.readouterr().err


def test_failed_demo_not_success(tmp_path, capsys):
    assert main(["--demo", "-d", str(tmp_path), "--format", "json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "failed"
    assert "prepare: no_context" in out["errors"]


def test_partial_demo_preserves_omissions(tmp_path, capsys):
    (tmp_path / "ok.py").write_text("answer=42\n")
    (tmp_path / "long.py").write_text("a\n" * (MAX_EXCERPT_LINES + 1))
    assert main(["--demo", "-d", str(tmp_path), "--format", "json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "partial"
    assert report["unreviewed_paths"] == ["long.py"]


@pytest.mark.skipif(os.name != "posix", reason="The isolated review launcher requires POSIX")
def test_actual_isolated_launcher_demo_no_target_execution(tmp_path):
    from reviewflow.install import install_launcher

    target = tmp_path / "target"
    target.mkdir()
    marker = tmp_path / "EXECUTED"
    payload = f"from pathlib import Path; Path({str(marker)!r}).touch()\n"
    for name in ("json.py", "sitecustomize.py", "reviewflow.py"):
        (target / name).write_text(payload)
    (target / "example.py").write_text("# REVIEWFLOW_DEMO_FINDING\n")
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    launcher = install_launcher(tmp_path / "bin", python=Path(sys.executable))
    result = subprocess.run(
        [str(launcher), "--demo", "--format", "json"],
        cwd=target,
        env={**os.environ, "PYTHONPATH": str(target), "LANGSMITH_TRACING": "true"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["execution"] == "simulated"
    assert not marker.exists()
    assert before == {p.name: p.read_bytes() for p in target.iterdir()}


def test_inventory_still_works_with_optional_imports_blocked(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.modules['pydantic']=None; sys.modules['langgraph']=None; "
            "from reviewflow.quick import main; raise SystemExit(main(['--source','directory','--format','json']))",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "snapshot" in json.loads(result.stdout)
