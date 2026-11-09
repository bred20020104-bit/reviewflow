# Verifier evidence diagnostics and bounded recovery

Approved scope: implement the diagnosis/recovery/offline-verification proposal accepted by the user with “修吧”. No paid API run, target edits, commits, pushes, or discarded evidence.

## Design

1. Introduce fixed evidence-validation error codes (finding identity, path scope, snapshot, hash, line range, source changes). Keep CollectionError compatibility; never serialize arbitrary exception text or invalid model responses. Unknown failures remain closed.
2. Preserve the original initial/final review reports. Only known recoverable verifier-result errors (finding ID, snapshot/hash, legal-range mismatch) may enter one correction attempt per failed finding per review. Scope violation, missing supplied context, changed source, transport errors, malformed output and unknown errors do not automatically retry.
3. Use a separate parent-authorized, single-request Verifier phase, so a full eight-call initial review can recover without exceeding the existing eight-call per-transport ceiling. The parent total remains at most48; exhausted/blocked/expired authority means no send. Bind this versioned policy into preview/approval; tests-only never invokes it.
4. Before sending, validate the original finding, current candidate snapshot, successful outcomes and both-role coverage. Provide full selected batch code (existing32KiB limit) with trusted evidence metadata and fixed correction reason. Do not repair the model result locally. Strictly validate the fresh result; failure or unresolved stops. Keep original errors plus separate recovery/effective-review audit records. Final tests and both reviewers' final gate remain mandatory.

## Tasks and verification

- [x] RED→GREEN: context diagnostics and safe workflow propagation; unchanged evidence rejection semantics.
- [x] RED→GREEN: original errors retained, valid independent correction permits normal product Patch/test/final review/writeback on owned synthetic fixtures only; rejected/unresolved/second-invalid recovery cases; out-of-scope and source changes never recover; full eight-call first phase and parent budget/unknown usage boundaries; policy tampering denied before sends.
- [x] Full pytest + Ruff, actual installed CLI + real Docker + local synthetic HTTP with an injected invalid verifier hash then independent correction. This is wiring acceptance, not actual target/model accuracy.
- [x] One narrow independent review; one RED→GREEN fix pass if needed. Update backend documentation and an absolute-path Chinese result report. Preserve all work and evidence.

## Rulings

- Existing failed target run does not retain the invalid response, so do not claim its exact wrong field has been reconstructed. Tests cover representative error classes, not a fabricated replay.
- Recovery uses one additional phase rather than relaxing the eight-call transport cap or deleting findings to free capacity. Costs one parent-budget call per recovered finding; if the cap is exhausted, stop.
- Keep the original failed reports rather than clearing their audit history. Effective reports are separately identified and must pass the same strict gate.
- No source packaging/publication claim is made until a newly built artifact is verified; previous ZIP/wheel remain the prior version.
