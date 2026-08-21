---
name: creating-pddl-problem-files
description: Apply after Fast Downward is selected to generate PDDL domain and problem files from Mission Intent and the current Hyper heartbeat snapshot.
version: '1.3.0'
---

# Creating PDDL Problem Files

## Procedure

1. Read Mission Intent, the accepted PlanningIntent, and the current MissionSnapshot. Resolve operational facts through every relevant key in its snapshot-authorized flexible `environment_data`.
2. Put reusable predicates, action preconditions/effects, and action costs in domain.pddl. Put current objects, initial predicates, and the goal in problem.pddl.
3. Use portable lowercase action names. Use the same action names and costs in the normalization template.
4. When `correction_feedback` is present, read its exact VAL diagnostic and references. Generate a fresh asset set that corrects the cited failure within the runtime's retry bound.
5. Complete generation only after VAL accepts the exact domain/problem pair, Fast Downward returns a plan, VAL independently accepts that exact persisted domain/problem/plan set, and the code-owned action checker accepts every returned action.

## Few-shot example

Read examples/survey-return/domain.pddl and
examples/survey-return/problem.pddl. They encode symbolic reachability:
survey must complete before return-to-base, and timing has no effect on
feasibility or value. Replace all predicates, actions, objects, and goals with
facts traceable to the current Mission's evidence.

## Must Not Do

- Do not reference undeclared predicates, actions, types, constants, or objects. Declare reusable symbols in `domain.pddl` and current Mission objects in `problem.pddl`.
- Do not mix current initial facts or the current goal into `domain.pddl`. Keep reusable action semantics in the domain and snapshot-authorized state in the problem.
- Do not let the problem's `:domain` name differ from the domain declaration, and do not use requirements unsupported by the configured Fast Downward translator.
- Do not repeat a rejected construct. Use the exact correction message and diagnostic references to repair the cited file and parser location.

## Authority boundary

PDDL files and correction feedback are non-authoritative planning artifacts.
A Fast Downward plan remains non-executable until independent VAL and the
code-owned action checker both accept it.
