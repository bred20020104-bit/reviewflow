import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from reviewflow.errors import InvalidInputError
from reviewflow.install import install_launcher, main


def test_launcher_isolated_in_target_directory(tmp_path):
    target = tmp_path / "target with ' quote 中文"
    target.mkdir()
    marker = tmp_path / "EXECUTED"
    payload = f"from pathlib import Path; Path({str(marker)!r}).write_text('executed')\n"
    for name in ("sitecustomize.py", "json.py", "reviewflow.py"):
        (target / name).write_text(payload)
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    launcher = install_launcher(tmp_path / "bin with ' quote", python=Path(sys.executable))
    env = {**os.environ, "PYTHONPATH": str(target)}
    result = subprocess.run(
        [str(launcher), "--format", "json"],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["snapshot"]["source"]["root"] == str(target.resolve())
    assert not marker.exists()
    assert {p.name: p.read_bytes() for p in target.iterdir()} == before
    assert os.access(launcher, os.X_OK)
    result = subprocess.run(
        [str(launcher), "-d", str(target), "--source", "directory"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert str(target.resolve()) in result.stdout
    assert not marker.exists()


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
def test_installer_never_overwrites(tmp_path, kind):
    target = tmp_path / "review"
    if kind == "file":
        target.write_bytes(b"user command")
    elif kind == "directory":
        target.mkdir()
    else:
        target.symlink_to(tmp_path / "missing")
    with pytest.raises(InvalidInputError, match="exist"):
        install_launcher(tmp_path, python=Path(sys.executable))
    if kind == "file":
        assert target.read_bytes() == b"user command"
    elif kind == "directory":
        assert target.is_dir()
    else:
        assert target.is_symlink()


def test_library_path_optional_and_shell_quoted(tmp_path):
    library = tmp_path / "lib ' $(touch BAD)"
    library.mkdir()
    path = install_launcher(tmp_path / "bin", python=Path(sys.executable), library_path=library)
    text = path.read_text()
    assert "DYLD_LIBRARY_PATH=" in text
    assert "-I -m reviewflow.quick" in text
    plain = install_launcher(tmp_path / "other", python=Path(sys.executable)).read_text()
    assert "DYLD_LIBRARY_PATH" not in plain
    result = subprocess.run(
        [str(path), "--help"], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "BAD").exists()


def test_installer_rejects_bad_python_or_library(tmp_path):
    with pytest.raises(InvalidInputError):
        install_launcher(tmp_path / "bin", python=tmp_path / "missing")
    with pytest.raises(InvalidInputError):
        install_launcher(
            tmp_path / "bin", python=Path(sys.executable), library_path=tmp_path / "missing"
        )
    assert not (tmp_path / "bin").exists()


def test_installer_cli_path_guidance(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", "")
    assert main(["--bin-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "PATH" in out and str(tmp_path) in out
    assert main(["--bin-dir", str(tmp_path)]) == 2
    assert capsys.readouterr().err
