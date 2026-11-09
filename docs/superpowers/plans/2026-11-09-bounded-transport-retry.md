# Bounded transient transport retries

> **For agentic workers:** Implement inline with TDD and verification-before-completion; no commits or pushes.

**Goal:** Enable automatic recovery from the observed HTTP408 and timeout failures without hiding attempts/unknown billing or weakening final source/test/review gates.

**Architecture:** Repair CLI defaults to `--transport-retries 2` (0 disables,1 permits one retry). The count and fixed policy are bound into a fresh preview/approval. Development transport executes each physical attempt with its own consumed grant, actual call count and record; retry authorization re-enters the repair parent to recheck original/candidate identity, expiry and effective remaining budget. Only a later fully validated success in the same owned transport can classify prior eligible failures as recovered; their failure code and unknown usage remain. Standalone/readonly transport defaults stay no-retry. No worker-internal retries.

**Tech Stack:** Python, Pydantic, existing DevelopmentTransport/RepairRunAuthority, isolated Responses worker, pytest/local HTTP fixtures.

**Spec:** User explicitly requested “那就开启一下重试机制呀” after discussion of finite retries and retaining unknown usage. Implementation is authorized; this does not silently reopen the completed run19 or automatically launch another paid full-target acceptance.

## Global constraints and rulings

- Retry only fixed codes `http_status_408`, `response_timeout`, `worker_timeout`; do not infer temporary failure from arbitrary messages. Authentication/parameter/TLS/protocol/output/usage errors, generic network_error and429/5xx are not retried in this first policy.
- At most two retries after the original request, backoff1s then2s. Every actual attempt retains the same payload and per-attempt60-second/2048-token/65536-byte limits.
- All attempts count toward phase8 and finite parent budget, including held final reserve. Existing optional aggregate no-call-limit still only removes that aggregate ceiling; no extra request/phase allowance.
- Recheck current candidate/origin, scope, expiry and budget AFTER waiting and BEFORE each retry. No patch is applied inside the transport; only one validated returned response reaches the patch controller.
- Preserve failed records, safe progress, unknown usage and unknown monetary cost. A successful retry does not rewrite the failed attempt as complete/free. A retry chain that never succeeds closes the parent; an arbitrary unknown-usage successful HTTP result remains fatal and non-retryable.
- Allow only in-process owned recovery receipts, never restored from report JSON. Seed loader remains unchanged and rejects unknown-usage histories, even if a later retry recovered.
- Fresh approval binds retry policy, exact allowed codes/backoffs/count/accounting semantics. Previous zero-retry approvals do not authorize retries. Tests-only sends no requests; readonly CLI remains unchanged.
- Actual target untouched; no host target execution, source uploads, live-provider calls, commit/push or automatic run20 during implementation/tests. Local HTTP fixtures use synthetic keys/source only.

## Review focus

1. Retry is an actual separately counted/granted attempt, never hidden inside the HTTP adapter; fresh intent checks include source changes during backoff.
2. Exact retry-code allowlist, hard cap, phase/parent/expiry and final reserve still enforce before network side effects.
3. Successful transient recovery may continue but failed request usage remains unknown; unrelated missing/invalid usage never becomes recoverable.
4. Approval policy/count tampering rejected; false/float values rejected; zero mode remains fail-closed and readonly behavior unchanged.
5. Actual CLI/local HTTP tests show408→success continuation, three408 stops, explicit0 stops and raw error bodies/secrets stay absent. Synthetic full repair still needs final tests/review before writing only fixture files.

## Tasks

- [x] Add RED tests for retry counts, per-attempt grants/accounting, transient success/exhaustion, non-retryable errors, budget/expiry/source-change stops, approval tampering and local HTTP CLI behavior.
- [x] Minimal implementation in development.py, repair_budget.py, CLI configuration/report boundary; preserve worker one-request behavior and seed input filtering.
- [x] GREEN focused suite and Ruff; one independent direct read-only review; fix confirmed issues with RED/GREEN tests.
- [x] Full product pytest and Ruff on frozen current source; inspect complete results.
- [x] Document defaults, disabling retries, accounting/unknown billing, actual acceptance not rerun. Update handoff.

## Ledger

- RED: `/tmp/reviewflow-retry-red.log`:33 failed,6 passed; missing config/CLI retry support and strict integer validation.
- Initial GREEN attempt:38 passed,1 failed. Local HTTP fixture omitted `system` and `response_schema` when reconstructing a wire for RepairFixtureAdapter, causing KeyError and an empty200 body. Fixed only fixture reconstruction from actual HTTP instructions/schema; production JSON validation stayed unchanged.
- Retry suite GREEN:39 passed in42.83s, `/tmp/reviewflow-retry-green.log`. Includes real CLI function and isolated HTTP worker against synthetic localhost provider; Docker execution is mocked, not target acceptance. Full product suite and independent read-only review started; results pending.

- Independent read-only review: one confirmed request-cap defect. Evidence correction promised one physical request, dependency/reassessment promised three, but retries only enforced generic phase8. Three new full-controller reproductions failed RED (3>1,4>3,4>3), `/tmp/reviewflow-retry-narrow-red.log`. Fixed by carrying the existing policy's `max_requests` through start_phase and checking it for every original/retry grant. Callers changed: verification_recovery.py, dependency_verification.py, reviewer_reassessment.py. No limits widened.
- Ruling: narrower existing recovery caps take precedence over retry availability; evidence correction's single-request phase cannot retry, and dependency/reassessment retries consume their three-request allowance. This preserves existing approval instead of silently multiplying it; cost: some transient failures still stop recovery before two retries are possible.
- Final retry suite GREEN:42 passed in48.84s; Ruff clean (`/tmp/reviewflow-retry-green-final.log`, `/tmp/reviewflow-retry-lint-final.log`). Initial full run deliberately stopped to fix review finding; not claimed as passing. Fresh full run completed on hashed source/tests (`/tmp/reviewflow-retry-verified-inputs.json`): **1243 passed in1005.26s (16m45s)**; Ruff `All checks passed!`; all154 source/profile/test file hashes unchanged. Read complete terminal logs `/tmp/reviewflow-retry-full-final.log`, `/tmp/reviewflow-retry-full-final-lint.log`; pytest/ruff/freeze all exit0. No failures or warnings reported. This is product regression, not paid-model/full-target acceptance.
- Actual launcher smoke: `review repair-run --help` displays default2/disable0; offline `review --demo -d examples/demo-project --source directory` exits0 and explicitly reports zero network calls. No paid provider or actual target run.
- Observed root cause of no retry: DevelopmentTransport.call unconditionally sets blocked after any adapter exception, and RepairRunAuthority.record_phase rejects every failed/unknown record. Retrying solely inside the HTTP adapter would hide attempts from parent accounting; do not do that.
- Ruling: default two retries only for repair CLI, with config/approval-bound count; existing direct/readonly DevelopmentConfig remains default0. The user asked to enable repair retries, not broaden every network consumer.
- Ruling: first version uses only observed408 and explicit timeout classes; generic networking, rate limiting and5xx remain non-retryable until separately designed. This can stop on other recoverable failures but avoids guessing/retrying invalid calls.
- Ruling: successful recovery receipt authorizes continued control flow, not billing completeness or resumed old authority. No automatic additional paid real-target run under the previous one-shot request.
