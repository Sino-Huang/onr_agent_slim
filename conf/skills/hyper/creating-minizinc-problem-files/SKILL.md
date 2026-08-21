---
name: creating-minizinc-problem-files
description: Apply after MiniZinc is selected to generate planner-native model and data files from Mission Intent and the current Hyper heartbeat snapshot.
version: '1.22.0'
---

# Creating MiniZinc Problem Files

## Procedure

1. Read the raw Mission Intent, accepted PlanningIntent, and `load_planning_context`. Inspect the current `environment_data` payload before choosing planner fields: its names and nesting are flexible and may change between environment adapters. Keep environment facts and `belief_snapshot` facts distinct, joining them only through identifiers present in the current evidence.
2. Separate reusable constraints into model.mzn and current authorized values into data.dzn. Preserve supplied entity IDs, positions, event times, risks, units, and mission limits.
3. Copy event times, coordinates, and risk probabilities verbatim from the evidence into float arrays in data.dzn, in evidence order. Declare each scale (for example `time_scale`, `risk_scale`) once in data.dzn and let model.mzn convert to integers with comprehensions such as `round(event_time_s[e] * time_scale)`. Never transform values one by one — not in private reasoning, not in data.dzn.
4. Produce one attempt-specific asset set at the exact `planner_asset_locations` returned by `load_planning_context`. Create `model.mzn` with one `write_file` call: copy the chosen example's model verbatim — it is already general and carries no mission values. Then build `data.dzn` incrementally: one `write_file` call for the scalars, the `entity_risk_p` array, and one skeleton per event array, each skeleton holding a unique sentinel comment (for example `event_time_s = [% NEXT_TIME\n];`), then one `edit_file` call per response replacing a sentinel with up to 75 values plus the same sentinel, until every array holds all records in evidence order. Never re-emit values or lines already written: `old_string` is always just the sentinel. Every extra response consumes the episode's step budget — never re-read files or re-verify written values.
5. When `correction_feedback` is present, read its exact MiniZinc error and diagnostic references. Generate a fresh asset set that corrects the cited failure within the runtime's retry bound.
6. Use the same maneuver IDs in MiniZinc output and the normalization template. Emit only the `assignments` JSON object expected by the independent solution checker. Put solver-selected waypoint values in each assignment's `parameters` object.
7. Pass those exact file locations and the normalization template to `persist_planner_assets`; it freezes the files and returns immutable references for `planner_executor`. The attempt number must match the workspace directory in `planner_asset_locations`: the first asset set is attempt 1 (`workspace/001`); increment the attempt number only after a rejected planner execution, when you write a fresh asset set into the new workspace.
8. Complete the generation todo only after static validation succeeds, MiniZinc reports an optimal solution, the independent solution checker accepts it, and every generated datum is traceable to Mission Intent or snapshot evidence.

## Generation discipline

- Before each chunk response, reason in exactly three short lines: the array or section name, the next record index, and this chunk's end index. Then emit the tool call. Any longer deliberation is a failure signal — stop mid-thought and emit the call immediately.
- There is nothing to compute for these files: `model.mzn` carries no mission values, and every `data.dzn` value is a verbatim copy. If a response seems to require computation, verification, or planning of upcoming chunks, stop and emit the current mechanical copy instead.

