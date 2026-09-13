---
name: physical-maneuver-selection
description: Use when selecting or preserving a physical maneuver, including rendezvous navigation, pursuit acquisition, and recovery after losing a target.
version: '1.3.0'
---

# Physical Maneuver Selection

For a Mission 2 observation assignment, retain its ship-pair, target, source/run,
forecast sequence and observation window. Navigate to a planner-authorized
observation location at the drone's operational altitude; forecast `z: null`
does not supply an altitude. Pursue only after target visibility/acquisition
permits it, and use visibility/measurement feedback to determine whether the
view is useful. A risk pair alone grants neither visibility nor a confirmed
collision. Refresh or ask Hyper to reconsider an expired/unreachable assignment
using the newest snapshot, while retaining separate Mission 1 objectives in a
joint run.

## Allowed Physical Actions

These are the only typed physical actions:

- `navigate`
- `takeoff`
- `land`
- `search_area`
- `pursue`
- `investigate`

Reject or use a nonphysical response for any other action kind.

## Physical Request Contract

Each tool call is an environment-agnostic command containing:

- a stable action ID;
- the exact Mission ID;
- the exact active plan/statechart revision;
- one allowed maneuver kind;
- the target entity or area identified by current Mission data; and
- typed intent parameters required by that maneuver kind.

Copy target facts from the active semantic state and current environment data. Express intent, not adapter calls, vehicle controls, environment-specific protocols, or an expected lifecycle result.

For deadline-driven navigation, normally omit the optional `speed` override:
the environment uses its configured capability. Early arrival is useful; wait
at the established viewpoint for the evidence window. Do not turn the planner's
conservative travel margin into a slower execution speed. Override speed only
for a current mission constraint, checking that the deadline remains feasible.

## Selection and Submission

1. Check the current Mission Snapshot, semantic FSM Status, environment context, and Active Maneuver before selecting an action.
2. Ensure the selected maneuver belongs to the current Mission and revision and has a valid target and typed parameters.
3. Prefer no physical request while a suitable action remains nonterminal.
4. A new physical tool call always submits and overrides the active action. Use this for inappropriate actions or emergencies, not as routine polling.
5. Inspect normalized lifecycle feedback in each injected environment payload.
   Active progress is folded until the next configured or actionable trigger;
   terminal feedback triggers an immediate heartbeat.
6. A `fixed_view` assignment selects `navigate`. A `pursue_ship` assignment
   retains that surveillance mode while Maneuver Control acquires and tracks
   the target. Before selecting an action for this mode, read
   [Pursuit acquisition and recovery](references/pursuit-acquisition.md).
   An unseen target away from the public rendezvous needs `navigate` first;
   arrival or a current target sighting permits `pursue(entity_id=target_entity_id)`.
   These are physical phases within the same FSM assignment, not transitions
   or a replacement fixed-view plan.
   A search that missed its acquisition deadline is unsuitable even if the
   observation-window gate is still future. With a newer GPS fix, navigate for
   recovery and notify Hyper about the missed report; otherwise use the
   reference's escalation-only response. An unsatisfied FSM gate does not
   prevent these within-state physical decisions.

The Mission Snapshot avoids known duplicate actions, but only the environment has final authority over what was accepted or executed. Preserve the action ID on retries or correlation; do not create a second action to work around uncertain feedback.

## Lifecycle Authority

- Only the environment authoritatively reports `accepted`, `active`, `completed`, `failed`, or `cancelled`.
- Do not claim or infer any lifecycle outcome from selection, submission, adapter return text, timeout, replanning, or FSM movement.
- Keep the Mission reserved until normalized terminal feedback is received and incorporated through Context Coordination.

## Override

- Override is technically always allowed. The environment cancels the displaced command with `reason: overridden` and activates the new one.
- Only environment feedback confirms the displaced and replacement lifecycles.

## Gotchas

- A new plan does not retract an already submitted command.
- Missing feedback means unknown state, not failure or cancellation.
- Several tools may run sequentially in one heartbeat; inspect live results between calls.
