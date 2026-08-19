# ADR 0003: Derived PlanningIntent provenance

- Status: Accepted
- Date: 2026-08-20

## Context

Hyper needs an opt-in structured interpretation to select a planner without
changing the source of mission authority. Existing runtime users still consume
the legacy MissionSpec mode. Planner-native inputs, outputs, and verification
evidence are produced later and need a provenance-binding record.

## Decision

The raw MissionInput and operator Mission Intent remain authority. An opt-in
PlanningIntent is derived, non-authoritative, and provenance-preserving: it
must retain the source mission identity and authority reference and may contain
only interpretation and flexible planner-selection facts in `details`. It must
not contain planner-native assets or verification evidence.

Hyper tracks interpretation, planner choice, and validation with todo tooling.
Those todos are neither mission authority nor rationale. Hyper returns only its
configured structured contract and, where supported, only a concise public
rationale—not private reasoning. Legacy MissionSpec mode remains supported and
does not replace raw source authority.

Use MiniZinc for timed scheduling or optimization. An opt-in PlanningIntent
uses Fast Downward with PDDL, exactly `fast-downward`, for symbolic reachability;
`null` remains only for legacy SymbolicMissionSpec compatibility where needed.
Risk-weighted objectives require risk scores from mission inputs or an explicit
code-owned derivation, never an LLM invention.

This reopens and supersedes ADR 0002's planner-signature non-goal only for
future translator-owned, planner-native translation or signature evolution. It
does not change ADR 0002's belief provenance boundary and does not make
PlanningIntent or MissionSpec the source authority.

## Future PlanningRecord

A future formal PlanningRecord will bind the MissionInput hash/reference; the
accepted PlanningIntent hash for an opt-in flow; Planner Choice; concise public
rationale; translator identity/version; generated planner asset
references/hashes; solver evidence references/hashes; code-owned verification
checks and outcome; and the NormalizedPlan reference/hash. Assets and evidence
are PlanningRecord outputs, not PlanningIntent `details`.

This ADR specifies the future provenance expectation only. It introduces no
Python contract in this issue.

## Consequences

- Consumers can opt into PlanningIntent while existing MissionSpec runtime
  integrations remain compatible.
- PlanningRecord, rather than PlanningIntent, is the provenance boundary for
  planner assets, solver evidence, verification, and NormalizedPlan output.
- Future planner-native translation may evolve without changing mission
  authority or the public planning contract.