- Keep private reasoning to a brief structural plan — name the arrays and their order in under ten lines, then emit the `write_file` call immediately. Never transcribe, round, or re-derive array values inside private reasoning; long transcription reasoning stalls the response and yields an empty reply.
- Copy at most about 75 values per response. Longer single-response transcriptions stall the model and return an empty reply; use successive `edit_file` sentinel replacements instead.
- Start each array with a small first chunk of about 20 values. The first chunk of an array is the most stall-prone; a quick first success sets the mechanical rhythm, and later chunks of up to 75 values follow the same pattern.
- Every `edit_file` call must carry all of `file_path`, `old_string` (the sentinel alone), and `new_string` (this chunk's values plus the sentinel). If any tool call is rejected — schema validation or runtime error — the very next response must re-emit the complete corrected call; never repeat a rejected call unchanged.
- Copy like reading a table column: for each record in order, take only the one field the array needs and skip everything else. Never describe, summarize, or narrate record contents — phrases like "continuing through the event log" or "tracking positions" in your reasoning mean the response is degenerating: stop and emit the tool call immediately.
- Handle exactly one chunk per response. Do not read ahead in the evidence, do not plan later chunks, and do not re-verify values already written. If unsure of progress, read the current data.dzn and continue after its last value.
- Copy values from the authorized evidence in evidence order. Apply no arithmetic while copying: every scaling or rounding lives in model.mzn comprehensions.
- Use every event as a candidate opportunity in evidence order. Never deduplicate, sort, cluster, or renumber records.
- Emit each file only through a `write_file` call carrying the complete contents. File text pasted into the message body is not persisted and the response is rejected.
- Write compact files: no comments and no decorative blank lines; keep each array on one or a few lines.

## Select an example by mission

- For patrols that must account for reported events by choosing where and when to dwell, read `examples/event-information-patrol/model.mzn` and copy it verbatim, and read the matching `examples/event-information-patrol/data.dzn`: it shows the raw-evidence → data-file mapping on a shrunk 3-entity/6-event instance. Its comments are teaching material — never emit comments in generated files; a real `data.dzn` carries every record in evidence order with no comments.
- For one abstract observation interval that weights stationary ships directly by risk, read `examples/risk-weighted-fov/model.mzn` and `examples/risk-weighted-fov/data.dzn`.

The data-file shape with sentinels:

```dzn
event_count = 253;
stop_count = 4;
entity_count = 20;
horizon_ticks = 600;
time_scale = 2;
dwell_ticks = 2;
maneuver_id = ["patrol-stop-1", "patrol-stop-2", "patrol-stop-3", "patrol-stop-4"];
event_time_s = [% NEXT_TIME
];
event_x_m = [% NEXT_X
];
event_y_m = [% NEXT_Y
];
event_entity = [% NEXT_ENTITY
];
entity_risk_p = [0.43896484375, 0.694, % up to 20 values, one per entity
];
drone_start_time = 0;
drone_start_x = 46;
drone_start_y = -86;
max_velocity = 20;
fov_radius = 30;
risk_scale = 1000;
```

Each chunk edit uses `old_string` `% NEXT_TIME` (or the matching sentinel) and
`new_string` of up to 75 values followed by the same sentinel.

Use an example as a shape guide. Replace every value with the current Mission's
snapshot-authorized evidence.

## Must Not Do

- Do not write an untyped assignment such as `max_velocity = 20;` in `model.mzn`. Declare every model parameter with its MiniZinc type, such as `int: max_velocity;`, and put the current value `max_velocity = 20;` in `data.dzn`.
- Do not assign the same parameter in both files. Keep declarations and reusable constraints in `model.mzn`; keep snapshot-authorized values in `data.dzn`.
- Do not use placeholders, representative samples, or truncated event lists. Encode every required record from the authorized planning context.
- Do not scale, round, or convert individual event values in data.dzn or in private reasoning. Copy them verbatim; conversions belong in model.mzn comprehensions.
- Do not repeat a rejected construct. Use the exact `correction_message` and `diagnostic_references` from `planner_executor` to repair the cited file and location.
- Do not assume environment keys such as `static_info`, `scene_graph`, `entities`, or `location` exist. Inspect the payload returned for this episode and map its actual structure into planner data.
- Do not copy example data values into a generated attempt. Examples provide MiniZinc structure; current values must come from the authorized planning context.
- Do not emit both complete planner files in one model response. Write and confirm `model.mzn` before generating `data.dzn` so each tool call remains bounded.

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
