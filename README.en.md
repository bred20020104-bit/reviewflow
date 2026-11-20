# ReviewFlow

[中文](README.md)

ReviewFlow is a finished pre-release review system for the COMP5584 English course. It connects repository inventory, a five-role model review, numbered human confirmation, and source write-back into one complete pipeline. It reviews Python and Java projects. A local directory receives the edits you select. A Git repository is repaired in its own worktree: the patch is tested in Docker, and selected sources are written back while the original commit remains intact.

Terminal progress, inventories, and reports are in English, ready for a class demonstration.

## Delivered capabilities

The full chain is complete and has been exercised on a real Java project.

| Capability | What you get |
| --- | --- |
| Repository inventory | Project structure, included files, sizes, and a snapshot in one view |
| Five-role review | Planner, Correctness, Security, Verifier, and Patch, with live step progress |
| Evidence-backed advice | Every suggestion has a title, an explanation, and the concrete edit |
| Human confirmation | Accept numbers, accept a range, or leave selected numbers out |
| Local write-back | Confirmed edits update `.py` and `.java` files in the target project |
| Git worktree repair | Patch, container tests, and write-back happen in a separate worktree |
| Cross-platform entry | `.\review` on Windows, `review` on macOS and Linux |

A full local review reads the source in batches. Each batch is planned, reviewed for correctness, reviewed for security, verified finding by finding, and then turned into edits for accepted findings. The final write stays in your hands.

## Five roles

Each role has its own system prompt: the shared contract in `SOUL.md`, a role file `AGENT.md`, and one professional skill. Those prompts travel with the request. The program controls scope, call budget, and writes.

| Role | Responsibility |
| --- | --- |
| Planner | Organize the batch into correctness tasks and security tasks |
| Correctness | Find functional defects: wrong results, state transitions, error handling, and lost data |
| Security | Find security defects: input handling, authority boundaries, and dangerous operations |
| Verifier | Judge each finding against the source and accept, reject, or set it aside |
| Patch | Write the full replacement and a clear explanation for every accepted finding |

Correctness and Security work independently and do not use each other's conclusions. Verifier is the quality gate. Patch handles accepted findings only. The single-role baseline starts with `--mode single` and uses `general` to cover correctness and security in one pass.

## Setup

ReviewFlow requires Python 3.11 or newer. The review flow uses Pydantic 2 and LangGraph 1. Git repair uses a local Git installation and a prepared Docker image.

Run the commands in the ReviewFlow directory.

Windows:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,review]"
.\review --help
```

macOS / Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,review]'
.venv/bin/review --help
```

On Windows, `review.cmd` in the repository root launches the virtualenv Python.

## Model configuration

Create two private files in the ReviewFlow directory. `.gitignore` already excludes them, so the key stays on your machine.

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

Set `api_type` to `responses`. End `base_url` at `/v1`; ReviewFlow calls `/responses`. `local-fix` loads this configuration automatically.

```powershell
.\review config-check --config config.local.toml
```

## Commands

Run the examples from the ReviewFlow directory. Use `.\review` on Windows and `review` or `.venv/bin/review` on macOS / Linux.

### 1. Show the inventory

```powershell
.\review -d E:\path\to\project
.\review -d E:\path\to\project --source directory --verbose
.\review -d E:\path\to\project --format json
```

A Git repository shows the committed `HEAD` by default. `--source directory` shows the current working tree. This is a strong opening shot for a demo: the audience sees the size of the project first.

### 2. Walk the roles offline

```powershell
.\review --demo -d examples\demo-project --source directory
.\review --demo --mode single -d examples\demo-project --source directory
```

The bundled sample runs the review, verification, and report flow. The default mode is `multi`.

### 3. Run a full local review

This command calls all five roles, reviews the Python and Java sources in batches, and saves a numbered preview. The terminal keeps printing the batch, the active role, and the wait for the model.

```powershell
.\review local-fix -d E:\path\to\project
```

When the run finishes, the terminal lists every suggestion with its number, title, file, explanation, and edit. Show that same result again:

```powershell
.\review local-fix -d E:\path\to\project --list
```

### 4. Write the chosen numbers

Quote the selection in PowerShell.

```powershell
.\review local-fix -d E:\path\to\project --apply "1,3,5"
.\review local-fix -d E:\path\to\project --apply "4-6"
.\review local-fix -d E:\path\to\project --apply "except 4"
.\review local-fix -d E:\path\to\project --apply
```

| Command | Effect |
| --- | --- |
| `--apply "1,3,5"` | Accept the listed numbers |
| `--apply "4-6"` | Accept a continuous range |
| `--apply "except 4"` | Accept every remaining number except 4 |
| `--apply` | Accept every writable item still in the preview |

Each command continues from the numbers that are still waiting. Accept a few items, then choose again from what remains. After the last item is written, that preview is complete. A new `local-fix` without `--list` or `--apply` starts a fresh five-role review.

### 5. Professional repair for a Git repository

For a repository that already has commits, ReviewFlow repairs it in a separate worktree. Generate the preview, then approve it with the printed digest:

```powershell
.\review repair-run --development --config E:\PythonProject\LLM_Project\config.local.toml -d E:\path\to\repo --include src\App.java --image sha256:your-image-digest --output E:\path\to\new-report
.\review repair-run --development --config E:\PythonProject\LLM_Project\config.local.toml -d E:\path\to\repo --include src\App.java --image sha256:your-image-digest --output E:\path\to\new-report --approve THE_DIGEST
```

Repeat `--include` for additional relative paths. `--output` is a new directory outside the repository. The approved run executes the tests in a container and writes the validated edits back to the selected sources. The report directory keeps `report.json`, `report.md`, and `candidate.diff` as the deliverable record of the repair.

Container tests for the committed revision:

```powershell
.\review repair-run --development --tests-only --config E:\PythonProject\LLM_Project\config.local.toml -d E:\path\to\repo --include src\App.java --image sha256:your-image-digest --output E:\path\to\new-test-report
```

Read the preview, then add `--approve`.

## Suggested demonstration

1. Open with the inventory so the audience sees the project scale.
2. Spend a minute on the five roles and the independent verification gate.
3. Show live `local-fix` progress, or use `--list` to present a numbered result that is already prepared.
4. Pick a few clear logic edits, write them with `--apply "numbers"`, and open the files to point out the change.
5. Close with the Git path: the original commit stays intact while the patch is tested in its own worktree and then written back.

## Development checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
```

Private model settings and review previews stay on the machine. The submitted project contains the ReviewFlow source, tests, and these guides.
