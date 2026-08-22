# Revision and reference identity for planning artifacts

Status: accepted in part; its Normalized Plan portions are superseded by ADR 0009. It supersedes the digest-bound planning identity in ADR 0003 and the provenance shape retained by ADR 0006.

Planning and execution artifacts are correlated by Mission ID, revision, event ID, and operational references instead of content digests. Planning Intent, planner decisions, Normalized Plans, transport, Mission Snapshots, Statecharts, and handoff therefore carry only the identity needed at runtime, while submitted planner bytes are cached and checked directly and Bayesian belief storage retains its own hash-addressed persistence contract. This keeps independent MiniZinc, solution, and VAL validation intact without propagating artifact fingerprints through every boundary.
