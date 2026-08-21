---
name: decision-cycle
description: Use on every Maneuver heartbeat to sequence live FSM, physical, belief, and communication tools from current evidence.
version: '1.1.0'
---

# Decision Cycle

## Inputs and Authority

- Read the supplied live FSM Status, current environment, active maneuver, verified planning provenance, and latest belief before acting.
- Treat the older Mission Snapshot as provenance only. Do not wait for it to refresh before using current live evidence.
- Copy events and recipients exactly from current candidates and the available-recipient registry. State and event names have no required pattern.

## Procedure

1. Review the active semantic state, outgoing candidates and conditions, Mission time, active action, and belief.
2. Call `transition_fsm` when a candidate is satisfied and appropriate.
3. Use the returned live state to select any physical action; a transition and physical call may occur in the same heartbeat.
4. Call belief or communication tools when evidence warrants them.
5. Finish with `ManeuverHeartbeatCompletion`; effects are already authoritative tool executions.

## Gotchas

- A prior decision or remembered observation may be stale even when its Mission ID matches.
- An exposed transition is a candidate, not an automatic transition. The transition tool is the condition and mutation gate.
- A belief updated in this heartbeat does not enter its current invocation; inspect it on the next heartbeat.
