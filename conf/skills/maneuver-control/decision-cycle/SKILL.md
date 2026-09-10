---
name: decision-cycle
description: Use for Maneuver intent bootstrap, transitions, retargeting, belief ingestion, or communication. Routine fixed-view waiting is covered by the system prompt; pursuit continuity requires physical-maneuver-selection.
version: '2.2.1'
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

1. Assess the checks below once against the injected snapshot and available
   guidance. Todos are optional for short heartbeats; use them when tracking
   dependent multi-step work is useful. If used, update at evidence boundaries
   and combine updates with independent calls. Required skill reads and
   dependent tool results must arrive before their work is marked complete.
2. Inspect current intent, candidates, environment, active action, pending
   perceptions, and Hyper outcomes.
3. If no valid intent exists, select one exact candidate and assess it
   immediately as the initial/replan/recovery bootstrap exception. Otherwise
   assess the injected intent before considering another target. Apply the
   system prompt's time-readiness check before evidence confidence: retain an
   intent whose explicit time requirement is still pending. Once time-ready,
   use `satisfied_with_uncertainty` when judged acceptable despite missing or
   occluded evidence; uncertainty qualifies sensing, not future elapsed time.
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
   For a pursuit assignment or active pursuit, read
   `/conf/skills/maneuver-control/physical-maneuver-selection/SKILL.md` and its
   acquisition reference before judging the action suitable, including on
   tool-free heartbeats. A future FSM evidence gate does not extend a missed
   acquisition deadline.
7. Inspect `observation_kind` before belief ingestion. Call `ingest_perceptions`
   once when the pending batch contains `event` observations; it processes all
   events in order. `entity` observations are sightings for tracking and physical
   decisions, not event evidence. For example, two ship-position sightings with
   `observation_kind: "entity"` need no ingestion call. An empty or entity-only
   batch leaves belief unchanged. If the tool returns
   `no_pending_event_observations`, continue the heartbeat without retrying it
   or claiming ingestion succeeded. Send evidence-driven communications
   independently. A declared
   `hyper_evaluation` is sent with its exact kind, reason, evaluation ID, and
   delivery policy. Unmarked queries, reports, and replans remain unrestricted.
8. Complete any todos you created and call `ManeuverHeartbeatResponse` with one
   concise public summary. A no-effect cycle needs no bookkeeping call. Python supplies
   the authoritative identities in `ManeuverHeartbeatCompletion`. Durable tool
   records distinguish tool-free, rejected-tool, intent-only, and effectful
   cycles.

Only the bootstrap exception may select and assess an intent in the same
heartbeat. Never make a second successful transition or assess a newly selected
post-transition/replacement intent before fresh heartbeat evidence. A physical
action requires a valid intent whenever the live state has candidates. If the
runtime resumes this episode to correct a missing post-transition selection,
use its latest focused context and do not call `transition_fsm` again.

A completed fixed-view navigation that established the current state's desired location
remains suitable while a time or observation gate is pending. Do not submit a
hold, repeat navigation, or renamed copy merely because its lifecycle is
terminal; replace it only when current-state evidence requires a different
physical action.
For pursuit acquisition, completed rendezvous navigation instead hands off to
`pursue` within the same state; use the physical-maneuver-selection skill for
visibility checks, bounded search, and public-position recovery.

## Live reconciliation

- Fallback, deadline, relevant evidence-change, actionable terminal-feedback,
  and `replan-activated:<revision>` heartbeats use the same assessment cycle.
  A wake-up does not assert condition satisfaction. Active feedback remains live progress evidence
  and is folded until another trigger. Replan activation supplies the Hyper
  outcome at the same Mission time, before an environment tick.
- A stale intent is invalidated when its source state, Statechart revision,
  plan revision, or state-entry revision no longer matches live authority.
- A queued communication acknowledgement is transport evidence; the correlated
  Hyper decision arrives in `hyper_outcomes`.
