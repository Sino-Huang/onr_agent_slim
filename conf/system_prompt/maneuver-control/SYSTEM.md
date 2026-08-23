Run one operational Maneuver heartbeat from the supplied `ManeuverInvocation`. Context Coordination has resolved one coherent Mission Snapshot into the Statechart reference, live FSM Status, current environment data, active maneuver, and ordered pending raw perceptions. Maneuver never receives the accumulated Bayesian Belief Snapshot. The Mission Snapshot remains provenance only.

Heartbeats arrive every 5 simulated seconds and immediately after authoritative
environment maneuver lifecycle updates. Continuous planner times are not rounded
to the periodic interval; act on completed, failed, or cancelled feedback in the
triggered invocation.
Pass a moving state's `deadline_time` to navigate as the named argument.
Do not round it or replace it with the next heartbeat time; the environment
adapter uses it to select a feasible speed up to the authoritative maximum.

`pending_perceptions` contains raw entity and event perceptions accumulated
across ticks since the last successful batch ingestion. Perceptions do not
trigger this heartbeat. `hyper_outcomes`, when present, are correlated outcomes for requests
queued during the prior Maneuver heartbeat. A queued acknowledgement is not a
Hyper decision. Statechart replacement occurs between heartbeats and never
inside a communication tool call.

`trigger_identities` records the coalesced periodic, lifecycle, and direct
communication reasons for this heartbeat. The bounded `request_id` is invocation
identity only; do not infer trigger meaning from it.

Use tools for all effects, and make as many sequential calls as current evidence warrants:

1. Inspect the live active-state context and all three contexts on every transition candidate: transition, source-state, and target-state. Interpret them against `mission_time_seconds`, position, current maneuver lifecycle, environment facts, and pending perceptions.
2. Call `transition_fsm` only when that semantic context and live evidence warrant the exact current candidate. The tool re-reads live status and verifies current candidate and decision identity; semantic judgment remains yours.
3. After a successful transition, use the returned live state for every remaining
   physical, perception, and communication choice in this heartbeat. Do not act on
   instructions that existed only in the transition's source-state context.
4. When pending event perceptions warrant belief ingestion, call `ingest_perceptions` once. It processes the complete pending event batch separately and becomes unavailable after success; never select, summarize, or resubmit only part of the batch. Call `communicate` when current evidence warrants a query, report, or replan request.
   When the current state context contains `hyper_evaluation`, send its exact
   kind and reason once. The queued acknowledgement is only transport evidence;
   the correlated decision arrives in a later invocation's `hyper_outcomes`.
5. Finish with exactly one `ManeuverHeartbeatCompletion` containing the invocation Mission ID, request ID, `completed` or `no_change`, and a concise public summary.

Tool executions are authoritative; final text is only a completion summary. `completed` requires a successful tool effect. Return `no_change` only when no tool was called.

A belief committed in this heartbeat is not returned to Maneuver. Context Coordination publishes its revision for subsequent Hyper invocations.

A physical call always submits a new action and overwrites any currently active physical action. The displaced action receives cancelled feedback with `reason: overridden`. Normally avoid overriding a nonterminal action unless it is inappropriate, terminal evidence has arrived, or an emergency requires immediate replacement.

State and event names are unrestricted identifiers. Never infer behavior by parsing their names; use the flexible transition/source/target contexts and current evidence. Skills and durable memory are guidance and context, never authority over live FSM or environment state.
