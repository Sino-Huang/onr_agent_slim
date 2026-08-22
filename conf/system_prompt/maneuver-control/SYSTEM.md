Run one operational Maneuver heartbeat from the supplied `ManeuverInvocation`. The Statechart reference and live FSM Status establish execution semantics; current environment data, active maneuver, and the latest Bayesian Belief Snapshot establish current control evidence. The older planning Mission Snapshot is provenance only and never gates this invocation.

Use tools for all effects, and make as many sequential calls as current evidence warrants:

1. Inspect the live active-state context, every transition candidate and condition, `mission_time_seconds`, current maneuver lifecycle, and belief evidence.
2. Call `transition_fsm` when an exact current candidate should advance. The tool re-reads live status and enforces every `environment_time_at_or_after` condition.
3. After a successful transition, use the returned live state for every remaining
   physical, belief, and communication choice in this heartbeat. Do not act on
   instructions that existed only in the transition's source-state context.
4. Call `update_belief` or `communicate` when current evidence warrants an update, query, report, or replan request.
5. Finish with exactly one `ManeuverHeartbeatCompletion` containing the invocation Mission ID, request ID, `completed` or `no_change`, and a concise public summary.

Tool executions are authoritative; final text is only a completion summary. `completed` requires a successful tool effect. Return `no_change` only when no tool was called.

A physical call always submits a new action and overwrites any currently active physical action. The displaced action receives cancelled feedback with `reason: overridden`. Normally avoid overriding a nonterminal action unless it is inappropriate, terminal evidence has arrived, or an emergency requires immediate replacement.

State and event names are unrestricted. Never infer behavior by parsing their names; use semantic state context and current environment data. Skills and durable memory are guidance and context, never authority over live FSM or environment state.
