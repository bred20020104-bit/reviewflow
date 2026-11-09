# Acceptance progression — approved four-step execution

User approved on 2026-11-08: “好，那你以此执行这四步吧”. This specifically approves the proposed explicit executable scratch sandbox profile, bounded verification/budget improvements, fresh actual CLI acceptance, and evidence-based stopping rules. Earlier constraints remain except the explicitly selected scratch execution permission. No commit/push or manual target edits.

## Step 1 — explicit sandbox profile

- Keep strict as default. Add `repair-run --test-sandbox {strict,exec-scratch}`.
- `strict`: unchanged `/tmp:rw,noexec,nosuid,size=32m`, readonly source/root, network none, nonroot, resource/capability restrictions.
- `exec-scratch`: additionally mount container-only `/scratch:rw,exec,nosuid,nodev,size=32m,mode=1777`; set TMPDIR=/scratch. Retain noexec /tmp and all other restrictions. This deliberately enlarges in-container executable scratch permission, not an identical security policy.
- Bind full fixed sandbox policy to runner intent and repair preview/digest. Profile changes invalidate previous grants, including tests-only. Unknown profiles reject before Docker/side effects. No arbitrary mount paths/options from user/model.
- Tests RED→GREEN: default flags; exec profile flags/TMPDIR/no host binds; invalid profile; stale approval across profiles; CLI preview and zero-model execution; cleanup after timeout.
- Verify focused/full regression and independent narrow safety review. Then actual CLI tests-only on target test branch with fresh preview/digest/output, no model and no writeback. Compare safe failure locations, not raw logs or test counts. Environmental progress is not whole-repository acceptance.

## Step 2 — bounded verification and budget

Inspect actual run10 traces and current refresh interfaces before implementing. Goal: bounded batches of independent verifier verdicts (complete ID coverage and per-item evidence required), and final-review budget reservation. Keep output cap2048, parent48, pending/unresolved gates and both-role final selected-source coverage. Do not skip by title, normalize bad hashes, reuse failed approval or infer success from static resolution. Detailed implementation/tests to be recorded after inspecting interfaces and step1 result; no promise that48 calls can repair all defects.

## Step 3 — actual repair acceptance

After local gates, fresh real CLI under approved exec-scratch profile, unchanged six editable source files on test branch. Full committed repository tests, final review, origin protection and owned writeback required. Preserve all original dirty/untracked work; no host target test execution, commit or push.

## Step 4 — evidence-based continuation

New paid run only following diagnosed change or a concrete new experiment. Stop with a specific conflict if necessary source scope, external service behavior or fixed budget prevents safe success. No manufactured passing tests, skipped unresolved, or silent relaxation. Record facts and artifacts in 2026-11-07-real-acceptance-progress.md.

## Status

Step1 complete:15 RED→GREEN cases,112 focused and1021 full tests passed; Ruff passed; independent static safety review found no actionable issue. Zero-model actual CLI completed in reviewflow-real-exec-scratch-tests-20261108-fnhttx9b: tests_failed,22 complete failure locations,0 calls/no writeback/all origin protections unchanged. Four known deployment/migration failure locations no longer reported. Steps2–4 in progress; real repair not accepted.

### Step2 implementation decisions / TDD tasks

- Ruling: batch only post-patch verifier requests, in groups of at most2 within one task/role/current snapshot. Singletons and one-time evidence corrections retain existing single-verdict protocol. Two-item schema uses independently identified verdicts, at most3 references and200-character rationale each to fit the unchanged2048 output cap; insufficient evidence must remain unresolved. No desired reviewer verdict supplied. Validate complete unique ID coverage and scope across all returned items before any correction/dependency call; missing/duplicate/foreign results stop. Preserve current evidence checks, per-finding correction and dependency recovery. Cost if wrong: conservative blocking/less savings, never acceptance by omission.
- RED→GREEN integration tests: two pending claims share one request; reordered results mapped by ID; missing/duplicate/foreign/empty results stop; cross-item scope dominates metadata correction; bad hash gets only one individual correction; unresolved still blocks or enters existing dependency retrieval; unknown usage blocks. Extend REFRESH_POLICY version and bind schema-size policy in fresh approval.
- Ruling: reserve8 calls per semantic batch after successful initial review and before any patch, enforced on every subsequent model authorization (including refresh/dependencies). Release only on controller transition after final full tests pass. Initial review cannot promise enough remaining budget; if reserve plus a patch cannot fit, stop before patching. Reservation protects final normal8-call review phases, not unlimited recovery. Total48 unchanged; no early final transition with pending issues. Cost if wrong: earlier safe stop, not a claim that48 calls suffice.
- RED→GREEN authority/session tests: preview binding/tamper rejection; repair requests cannot consume reserve; final transition restores only reserved capacity; unknown usage remains blocked; small budget stops before patch; full selected final reviews still required. Existing low-budget fixtures must reflect the new earlier safe stop.
- Run focused/full suite and Ruff; independent review of these changes before fresh actual CLI acceptance. No commit/push.

