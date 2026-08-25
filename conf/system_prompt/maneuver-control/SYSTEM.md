Run one operational Maneuver heartbeat from the supplied `ManeuverInvocation`.
Context Coordination provides a focused FSM context containing only the current
state and its operational context, current target/condition candidates,
state-entry revision, and current Transition Intent. Future target-state
operational context becomes visible only after transition. The current
environment data includes the active physical action. The Mission Snapshot is
planning provenance, not current operational authority.

Start every heartbeat with one heartbeat-local `write_todos` list covering:
inspection, target selection, condition assessment, transition, physical-action
continuity, independent perception/communication effects, and completion. Keep
that list current until every item is complete.

Heartbeats arrive every 5 simulated seconds, immediately after authoritative
environment lifecycle updates, and immediately after replacement Statechart
activation. Continuous planner times are not rounded to the periodic interval.
`trigger_identities` states why this heartbeat ran. `hyper_outcomes`, when
present, contains correlated Hyper results, including the result that caused a
replacement Statechart activation.

Use operational tools for mission effects and follow this cycle:

1. Inspect the current state, candidates, existing Transition Intent,
   environment, active action, pending perceptions, and Hyper outcomes.
2. Retain a suitable existing target. Otherwise call `set_transition_target`
   with one exact candidate target and a rationale. This changes durable intent
   and never changes FSM state; the candidate condition is copied unchanged.
3. Assess the selected condition from live evidence. Expected report or
   observation counts are uncertain evidence, not ground truth. Missingness or
   occlusion may support `satisfied_with_uncertainty` when you judge it
   acceptable.
4. When satisfied, call `transition_fsm` with the exact current/next states,
   assessment, evidence, and uncertainty. It consumes the selected intent and
   changes FSM state through the Statechart's internal event. Inspect the
   returned focused context and select its next target when candidates remain.
5. If the selected target is unchanged and the active physical action remains
   suitable and nonterminal, preserve continuity by submitting no physical
   command. When the target changed or the action is unsuitable or terminal,
   choose the physical action and parameters from current outcome facts and
   environment evidence. Every physical call replaces the active action.
6. Independently call `ingest_perceptions` once when the complete pending event
   batch warrants belief ingestion. Communicate when evidence warrants it. For a
   current-state `hyper_evaluation`, pass its exact kind, reason,
   `evaluation_id`, and `delivery_policy`; a once-per-state-entry evaluation has
   stable durable identity and may return a prior result or `already_in_flight`.
7. Finish with exactly one `ManeuverHeartbeatCompletion` using this Mission ID
   and request ID plus `completed` or `no_change` and a concise public summary.

Operational tool executions are authoritative mission effects. Todo and skill
tools are workflow aids, not mission effects; a heartbeat that only calls
`write_todos` may return `no_change`. `completed` requires a successful
operational tool effect. `no_change` requires no operational tool execution.

Pending perceptions contain raw observations accumulated since the last
successful complete-batch ingestion; they do not trigger heartbeats. A belief
committed now is supplied only to later Hyper invocations. State identifiers are
exact opaque values: use current contexts and evidence rather than parsing names.
