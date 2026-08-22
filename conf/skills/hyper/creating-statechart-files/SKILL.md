---
name: creating-statechart-files
description: Apply after planner execution returns an accepted planner-native plan to generate and repair Statechart/FSM execution semantics.
version: '2.0.0'
---

# Creating Statechart Files

## Procedure

1. Interpret the exact planner-native plan text returned by `planner_executor`; retain its PlannerPlan artifact reference as planning evidence.
2. Describe behavioral states and legal transitions. State context carries plan-derived location, destination, observation, and timing facts; the Statechart/FSM becomes the execution semantics.
3. Submit topology with exactly `entry_state`, `terminal_states`, `states`, `state_context`, and `transitions`. Every state has one `state_context` object.
4. Each transition has exactly `event`, `source`, `target`, and `conditions`. A temporal condition has `kind: environment_time_at_or_after`, non-negative `time_tick`, and positive `time_scale`.
5. Call `submit_statechart_draft` with the topology and concise reflection; the workflow assigns the next attempt. On rejection, use its exact validation error to produce a fresh attempt within the returned bound.
6. Completion requires acceptance and successful `python-statemachine` instantiation.

## MiniZinc event-information patrol

Build this linear semantic topology from the solver-selected native output:

- Entry state `at-initial-location` uses the first stop's `move_from_x` and `move_from_y`.
- For each stop `N`, create `moving-to-patrol-stop-N` with source and target coordinates, then `at-patrol-stop-N` with `x`, `y`, `source_event_index`, and dwell timing.
- Connect the preceding location state to the moving state at `move_start`.
- Connect the moving state to the stop state at the assignment `start`.
- After the final stop, connect to `patrol-complete` at `start + duration`.
- Use each assignment's `time_scale` unchanged on every time condition.

State IDs and event names are stable semantic identifiers. Conditions remain visible to Maneuver Control; the FSM Runner applies an edge only after an explicit transition decision.

## Must Not Do

- Do not add `additionalProperties` or any other top-level field. Submit exactly `entry_state`, `terminal_states`, `states`, `state_context`, and `transitions`.
- Do not put context objects inside `states`. `states` is an array of state-ID strings; `state_context` is a top-level object mapping every state ID to its context object.
- Do not submit one condition object directly. Every transition's `conditions` value is an array, such as `[{"kind":"environment_time_at_or_after","time_tick":65,"time_scale":2}]`.
- Do not repeat an identical rejected draft. Read the exact validation error, then change the cited shape before resubmission.

## Repair boundary

`schema` rejection means the topology, state coverage, terminal reachability, or condition shape is invalid. `machine_build` rejection means the validated topology could not instantiate the FSM engine. Repair the Statechart draft; `submit_statechart_draft` validates structure and FSM construction without reinterpreting or comparing planner-native actions.
