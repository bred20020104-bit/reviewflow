# One explicitly uncapped-call acceptance implementation plan

> **For agentic workers:** Execute inline using TDD and verification-before-completion. No commit/push; preserve the user's existing work.

**Goal:** Under the user's 2026-11-09 request “那能不能 取消一次这个上限，全流程跑一下，我看看链路到底有没有问题，先看看行不行的问题 再说预算的问题。”, permit ONE fresh actual CLI acceptance with no aggregate model-request-count ceiling, preserving defaults and all validation/security gates.

**Architecture:** An explicit `repair-run --no-call-limit` flag (mutually exclusive with `--max-calls`) produces an approval-bound null aggregate cap, not a large fake integer. Finite runs retain their current 1..48 limits and final-review reservation. Uncapped-call runs retain the same final-review transition and independent gates, but no numeric reservation can block issuance. Reports explicitly distinguish null/unlimited call count from zero and unknown cost. Per-phase/request and bounded repair/recovery policies are unchanged.

**Tech Stack:** Python, argparse, pytest, existing deterministic repair authority, isolated Responses HTTP worker, Docker runner.

**Spec:** This document records the narrow implementation ruling under the user's explicit one-run request and standing instruction to execute without further questions; no broader unlimited autonomous retry service is authorized.

## Global constraints

- Product work only in `/Users/bred/PycharmProjects/project/reviewflow`, branch `feat/project-scaffold`; no commit/push.
- Actual target `/Users/bred/PycharmProjects/bigdata-welcome-diag`, branch `test`, HEAD `7a9539b5380a129a935b44d2eb1eb76c2eccd1e9`; preserve index, dirty `.gitignore` and all existing untracked files.
- Upload/edit only `mcm_analysis/{events,protocol,relay,task,throttle,transport}.py`; no manual target-source modifications.
- Actual target tests only in Docker, fixed existing immutable image, exec-scratch already approved, total container test deadline180 seconds; all discoverable allowed committed HEAD tests.
- New output directory, new preview and digest. Fresh HEAD candidate for this full-chain run, no seed import or reused authority. Preserve prior observed run16–18 accounting (50 attempts,2 unknown usage records,8 member attempts) separately; no claim those requests were free or cancelled.
- Remove aggregate call count only. Keep per-phase8 calls, per-request60-second deadline/body/context/output limits, one-hour run authority lifetime, member/refresh/recovery bounds, no automatic transport retry and unknown-usage stop. These remain possible stopping causes and must be reported honestly.
- No simulated result substituted for actual target acceptance. No bypass of unresolved findings, failed tests, final review or dirty-work protections.
- No model request during implementation/tests. ONE paid actual acceptance after product verification and a fresh review; do not auto-relaunch on failure.

## Review focus

1. Absent flag/default still rejects None, bool,0,49 and malformed budgets; uncapped is explicit and approval-bound.
2. Default/uncapped previews cannot reuse each other's approval; tests-only rejects the incompatible uncapped flag.
3. Calls beyond48 pass through actual parent authorization in uncapped mode, but per-phase8 and failed-transport stopping remain enforced.
4. Final review is still required; an uncapped budget does not allow early release/writeback or weaken application authority.
5. JSON and Markdown do not claim zero requests/cost or numeric seed-history safety for uncapped results. Existing seed loader remains conservative.

## Tasks

### Task1: Explicit aggregate budget policy
- [x] Read existing budget/CLI/test interfaces and targeted coupling inventory.
- [x] Add failing tests for explicit flag, valid/invalid modes, approval separation, >48 real parent-issued calls, normal cap and unchanged final/transport gates.
- [x] Implement minimal CLI/preview/authority/report changes; preserve bounded defaults.
- [x] Focused tests and Ruff; independent targeted review (no verified findings); full pytest and Ruff.

### Task2: Actual one-shot end-to-end acceptance
- [x] Fresh external run directory and driver; verify expected target branch/HEAD/workspace, image and tested product hash manifest.
- [x] Preserve prior execution accounting; bind current user request, uncapped preview, precise unchanged safety policies and single-execution intent.
- [x] Run actual CLI preview, inspect, approve same digest once. No seed, no automatic retry, no original edits outside product final gates.
- [x] Inspect actual report/exit code, all target protection hashes, phases reached, final tests/reviews/application, safe request diagnostics and actual accounting. Driver exit0 is not acceptance success.
- [x] Update this record and handoff with verified outcome: blocked on HTTP408 during initial verification; full chain NOT accepted.

## Execution ledger

