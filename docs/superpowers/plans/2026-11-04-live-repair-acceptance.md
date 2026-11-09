# Live Repair Acceptance Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline. No commits/pushes authorized. Each task requires observed RED→GREEN. Retain evidence because product repository has no HEAD.

**Goal:** A genuine CLI review→model patch→container tests→new review→guarded working-tree application, never assistant-authored target fixes.

**Architecture:** Preserve existing strict offline repair/publication services. Add narrowly scoped live orchestration, exact-edit translation, an owned HEAD-derived candidate, and Docker-volume test transport. Existing transport limits remain per phase; a parent authority enforces the approved total across phases.

**Tech Stack:** Python 3.12, Pydantic 2, existing LangGraph review, Responses development adapter, Docker/Colima.

**Spec:** docs/superpowers/specs/2026-11-04-live-repair-acceptance.md

## Global Constraints

- No target execution on host; no target hand-fixes, synthetic acceptance, commits/push/merge/stash/reset.
- No real model calls until fresh concrete approval; 48 is a design ceiling only.
- 8 calls/phase, 65536-byte body, 2048 output tokens, 60-second request, no implicit retries; unknown usage closes the entire run.
- Export <=200 files, <=262144 bytes/file, <=8388608 bytes total; no credential paths, symlinks or submodules. Local `.env.example`/shell inputs are explicitly disclosed, never implicitly uploaded.
- Image is immutable digest; no runtime pulls/install; no host shares; target containers nonroot, read-only, network none and resource bounded.
- Existing local/index/untracked work preserved. No claim of transactional safety against arbitrary concurrent external editors.
- Reports and development evidence outside target, with copyable absolute paths.

## Review Focus

- Copy/timeout/cleanup errors must never produce success or execute target before successful export (Task 1).
- Old finding identity/hash after an earlier overlapping patch must not authorize stale edits (Tasks 2/4).
- User changes to destination/parents/index during long model runs must stop writeback without deleting user bytes (Task 3).
- Batch-scoped partial reports must retain partial status while explicit per-role selected coverage gates application (Task 4).
- An all-skipped/collection-error pytest run, exhausted budget or transport uncertainty cannot mint application permission (Tasks 1/4).

### Task 1: Isolated recursive test execution without host shares

**Files:** Modify backend/validation.py; create backend/volume_runner.py; tests/test_volume_runner.py; existing tests/test_validation_publication.py.
**Interfaces:** Consume ApprovalGate and ToolRun/content_digest. Produce `VolumeDockerRunner(image, *, timeout_seconds=60, execute=subprocess.run)` with `intent(files, suite='repository')`, `run(files, suite, gate, grant)`, `owns(result)`. Existing DockerRunner additionally accepts strict `sha256:<64 lowercase hex>` IDs; legacy suites unchanged.

- [ ] Write/run failing tests for local image ID acceptance at execution boundary and mutable/malformed refs rejection.
- [ ] Implement strict image validation; run old and new validation tests.
- [ ] Write/run failing tests for repository volume lifecycle, no implicit execution before valid approval, bounded safe exports, copy failure, test timeout, cleanup failure, real exit status classification and foreign result rejection.
- [ ] Implement controlled create-volume/create-helper/copy/run/remove sequence. Helper never executes target; execution container mounts only approved copy read-only. No host mounts/socket/config. Preserve fixed sandbox flags and discard raw output.
- [ ] Add a fixed pytest wrapper: recursive /workspace, fixed configuration, actual executed-test requirement; collect/setup/teardown/internal errors blocked, ordinary call failures failed, all skipped/no tests blocked. Test wrapper behavior on local product-owned fixtures (not user target), plus actual container once image ready.
- [ ] Run `DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib .venv/bin/python -m pytest tests/test_volume_runner.py tests/test_validation_publication.py -q` → all pass; full suite before task completion.
- [ ] Record immutable image/base IDs and actual sandbox smoke evidence; no claim target repaired.

### Task 2: Exact-edit live Patch adapter

**Files:** create backend/live_patch.py; modify agents/roles/patch/AGENT.md and skills/patching/SKILL.md; tests/test_live_patch.py.
**Interfaces:** `ExactEdit(path, before_sha256, old_text, new_text)`; `EditProposal(snapshot_id, finding_id, edits, explanation)` Pydantic models. `translate_edits(context, task, finding, proposal) -> PatchProposal`; `propose_live_patch(context, task, finding, gateway, profiles, *, feedback=None) -> PatchProposal`.

