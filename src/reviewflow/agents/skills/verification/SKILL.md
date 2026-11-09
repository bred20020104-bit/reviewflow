# Independent verification skill

## Reconstruct
Restate the candidate as explicit premises: input/state, reachable path, faulty operation and consequence. Inspect cited lines and necessary nearby logic independently; a reviewer narrative is not evidence.

## Counterevidence
Actively seek guards, caller validation, alternate control flow, normalization, authorization, exception handling or lifecycle invariants that prevent the alleged failure. Challenge attacker access and concurrency assumptions. Use scoped read/search for material unknowns within the remaining budget.

## Decide
- accepted: the concrete failure follows from supplied evidence and no discovered guard defeats it; explain why.
- rejected: supplied evidence contradicts a necessary premise or demonstrates the behavior is intentional under the visible contract; cite that counterevidence.
- unresolved: a material premise cannot be established within available scope/budget; name the missing evidence.

Keep the original finding_id. Never turn absence of counterevidence into proof, reviewer agreement into verification, or static reasoning into a claim of executed tests. Do not propose unrelated findings or changes.
