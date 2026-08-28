---
name: creating-pddl-problem-files
description: Apply after Fast Downward is selected to generate and repair PDDL domain and problem files from current Mission evidence.
version: '2.0.0'
---

# Creating PDDL Problem Files

## Procedure

1. Read Mission Intent, the accepted Planning Intent, and the current snapshot-authorized environment and belief evidence.
2. Put reusable predicates plus action preconditions and effects in `domain.pddl`. Put current objects, initial predicates, and the goal in `problem.pddl`.
3. Write both files at the exact sandbox paths returned by `record_planning_intent`.
4. Call `submit_planner_attempt` with `planner_choice: "fast-downward"`, the exact returned `domain.pddl` path as scalar `model_path`, the exact `problem.pddl` path as scalar `data_path`, and reflection. Do not put the paths in an array or a quoted array. Static completion requires `status: success` from VAL's domain/problem check.
5. Call `planner_executor` with the same planner choice and identical scalar `model_path` and `data_path` values. Execution completion requires Fast Downward to produce `sas_plan` and VAL to accept that exact domain/problem/plan set; the tool returns the exact `sas_plan` text and artifact reference.
6. On either failure, call `write_todos` as instructed, use `edit_file` on the same submitted files, and resubmit those same paths. The Hyper Agent remains the todo owner.

## PDDL discipline

- Declare every predicate, action, type, constant, and object before use.
- Keep reusable action semantics in the domain and current state plus goal in the problem.
- Match the problem's `:domain` name to the domain declaration and use requirements supported by the configured Fast Downward translator.
- Use the exact VAL or Fast Downward diagnostic to repair the cited file without inventing new evidence.

## Example

Read `examples/survey-return/domain.pddl` and `examples/survey-return/problem.pddl` for symbolic reachability where timing does not affect feasibility or value. Replace its objects, facts, actions, and goal with current Mission evidence.

## Authority boundary

PDDL files and `sas_plan` are planning artifacts, not Mission authority. An accepted `sas_plan` remains planner-native and is interpreted into execution semantics only by the accepted Statechart/FSM.