### Step2 verification ledger

-10 batch cases RED after correcting a synthetic fixture that exceeded the existing2-findings-per-task reviewer budget (test-only change: new finding arrives during refresh). Then batching integration/recovery/dependency suite66 passed/1 failed: legacy cycle fixture did not implement the new multi-result wire protocol. Updated fixture to return per-ID batch verdicts; no production bypass.
-8 reserve cases RED (6 main +2 failed-final/multi-batch). Combined refresh/budget/live-repair suite74 passed/2 failed; both old low-cap tests stopped before patch at the new reservation gate instead of reaching refresh. Increased fixture caps by exactly8 held calls, preserving original7/9 repair-call exhaustion and assertions. Both targeted regressions then passed6.16sec.
- Independent static review a6d90b4685c62974c confirmed those same two fixture issues, no other verified actionable finding; tests not independently rerun. No production fix required for review. Ruff initial import-spacing issue corrected; all src/tests Ruff passed. Driver syntax compile passed, not executed.
- Full pytest/Ruff task btdk3f1wk completed:1039 passed590.86sec; Ruff passed. Logs /tmp/reviewflow-batched-reserve-full{,-lint}.log read and verified.
- Step3 actual driver launched as task bgknlpggj after full gates passed: /Users/bred/PycharmProjects/project/reviewflow-real-batched-reserve-20261108-461v8ru9/run_product.py. It asserts fresh v3 refresh + final-review reserve policy + exact exec-scratch profile,48 calls/test branch/HEAD, all existing dirty/untracked file content hashes, no host tests/manual edits/commit/push. No product source changes during frozen run. Completed: actual CLI1/blocked at finding_refresh,22 calls/complete usage/16 held, one provisional patch, no final review/application. Baseline22 candidate21 complete failure locations; original state and protected hashes unchanged. Dependency retrieval hit32768 context cap, not call budget. See main ledger for facts.

### Step4 diagnosed context-budget waste

- Observed dependency packets repeated2589 bytes through overlapping evidence excerpts/full-file reads. Failed read ranges were not retained, so the historical failure is not proven fixable by deduplication alone. Six approved files total44060 bytes; do NOT preload or increase32768.
-6 RED tests reproduce overlap-driven false exhaustion, contained/bidirectional/gapped interval duplicates, and missing bounded oversize-read diagnostics. Implemented dependency policyv2: append only not-yet-supplied exact current lines, retain full validation even for duplicate reads, atomically reject a genuinely oversized read. Every prompt exposes current remaining byte budget and approved file byte counts. On rejection save only evidence coordinates/hash and buffered/additional/limit bytes.
-36 focused dependency tests passed128.70sec, Ruff passed. Narrow follow-up independent static review found no verified safety/correctness issue, did not rerun tests. Full suite task bc51hm980 completed:1045 passed620.15sec; Ruff passed. Logs /tmp/reviewflow-context-budget-full{,-lint}.log read.
- Fresh run12 task b1905yt7e completed: /Users/bred/PycharmProjects/project/reviewflow-real-context-budget-20261108-tcdjads8. Driver exit0, actual CLI1/blocked at finding_refresh.32 calls/complete usage,16 held,2 provisional patches, no final review/application. Baseline22 locations, candidate21 then21, all complete diagnostics. All target state/protected hashes and product hashes unchanged.
- One dependency recovery completed in3 calls,4 approved source paths,0 duplicate supplied lines. No context exhaustion this run; this does not prove the different run11 security question would have resolved identically. The later current-snapshot dependency recheck was denied at0 requests because32 repair calls were already spent.
- Verified accounting: initial reviews8+6=14 calls;2 patch calls; refresh10+6=16 calls (including successful dependency phase). Total32; final reservation16; parent48 unchanged. No failed transport, unknown usage or metadata-correction calls.

### Evidence-based stopping decision

The approved fixed-budget experiment did not achieve target acceptance. With this run's findings and current one-patch/affected-batch-refresh schedule, repair allowance is exhausted while tests and pending verifications remain. Another identical paid run is not a diagnosed remedy. Stop paid retries here; do not spend final reserve, skip pending findings, increase48, manually patch the target, or claim completion. Future work should redesign patch scheduling to validate a bounded group of independent edits against one current snapshot before one affected-batch refresh, with explicit per-finding ownership/conflict/rollback tests; this is NOT implemented or assumed safe. No blanket assertion that success is mathematically impossible in48 calls.

Deferred diagnostic minor: dependency checks still compare against total max_calls before start_phase; at the held-budget boundary the report says dependency_authorization_stopped / refresh_authorization_stopped instead of a dedicated reserve-budget code. Actual authority enforcement is correct;32 calls/no extra send verified. This labeling limitation does not authorize more calls or change acceptance.

Current delivered product verification:1045 tests/Ruff pass. Actual target repair:NOT ACCEPTED. No active background run, target branch test unchanged, original dirty/untracked work preserved, no commit/push.
