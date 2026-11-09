"""Exclusive, whitelist-based source archives with bounded reads and a hash manifest."""

import hashlib
import json
import os
import re
import stat
import zipfile
from pathlib import Path

from reviewflow.backend.config import read_bounded
from reviewflow.policy import SENSITIVE_NAMES, SENSITIVE_SUFFIXES

ROOT_FILES = {
    "README.md",
    "CONTRIBUTING.md",
    "pyproject.toml",
    "config.example.toml",
    "secrets.example.toml",
    ".env.example",
    ".gitignore",
    "LICENSE",
}
TREES = ("src/reviewflow", "tests", "docs", "examples", "benchmarks")
SUFFIXES = {".py", ".md", ".json", ".toml", ".txt", ".rst", ".yaml", ".yml", ".ini", ".cfg"}
EXCLUDE = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".git",
    ".venv",
    ".superpowers",
    ".claude",
    "node_modules",
    "results",
    "artifacts",
}
SECRET = re.compile(
    rb"(?:sk-[A-Za-z0-9_-]{24,}|AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)


def package_source(project: Path, output: Path):
    root = project.resolve(strict=True)
    if not (root / "pyproject.toml").is_file() or not (root / "src/reviewflow").is_dir():
        raise ValueError("Expected trusted ReviewFlow source root")
    destination = output.parent.resolve(strict=True) / output.name
    if root == destination or root in destination.parents or os.path.lexists(destination):
        raise ValueError("Archive must be a new path outside the source tree")
    paths = []
    for name in ROOT_FILES:
        if os.path.lexists(root / name):
            paths.append(root / name)
    for name in TREES:
        base = root / name
        if any(
            (root / Path(*Path(name).parts[:i])).is_symlink()
            for i in range(1, len(Path(name).parts) + 1)
        ):
            raise ValueError("Symlink in package tree")
        if not base.is_dir():
            continue
        for directory, dirs, files in os.walk(base, followlinks=False):
            for entry in dirs + files:
                if (Path(directory) / entry).is_symlink():
                    raise ValueError("Symlink in package tree")
            dirs[:] = [d for d in dirs if d not in EXCLUDE and not d.endswith(".egg-info")]
            for filename in files:
                path = Path(directory) / filename
                if filename.lower() in SENSITIVE_NAMES or path.suffix.lower() in SENSITIVE_SUFFIXES:
                    raise ValueError("Known credential filename in package tree; inspect locally")
                if (
                    path.suffix.lower() in SUFFIXES
                    and not filename.startswith(".env")
                    and not filename.startswith("config.local.")
                ):
                    paths.append(path)
    contents, total = {}, 0
    for path in sorted(paths):
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("Nonregular package input")
        data = read_bounded(path, 1048576)
        total += len(data)
        if total > 16777216 or len(contents) >= 1000:
            raise ValueError("Package budget exceeded")
        if SECRET.search(data):
            raise ValueError("Potential credential in package; inspect locally")
        contents[path.relative_to(root).as_posix()] = data
    manifest = {
        "schema_version": 1,
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
        "notice": "Whitelist and heuristic secret checks do not replace manual inspection.",
    }
    # Exclusive creation avoids accidental replacement; no subsequent cleanup of user files.
    with destination.open("xb") as stream:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in contents.items():
                archive.writestr(name, data)
            archive.writestr(
                "PACKAGE-MANIFEST.json", json.dumps(manifest, indent=2, sort_keys=True)
            )
    return {
        "status": "created",
        "archive": str(destination),
        "files": len(contents),
        "source_bytes": total,
        "notice": manifest["notice"],
    }
