---
name: physical-maneuver-selection
description: Use when a Maneuver Decision may request, track, or cancel one environment-executed physical maneuver while preserving environment lifecycle authority.
version: '1.1.1'
---

# Physical Maneuver Selection

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

## Selection and Submission

1. Check the current Mission Snapshot, semantic FSM Status, environment context, and Active Maneuver before selecting an action.
2. Ensure the selected maneuver belongs to the current Mission and revision and has a valid target and typed parameters.
3. Prefer no physical request while a suitable action remains nonterminal.
4. A new physical tool call always submits and overrides the active action. Use this for inappropriate actions or emergencies, not as routine polling.
5. Inspect normalized lifecycle feedback in each injected environment payload.
   Active progress is folded until the next configured or actionable trigger;
   terminal feedback triggers an immediate heartbeat.
6. A `fixed_view` assignment selects `navigate`. A `pursue_ship` assignment
   selects `pursue(entity_id=target_entity_id)` with the numeric ID copied from
   Statechart context. Keep suitable pursuit active; a later assignment or
   replacement Statechart overrides it through the normal command lifecycle.

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
