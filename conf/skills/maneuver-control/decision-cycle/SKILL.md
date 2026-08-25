---
name: decision-cycle
description: Use on every Maneuver heartbeat to reconcile Transition Intent, live FSM evidence, physical continuity, belief, and communication effects.
version: '2.0.0'
---

# Decision Cycle

## Authority

- The focused FSM context exposes the current state context, exact
  target/condition candidates, state-entry revision, and current Transition
  Intent. Future target-state operational context arrives only after transition.
- Current environment and active-action facts outrank planning provenance and
  remembered observations. State identifiers are exact opaque values.
- Candidate conditions express desired outcomes and evidence expectations.
  Expected counts are uncertain evidence rather than ground truth.

## Procedure

1. Create one heartbeat-local todo list for inspection, target selection,
   condition assessment, transition, physical continuity, other effects, and
   completion. Maintain it through the heartbeat.
2. Inspect current FSM, intent, environment, active action, pending perceptions,
   and Hyper outcomes. Retain a suitable selected target or call
   `set_transition_target` with one exact candidate. This tool changes intent and
   never FSM state.
3. Assess the selected condition. Use `satisfied_with_uncertainty` when judged
   acceptable despite missing or occluded evidence.
4. If satisfied, call `transition_fsm` with exact current/next state identity,
   assessment, evidence, and uncertainty. This tool consumes the intent and
   changes FSM state. Use its returned focused context for the rest of the
   heartbeat and select a next target when candidates remain.
5. Preserve a suitable nonterminal active action when the target is unchanged;
   submitting it again would replace it. When replacement is warranted, choose
   the physical action and parameters at runtime from current outcome facts and
   environment evidence.
6. Independently ingest the complete pending perception batch once when
   warranted and send evidence-driven communications. A declared
   `hyper_evaluation` is sent with its exact kind, reason, evaluation ID, and
   delivery policy. Unmarked queries, reports, and replans remain unrestricted.
7. Complete every todo and return `ManeuverHeartbeatCompletion`. Todo and skill
   calls are workflow aids, so todo-only heartbeats may return `no_change`.

## Live reconciliation

- Periodic, lifecycle-triggered, and `replan-activated:<revision>` heartbeats use
  the same cycle. Replan activation supplies the Hyper outcome at the same
  Mission time, before an environment tick.
- A stale intent is invalidated when its source state, Statechart revision,
  plan revision, or state-entry revision no longer matches live authority.
- A queued communication acknowledgement is transport evidence; the correlated
  Hyper decision arrives in `hyper_outcomes`.
