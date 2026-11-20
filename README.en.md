# ReviewFlow

[中文](README.md)

ReviewFlow is a multi-agent pre-release review tool for COMP5584. It inventories a repository, then five roles read the Python and Java sources, check findings, and propose edits. Files change only after you confirm the item numbers.

A local directory receives the edits you select. A Git repository is repaired in a separate worktree: tests run in a container, and selected sources are written back after the gates pass.

## What it does

- **Inventory.** Lists included and skipped files, sizes, and a snapshot. No model call and no target tests.
- **Five-role review.** Planner assigns tasks. Correctness and Security look for functional and security defects separately. Verifier keeps only accepted findings. Patch proposes full-file replacements.
- **Confirmed local edits.** `local-fix` stores suggestions in a preview. `--list` shows the numbers. `--apply` writes the numbers you choose and does not call the model again.
- **Git worktree repair.** `repair-run` prints a preview digest. After approval it tests the candidate in a worktree and writes accepted edits back to the selected sources, leaving a report and a diff.

## Requirements

- Python 3.11 or newer
- Optional review dependencies: Pydantic 2 and LangGraph 1
- Git repair needs a local Git installation and a Docker image that is already present
- Progress lines, inventories, and reports are in English

## Install

Run these commands in the ReviewFlow directory.

Windows:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,review]"
.\review --help
```

`review.cmd` launches the virtualenv Python in this directory.

macOS / Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,review]'
.venv/bin/review --help
```

## Configuration

Keep two private files in the ReviewFlow directory. Both are listed in `.gitignore`. Do not commit the key.

`config.local.toml`:

```toml
secrets_file = "secrets.local.toml"
```

`secrets.local.toml`:

```toml
provider = "openai"
api_type = "responses"
model = "your-model-id"
base_url = "https://your-gateway.example/v1"
api_key = "your-key"
```

Leave `api_type` as `responses`. `base_url` ends at `/v1`; ReviewFlow calls `/responses`. `local-fix` reads this config by default, so the command does not need `--config`.

Check that the files can be read:

```powershell
.\review config-check --config config.local.toml
```

## Commands

Examples below run from the ReviewFlow directory. On Windows use `.\review`. On macOS / Linux use `review` or `.venv/bin/review`.

### Inventory

```powershell
.\review
.\review -d E:\path\to\project
.\review -d E:\path\to\project --source directory --verbose
.\review -d E:\path\to\project --format json
```

`--source auto` reads the committed `HEAD` of a Git repository. Use `--source directory` to inventory the current files, including uncommitted changes.

### Offline demo

This walks the role flow on the bundled sample and does not call a model:

```powershell
.\review --demo -d examples\demo-project --source directory
.\review --demo --mode single -d examples\demo-project --source directory
```

`multi` is the default. `single` uses the combined reviewer. `multi-no-verifier` skips verification.

### Local review and apply

The first command runs the five roles and writes suggestions to `local-fix.preview.json`. It does not change the target. It reviews Python and Java sources and skips test files. The terminal shows the current batch and the role waiting on the model.

```powershell
.\review local-fix -d E:\path\to\project
```

Show the same preview again without a model call:

```powershell
.\review local-fix -d E:\path\to\project --list
```

Write by number. Quote the argument in PowerShell:

```powershell
.\review local-fix -d E:\path\to\project --apply "1,3,5"
.\review local-fix -d E:\path\to\project --apply "4-6"
.\review local-fix -d E:\path\to\project --apply "except 4"
.\review local-fix -d E:\path\to\project --apply
```

| Form | Result |
| --- | --- |
| `--apply "1,3,5"` | Write those items |
| `--apply "4-6"` | Write a continuous range |
| `--apply "except 4"` | Write the remaining items, skipping any item that would blank a file |
| `--apply` | Write every item that would not blank a file |

Each command sees only the items still in the preview. After the last item is written, the preview file is removed. Another `local-fix` without `--list` or `--apply` calls the model again and replaces the current preview.

### Git repair

The target must already have commits. Preview first, then repeat the command with the printed digest:

```powershell
.\review repair-run --development --config E:\PythonProject\LLM_Project\config.local.toml -d E:\path\to\repo --include src\App.java --image sha256:your-image-digest --output E:\path\to\new-report
.\review repair-run --development --config E:\PythonProject\LLM_Project\config.local.toml -d E:\path\to\repo --include src\App.java --image sha256:your-image-digest --output E:\path\to\new-report --approve THE_DIGEST
```

Repeat `--include` for more than one relative path. `--output` must be a new directory outside the target. The approved run tests the candidate in a container and writes gated edits back to the selected sources. The report directory keeps `report.json`, `report.md`, and `candidate.diff`.

Run the committed tests without a model call:

```powershell
.\review repair-run --development --tests-only --config E:\PythonProject\LLM_Project\config.local.toml -d E:\path\to\repo --include src\App.java --image sha256:your-image-digest --output E:\path\to\new-test-report
```

Preview that command as well, then add `--approve`.

## Five roles

Each role is `src/reviewflow/agents/SOUL.md`, its own `AGENT.md`, and one skill. The program controls review limits, budgets, and writes.

| Role | Job |
| --- | --- |
| Planner | Split the batch into correctness and security tasks. It does not judge the code. |
| Correctness | Find functional defects. It does not see the Security result. |
| Security | Find input, authority, and dangerous-operation defects. It does not see the Correctness result. |
| Verifier | Accept, reject, or leave a finding unresolved from the evidence. Only accepted findings are patched. |
| Patch | Propose a full file and an explanation. It does not write to disk. |

`general` is used only by `--mode single`, combining correctness and security in one review.

## Development checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
```

Do not commit an API key, `secrets.local.toml`, `config.local.toml`, or `local-fix.preview.json`.
