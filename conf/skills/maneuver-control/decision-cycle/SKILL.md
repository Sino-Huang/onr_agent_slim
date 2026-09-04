---
name: decision-cycle
description: Use on every Maneuver heartbeat to reconcile Transition Intent, live FSM evidence, physical continuity, belief, and communication effects.
version: '2.1.1'
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

1. Create one heartbeat-local todo list for inspection, current-intent
   assessment or bootstrap, transition, next-target selection, physical
   continuity, other effects, and completion. Maintain it through the
   heartbeat.
2. Inspect current intent, candidates, environment, active action, pending
   perceptions, and Hyper outcomes.
3. If no valid intent exists, select one exact candidate and assess it
   immediately as the initial/replan/recovery bootstrap exception. Otherwise
   assess the injected intent before considering another target. Use
   `satisfied_with_uncertainty` when judged acceptable despite missing or
   occluded evidence.
4. If satisfied, call `transition_fsm` once with exact current/next state
   identity, assessment, evidence, and uncertainty. Use its returned focused
   current-state context and select one next target when candidates remain. Do
   not assess that new target in this heartbeat.
5. If unsatisfied, retain the injected intent normally. If it is unsuitable,
   select one replacement from the injected candidates and defer its assessment
   to a later heartbeat.
6. After the FSM decision and target selection, preserve a suitable nonterminal
   active action when the target is unchanged;
   submitting it again would replace it. When replacement is warranted, choose
   the physical action and parameters at runtime from current outcome facts and
   environment evidence.
7. Independently ingest the complete pending perception batch once when
   warranted and send evidence-driven communications. A declared
   `hyper_evaluation` is sent with its exact kind, reason, evaluation ID, and
   delivery policy. Unmarked queries, reports, and replans remain unrestricted.
8. Complete every todo and return one concise public summary. Python supplies
   the authoritative identities in `ManeuverHeartbeatCompletion`. Durable tool
   records distinguish tool-free, rejected-tool, intent-only, and effectful
   cycles.

Only the bootstrap exception may select and assess an intent in the same
heartbeat. Never make a second successful transition or assess a newly selected
post-transition/replacement intent before fresh heartbeat evidence. A physical
action requires a valid intent whenever the live state has candidates. If the
runtime resumes this episode to correct a missing post-transition selection,
use its latest focused context and do not call `transition_fsm` again.

A completed navigation that established the current state's desired location
remains suitable while a time or observation gate is pending. Do not submit a
hold, repeat navigation, or renamed copy merely because its lifecycle is
terminal; replace it only when current-state evidence requires a different
physical action.

## Live reconciliation

- Periodic, actionable terminal-feedback, and `replan-activated:<revision>`
  heartbeats use the same cycle. Active feedback remains live progress evidence
  and is folded until another trigger. Replan activation supplies the Hyper
  outcome at the same Mission time, before an environment tick.
- A stale intent is invalidated when its source state, Statechart revision,
  plan revision, or state-entry revision no longer matches live authority.
- A queued communication acknowledgement is transport evidence; the correlated
  Hyper decision arrives in `hyper_outcomes`.
