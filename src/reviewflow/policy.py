"""Pure path policy, applied before any file content is read."""

from pathlib import PurePosixPath

from reviewflow.models import EntryType, FileKind

EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".superpowers",
        ".claude",
        ".eggs",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "node_modules",
        "dist",
        "build",
        ".idea",
        ".vscode",
        ".reviewflow",
        ".worktrees",
        "workspaces",
        "target-repos",
    }
)
TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".md",
        ".rst",
        ".txt",
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".ini",
        ".cfg",
        ".sql",
    }
)
TEXT_NAMES = frozenset({"dockerfile", "makefile", ".gitignore"})
SENSITIVE_NAMES = frozenset(
    {".env", "id_rsa", "id_ed25519", "credentials.json", "secrets.local.toml"}
)
SENSITIVE_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx"})


def is_excluded_directory(name: str) -> bool:
    return name.lower() in EXCLUDED_DIRECTORIES or name.lower().endswith(".egg-info")


def path_reason(path: str, entry_type: EntryType) -> str | None:
    if entry_type in ("symlink", "submodule"):
        return entry_type
    parsed = PurePosixPath(path.lower())
    directories = parsed.parts if entry_type == "directory" else parsed.parts[:-1]
    if any(is_excluded_directory(part) for part in directories):
        return "excluded_directory"
    if (
        parsed.name in SENSITIVE_NAMES
        or parsed.name.startswith(".env.")
        or parsed.suffix in SENSITIVE_SUFFIXES
    ):
        return "sensitive_path"
    if parsed.suffix not in TEXT_SUFFIXES and parsed.name not in TEXT_NAMES:
        return "unsupported_type"
    return None


def classify_kind(path: str) -> FileKind:
    parsed = PurePosixPath(path.lower())
    if parsed.suffix in {".py", ".pyi"}:
        if "tests" in parsed.parts[:-1] or parsed.name.startswith("test_"):
            return "test"
        return "source"
    if parsed.suffix in {".md", ".rst", ".txt"}:
        return "documentation"
    if parsed.suffix in TEXT_SUFFIXES or parsed.name in TEXT_NAMES:
        return "config"
    return "other"
