---
name: creating-minizinc-problem-files
description: Apply after MiniZinc is selected to generate planner-native model and data files from Mission Intent and the current Hyper heartbeat snapshot.
version: '1.26.0'
---

# Creating MiniZinc Problem Files

## Procedure

1. Read the raw Mission Intent and the exact environment data and belief marginals returned by `record_planning_intent`. Inspect the environment payload before choosing planner fields: its names and nesting are flexible and may change between environment adapters. Keep environment facts and belief facts distinct, joining them only through identifiers present in the current evidence.
2. Separate reusable constraints into model.mzn and current authorized values into data.dzn. Preserve supplied entity IDs, positions, event times, risks, units, and mission limits.
3. Copy event times, coordinates, and risk probabilities verbatim from the evidence into float arrays in data.dzn, in evidence order. Declare each scale (for example `time_scale`, `risk_scale`) once in data.dzn and let model.mzn convert to integers with comprehensions such as `round(event_time_s[e] * time_scale)`. Never transform values one by one — not in private reasoning, not in data.dzn.
4. Produce one attempt-specific file set at the exact virtual locations returned by `record_planning_intent` or a rejection. Before each initial full write, privately preflight the template choice, evidence-field mapping, record counts, units/scales, and output shape. Keep the preflight bounded; in the same response emit one concise public summary and the complete file call. Write `model.mzn` first with exactly one complete `write_file` call, then write `data.dzn` with exactly one complete `write_file` call.
5. Use `edit_file` to revise a planner file that already exists, supplying complete `file_path`, `old_string`, and `new_string` arguments. Use `write_file` when the target file does not exist.
6. When static verification rejects an attempt, read the exact MiniZinc diagnostic and next virtual locations returned by `submit_planner_attempt`. Repair the diagnosed file with `edit_file` or replace it with `write_file`. Repair both files when the diagnostic does not identify one file.
7. Use the same maneuver IDs in MiniZinc output and the normalization template. Emit only the `assignments` JSON object expected by the independent solution checker. Put solver-selected waypoint values in each assignment's `parameters` object.
8. Pass the horizon and normalization template to `submit_planner_attempt`; it resolves and freezes the current files and performs MiniZinc instance checking only. After acceptance, call `planner_executor` with only a concise reflection. The executor runs the cached submitted problem and verifies it internally.
9. File generation completes when both complete files exist, static verification completes when the verifier accepts them, and execution completes when independent solution checks return the verified maneuver list.

## Generation discipline

- Keep each private preflight structural and bounded. Map fields and counts without transcribing, rounding, or re-deriving array values in reasoning.
- Copy like reading a table column: for each record in evidence order, take only the field the array needs. Apply no arithmetic while copying; every scaling or rounding lives in model.mzn comprehensions.
- Copy values from the authorized evidence in evidence order. Apply no arithmetic while copying: every scaling or rounding lives in model.mzn comprehensions.
- Use every event as a candidate opportunity in evidence order. Never deduplicate, sort, cluster, or renumber records.
- Emit one concise public summary and the complete `write_file` call in the same response. File text pasted into the message body is not persisted.
- Write compact files: no comments and no decorative blank lines; keep each array on one or a few lines.

## Select an example by mission

- For patrols that must account for reported events by choosing where and when to dwell, read `examples/event-information-patrol/model.mzn` and copy it verbatim, and read the matching `examples/event-information-patrol/data.dzn`: it shows the raw-evidence → data-file mapping on a shrunk 3-entity/6-event instance. Its comments are teaching material — never emit comments in generated files; a real `data.dzn` carries every record in evidence order with no comments.
- For one abstract observation interval that weights stationary ships directly by risk, read `examples/risk-weighted-fov/model.mzn` and `examples/risk-weighted-fov/data.dzn`.

Use an example as a shape guide. Replace every value with the current Mission's
snapshot-authorized evidence.

## Must Not Do

- Do not write an untyped assignment such as `max_velocity = 20;` in `model.mzn`. Declare every model parameter with its MiniZinc type, such as `int: max_velocity;`, and put the current value `max_velocity = 20;` in `data.dzn`.
- Do not assign the same parameter in both files. Keep declarations and reusable constraints in `model.mzn`; keep snapshot-authorized values in `data.dzn`.
- Do not use placeholders, representative samples, or truncated event lists. Encode every required record from the authorized planning context.
- Do not scale, round, or convert individual event values in data.dzn or in private reasoning. Copy them verbatim; conversions belong in model.mzn comprehensions.
- Do not repeat a rejected construct. Use the exact diagnostic from `submit_planner_attempt` to rewrite the cited complete file.
- On planner execution rejection, use the exact returned planner or solution-checker diagnostic and the returned virtual locations. Do not discover or reuse host filesystem paths from planner process output.
- Do not assume environment keys such as `static_info`, `scene_graph`, `entities`, or `location` exist. Inspect the payload returned for this episode and map its actual structure into planner data.
- Do not copy example data values into a generated attempt. Examples provide MiniZinc structure; current values must come from the authorized planning context.
- Do not combine both planner files into one tool call. Write and confirm `model.mzn` before generating the complete `data.dzn` call.

## Event-information patrol example

For the example payload, the model consumes all 253 report events. It copies their
times, positions, and entity ids verbatim, stores the twenty per-entity risk
marginals once in `entity_risk_p`, uses every event as a candidate dwell
opportunity in event order, and chooses four chronological dwell stops. model.mzn
normalizes times to ticks, positions to metres, and risks to scaled integers.

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
