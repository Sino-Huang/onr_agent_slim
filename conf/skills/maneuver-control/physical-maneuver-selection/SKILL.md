---
name: physical-maneuver-selection
description: Use when a Maneuver Decision may request, track, or cancel one environment-executed physical maneuver while preserving environment lifecycle authority.
version: '1.0.0'
---

# Physical Maneuver Selection

This skill is read-only guidance. It constrains Maneuver requests but never authorizes movement or determines a request's lifecycle.

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

Select at most one physical action per decision. Each request is an environment-agnostic command containing:

- a stable action ID;
- the exact Mission ID;
- the exact active plan/statechart revision;
- one allowed maneuver kind;
- the target entity or area identified by current Mission data; and
- typed intent parameters required by that maneuver kind.

Copy plan maneuver identifiers and targets exactly. Express intent, not adapter calls, vehicle controls, environment-specific protocols, or an expected lifecycle result.

## Selection and Submission

1. Check the current Mission Snapshot, FSM Status, plan constraints, and Active Maneuver before selecting an action.
2. Ensure the selected maneuver belongs to the current Mission and revision and has a valid target and typed parameters.
3. Prefer no physical request when the Mission is already reserved by a submitted nonterminal action.
4. Submit the command once with its stable action ID. Submission reserves the Mission until environment feedback reports a terminal outcome.
5. Route normalized lifecycle feedback to Context Coordination so a later Mission Snapshot can represent the authoritative Active Maneuver state.

The Mission Snapshot avoids known duplicate actions, but only the environment has final authority over what was accepted or executed. Preserve the action ID on retries or correlation; do not create a second action to work around uncertain feedback.

## Lifecycle Authority

- Only the environment authoritatively reports `accepted`, `active`, `completed`, `failed`, or `cancelled`.
- Do not claim or infer any lifecycle outcome from selection, submission, adapter return text, timeout, replanning, or FSM movement.
- Keep the Mission reserved until normalized terminal feedback is received and incorporated through Context Coordination.

## Cancellation

- Cancellation is the nonphysical request `cancel_maneuver(action_id)`.
- Never cancel automatically because Hyper replans or a plan revision changes.
- Keep the action correlated and reserved while cancellation is pending.
- Only environment feedback confirms `cancelled`; a cancellation request is not a lifecycle outcome.

## Gotchas

- A new plan does not retract an already submitted command.
- Missing feedback means unknown state, not failure or cancellation.
- Do not combine cancellation or an FSM transition with a new physical action in the same decision.
