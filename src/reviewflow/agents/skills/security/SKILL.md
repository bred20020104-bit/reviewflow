# Security analysis skill

## Trust boundaries
Map external inputs and attacker capabilities to privileged operations. Trace source → transformations/validation → sensitive sink. Identify where trust changes: user to service, tenant to tenant, repository content to controller, or local data to remote service. Do not infer internet exposure merely from a function name.

## Controls to examine
Check authorization at the actual object/action boundary, tenant isolation, traversal and symlink handling, injection into interpreters or queries, unsafe deserialization, outbound destination validation, credential exposure and unbounded work caused by untrusted input when applicable. Distinguish authentication from authorization. Check canonicalization order and whether validation applies to the value ultimately consumed.

## Counterchecks
Look for effective parameterization, escaping, allowlists, permissions, size limits and isolation. A dangerous primitive alone is not a vulnerability. State deployment or attacker prerequisites explicitly and avoid inflating impact beyond the shown data flow.

## Finding threshold
Require evidence for attacker influence, a reachable operation, a missing/ineffective control and concrete confidentiality/integrity/availability impact. Cite exact lines without reproducing secret values. Do not execute an exploit or claim exploitation. Missing material context warrants uncertainty, not a fabricated vulnerability.
