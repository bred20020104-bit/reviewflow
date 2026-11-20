import json

import pytest

from reviewflow.backend.sessions import load_session, resume_session, start_session
from reviewflow.models import ScanPolicy
from reviewflow.quick import main
from reviewflow.snapshot import build_inventory
from reviewflow.sources.directory import DirectorySource


def fixture(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    (root / "a.py").write_text("# REVIEWFLOW_DEMO_FINDING\nx=1\n")
    source = DirectorySource(root)
    inventory = build_inventory(source, ScanPolicy())
    return root, source, inventory, tmp_path / "session"


def test_session_is_report_first_and_reopens_without_authority(tmp_path):
    root, source, inventory, folder = fixture(tmp_path)
    doc = start_session(folder, source, inventory)
    assert doc.report.execution == "simulated"
    assert doc.accounting.calls == doc.report.calls
    assert doc.accounting.calls >= 4
    assert not doc.commit_ready and not doc.report.target_tests_executed
    loaded = load_session(folder, source, inventory)
    assert loaded == doc
    resumed = resume_session(folder, source, inventory)
    assert resumed.revision == 2
    assert not resumed.commit_ready
    assert len(list(folder.glob("run-*.json"))) == 2
    text = (folder / "run-0001.json").read_text(encoding="utf-8")
    assert "grant" not in text and "api_key" not in text
    assert (root / "a.py").read_text() == "# REVIEWFLOW_DEMO_FINDING\nx=1\n"


def test_session_refuses_changed_source_and_different_root(tmp_path):
    root, source, inventory, folder = fixture(tmp_path)
    start_session(folder, source, inventory)
    (root / "a.py").write_text("changed\n")
    new_source = DirectorySource(root)
    with pytest.raises(ValueError):
        load_session(folder, new_source, build_inventory(new_source, ScanPolicy()))
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.py").write_text("# REVIEWFLOW_DEMO_FINDING\nx=1\n")
    new_source = DirectorySource(other)
    with pytest.raises(ValueError):
        load_session(folder, new_source, build_inventory(new_source, ScanPolicy()))


@pytest.mark.parametrize("tamper", ["approval", "execution", "snapshot", "symlink"])
def test_disk_session_cannot_restore_permissions_or_real_results(tmp_path, tamper):
    root, source, inventory, folder = fixture(tmp_path)
    start_session(folder, source, inventory)
    path = folder / "run-0001.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if tamper == "approval":
        data["approval"] = True
    elif tamper == "execution":
        data["report"]["execution"] = "real"
        data["commit_ready"] = True
    elif tamper == "snapshot":
        data["snapshot_id"] = "0" * 64
    else:
        path.unlink()
        path.symlink_to(root / "a.py")
    if tamper != "symlink":
        path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_session(folder, source, inventory)


def test_session_destination_is_new_and_outside_source(tmp_path):
    root, source, inventory, folder = fixture(tmp_path)
    with pytest.raises(ValueError):
        start_session(root / "report", source, inventory)
    start_session(folder, source, inventory)
    with pytest.raises((ValueError, FileExistsError)):
        start_session(folder, source, inventory)


def test_session_cli_export_and_resume_require_explicit_demo(tmp_path, capsys):
    root, source, inventory, folder = fixture(tmp_path)
    args = ["-d", str(root), "--source", "directory"]
    assert main(["session-start", *args, "--output", str(folder)]) == 2
    assert not folder.exists()
    capsys.readouterr()
    assert main(["session-start", *args, "--output", str(folder), "--demo"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["report"]["execution"] == "simulated"
    assert main(["session-show", *args, "--session", str(folder)]) == 0
    shown = json.loads(capsys.readouterr().out)
    finding = shown["report"]["findings"][0]["finding"]["finding_id"]
    output = tmp_path / "export"
    assert (
        main(
            [
                "session-export",
                *args,
                "--session",
                str(folder),
                "--finding",
                finding,
                "--output",
                str(output),
            ]
        )
        == 0
    )
    export = json.loads(capsys.readouterr().out)
    assert export["commit_ready"] is False
    assert (output / "finding.json").is_file() and (output / "task.json").is_file()
    assert main(["session-resume", *args, "--session", str(folder), "--demo"]) == 0
    assert json.loads(capsys.readouterr().out)["revision"] == 2


def test_incomplete_session_returns_nonzero_and_keeps_report(tmp_path, capsys):
    root, _, _, folder = fixture(tmp_path)
    (root / "a.py").write_text("x=1\n" * 1700)
    args = ["-d", str(root), "--source", "directory"]
    assert main(["session-start", *args, "--output", str(folder), "--demo"]) == 1
    assert json.loads(capsys.readouterr().out)["report"]["status"] in {"partial", "failed"}
    assert (folder / "run-0001.json").is_file()
    assert main(["session-resume", *args, "--session", str(folder)]) == 2
    assert not (folder / "run-0002.json").exists()
    capsys.readouterr()
    assert main(["session-resume", *args, "--session", str(folder), "--demo"]) == 1
    assert json.loads(capsys.readouterr().out)["revision"] == 2
    assert main(["session-show", *args, "--session", str(folder)]) == 0


def test_empty_session_input_is_clear_rejection(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    source = DirectorySource(root)
    with pytest.raises(ValueError):
        start_session(tmp_path / "session", source, build_inventory(source, ScanPolicy()))
