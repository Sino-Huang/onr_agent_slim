---
name: creating-minizinc-problem-files
description: Apply after MiniZinc is selected to generate and repair planner-native model and data files from current Mission evidence.
version: '2.1.0'
---

# Creating MiniZinc Problem Files

## Procedure

1. Read the raw Mission Intent and the exact environment data and belief marginals returned by `record_planning_intent`. Inspect their current structure and preserve evidence values, identifiers, order, units, and scales.
2. Put reusable declarations and constraints in `model.mzn`; put current authorized values in `data.dzn`. Assign each parameter in one file only.
3. Write `model.mzn` at the exact returned path and wait for the successful write result. When the model consumes event-indexed arrays, call `initialize_event_data_materialization` with their exact total count and field objects containing exactly `target`, `dzn_type`, and `normalization`; normalization is `identity` or `first_seen_index`. Submit the raw records in contiguous batches of at most 25 through `materialize_event_information_data`; each batch record contains exactly `event_number` and `event`, and every declared target maps to a non-empty list of string object keys and integer array indices. A later batch may use different paths when its raw schema differs.
4. After materialization completes, read the generated `model.mzn` and `data.dzn`, then add the missing planner-authored and belief-derived non-event assignments to `data.dzn` with `edit_file`. For models without event-indexed arrays, write `data.dzn` directly. Exit generation only when both files are complete.
5. Call `submit_planner_attempt` with `planner_choice: "minizinc"`, the exact returned `model.mzn` and `data.dzn` paths, and reflection. Static completion requires `status: success` from MiniZinc instance checking.
6. Call `planner_executor` with the same planner choice and exact paths. Execution completion requires `status: success` and returns the exact MiniZinc solver-native plan text plus its artifact reference.
7. On either failure, call `write_todos` as instructed, use `edit_file` on the same submitted files, and resubmit those same paths. The Hyper Agent remains the todo owner.

## Generation discipline

- Copy current evidence values without sampling, truncating, sorting, deduplicating, or inventing values.
- Treat identical records at different event numbers as separate evidence. Let the materializer own event ordering, aligned array lengths, DZN scalar serialization, and stable first-seen categorical indices.
- Use `restart: true` only to intentionally discard every accepted event row and restart with new field definitions. Submission remains unavailable until the restarted materialization completes.
- Put scaling and rounding in MiniZinc expressions, not in copied data values.
- Keep files complete and compact. Use an example only for structure; replace its values with current evidence.
- A verifier or executor diagnostic is the repair authority. Change the cited construct and preserve unaffected evidence.

## Examples

- For event-accounting patrols, read `examples/event-information-patrol/model.mzn` and `examples/event-information-patrol/data.dzn`.
- For one risk-weighted observation interval, read `examples/risk-weighted-fov/model.mzn` and `examples/risk-weighted-fov/data.dzn`.

## Authority boundary

Planner files and solver output are planning artifacts, not Mission authority. The externally successful solver output remains planner-native and is interpreted into execution semantics only by the accepted Statechart/FSM.
