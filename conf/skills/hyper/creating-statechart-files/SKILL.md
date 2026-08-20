---
name: creating-statechart-files
description: Apply after MiniZinc returns a verified NormalizedPlan to generate and repair semantic Statechart topology for execution readiness.
version: '1.0.0'
---

# Creating Statechart Files

## Procedure

1. Use only the verified `normalized_plan` returned by `planner_executor` and the snapshot-authorized planning context.
2. Describe behavioral states and time-conditioned transitions. State context carries plan-derived location, destination, observation, and timing facts; it never selects a physical action.
3. Submit topology with exactly `entry_state`, `terminal_states`, `states`, `state_context`, and `transitions`. Every state has one `state_context` object.
4. Each transition has exactly `event`, `source`, `target`, and `conditions`. A temporal condition has `kind: environment_time_at_or_after`, non-negative `time_tick`, and positive `time_scale`.
5. Call `submit_statechart_draft` with the next attempt number. On rejection, use only its correction stage and message to produce a fresh attempt within the returned bound.
6. Completion requires `outcome: verified`, an immutable Statechart reference and digest, and successful `python-statemachine` instantiation.

## Event-information patrol

Build this linear semantic topology from the solver-selected parameters:

- Entry state `at-initial-location` uses the first stop's `move_from_x` and `move_from_y`.
- For each stop `N`, create `moving-to-patrol-stop-N` with source and target coordinates, then `at-patrol-stop-N` with `x`, `y`, `source_event_index`, and dwell timing.
- Connect the preceding location state to the moving state at `move_start`.
- Connect the moving state to the stop state at the assignment `start`.
- After the final stop, connect to `patrol-complete` at `start + duration`.
- Use each assignment's `time_scale` unchanged on every time condition.

State IDs and event names are stable semantic identifiers. Conditions remain visible to Maneuver Control; the FSM Runner applies an edge only after an explicit transition decision.

## Repair boundary

`schema` rejection means the topology, state coverage, terminal reachability, or condition shape is invalid. `machine_build` rejection means the validated topology could not instantiate the FSM engine. Repair the Statechart draft; planner assets remain unchanged after a verified plan.
