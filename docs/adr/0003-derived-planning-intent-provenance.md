# ADR 0003: Derived PlanningIntent provenance

- Status: Accepted
- Date: 2026-08-20

## Context

Hyper needs a structured interpretation to select a planner without changing
the source of mission authority. Every Mission Run now uses planner-native
inputs and provenance-only Normalized Plans; the pre-planner mission
specification path has been retired.

## Decision

The raw MissionInput and operator Mission Intent remain authority.
PlanningIntent is derived, non-authoritative, and provenance-preserving: it
retains source mission identity and authority and contains only interpretation
and flexible planner-selection facts in `details`. Planner-native assets and
verification evidence are produced after planner choice.

Hyper tracks interpretation, planner choice, and validation with todo tooling.
Those todos are neither mission authority nor rationale. Hyper returns only its
PlanningIntent contract with a concise public rationale, never private
reasoning.

Use MiniZinc for timed scheduling or optimization. PlanningIntent uses Fast
Downward with PDDL, exactly `fast-downward`, for symbolic reachability.
Risk-weighted objectives require risk scores from mission inputs or an explicit
code-owned derivation, never an LLM invention.

This reopens and supersedes ADR 0002's planner-signature non-goal only for
translator-owned, planner-native translation and signature evolution. It
does not change ADR 0002's belief provenance boundary and does not make
PlanningIntent the source authority.

## Plan provenance

Every NormalizedPlan carries PlanProvenance binding mission identity and
authority, PlanningIntent, Planner Choice, the authorized Operational Scene
Graph, generated planner assets, and solver evidence through verifiable
references and hashes.

The application creates no intermediate authoritative planning schema before
external planning. Translators consume planner-native generated assets and
produce a NormalizedPlan only after code-owned verification succeeds.

## Consequences

- PlanningIntent is the sole Hyper planning interpretation contract.
- PlanProvenance is the boundary for planner assets, solver evidence,
  verification, and NormalizedPlan output.
- Planner-native translation may evolve without changing raw mission authority.
