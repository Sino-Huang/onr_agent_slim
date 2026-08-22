---
name: creating-minizinc-problem-files
description: Apply after MiniZinc is selected to generate and repair planner-native model and data files from current Mission evidence.
version: '2.3.0'
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

- An event-information patrol that maximizes risk-weighted FoV observations
  uses the action-DAG route below.
- Any other MiniZinc formulation uses the generic route.

## Event-information action DAG

1. Read `examples/event-information-patrol/model.mzn` and
   `examples/event-information-patrol/generate_data.py` completely. The
   checked-in `examples/event-information-patrol/data.dzn` is its current demo
   result; example values are teaching values only and current evidence owns
   every mission-specific value. For each source file, call `write_file` with its contents and the
   corresponding `model.mzn` or `generate_data.py` path in the returned
   numbered workspace.
2. In the workspace generator, replace the example belief marginals with the
   current supplied marginals and adapt the schema-extraction functions to the
   paths proved by the current `jq` output. Keep the schema-neutral graph and
   DZN section stable: MiniZinc rounding and risk scaling, intersection
   discovery, observation-interval enumeration, equivalent-action
   deduplication, source/action/sink reachability, transitive reduction, the
   independent longest-path oracle, and DZN serialization.
3. Run the workspace script with the minimal shell Python:
   `/usr/bin/python3 <workspace>/generate_data.py <environment-file> <workspace>/data.dzn`.
   Python is one-run preprocessing because DZN cannot calculate and reduce this
   action graph. The script is an agent-authored planning artifact, not a
   production compiler; leave it in the numbered workspace.
4. Inspect the script's JSON manifest before submission. It must report
   source-event, intersection, raw-action, unique-action, full-arc,
   reduced-arc, longest-route, optimum-gain, and optimum-stop counts. Confirm
   that `data.dzn` has aligned action and arc arrays, forward topological arcs,
   a source-to-sink route, and manifest-consistent counts. Generation is
   complete only when `model.mzn`, `generate_data.py`, and `data.dzn` all exist
   and the manifest is coherent.
5. Call `submit_planner_attempt` with `planner_choice: "minizinc"` and exactly
   `model.mzn` and `data.dzn`. After static acceptance, call
   `planner_executor` with `minizinc_solver: "coin-bc"`. This
   `network_flow_cost` model is linear integer flow; `highs` is the secondary
   linear-MIP choice. Reserve `gecode` for CP-oriented models.

This route bypasses `initialize_event_data_materialization` and
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
5. Submit the exact returned model/data paths. After static acceptance, call
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
