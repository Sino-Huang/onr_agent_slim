---
name: creating-minizinc-problem-files
description: Apply after MiniZinc is selected to generate and repair planner-native model and data files from current Mission evidence.
version: '2.8.2'
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

1. Read
   `/conf/skills/hyper/creating-minizinc-problem-files/examples/event-information-patrol/model.mzn`
   and
   `/conf/skills/hyper/creating-minizinc-problem-files/examples/event-information-patrol/generate_data.py`
   completely; these stable reference locations replace filesystem discovery.
   The corresponding relative paths are
   `examples/event-information-patrol/model.mzn` and
   `examples/event-information-patrol/generate_data.py`. The
   checked-in `examples/event-information-patrol/data.dzn` is its current demo
   result. `replan-environment.json`, `replan-belief.json`, and
   `replan-data.dzn` show the same formulation rematerialized after Mission
   time advances and report checks update the posterior; example values are teaching values only.
   The current evidence owns every mission-specific value. For each source file,
   call `write_file` with its contents and the
   corresponding `model.mzn` or `generate_data.py` path in the returned
   numbered workspace.
   When the Mission Snapshot already names an active positive plan revision,
   write a fresh copy of every planner file in the newly returned revision
   workspace. The previous revision's files are immutable evidence, never a
   repair workspace. Begin with the `write_file` calls: the first call creates
   the numbered workspace and its parent directories.
2. Preserve the current reporting-reliability snapshot as `belief.json` in the
   workspace. Do not reinterpret `detected_issues` or raw Event Observations as
   updates. The example generator calls the installed code-owned builder used
   by Context Coordination's advisory oracle. It excludes expired, checked,
   duplicated, unreachable opportunities and emits both `fixed_view` and
   `pursue_ship` candidates with opaque report IDs, numeric ship IDs, timing,
   and recall/estimation/omission utility components.
   It uses the current vehicle FoV and maximum velocity without capability caps.
3. Keep the returned path forms distinct: leading-`/` virtual paths belong in
   `write_file`, `read_file`, `edit_file`, `submit_planner_attempt`, and
   `planner_executor`; repository-relative execute paths belong in shell commands.
   The execute backend already starts at the repository root. Start the
   workspace command directly with the activated `onr` Python:
   `python <workspace>/generate_data.py <environment-file> <workspace>/belief.json <workspace>/data.dzn`.
   Build this command from the labeled shell workspace and execute environment
   path returned by `record_planning_intent`. The working directory remains the repository root
   for generation and DZN inspection, and every returned shell path is passed
   unchanged relative to that root.
   Python is one-run preprocessing because DZN cannot calculate and reduce this
   action graph. The script is an agent-authored planning artifact, not a
   production compiler; leave it in the numbered workspace.
4. Inspect the script's JSON manifest before submission. It must report
   candidate and arc counts, advisory score, maneuver count, duration,
   `covered_report_count`, and unique covered report IDs. Copy its scalar values
   verbatim into the submission reflection; `covered_report_count` is the
   authoritative array count. Confirm that `data.dzn` has aligned candidate and arc
   arrays, forward topological arcs, a source-to-sink route, nondecreasing
   `outgoing_start` and `incoming_start` offsets ending at `arc_count + 1`, an
   `incoming_edge` permutation of every arc, and manifest-consistent counts.
   Repeated offsets are valid empty adjacency windows; validate incoming
   windows through the `incoming_edge` indirection.
   For a replacement revision, also confirm the environment Mission time and
   belief input revision are newer than the prior plan inputs and that expired
   and checked report IDs are absent from every candidate report array.
   Generation is complete only when `model.mzn`, `generate_data.py`, and
   `data.dzn` all exist and the manifest is coherent.
5. Call `submit_planner_attempt` with `planner_choice: "minizinc"`, the exact
   `model.mzn` path as scalar `model_path`, and the exact `data.dzn` path as
   scalar `data_path`. Do not put the paths in an array or a quoted array.
   After static acceptance, call
   `planner_executor` with `minizinc_solver: "coin-bc"`. This sparse unit-flow
   model is linear integer flow and its incidence indexes prevent MiniZinc from
   scanning every arc separately for every node during flattening.

MiniZinc maximizes combined utility, then minimizes maneuver count and total
surveillance duration. This route bypasses `initialize_event_data_materialization` and
`materialize_event_information_data`; the mission-specific generator owns the
complete schema adaptation and DAG construction.

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
