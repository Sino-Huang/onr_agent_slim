---
name: decision-cycle
description: Use on every Maneuver heartbeat to sequence live FSM, physical, belief, and communication tools from current evidence.
version: '1.1.1'
---

# Decision Cycle

## Inputs and Authority

- Read the supplied live FSM Status, current environment, active maneuver, verified planning provenance, and pending raw perception batch before acting.
- Treat the older Mission Snapshot as provenance only. Do not wait for it to refresh before using current live evidence.
- Copy events and recipients exactly from current candidates and the available-recipient registry. State and event names have no required pattern.
- A heartbeat may be periodic or triggered immediately by authoritative
  environment lifecycle feedback. Treat both identically and do not wait for a
  5-second boundary after navigation becomes completed, failed, or cancelled.

## Procedure

1. Review each candidate's transition, source-state, and target-state contexts against Mission time, position, active-maneuver lifecycle, environment facts, and pending perceptions.
2. Call `transition_fsm` when live evidence makes that candidate appropriate.
3. After a successful transition, use its returned live state for every remaining
   tool choice. Do not act on instructions that existed only in the source-state
   context. A transition and physical call may occur in the same heartbeat.
4. Call `ingest_perceptions` once when the pending event batch warrants Bayesian
   ingestion. The tool processes every pending event separately and becomes
   unavailable after success. Call communication tools when the current live
   state or environment evidence warrants them.
   Pass documented `navigation_adapter_parameters` through the matching named
   arguments of the navigate tool;
   preserve the continuous `deadline_time`, observation timing, source-event
   identity, expected count, coordinates, and seconds units exactly. The
   environment adapter derives speed from distance and remaining deadline time.
   Treat `hyper_outcomes` as correlated results from the previous heartbeat's
   queued communication, never as authority to edit a Statechart locally.
5. Finish with `ManeuverHeartbeatCompletion`; effects are already authoritative tool executions.

## Gotchas

- A prior decision or remembered observation may be stale even when its Mission ID matches.
- An exposed transition is a legal candidate, not an automatic transition. Maneuver Control owns semantic judgment; the transition tool checks current identity and performs the mutation.
- Once a transition succeeds, its source-state context is stale for the rest of
  the heartbeat; do not send a report or update a belief from that old context.
- A belief updated in this heartbeat never enters a Maneuver invocation; Context Coordination supplies it to later Hyper invocations.
