# Repair Refresh and Safe Test Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Continue safely after owned candidate edits invalidate pending findings, and preserve bounded test failure diagnostics for each run.

**Architecture:** Retain immutable initial reports; explicitly reassess stale findings using current evidence and independent verification, then replace affected queue entries. Keep diagnostics separate from test verdicts: collect a bounded structured channel, validate it on the host, and expose only safe fields. Existing final tests, dual-role review, authority checks and writeback gates remain mandatory.

**Tech Stack:** Existing Python, Pydantic contracts, pytest, Docker volume runner; no new dependencies.

**Spec:** Approved conversation scope (2026-11-06): fix stale evidence handling and test diagnostics, verify locally; no paid-model/real-target acceptance or commits/pushes.

## Global Constraints

- Never silently rewrite evidence hashes, skip findings by title, or treat omission as resolution.
- Unknown reassessment blocks writeback. Independent accepted current evidence is required for further patches.
- Refreshes share the existing approved model-call budget; bounded rounds and patch attempts are approval-bound.
- No raw target logs, exception messages, assertion values, locals or parameterized test IDs in published diagnostics.
- Target execution stays in Docker; tests may execute product-owned synthetic fixtures locally.
- No change to the original real target workspace; no commit/push.

## Review Focus

- Two distinct defects in one file must survive evidence invalidation independently (Task 1).
- Repeated/oscillating findings must not bypass per-run bounds (Task 1).
- Evidence outside scope, source tampering or incomplete role coverage must remain blocked (Task 1).
- Target-controlled diagnostic content must not leak secrets or inject terminal/Markdown content (Task 2).
- Missing/truncated diagnostics and failed cleanup must never turn test failure into success (Tasks 2–3).

### Task 1: Current-snapshot reassessment and bounded queue refresh

**Files:** Create `src/reviewflow/backend/finding_refresh.py`, `tests/test_finding_refresh.py`; modify `live_repair.py`, `repair_budget.py`, `tests/test_live_repair.py`, `tests/test_repair_budget.py`.

**Interfaces:** `refresh_findings(candidate, report, outcomes, authority, profiles, adapter, audit)` returns current scoped report and current accepted outcomes; preserves audit links to old snapshot/finding. `REFRESH_POLICY` binds limits to approval.

- [x] Write regression fixtures for overlapping resolved findings, a remaining same-file defect, unresolved/invalid evidence, parent exhaustion and repeated refreshes; run and observe expected failures.
- [x] Implement explicit reassessment contract with old issue as historical context, current file packets, and current finding/evidence outputs. Independently verify refreshed findings; independently verify explicit resolution evidence. Do not reuse historical evidence as current provenance.
- [x] Integrate queue replacement after an owned patch invalidates pending review context; retain initial reports and append refresh audit. Enforce fixed refresh and total patch bounds within parent budget, preserving final tests/review/writeback.
- [x] Run refresh, live repair, verifier recovery and budget tests; update former overlap-blocked expectation to successful verified resolution.

### Task 2: Bounded safe test diagnostic channel

**Files:** `test_projects.py`, `volume_runner.py`, `validation.py`; create focused diagnostic protocol module/tests as needed; modify `tests/test_test_projects.py`, `tests/test_volume_runner.py`.

**Interfaces:** Append defaulted immutable diagnostic fields to `ToolRun`, preserving existing positional construction. Expose finite failure records and availability/truncation state via `asdict(result)`; publish field names to integration task.

- [x] Write failing tests for structured per-project/per-phase failures, bounded transfer, malformed output, secret-bearing messages/parameter IDs and unchanged exit-code semantics.
- [x] Implement pytest structured collection with no arbitrary target text, a bounded dedicated transfer path, and strict host validation against approved source files. Keep raw stdout/stderr discarded and all isolation settings unchanged.
- [x] Bind diagnostics policy/version into runner intent. Keep diagnostic details separate from pass/fail and ownership checks.
- [x] Run project-script/volume-runner tests, including timeout/cleanup degradation and synthetic secret non-disclosure.

### Task 3: Traceable repair report and regression verification

**Files:** `live_repair.py`, `live_repair_cli.py`, `tests/test_live_repair.py`, `tests/test_live_repair_cli.py`, user/backend guide.

**Interfaces:** Test records gain `stage`, `finding_id`, `patch_sha256` and stable run sequence; existing `ToolRun` fields remain intact. Markdown renders only verified bounded diagnostics and explicitly distinguishes static resolution from successful overall repair.

- [x] Write failing tests for baseline/candidate/final association, retained records on blocked runs, safe Markdown and refresh summaries.
- [x] Implement report integration, safe fixed-code failure explanations and documentation. No automatic target rerun or new model upload of test details.
- [x] Run full `.venv/bin/python -m pytest -q` and relevant lint. Report every failure and distinguish synthetic validation from real-target acceptance.
- [x] Independent focused review of changed behavior; fix verified defects and rerun affected tests.

Commits are intentionally omitted: user has not authorized them.
