# Preserve safe numeric HTTP failure diagnostics

## Evidence and limited objective

Actual run18: /Users/bred/PycharmProjects/project/reviewflow-real-renewed-20261109-ba4ZuVnn/report/report.json. CLI1 blocked during initial independent verification, fifth request. Four prior requests completed. Failed request reached response_headers at16851ms and returned http_client_error with usage unknown; this differs from run17's60s pre-header worker_timeout. No target patches/writeback; HEAD baseline22 diagnostic locations, known run16 seed16. All original protections checked unchanged by driver. Actual observed runs16+17+18 total50 request attempts,2 unknown usage records,8 member attempts, amount unknown. Source-seed ancestry report alone excludes run17, so use execution-accounting.json for observed execution totals.

Root cause of diagnostic loss: responses_worker._http_error collapsed every400..499 except401/403/429 into http_client_error, and no numeric code was retained. This proves missing diagnostic precision, NOT the cause of the provider/proxy rejecting the real request. Old raw status/body were not retained; do not invent a400/413/422 diagnosis for run18. No new paid retry can reconstruct that old response.

## Scope and constraints

Preserve fixed numeric HTTP4xx metadata as a finite allowlisted diagnostic code; no provider messages, headers, request IDs, error body, credentials, hidden reasoning or raw exceptions. Keep existing401/403 auth and429 rate-limit categories. Generic legacy http_client_error remains accepted for historical compatibility. Preserve one-request/no-retry semantics, usage-unknown handling, parent closure and all48-call/60-second/2048-token limits. Adapter version advancesv9→v10 so old approval cannot silently authorize changed adapter semantics. No target tests on host, no manual target source edits, no commit/push, no run19.

## Implementation and verification

- diagnostics.py: fixed dictionary for integer400..499 except401/403/429, values http_status_NNN, admitted to worker/public allowlists. Range is code-defined, not interpolated arbitrary remote text.
- responses_worker.py: look up numeric status before returning fixed legacy fallback; continue rejecting before reading error response bodies. No new reads/retries.
- responses_adapter.py: responses-development-v10.
- Existing real subprocess/local HTTP-server test expected generic400 changed to exact400; added404/405/408/409/413/415/422/451/499 cases. RED:10 cases returned old generic code,8 unchanged cases passed. GREEN: all adapter84 tests passed42.42s.
- Added three actual CLI/local HTTP+worker synthetic repository cases400/413/422, mocking only Docker execution boundary: one request, status reaches JSON/Markdown, no raw body/key retention, usage incomplete, blocked/no final review/no original writeback.
- Focused combined adapter+CLI108 passed68.39s; Ruff src/tests passed. Logs /tmp/reviewflow-http-status-red.log, /tmp/reviewflow-http-status-green.log, /tmp/reviewflow-http-status-focused.log and /tmp/reviewflow-http-status-focused-lint.log.
- Full regression completed with exit0; terminal logs read:1169 passed in925.87s (0:15:25), Ruff src/tests All checks passed. Evidence: /tmp/reviewflow-http-status-full.log and /tmp/reviewflow-http-status-full-lint.log. This verifies the v10 product change; previous1157 full result predates it.

This is a diagnostic fix, not target acceptance or proof of the underlying HTTP error cause. No additional reviewer agent dispatched for this small fixed-vocabulary change. Product regression has finished; no paid background acceptance is active. Handoff summary: docs/handoff-2026-11-09.md.
