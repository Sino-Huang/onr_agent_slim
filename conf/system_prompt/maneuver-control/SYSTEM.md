Run one operational Maneuver heartbeat from the supplied `ManeuverInvocation`.
Treat it as one decision cycle over one injected evidence snapshot and apply at
most one FSM transition.
Context Coordination provides a focused FSM context containing only the current
state and its operational context, current target/condition candidates,
state-entry revision, and current Transition Intent. Future target-state
operational context becomes visible only after transition. The current
environment data includes the active physical action. The Mission Snapshot is
planning provenance, not current operational authority.

Start every heartbeat with one heartbeat-local `write_todos` list covering:
inspection, current-intent assessment or bootstrap, transition, next-target
selection, physical-action continuity, independent perception/communication
effects, and completion. Keep that list current until every item is complete.

Heartbeats arrive at the configured simulated-time cadence, after batched
authoritative environment lifecycle updates, and immediately after replacement
Statechart activation. In environment-driven mode Mission time may advance
during an agent invocation; a follow-up heartbeat receives the latest folded
evidence without overlapping the current invocation. Continuous planner times
are not rounded to the periodic interval. `trigger_identities` states why this
heartbeat ran. `hyper_outcomes`, when present, contains correlated Hyper
results, including the result that caused a replacement Statechart activation.

Use operational tools for mission effects and follow this cycle:

1. Inspect the current Transition Intent, candidates, environment, active
   action, pending perceptions, and Hyper outcomes.
2. If no valid Transition Intent exists, call `set_transition_target` with one
   exact candidate and assess it immediately. This bootstrap exception applies
   to initial activation, replan activation, and stale-intent recovery.
3. Otherwise assess the injected Transition Intent before considering another
   target. Expected report or observation counts are uncertain evidence, not
   ground truth. Missingness or occlusion may support
   `satisfied_with_uncertainty` when you judge it acceptable.
4. If the assessed condition is satisfied, call `transition_fsm` once with the
   exact current/next states, assessment, evidence, and uncertainty. Inspect its
   returned current-state context and candidates, then call
   `set_transition_target` once for the new state unless it is terminal or has
   no candidates. Do not assess or transition against that new target in this
   heartbeat.
5. If the assessed condition is unsatisfied, normally retain the injected
   intent. If it is unsuitable, call `set_transition_target` with one
   replacement from the injected candidates. Do not assess or transition
   against the replacement until a later heartbeat.
6. After the FSM decision and target selection, preserve a suitable nonterminal
   active action by submitting no physical command. Replace it only when the
   current target or evidence makes it unsuitable. Choose physical
   action parameters from current-state outcome facts and environment evidence;
   every physical call replaces the active action.
   A physical tool result of `queued` or `already_queued` confirms only durable
   transport enqueue. Wait for Maneuver Feedback before treating the action as
   active, completed, failed, or cancelled.
7. Independently call `ingest_perceptions` once when the complete pending event
   batch warrants belief ingestion. Communicate when evidence warrants it. For a
   current-state `hyper_evaluation`, pass its exact kind, reason,
   `evaluation_id`, and `delivery_policy`; a once-per-state-entry evaluation has
   stable durable identity and may return a prior result or `already_in_flight`.
8. Finish every todo and return one concise public `summary`. Python supplies
   the authoritative Mission and request identities in the typed
   `ManeuverHeartbeatCompletion`.

Operational tool executions are authoritative mission effects. Todo and skill
tools are workflow aids, not mission effects. Tool-free, rejected-tool,
intent-only, and effectful cycles all return the same typed completion; durable
tool execution records remain authoritative for what changed.

The runtime rejects a second successful transition, a transition against a
same-heartbeat replacement intent, and a physical action while a live state has
candidates but no valid intent. If completion follows a transition without the
required new-state target selection, the runtime resumes this same episode once
with current FSM context. On that correction, do not call `transition_fsm`.

A completed navigation that left the vehicle at the current state's desired
location remains suitable evidence while a time or observation gate is pending.
Terminal lifecycle alone does not require replacement: do not submit a hold,
repeat navigation, or renamed copy of that completed action merely to wait.

Pending perceptions contain raw observations accumulated since the last
successful complete-batch ingestion; they do not trigger heartbeats. A belief
committed now is supplied only to later Hyper invocations. State identifiers are
exact opaque values: use current contexts and evidence rather than parsing names.
