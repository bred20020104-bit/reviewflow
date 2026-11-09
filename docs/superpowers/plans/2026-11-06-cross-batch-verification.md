# Cross-batch dependency verification

Goal: permit an independently unresolved finding to obtain approved cross-batch source excerpts, without treating unresolved as accepted or changing reviewer/patch scope.

Scope: continue the requested local repair without further questions. No real target/model/Docker rerun, commits or pushes. Existing Python/Pydantic/gateway/authority interfaces; no new dependencies.

Design decisions:
- Preserve original reports, batch coverage, task paths and finding hashes. A separately scoped verification outcome records its approved inventory scope; scope changes alter snapshot IDs, so original evidence is validated before projecting unchanged bytes into the verification inventory. Never re-anchor changed source.
- Resolve semantic unresolved verdicts through a separate independent verifier phase, not the malformed-evidence correction mechanism. Supply finding excerpts plus approved path/line-count metadata; permit bounded explicit reads only. No rationale parsing to authorize paths.
- Maximum 3 requests per dependency attempt, 3 reads per turn, 32768 cumulative source bytes, 8 attempts per run; all consume the same parent call budget and transport limits. One attempt per originating batch/task/finding/current snapshot/stage. Policy is approval-bound.
- Retain original and effective reports plus attempt scope, snapshots, supplied evidence, result, request count and safe stop code. Decisive results require an actually supplied cross-batch dependency. Invalid, still unknown, unauthorized, over-budget or externally mutated inputs block.
- Initial and final reviews use the same recovery gate. Previously dependency-verified queued findings must be independently checked again after snapshot changes. Post-patch independent unresolved verification can use the same mechanism; reviewer unresolved assessments still block rather than invent fresh evidence.
- Final tests, both-role full selected coverage and writeback checks remain unchanged.

Tasks:
- [x] Reproduce approved dependency in a later byte-sized batch with a synthetic controller test; observe failure before product changes.
- [x] Implement bounded dependency verification, separately scoped provenance validation, approval binding and orchestration/audit integration.
- [x] Cover rejection, unresolved, unapproved/unsupplied/stale evidence, context/call limits, repeated attempts, mutation, final gates and post-patch invalidation.
- [x] Run targeted and full suites/lint, one independent review, fix verified findings and document limitations.

Progress:
- Root cause confirmed from real retest report and controller: valid unresolved verdict has no report.errors; per-batch validation stops before approved dependencies in later batches become available. Existing interactive verifier scope is the batch inventory, not parent-approved selection.
- Ruling: work inline on the existing unborn scaffold, with explicit changed-file review rather than git ranges; preserve all user files and do not create a commit. Cost: no commit-based diff baseline.
- Initial synthetic RED: six controller regressions failed, including later-batch approved dependency stopping before patch generation. After implementation the five semantic/scope cases passed; corrected the budget fixture from cap=4 (initial review incomplete) to cap=6 so exhaustion occurs inside the dependency phase; it then passed.
- Refresh integration RED: independent refreshed verifier unresolved could not fetch later-batch dependency; implemented the same bounded helper while retaining explicit reviewer dispositions and independent agreement.
- Additional RED: mixed malformed evidence plus semantic unknown, and corrected evidence still unknown, both stopped before dependency recovery. Separated structural provenance preflight from decisive-result gate, with strict patch/writeback validation still mandatory.
- Additional RED: a previously accepted unchanged finding became unknown after an earlier patch; current-snapshot recheck now uses the same dependency path. Prior dependency-backed outcomes are also independently rechecked after snapshot changes.
- Safe Markdown summary RED then GREEN; arbitrary diagnostic strings remain excluded.
- Earlier focused checkpoint: 58 dependency/live-repair/refresh/budget tests passed in 212.52s. Further boundary and interoperability cases added afterward; full suite and lint running, independent read-only review pending.
- Ruling: only explicit line reads, not arbitrary search/path discovery; read only parent-approved source and retain read evidence without additional source text in audit. Cost: deep dependency chains may remain unresolved within 3 requests and must block rather than expand budgets.
- Ruling: dependency recovery does not override a reviewer unresolved refresh assessment; fresh current finding evidence still has to exist first. Cost: some post-patch uncertainty remains blocking by design.
- Independent review confirmed two P2 availability defects: batch-local finding IDs collided after full-scope snapshot projection, and adjacent supplied excerpts could not support a spanning citation. Three regression cases RED; fixed attempt identity with originating scope and gap-free interval coverage. Focused five cases GREEN, including actual same-scope duplicate denial and citation gap denial.
- First full suite hung mid Responses HTTP tests and was stopped; no success claimed. Isolated Responses tests: 62 passed in 28.59s, hang not reproduced. Full rerun with faulthandler: 903 passed in 538.02s; Ruff passed. Logs /tmp/reviewflow-cross-batch-final.log and /tmp/reviewflow-cross-batch-final-lint.log.
- New user instruction on 2026-11-07 authorizes ongoing real-target repair/test cycles without further confirmations on the test branch. Further work and actual acceptance results tracked in 2026-11-07-real-acceptance-progress.md; this local fix is not itself proof of real repair acceptance.
