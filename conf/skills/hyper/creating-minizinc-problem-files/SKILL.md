---
name: creating-minizinc-problem-files
description: Apply after MiniZinc is selected to generate planner-native model and data files from Mission Intent and the current Hyper heartbeat snapshot.
version: '1.4.0'
---

# Creating MiniZinc Problem Files

## Procedure

1. Keep the Hyper todo list explicit: parse Mission Intent into PlanningIntent, decide the planner, then generate planner problem files.
2. Read the raw Mission Intent, accepted PlanningIntent, and `load_planning_context`. Keep source roles distinct: `static_info` supplies report events, `scene_graph` supplies current drone state/capabilities, and `belief_snapshot` supplies entity risks.
3. Separate reusable constraints into model.mzn and current authorized values into data.dzn. Preserve supplied entity IDs, positions, event times, risks, units, and mission limits.
4. Scale finite decimal times, coordinates, and probabilities to integers once and record each scale in data.dzn.
5. Produce one attempt-specific asset set. Use `write_file` to create complete `model.mzn` and `data.dzn` at the exact `planner_asset_locations` returned by `load_planning_context`.
6. When `correction_feedback` is present, treat its sanitized validation stage and message as the complete diagnosis. Generate a fresh asset set that corrects that failure within the runtime's retry bound.
7. Use the same maneuver IDs in MiniZinc output and the normalization template. Emit only the `assignments` JSON object expected by the independent solution checker. Put solver-selected waypoint values in each assignment's `parameters` object.
8. Pass those exact file locations and the normalization template to `persist_planner_assets`; it freezes the files and returns immutable references for `planner_executor`.
9. Complete the generation todo only after static validation succeeds, MiniZinc reports an optimal solution, the independent solution checker accepts it, and every generated datum is traceable to Mission Intent or snapshot evidence.

## Select an example by mission

- For patrols that must account for reported events by choosing where and when to dwell, read `examples/event-information-patrol/model.mzn` and `examples/event-information-patrol/data.dzn`.
- For one abstract observation interval that weights stationary ships directly by risk, read `examples/risk-weighted-fov/model.mzn` and `examples/risk-weighted-fov/data.dzn`.

Use an example as a shape guide. Replace every value with the current Mission's
snapshot-authorized evidence.

## Event-information patrol example

The example consumes all 253 unchanged `static_info` events. It normalizes time
to half-second ticks and horizontal positions to metres, derives 155 unique
candidate opportunities, and chooses four chronological dwell stops.

For each event, the objective adds scaled
`1 - belief_snapshot.marginals[entity_id].probability_risk` exactly when the
event time is inside a selected dwell interval and its position is within the
scene-graph drone's 30 m FoV radius. Consecutive stops must be reachable from
the current drone position at its 20 m/s maximum velocity.

Its normalization template declares `patrol-stop-1` through `patrol-stop-4`,
each with action `navigate_and_observe`, duration two ticks, and chain
dependencies in stop order. Solver output supplies `x`, `y`,
`source_event_index`, and `time_scale`, plus explicit preceding `wait_start`,
`wait_duration`, `move_start`, `move_duration`, `move_from_x`, and
`move_from_y` parameters. Together, these describe the wait, direct move, and
dwell timeline for each stop without changing the optimization semantics.

## Authority boundary

Planner files are attempt artifacts, not Mission authority and not
PlanningIntent.details. A generated file becomes usable only after the
translation slice records it as accepted; rejected attempts remain auditable.
