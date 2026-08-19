---
name: creating-minizinc-problem-files
description: Apply after MiniZinc is selected to generate planner-native model and data files from Mission Intent and the current Hyper heartbeat snapshot.
version: '1.1.0'
---

# Creating MiniZinc Problem Files

## Procedure

1. Keep the Hyper todo list explicit: parse Mission Intent into PlanningIntent, decide the planner, then generate planner problem files.
2. Read the raw Mission Intent, accepted PlanningIntent, and the current Hyper heartbeat MissionSnapshot. Resolve operational facts only through the snapshot-referenced Operational Scene Graph and other authorized evidence.
3. Separate reusable constraints into model.mzn and current authoritative values into data.dzn. Preserve supplied entity IDs, positions, event times, risk values, units, and mission limits.
4. Scale finite decimal risk values to integers once and record the scale in data.dzn; an explicit code-owned conversion keeps the objective reproducible.
5. Produce one attempt-specific asset set. Record its translator identity/version, snapshot ID, references, SHA-256 values, and accepted or rejected outcome through the planning-evidence path.
6. When `correction_feedback` is present, treat its sanitized validation stage and message as the complete diagnosis. Generate a fresh asset set that corrects that failure within the runtime's retry bound.
7. Use the same maneuver IDs in MiniZinc output and the normalization template. Emit only the `assignments` JSON object expected by the independent solution checker.
8. Complete the generation todo only after static validation succeeds, MiniZinc reports an optimal solution, the independent solution checker accepts it, and every generated datum is traceable to Mission Intent or snapshot evidence.

## Few-shot example

Read examples/risk-weighted-fov/model.mzn with
examples/risk-weighted-fov/data.dzn. The pair models the scene-graph shape
emitted by FakeEnvironment: five ships have positions and supplied risk
values, while the drone has a starting position. Event times and movement/FoV
limits come from authorized mission inputs. The example is a shape guide;
replace every value with the current Mission's evidence.

The example's normalization template has one abstract maneuver named
`risk-weighted-observation`, starting at zero and lasting for the complete
horizon. Its MiniZinc output uses that exact ID. A merely satisfiable result is
incomplete; only a verified `OPTIMAL_SOLUTION` can produce a Normalized Plan.


## Authority boundary

Planner files are attempt artifacts, not Mission authority and not
PlanningIntent.details. A generated file becomes usable only after the
translation slice records it as accepted; rejected attempts remain auditable.
