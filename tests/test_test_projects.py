"""Real subprocess tests use only product-owned synthetic trees, never user code."""

import os
import subprocess
import sys

import pytest

from reviewflow.backend.test_projects import project_script
from reviewflow.backend.test_projects import test_projects as plan_projects


def run_projects(tmp_path, files):
    for path, text in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "DYLD_LIBRARY_PATH")}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return subprocess.run([sys.executable, "-I", "-B", "-c",
                           project_script(plan_projects(files), root=str(tmp_path))],
                          env=env, capture_output=True, timeout=30)


def test_many_projects_do_not_duplicate_wrapper_past_linux_argument_limit():
    files = {f"project{i:03d}/tests/test_ok.py": "def test_ok(): pass\n" for i in range(110)}
    script = project_script(plan_projects(files))
    # Linux with 4KiB pages limits any single argument to 131072 bytes.
    assert len(script.encode()) < 131072


def test_project_plan_assigns_every_test_once_without_source_execution():
    files = {p: "raise RuntimeError('must not execute during discovery')" for p in (
        "tests/test_root.py", "a/tests/unit/test_one.py", "b/test/test_two.py",
        "b/extra_test.py", "tools/test_three.py", "not_a_test.txt", "a/helper.py")}
    assert plan_projects(files) == [
        {"root": ".", "files": ["tests/test_root.py"]},
        {"root": "a", "files": ["a/tests/unit/test_one.py"]},
        {"root": "b", "files": ["b/extra_test.py", "b/test/test_two.py"]},
        {"root": "tools", "files": ["tools/test_three.py"]},
    ]


def test_nested_standalone_project_gets_own_cwd_and_import_root(tmp_path):
    result = run_projects(tmp_path, {
        "standalone/sample_module.py": "value = 2\n",
        "standalone/resource.txt": "fixture",
        "standalone/tests/test_value.py": "import sample_module\nfrom pathlib import Path\n"
            "def test_value():\n    assert sample_module.value == 2\n"
            "    assert Path('resource.txt').read_text() == 'fixture'\n",
    })
    assert result.returncode == 0, result.stdout + result.stderr


def test_projects_with_identical_module_and_test_names_are_isolated(tmp_path):
    files = {}
    for name, value in (("a", 11), ("b", 22)):
        files[f"{name}/shared.py"] = f"value = {value}\n"
        files[f"{name}/tests/test_same.py"] = (
            f"import shared\ndef test_value(): assert shared.value == {value}\n")
    result = run_projects(tmp_path, files)
    assert result.returncode == 0, result.stdout + result.stderr


def test_package_qualified_and_root_imports_still_work(tmp_path):
    result = run_projects(tmp_path, {
        "common.py": "value = 3\n", "pkg/__init__.py": "",
        "pkg/module.py": "value = 4\n", "pkg/tests/__init__.py": "",
        "pkg/tests/test_value.py": "from pkg.module import value\nimport common\n"
            "def test_value(): assert value + common.value == 7\n",
        "tests/test_root.py": "import common\ndef test_root(): assert common.value == 3\n",
    })
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("body,expected", [
    ("def test_no(): assert False\n", 1),
    ("raise ImportError('SECRET_ERROR_DETAIL')\n", 20),
    ("import pytest\n@pytest.fixture\ndef fixture(): raise RuntimeError('SECRET')\n"
     "def test_no(fixture): pass\n", 21),
    ("import pytest\n@pytest.fixture\ndef fixture():\n    yield\n    raise RuntimeError('SECRET')\n"
     "def test_no(fixture): pass\n", 22),
    ("import pytest\n@pytest.mark.skip(reason='optional')\ndef test_no(): pass\n", 0),
])
def test_all_groups_run_and_infrastructure_errors_override_assertions(tmp_path, body, expected):
    marker = tmp_path / "later-group-ran"
    result = run_projects(tmp_path, {
        "a/tests/test_first.py": body,
        "b/tests/test_later.py": f"from pathlib import Path\ndef test_later():\n"
            f"    Path({str(marker)!r}).write_text('yes')\n",
    })
    assert marker.read_text() == "yes"
    assert result.returncode == expected, result.stdout + result.stderr
    assert b"SECRET" not in result.stdout + result.stderr


def test_collection_failure_dominates_ordinary_assertion_failure(tmp_path):
    result = run_projects(tmp_path, {
        "a/tests/test_bad.py": "def test_bad(): assert False\n",
        "b/tests/test_missing.py": "import nonexistent_fixture_module\n",
    })
    assert result.returncode == 20


@pytest.mark.parametrize("files", [
    {"module.py": "value = 1\n"},
    {"a/tests/test_skipped.py": "import pytest\n@pytest.mark.skip(reason='fixture')\ndef test_skip(): pass\n"},
    {"a/tests/test_empty.py": "value = 1\n"},
])
def test_no_executed_tests_never_passes(tmp_path, files):
    assert run_projects(tmp_path, files).returncode == 5


def test_project_config_cannot_hide_explicit_test_files(tmp_path):
    result = run_projects(tmp_path, {
        "a/pytest.ini": "[pytest]\naddopts = --ignore=tests\ntestpaths = nonexistent\n",
        "a/tests/test_bad.py": "def test_bad(): assert False\n",
    })
    assert result.returncode == 1