- [ ] Tests: unique nonempty original match; current hashes/snapshot/finding; overlapping edits, no-ops, NUL, unsupported paths, test/config writes and total size rejected; multiple nonoverlapping edits refer to original bytes, not sequentially created matches.
- [ ] Observe RED, implement translation via existing validate_patch, observe GREEN.
- [ ] Tests: real ModelRequest uses frozen patch role, current CodePackets and supplied response schema; actual WorkflowGateway/loopback provider validates it; stale evidence and bad JSON reject without unsafe writes.
- [ ] Observe RED, implement request builder; preserve offline full-file Patch contract; GREEN plus existing agent-profile/backend tests.

### Task 3: Owned HEAD candidate and guarded writeback

**Files:** create backend/repair_candidate.py; tests/test_repair_candidate.py.
**Interfaces:** `CandidatePlan.from_head(source, selected_paths, destination)` produces immutable export/selection/state manifest; `.preview()` pure. `RepairCandidate.create(plan, gate, grant)`, `.files()`, `.apply(proposal, context, task, finding)`, `.restore_attempt()`, `.source_inventory(paths)`, `.writeback(permit)`; all effects require current process-owned state. `ApplicationPermit` minted only by Task 4 authority.

- [ ] Tests: preview no effects; no-checkout detached worktree exact HEAD bytes; scripts/templates supported only in local export; unsafe/oversized/binary tree rejected; actual secrets rejected, not silently excluded.
- [ ] Observe RED, implement materialization using safe Git and exclusive writes, GREEN.
- [ ] Tests: only approved existing source .py changes; stale bytes/foreign root/symlink/metadata changes reject; no index or unrelated dirty/untracked alteration; failed multi-file write rolls back only owned bytes; candidate/export digest changes invalidate permit.
- [ ] Observe RED, implement no-follow checked replacement and bounded original-state checks; GREEN. Preserve worktree/evidence after failures.

### Task 4: Bounded live repair controller and application authority

**Files:** create backend/live_repair.py and backend/repair_authority.py; tests/test_live_repair.py.
**Interfaces:** `RepairRunAuthority(preview, approved_digest, gate)` binds all phase intents, total attempts and stop state; `.authorize_model(intent)`, `.record_phase(transport)`, `.finish(candidate, test_result, final_reviews) -> ApplicationPermit`. `run_repair_session(plan, config, authority, profiles, runner)` returns report dictionary with genuine statuses/accounting.

- [ ] Tests: absent/mismatched approval has no side effects; exact budget across transports; any unknown usage/failure closes subsequent sends; scope/model/profile/image/source changes reject.
- [ ] Observe RED, implement parent authority over existing transports (no altered per-transport limits), GREEN.
- [ ] Tests: initial real review coverage by correctness AND security; no fake complete rewrite of partial reports; only accepted findings; up to two attempts per selected candidate; fresh current-snapshot verification after changed evidence; unsupported/rebase-ambiguous findings stop rather than guess.
- [ ] Observe RED, implement candidate review/patch sequencing using Tasks 2/3, GREEN.
- [ ] Tests: baseline ordinary failures may proceed but infrastructure failures stop; final actual container pass plus same-snapshot complete selected coverage with no accepted/unresolved findings required; foreign/simulated/stale results or disk-restored records never authorize writeback; failures preserve original target.
- [ ] Observe RED, implement process-local application gate (not old publication receipt), GREEN.

### Task 5: CLI and end-to-end product validation

**Files:** backend/cli.py (register command), backend/live_repair_cli.py (new parser/preview/report boundary), README.md, docs/backend-guide.md, tests/test_live_repair_cli.py.
**Interfaces:** `run_live_repair(args) -> int`; `review repair-run --development --config PATH -d ROOT --include PATH ... --image DIGEST --output PATH [--max-calls 1..48] [--approve DIGEST]`.

- [ ] Tests: zero-network/effect preview; external new output path; exact preview digest; scope/output/root changes reject before requests; truthful stdout/report/exit codes and absolute report location.
- [ ] Observe RED, implement CLI boundary, GREEN.
- [ ] Drive actual installed CLI against product-owned synthetic repository and loopback model, using real Docker for test subprocesses; fixtures explicitly labeled synthetic, never reported as user-target accuracy.
- [ ] Run full pytest and Ruff src/tests; independent focused review once for whole feature; address important findings with RED→GREEN and full suite. No commits or automatic cleanup.
- [ ] Produce new real-run preview for the user's feat/test source only after all gates above. Fresh numerical paid-call authorization is a separate prerequisite. Execute actual product CLI only under that grant, never manual target patches. Report partial/blocked outcome faithfully if any prerequisite fails.
