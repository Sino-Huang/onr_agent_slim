---
name: decision-cycle
description: Use on every Maneuver heartbeat to choose a bounded response from the current Mission Snapshot and FSM transition candidates without assuming mission, plan, or environment authority.
version: '1.0.0'
---

# Decision Cycle

This skill is read-only guidance. It helps Maneuver produce a `ManeuverControlDecision`; it never supplies mission facts, plan authority, or lifecycle authority.

## Inputs and Authority

- Read the supplied Mission Snapshot and current FSM Status, including enabled Transition Candidates, before deciding.
- Treat the Mission Snapshot as an immutable versioned manifest, not hidden ground truth. Do not infer unreported environment state.
- Use an Invocation Overlay only for the current invocation. It does not become durable Mission authority.
- Copy `mission_id`, `plan_revision`, selected `maneuver_id`, and selected `transition_event` exactly from the current inputs. Never repair, translate, or synthesize these identifiers.
- If Mission IDs or plan/statechart revisions disagree, do not guess which is current. Return an appropriate nonphysical report, query, replan request, or no-change response through the provided contract.

## Procedure

1. Confirm the Mission and plan/statechart revision are consistent across the Mission Snapshot and FSM Status.
2. Review the active semantic state, all outgoing Transition Candidates and their conditions, current environment data, and any Active Maneuver lifecycle state.
3. Select at most one physical action permitted by the active state and current environment context.
4. Separately select the applicable nonphysical response: `transition`, `replan`, `report`, `query`, `no_change`, or `cancel_maneuver`.
5. Do not combine a physical action with `transition` or `cancel_maneuver`. Use `no_change` when no other response is justified.
6. Return only the exact structured `ManeuverControlDecision` fields required by the runtime contract, with no prose or extra fields.

## Memory Discipline

- Store only durable, useful execution context in Maneuver's isolated per-Mission memory namespace.
- Never write Maneuver memory to shared memory or another Mission's namespace.
- Memory is context, not authority: it cannot override a Mission Snapshot, FSM Status, plan revision, or environment feedback.
- Never store or manufacture hidden ground truth. V1 has no standalone memory-hygiene skill; apply these rules within this decision cycle.

## Gotchas

- A prior decision or remembered observation may be stale even when its Mission ID matches.
- An enabled transition is a candidate, not an automatic transition and not permission to invent a target state.
- Do not claim accepted, active, completed, failed, or cancelled. Consume normalized environment lifecycle feedback after Context Coordination incorporates it into authoritative Mission state.
