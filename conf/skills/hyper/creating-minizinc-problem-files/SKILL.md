---
name: creating-minizinc-problem-files
description: Apply after MiniZinc is selected to generate and repair planner-native model and data files from current Mission evidence.
version: '2.0.0'
---

# Creating MiniZinc Problem Files

## Procedure

1. Read the raw Mission Intent and the exact environment data and belief marginals returned by `record_planning_intent`. Inspect their current structure and preserve evidence values, identifiers, order, units, and scales.
2. Put reusable declarations and constraints in `model.mzn`; put current authorized values in `data.dzn`. Assign each parameter in one file only.
3. Write both files at the exact sandbox paths returned by `record_planning_intent`.
4. Call `submit_planner_attempt` with `planner_choice: "minizinc"`, the exact returned `model.mzn` and `data.dzn` paths, and reflection. Static completion requires `status: success` from MiniZinc instance checking.
5. Call `planner_executor` with the same planner choice and exact paths. Execution completion requires `status: success` and returns the exact MiniZinc solver-native plan text plus its artifact reference.
6. On either failure, call `write_todos` as instructed, use `edit_file` on the same submitted files, and resubmit those same paths. The Hyper Agent remains the todo owner.

## Generation discipline

- Copy current evidence values without sampling, truncating, sorting, deduplicating, or inventing values.
- Put scaling and rounding in MiniZinc expressions, not in copied data values.
- Keep files complete and compact. Use an example only for structure; replace its values with current evidence.
- A verifier or executor diagnostic is the repair authority. Change the cited construct and preserve unaffected evidence.

## Examples

- For event-accounting patrols, read `examples/event-information-patrol/model.mzn` and `examples/event-information-patrol/data.dzn`.
- For one risk-weighted observation interval, read `examples/risk-weighted-fov/model.mzn` and `examples/risk-weighted-fov/data.dzn`.

## Authority boundary

Planner files and solver output are planning artifacts, not Mission authority. The externally successful solver output remains planner-native and is interpreted into execution semantics only by the accepted Statechart/FSM.
