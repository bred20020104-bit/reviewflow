# Correctness analysis skill

## State transitions
Trace initialization, normal updates, completion, cancellation and failure cleanup. Check whether each transition preserves invariants and whether retries, duplicate events or partial failures lose or repeat work. For concurrency claims, identify the actual shared state and possible ordering; do not assume parallel execution without evidence.

## Contracts and boundaries
Trace concrete inputs through branches and returned values. Check empty/singleton collections, zero and negative values, inclusive/exclusive bounds, units, rounding, ordering, missing values and mutable defaults where relevant. Compare caller expectations with callee behavior only when those contracts are visible.

## Failure handling
Check exception scope, resource ownership, cleanup and error propagation. Distinguish intentional fallback from silent corruption. Look for guards or later normalization that invalidate an apparent defect.

## Finding threshold
For each candidate, state the smallest concrete triggering input/state, the faulty operation, expected versus actual behavior grounded in source, and the observable consequence. Cite the narrowest useful lines. Do not report style, speculative requirements or an absent test as a functional defect. Request missing context or withhold the claim when a necessary premise cannot be established.
