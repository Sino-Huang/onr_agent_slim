# ADR 0002: Bayesian belief context provenance

- Status: Accepted
- Date: 2026-08-19

## Context

Mission perception is uncertain both in the observed binary risk and in the
association between an observation and an entity. Logical rules may also forbid
joint assignments. Belief state must affect planning without becoming an
unverified payload in Context Coordination or creating a direct path from a
belief component to an agent.

## Decision

Bayesian beliefs are generic binary variables identified by
`(entity_id, risk_type)`. A standard-library Sequential Importance Resampling
(SIR) particle filter marginalizes candidate entity-association weights and
enforces typed forbidden combinations. No entity name or risk category is built
into the model.

The Bayesian belief application service consumes typed `risk.observed` and
`belief.constraints` events with monotonically increasing input revisions. For
an accepted observation it writes a new immutable generation containing the
artifact, restart checkpoint, and at most one pending-output template. A single
atomically replaced committed-state pointer binds their hashes and is the only
authority for current state. A crash before pointer replacement leaves an
ignored partial generation; a crash after replacement leaves a complete,
recoverable generation. History is bounded only after the pointer is durable.

The pending template is sequence-free and reference-only. Publication allocates
the then-current topic sequence; a competing Context Coordination producer causes
the service to rebase and retry without changing event identity or pending state.
Pending state is cleared by another committed generation only after publication
is visible. Constraint-only inputs likewise commit constraints, particles, RNG,
and input cursor without manufacturing a belief artifact revision.

`belief.updated` contains no marginals or other belief payload. It contains the
canonical source name `bayesian_belief_snapshot`, belief revision, health and
freshness, a SHA-256 digest, and a storage-root-relative reference to the
physical immutable content-addressed generation artifact bound to that digest.
Shared-volume consumers can open that file directly. The mutable committed-state
pointer remains a private store implementation detail and is never published.
Context Coordination remains the sole aggregator. It turns a
changed durable belief fact or health state into a new immutable,
monotonically-versioned MissionSnapshot and otherwise publishes nothing. The
snapshot retains only source revision, reference, hash, health, and freshness.
Mission IDs use one percent-encoded path component in both storage and manifest
references. Runtime resolution parses the physical artifact reference, confines
it to the configured storage root, rejects symlinks, and validates committed
mission, generation, revision, and hash binding before returning the snapshot.
Every read and write uses the same confined path builder. Existing symlink
components and paths resolving outside that root are rejected before directory,
temporary-file, generation, or committed-state creation.

Hyper receives a typed `BayesianBeliefSnapshot` as a separate optional heartbeat
context input. Before accepting it, Hyper requires the authoritative
MissionSnapshot to identify the same mission, belief revision, canonical
reference, and content hash and to report healthy, fresh provenance. Artifact
loading and hash verification happen through the durable store before this
call. A missing or mismatched typed artifact is rejected. A changed belief
source revision follows the existing source-revision replan path.

## Consequences

- Artifact durability precedes event visibility.
- Context Coordination remains reference-only and the only mission-context
  aggregation authority.
- Hyper cannot consume a belief artifact that is absent from, or inconsistent
  with, its authoritative MissionSnapshot.
- Checkpoint history remains bounded while preserving deterministic restart.
- Resume configuration is checkpoint-authoritative. Explicit particle-count,
  transition, key, or constraint settings must match; `None` means reuse the
  checkpoint, and a new seed cannot be applied to resumed RNG state.
- Revisions and seeded state transitions replay deterministically. The default
  timestamp uses wall-clock UTC, so byte-identical artifact hashes require an
  injected deterministic clock; with that clock, canonical hashes are stable.

## Non-goals

- Belief payloads are not embedded in MissionSnapshot manifests or
  `belief.updated` events.
- PDDL, MiniZinc, and planner method signatures are not changed.
- The belief manager does not call Context Coordination, Hyper, or any other
  agent directly.
