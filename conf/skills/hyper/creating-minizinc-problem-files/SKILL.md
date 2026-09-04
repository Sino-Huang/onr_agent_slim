---
name: creating-minizinc-problem-files
description: Apply after MiniZinc is selected to generate and repair planner-native model and data files from current Mission evidence.
version: '2.10.0'
---

# Creating MiniZinc Problem Files

## Choose the generation route

After `record_planning_intent`, inspect the returned environment file with
`execute` and `jq 'keys' <file>`. Follow the discovered containers with further
`jq` key and representative-record queries until the event collection, event
identity/type/time/position, drone collection/identity/location/velocity/FoV,
and their nesting are explicit. Inspect the event-type distribution and match
each event entity to the supplied belief marginals. Preserve identifiers,
order, units, and scales.

Use one independent inspection per `execute` call so its exit status belongs
to that query; connect genuinely dependent shell operations with `&&`. For the
Mission 1 physical shape, sample report streams with
`jq '.world_model_info.ship_event_reports | to_entries | .[0]' <environment-file>`
and read spaced keys with
`jq -r '.static_info[]["event type"]' <environment-file>`.

- A Mission 1 reliability patrol that chooses fixed views and ship pursuit
  uses the code-owned candidate-DAG route below.
- Any other MiniZinc formulation uses the generic route.

## Mission 1 reliability candidate DAG

Read [Mission 1 mixed-action reference](references/mission1-mixed-action.md)
before generating or interpreting this planner shape. Then:

1. Use these checked-in, parameterized helpers at their stable
   repository-relative execute paths:
   `conf/skills/hyper/creating-minizinc-problem-files/examples/event-information-patrol/inspect_inputs.py`,
   `conf/skills/hyper/creating-minizinc-problem-files/examples/event-information-patrol/prepare_problem.py`,
   and
   `conf/skills/hyper/creating-minizinc-problem-files/examples/event-information-patrol/inspect_problem.py`.
   The paired assets are
   `conf/skills/hyper/creating-minizinc-problem-files/examples/event-information-patrol/model.mzn`,
   `conf/skills/hyper/creating-minizinc-problem-files/examples/event-information-patrol/data.dzn`,
   and
   `conf/skills/hyper/creating-minizinc-problem-files/examples/event-information-patrol/generate_data.py`.
2. Use the environment and belief execute paths returned by
   `record_planning_intent` directly. Do not copy, rewrite, or transcribe the
   reporting-reliability JSON into the numbered workspace. Do not reinterpret
   `detected_issues` or raw Event Observations as updates. The current evidence
   paths own every mission-specific value.
3. Keep the returned path forms distinct: leading-`/` virtual paths belong in
   `write_file`, `read_file`, `edit_file`, `submit_planner_attempt`, and
   `planner_executor`; repository-relative execute paths belong in shell commands.
   The execute backend already starts at the repository root and provides the
   activated `onr` Python. First run the compact input inspection, substituting the two labeled execute paths verbatim:
   `python conf/skills/hyper/creating-minizinc-problem-files/examples/event-information-patrol/inspect_inputs.py <environment-file> <belief-file>`.
   Then create both returned planner files in one command:
   `python conf/skills/hyper/creating-minizinc-problem-files/examples/event-information-patrol/prepare_problem.py <environment-file> <belief-file> <shell-workspace>/model.mzn <shell-workspace>/data.dzn`.
   Validate the generated DZN through its compact inspector:
   `python conf/skills/hyper/creating-minizinc-problem-files/examples/event-information-patrol/inspect_problem.py <shell-workspace>/data.dzn`.
   Shell-quote paths containing whitespace. The execute backend starts at the
   repository root with the `onr` Python activated; the working directory remains the repository root.
   Use one independent inspection per `execute` call. Do not author an ad-hoc inspection script. Do not read the generated
   DZN into model context. The same commands apply to initial planning and
   every replacement revision.
4. Require `valid: true` from `inspect_problem.py`. Inspect the preparation JSON manifest
   for candidate counts by mode, pursuit ship IDs and risk/rate inputs,
   selected advisory modes, component-score consistency, arc count, advisory
   score/maneuvers/duration, `covered_report_count`, and unique covered report
   IDs. Copy its scalar values into the submission reflection. On a replacement,
   also confirm newer Mission time and belief input revision and the absence of
   expired or checked report IDs.
5. Call `submit_planner_attempt` with `planner_choice: "minizinc"`, the exact
   `model.mzn` path as scalar `model_path`, and the exact `data.dzn` path as
   scalar `data_path`. Do not put the paths in an array or a quoted array.
   After static acceptance, call
   `planner_executor` with `minizinc_solver: "coin-bc"`.

This route bypasses `initialize_event_data_materialization` and
`materialize_event_information_data`.

## Generic MiniZinc route

1. Put reusable declarations and constraints in `model.mzn`; put current
   authorized values in `data.dzn`. Assign each parameter in one file only.
2. For event-indexed arrays, write `model.mzn`, then call
   `initialize_event_data_materialization` with the exact `jq` event count and
   fields containing exactly `target`, `dzn_type`, and `normalization`.
   Normalization is `identity` or `first_seen_index`.
3. For each returned `next_batch`, run one `jq` slice that returns records with
   exactly `event_number` and `event`, then immediately call
   `materialize_event_information_data` with that output. Wait for accepted
   progress before reading the next slice. A target mapping is a non-empty path
   of string object keys and integer array indices; later batches may use
   different paths when the raw schema differs.
4. After materialization completes, add planner-authored and belief-derived
   non-event assignments to `data.dzn`. For models without event-indexed
   arrays, write `data.dzn` directly.
5. Submit the exact returned model/data paths as scalar `model_path` and
   `data_path`. After static acceptance, call
   `planner_executor` with a solver: `coin-bc` for linear/integer-flow models,
   `highs` as the secondary linear-MIP option, or `gecode` for CP models.

## Repair and evidence

Completion requires successful MiniZinc instance checking and planner
execution. On failure, call `write_todos` for the returned todo rollback, use
`edit_file` on the same files,
and resubmit the same submitted files at the same paths. Treat verifier and executor diagnostics as the
repair authority while preserving unaffected evidence. Use `restart: true`
only to intentionally discard a generic materialization and its accepted rows.
On success, retain the exact solver-native plan text for Statechart generation.

Planner files, the retained generator, and solver output are planning
artifacts, not Mission authority. Execution semantics begin only after the
accepted Statechart/FSM interprets the planner-native plan.
