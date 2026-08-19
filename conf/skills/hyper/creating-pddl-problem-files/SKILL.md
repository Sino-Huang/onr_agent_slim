---
name: creating-pddl-problem-files
description: Apply after Fast Downward is selected to generate PDDL domain and problem files from Mission Intent and the current Hyper heartbeat snapshot.
version: '1.0.0'
---

# Creating PDDL Problem Files

## Procedure

1. Read Mission Intent, the accepted PlanningIntent, and the current MissionSnapshot. Resolve operational facts only through its referenced Operational Scene Graph and other authorized evidence.
2. Put reusable predicates, action preconditions/effects, and action costs in domain.pddl. Put current objects, initial predicates, and the goal in problem.pddl.
3. Use portable lowercase action names. Use the same action names and costs in the normalization template.
4. When correction_feedback is present, use its sanitized validation stage and message as the complete diagnosis and generate a fresh asset set within the runtime's retry bound.
5. Complete generation only after Fast Downward translates both files, returns a plan, independent VAL accepts that exact persisted domain/problem/plan set, and the code-owned action checker accepts every returned action.

## Few-shot example

Read examples/survey-return/domain.pddl and
examples/survey-return/problem.pddl. They encode symbolic reachability:
survey must complete before return-to-base, and timing has no effect on
feasibility or value. Replace all predicates, actions, objects, and goals with
facts traceable to the current Mission's evidence.

## Authority boundary

PDDL files and correction feedback are non-authoritative planning artifacts.
A Fast Downward plan remains non-executable until independent VAL and the
code-owned action checker both accept it.
