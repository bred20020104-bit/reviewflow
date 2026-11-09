"""Explicit opt-in installer for a POSIX launcher; never edits shell profiles."""

import argparse
import os
import shlex
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from reviewflow.errors import InvalidInputError
from reviewflow.report import safe_text


def install_launcher(bin_dir: Path, *, python: Path, library_path: Path | None = None) -> Path:
    if os.name != "posix":
        raise InvalidInputError("The review launcher currently requires POSIX.")
    # Do not resolve the interpreter symlink: its venv path determines sys.prefix.
    python = python.expanduser().absolute()
    if not python.is_file() or not os.access(python, os.X_OK):
        raise InvalidInputError("Python must name an existing executable.")
    prefix = ""
    if library_path is not None:
        library_path = library_path.expanduser().absolute()
        if not library_path.is_dir() or ":" in str(library_path):
            raise InvalidInputError("Library path must be one existing directory, without ':'.")
        prefix = f"DYLD_LIBRARY_PATH={shlex.quote(str(library_path))} "
    bin_dir = bin_dir.expanduser().absolute()
    if bin_dir.is_symlink():
        raise InvalidInputError("Installation directory must not be a symlink.")
    path = bin_dir / "review"
    script = (
        "#!/bin/sh\n"
        "# ReviewFlow isolated launcher; reinstall if its environment moves.\n"
        f'{prefix}exec {shlex.quote(str(python))} -I -m reviewflow.quick "$@"\n'
    )
    bin_dir.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise InvalidInputError("review already exists; inspect it before reinstalling.") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(script)
        stream.flush()
        os.fchmod(stream.fileno(), 0o700)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    parser.add_argument(
        "--library-path", type=Path, help="optional machine-specific libexpat directory"
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    existing = shutil.which("review")
    try:
        path = install_launcher(
            args.bin_dir, python=Path(sys.executable), library_path=args.library_path
        )
    except InvalidInputError as exc:
        print(f"reviewflow install: {safe_text(exc)}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"reviewflow install: {safe_text(exc)}", file=sys.stderr)
        return 1
    print(f"Created launcher: {safe_text(path)}")
    if existing:
        print(f"Existing command on PATH: {safe_text(existing)}; check command -v review.")
    path_dirs = {Path(p).absolute() for p in os.environ.get("PATH", "").split(os.pathsep) if p}
    if path.parent not in path_dirs:
        print("Directory is not on PATH. No shell configuration was changed.")
        print(f'For this terminal: export PATH={safe_text(shlex.quote(str(path.parent)))}:"$PATH"')
        print("To persist it, add that line to your shell configuration after reviewing it.")
    else:
        print("Check command -v review, then run review --help from any project directory.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
