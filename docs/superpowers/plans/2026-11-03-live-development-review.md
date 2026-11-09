# Live Development Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a real, opt-in, bounded read-only Responses/LangGraph CLI without weakening priced production controls.

**Architecture:** Separate development config/transport, deterministic raw Responses adapter in a timeout-controlled isolated subprocess, run-scoped approval with per-request child grants, existing interactive graph and new truthful report wrapper. No target imports, repairs, or changes to private configuration.

**Tech Stack:** Python >=3.11, stdlib HTTP/subprocess, existing Pydantic 2 and LangGraph 1.

**Spec:** `docs/superpowers/specs/2026-11-03-live-development-review.md`

**Execution status (2026-11-04):** Selected-scope real end-to-end acceptance passed after explicitly authorized follow-up work: actual CLI/Responses/LangGraph,2 completed Reviewer tasks,4 independently verified candidates,0 workflow errors,0 unreviewed selected paths,8 completed requests. Repository-wide status remains partial because other files/directories were not reviewed; target unchanged and never executed. Offline suite503 passed. Earlier failed runs retained. Live review policy now preloads complete selected chunks within existing context bounds and caps priority findings at2 per Reviewer to reserve verification calls; this is not exhaustive auditing. Detailed evidence and authorization accounting: this plan's `.superpowers/sdd/2026-11-03-live-development-review/progress.md` (local, excluded from delivery).

## Global Constraints

- Development caps: 8 requests/run, 65,536 final body bytes/request, 2,048 requested output tokens, 60 seconds wall timeout, no retries or redirects.
- Unknown USD cost is null, never zero or a claimed monetary guarantee. Existing GuardedTransport remains unchanged.
- User authorized at most 8 live requests total this iteration, read-only on the named project; offline fixtures do not send externally.
- Source/snapshot/model/endpoint/scope/limits/output bind approval. No network before matching explicit run approval.
- Fresh external report directory; no secrets/raw provider errors/requests persisted. No target execution, patches, commits, pushes, frontend or private-file changes.
- Existing unborn feature workspace preserved. No project commits or worktree creation; manually record RED/GREEN evidence in a new owned ledger.

## Review Focus

- HTTP approval cannot silently leak into normal config or priced transport: Task 1 rejects HTTP outside development.
- Adapter transformations/schema bytes could differ from approved bounds: Task 2 counts and sends identical final request bytes.
- Delayed DNS/socket/oversized responses and redirects must not leave a background sender: Task 2 parent process timeout/kill and worker bounded-read tests.
- Model output or changed files must not broaden child approval: Task 3 changed snapshot/config/scope and forged intent tests.
- Live execution facts must not contaminate simulated session claims: Task 3 rendering and unchanged demo regression tests.

### Task 1: Development-only configuration and bounded request control

**Files:** Create `src/reviewflow/backend/development.py`, `tests/test_development_controls.py`.

**Interfaces:** `load_development_config(config_path, source, inventory)` returns a validated development-only model/budget/privacy object with Key reference only. `DevelopmentTransport(config, adapter, approvals)` implements existing gateway `intent/call`, where its bound is explicitly final request bytes, not tokens. Adapter supplies `body_size(wire)` and callable `Reply/Usage` contract. Defaults are fixed approved caps.

- [x] Write tests for HTTP opt-in isolation, five-field private loading, scope/size/call caps, exact grant binding/replay, unknown/malformed/excess usage blocks, no money claim, exception redaction.
- [x] Run `pytest tests/test_development_controls.py -q`; expected missing module/behavior RED.
- [x] Implement minimal independent config and transport; retain existing production types and controls unchanged.
- [x] Run focused tests and full suite; expected all pass. Record evidence, no commit.

### Task 2: Deterministic bounded Responses adapter

**Files:** Create `src/reviewflow/backend/responses_adapter.py`, `src/reviewflow/backend/responses_worker.py`, `tests/test_responses_adapter.py`.

**Interfaces:** `ResponsesAdapter(model, key_loader)` has `body_size(wire)` and `__call__(wire, *, max_output_tokens, timeout_seconds) -> Reply`. Worker receives bounded JSON stdin, sends one HTTP request, prints only validated response/usage or a fixed diagnostic.

- [x] Write tests for strict schema conversion, exact byte counts, output caps, key exclusion, completed responses and usage parsing, malformed/refused/incomplete responses, redirect refusal, oversized body and parent timeout. Use local HTTP fixtures only.
- [x] Run focused tests; expect RED before module implementation.
- [x] Implement deterministic mapping and isolated worker. No SDK install, ambient proxy discovery, retries or remote tools.
- [x] Run focused and full tests; expected green. Record evidence, no commit.

### Task 3: Live CLI, bounded run approval and truthful reports

**Files:** Create `src/reviewflow/backend/live_review.py`, `tests/test_live_review.py`; modify `src/reviewflow/quick.py` and `src/reviewflow/review/report.py` only as needed.

**Interfaces:** `run_live(args) -> int` handles explicit development-only config, exact source selection, preview digest, optional approved run, runtime transport/gateway construction, interactive graph and report persistence. Command: `review --review --development --config PATH -d TARGET --source directory --include FILE --output NEWDIR [--approve DIGEST]`. No approval means preview only. Supports existing mode/format options.

- [x] Write tests for preview no send/no output directory, missing mode/args fail closed, changed inputs deny stale digest, forged child intents denied, unchanged input and external exclusive report output, real vs simulated metadata, partial failures retained.
- [x] Run tests RED.
- [x] Implement bounded approval controller, gateway composition, live report JSON/text/markdown plus transport usage; never deserialize saved permissions.
- [x] Run full tests plus real CLI against a local fixture service (no external API); expect graph invocation with real HTTP protocol, honest report and untouched fixture.

### Task 4: Documentation, authorized live acceptance and review

**Files:** Modify `README.md`, `docs/model-setup.md`, `docs/backend-guide.md`, `docs/user-guide.md`; external test report only for real project.

- [x] Document development-only invocation, explicit preview/approval, caps and unknown cost, raw HTTP adapter not SDK, no automatic review/repair, existing production/priced blocker, report paths.
- [x] Run Ruff and full pytest suite. Run real CLI preview then approved run on selected files in authorized project, with at most 8 external calls total. Preserve partial results, no automatic rerun after failure.
- [x] Fresh read-only final review across changed files; fix Important issues with RED→GREEN and full suite, no re-review.
- [x] Final report: actual outcomes, calls/tokens/unknown cost, unchanged-source check, limitations and no commit/push. Do not rebuild old delivery ZIP in this task.
