import json
import zipfile
from pathlib import Path

import pytest

from reviewflow.backend.delivery import package_source
from reviewflow.quick import main


def test_package_whitelist_excludes_credentials_and_caches(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    for path, text in {
        "pyproject.toml": '[project]\nname="reviewflow"\n',
        "README.md": "Example",
        ".env.example": "KEY=TODO_LOCAL",
        ".env": "SECRET",
        "config.local.toml": "SECRET",
        "src/reviewflow/__init__.py": "",
        "src/reviewflow/__pycache__/bad.pyc": "bad",
        ".venv/secret.txt": "SECRET",
        "tests/test_example.py": "assert True\n",
        ".superpowers/private.txt": "SECRET",
    }.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    output = tmp_path / "delivery.zip"
    report = package_source(root, output)
    assert report["status"] == "created"
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert names == {
            "pyproject.toml",
            "README.md",
            ".env.example",
            "src/reviewflow/__init__.py",
            "tests/test_example.py",
            "PACKAGE-MANIFEST.json",
        }
        assert not any(b"SECRET" in archive.read(name) for name in names)
        manifest = json.loads(archive.read("PACKAGE-MANIFEST.json"))
        assert len(manifest["files"]) == 5
    with pytest.raises(ValueError):
        package_source(root, output)


def test_package_includes_unified_model_template_but_not_local_values(tmp_path):
    root = tmp_path / "project"
    (root / "src/reviewflow").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname="reviewflow"\n')
    (root / "secrets.example.toml").write_text('api_key = "TODO_REPLACE_LOCALLY"\n')
    (root / "secrets.local.toml").write_text('api_key = "synthetic-private-value"\n')
    output = tmp_path / "delivery.zip"
    package_source(root, output)
    with zipfile.ZipFile(output) as archive:
        assert "secrets.example.toml" in archive.namelist()
        assert "secrets.local.toml" not in archive.namelist()
        assert all(
            b"synthetic-private-value" not in archive.read(name) for name in archive.namelist()
        )


def test_package_refuses_symlinks_and_likely_keys(tmp_path):
    root = tmp_path / "project"
    (root / "src/reviewflow").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname="reviewflow"\n')
    (root / "src/reviewflow/link.py").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError):
        package_source(root, tmp_path / "bad.zip")
    assert not (tmp_path / "bad.zip").exists()
    (root / "src/reviewflow/link.py").unlink()
    (root / "src/reviewflow/key.py").write_text('KEY = "sk-' + "A" * 48 + '"')
    with pytest.raises(ValueError):
        package_source(root, tmp_path / "bad.zip")
    assert not (tmp_path / "bad.zip").exists()


def test_self_authored_corpus_has_grouped_splits_and_no_answer_leak():
    root = Path("benchmarks/corpus")
    manifest = json.loads((root / "manifest.json").read_text())
    assert len(manifest["cases"]) == 30
    assert sum(c["split"] == "dev" for c in manifest["cases"]) == 10
    assert sum(c["split"] == "test" for c in manifest["cases"]) == 20
    groups = {}
    for case in manifest["cases"]:
        assert groups.setdefault(case["group"], case["split"]) == case["split"]
        target = root / "cases" / case["case_id"]
        assert (target / "app.py").is_file()
        assert (target / "README.md").is_file()
        assert not (target / "answers.json").exists()
        assert "REVIEWFLOW_DEMO_FINDING" not in (target / "app.py").read_text()
    assert len(groups) == 15
    annotations = json.loads((root / "annotations.json").read_text())
    assert len(annotations["defects"]) == 15


def test_evaluate_cli_scores_only_adjudicated_real_records(tmp_path, capsys):
    from test_context_evaluation import dataset, results

    manifest, result = tmp_path / "manifest.json", tmp_path / "results.json"
    manifest.write_text(json.dumps(dataset()))
    result.write_text(json.dumps(results()))
    assert main(["evaluate", "--manifest", str(manifest), "--results", str(result)]) == 0
    assert json.loads(capsys.readouterr().out)["precision"] == 0.5
    result.write_text(json.dumps({**results(), "execution": "simulated"}))
    assert main(["evaluate", "--manifest", str(manifest), "--results", str(result)]) == 2