- Ruling: use a fresh HEAD candidate rather than run16 seed — this exercises the whole current pipeline without inheriting prior candidate progress or continuation rules; cost is repeating initial work and potentially more paid requests.
- Ruling: remove only aggregate model-call ceiling — the user identified that ceiling as the experiment variable; per-request safety, finite repair policy and authority expiry remain unchanged and are disclosed, so this is not a guarantee of unlimited repair rounds or successful convergence.
- Implementation added: explicit mutually-exclusive CLI flag, approval-bound `no_call_limit=true`/`max_calls=null`, strict mode validation, optional-aware effective-budget property shared by all recovery prechecks, numeric-free final-reservation transition and explicit report label. Tests-only and seed import reject this mode; ordinary seed/default policies unchanged.
- RED: new32-case suite had21 failures/11 passes before implementation (missing API/CLI and unchecked inconsistent policy). Initial GREEN attempt31 passed/1 failed: the test incorrectly assumed a pre-send phase-limit rejection permanently closed the parent. Inspected DevelopmentTransport.call and parent authorization: only attempted failed transport/unknown usage closes it; corrected the test to verify ninth call denied, new authorized phase allowed. No production behavior weakened for that assertion.
- Independent read-only review completed with no verified issues, covering optional aggregate-limit consumers, finite mode, approval binding and final gates. Reviewer ran no tests/network and did not inspect target/private configuration.
- Target preflight verified test branch, expected HEAD and existing dirty/untracked status; immutable local Docker image exists. Prepared ONE fresh-HEAD acceptance driver at `/Users/bred/PycharmProjects/project/reviewflow-real-uncapped-20261109-480786tq/run_product.py`; syntax checked only, not executed. Prior16–18 accounting retained:50 requests,8 member attempts,2 unknown usage records, cost unknown.
- Focused regression completed exit0:192 passed in520.00s; Ruff src/tests All checks passed. Read terminal logs `/tmp/reviewflow-uncapped-focused.log` and `/tmp/reviewflow-uncapped-focused-lint.log`.
- Full suite completed exit0:1201 passed in952.04s; Ruff src/tests All checks passed. Read terminal logs `/tmp/reviewflow-uncapped-full.log` and `/tmp/reviewflow-uncapped-full-lint.log`. Rechecked frozen86 source/profile files and67 test files unchanged. Saved verification-gates.json in the acceptance directory.
- Launched the one-shot actual acceptance driver after all gates: `/Users/bred/PycharmProjects/project/reviewflow-real-uncapped-20261109-480786tq/run_product.py`, log `/tmp/reviewflow-real-uncapped-driver.log`. Driver checked unchanged original state and two fresh CLI previews (only approved aggregate-budget fields differ), then executed exactly once.

## Actual run19 result — NOT accepted

- Driver exit0, actual CLI exit1, status blocked, failure_stage initial_review. `preview.max_calls=null`, `no_call_limit=true`, no seed, final_review_calls_held0. This was not a48-call/32-effective-call stop.
- Baseline Docker test executed with complete safe diagnostics:22 failing locations, same baseline content digest as prior original HEAD. This count is neither independent bugs nor total failed pytest instances.
- First scope Correctness and Security reviewer tasks completed. First finding independently accepted; next Verifier request failed with `http_status_408`. Remaining unsent verifier outcomes were backend_error after transport closed, not additional failed HTTP requests.
- Six actual request attempts: first five complete with known usage; sixth HTTP408 with unknown usage. Worker milestones: started0ms, connection_established3ms, request_sent3ms, response_headers44835ms. An HTTP peer returned408 before the local60-second worker deadline; this is NOT the same as local worker_timeout. No raw error body was retained; status alone cannot locate the gateway/provider cause or establish whether processing/billing occurred.
- No patch attempts/groups, no refresh, no final tests, no final reviews, application null. Cannot claim the full chain is healthy or repaired: execution did not reach those gates.
- Verified before==after for test branch, expected HEAD, index, status, selected hashes, original .gitignore and protected dirty/untracked hashes;86 source/profile files still match tested freeze. No manual target-source edits, host target testing, commit or push.
- Observed runs16–19 total56 request attempts,8 member attempts,3 unknown-usage records, total amount unknown. `execution-accounting.json` preserves the prior interrupted runs; this is not entire project history or provider billing.
- User-authorized ONE uncapped-call acceptance is consumed. No automatic run20, no new paid retry. Next investigation is remote408/transport compatibility and timeout evidence, not changing aggregate-call allowance or falsely declaring downstream gates passed.
- Artifacts: `report/report.json`, `verification.json`, `execution-accounting.json`, `before.json`, `after.json`, previews and manifests in the run19 directory above. Treat reports/candidates as private target materials.
