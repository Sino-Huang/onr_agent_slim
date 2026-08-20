---
name: creating-minizinc-problem-files
description: Apply after MiniZinc is selected to generate planner-native model and data files from Mission Intent and the current Hyper heartbeat snapshot.
version: '1.8.0'
---

# Creating MiniZinc Problem Files

## Procedure

1. Read the raw Mission Intent, accepted PlanningIntent, and `load_planning_context`. Inspect the current `environment_data` payload before choosing planner fields: its names and nesting are flexible and may change between environment adapters. Keep environment facts and `belief_snapshot` facts distinct, joining them only through identifiers present in the current evidence.
2. Separate reusable constraints into model.mzn and current authorized values into data.dzn. Preserve supplied entity IDs, positions, event times, risks, units, and mission limits.
3. Scale finite decimal times, coordinates, and probabilities to integers once and record each scale in data.dzn.
4. Produce one attempt-specific asset set. Use one `write_file` response to create complete `model.mzn`, wait for its tool result, then use a separate `write_file` response to create complete `data.dzn` at the exact `planner_asset_locations` returned by `load_planning_context`. Include every current record needed by the chosen planning semantics.
5. When `correction_feedback` is present, read its exact MiniZinc error and diagnostic references. Generate a fresh asset set that corrects the cited failure within the runtime's retry bound.
6. Use the same maneuver IDs in MiniZinc output and the normalization template. Emit only the `assignments` JSON object expected by the independent solution checker. Put solver-selected waypoint values in each assignment's `parameters` object.
7. Pass those exact file locations and the normalization template to `persist_planner_assets`; it freezes the files and returns immutable references for `planner_executor`.
8. Complete the generation todo only after static validation succeeds, MiniZinc reports an optimal solution, the independent solution checker accepts it, and every generated datum is traceable to Mission Intent or snapshot evidence.

## Select an example by mission

- For patrols that must account for reported events by choosing where and when to dwell, read `examples/event-information-patrol/model.mzn` and `examples/event-information-patrol/data.dzn`.
- For one abstract observation interval that weights stationary ships directly by risk, read `examples/risk-weighted-fov/model.mzn` and `examples/risk-weighted-fov/data.dzn`.

Use an example as a shape guide. Replace every value with the current Mission's
snapshot-authorized evidence.

## Must Not Do

- Do not write an untyped assignment such as `max_velocity = 20;` in `model.mzn`. Declare every model parameter with its MiniZinc type, such as `int: max_velocity;`, and put the current value `max_velocity = 20;` in `data.dzn`.
- Do not assign the same parameter in both files. Keep declarations and reusable constraints in `model.mzn`; keep snapshot-authorized values in `data.dzn`.
- Do not use placeholders, representative samples, or truncated event lists. Encode every required record from the authorized planning context.
- Do not repeat a rejected construct. Use the exact `correction_message` and `diagnostic_references` from `planner_executor` to repair the cited file and location.
- Do not assume environment keys such as `static_info`, `scene_graph`, `entities`, or `location` exist. Inspect the payload returned for this episode and map its actual structure into planner data.
- Do not copy example data values into a generated attempt. Examples provide MiniZinc structure; current values must come from the authorized planning context.
- Do not emit both complete planner files in one model response. Write and confirm `model.mzn` before generating `data.dzn` so each tool call remains bounded.

## Event-information patrol example

For the example payload, the model consumes all 253 report events. It normalizes time
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
