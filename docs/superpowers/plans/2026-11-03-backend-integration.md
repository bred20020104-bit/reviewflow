# Backend Integration Implementation Plan

> Execute inline with TDD and one final independent review. User ordered continuation without questions; file-based credentials are now preferred, and commit/push/frontend come last.

**Goal:** Provide a dedicated local credentials file and connect the offline-testable review backend into an inspectable, resumable CLI session; add bounded retrieval, Verifier supplementation, conservative duplicate grouping and sequential candidate composition.

**Architecture:** Keep the existing inventory/demo API compatible. A new session workflow reuses LangGraph and validated contracts, with a gateway joining request evidence, operation approval and budget reservation. Only a built-in simulated adapter is exposed until provider/API selection. Persisted sessions are untrusted reports, not executable state, credentials, grants or validation receipts. Reopening requires the same explicitly selected source and revalidation; resuming review uses a fresh process-local gate. Candidate composition is explicitly unverified and does not publish.

**Tech Stack:** Python>=3.11, existing Pydantic/LangGraph, pytest/Ruff, stdlib TOML/JSON/filesystem.

**Spec:** ../specs/2026-11-02-reviewflow-design.md and the user's 2026-11-03 direction to use a dedicated credentials file and defer commit/push/frontend.

## Global constraints
- Never print, package or send the real Key. Create only an ignored `secrets.local.toml` placeholder, owner-only permissions; config refers to its path. No target dotenv/config discovery. Existing env-name configuration remains backward compatible.
- No real API call or target execution without explicit approval. No vendor SDK guessed while provider/API unknown. Keep `--review` fail closed.
- No main-project Git commits/pushes, no frontend or publication feature development in this plan. Existing publication gates remain intact.
- Read source via ReviewContext, never import target Python. Tools are read/search only, bounded and provenance checked.
- Report first. A disk report cannot mint approval, execution evidence or commit readiness. No serialized credential or token.
- Each repair candidate needs exact human approval. Composed candidates are not validated repairs and cannot acquire a commit receipt from a disk session.

## Review focus
- Credential bytes never occur in request payloads, exception text, session JSON, archives or ordinary inventory; symlinks and permissive key files fail closed.
- Gateway binds the actual full request and actual evidence to the configured root/scope; authorization and budget precede the adapter.
- Retrieval cannot escape task/approved scope, exceed cumulative context/call limits or claim full-file coverage from one chunk; initial reviewers stay independent.
- Verifier gets at most one supplement; duplicate suggestions retain every original finding/provenance and cannot suppress verification.
- Session loading rejects snapshot/root mismatch and unexpected fields; no stale approval/receipt, target execution or automatic repair on resume.
- Failed sequential candidate application restores the previous candidate, not the original tree; all candidates stay unverified pending genuine validation.

## Tasks
- [x] 1. File credentials: optional exclusive api_key_file/api_key_env config; bounded private TOML loader returning redacted SecretStr; explicit credentials-check CLI; ignored local placeholders and inventory/archive filtering. RED tests for configuration exclusivity, changed cwd, unsafe permissions/symlinks/TODO/errors and secret exclusion.
- [x] 2. Gateway: ModelRequest serialization plus evidence/root/scope validation, explicit grant and token-bound callbacks into GuardedTransport. Built-in in-process simulated adapter with simulated usage labels; no live credential resolution. RED tests for actual request binding, denial before calls, scope/source tampering and cumulative budget.
- [x] 3. Enhanced LangGraph: bounded structured read/search turns, chunk coverage accounting, at most two reviewer retrieval turns/one Verifier supplement, Supervisor duplicate suggestions retaining originals. RED tests for long files, malicious tool requests, call/context limits, independent role input, duplicate provenance and unresolved results.
- [x] 4. Persisted CLI sessions and candidate composition: exclusive external session storage, source-bound report loading, explicit demo resume, report/finding export, sequential explicitly approved worktree candidates with checkpoints and rollback. No serialized permissions or publication. RED tests for changed source/session, fresh approvals, no automatic writes, chained candidates and unsafe rollback.
- [x] 5. Integration and delivery: actual launcher smoke, docs and updated gap matrix; install only declared tool build dependencies if obtainable, build/install from a source ZIP in a new environment without publishing. Docker availability preflight only; no install/pull/target execution. Full suite/Ruff and one final review; TDD fixes for important findings, then fresh local source package.

## Completion contract
Every new behavior first fails, then passes. Full suite after each task. Distinguish offline integration from real model/runtime acceptance. Existing session/CLI compatibility retained. No claim that simulated analysis is semantic review or that candidate composition is a validated repair chain.
