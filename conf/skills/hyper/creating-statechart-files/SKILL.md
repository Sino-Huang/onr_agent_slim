---
name: creating-statechart-files
description: Apply after planner execution returns an accepted planner-native artifact and exact Statechart workspace paths to author, inspect, submit, and repair schema-flexible execution semantics.
version: '3.1.0'
---

# Creating Statechart Files

## Procedure

1. Inspect and decode the exact `planner_native_plan_artifact_reference` with `jq` or standard-library Python. Treat the artifact, not the tool-call transcript, as the input.
2. Read `examples/event-information-patrol/generate_statechart.py` as a few-shot. Author a mission-specific `generate_statechart.py` at the exact location returned by `planner_executor`.
3. Keep two sections obvious in the generator: planner-output extraction and semantic-topology construction. Adapt extraction to the observed artifact schema; do not introduce a production-owned planner schema.
4. Preserve every planner-selected item’s order, dependencies, parameters, timing, units, and identifiers in self-explanatory state or transition contexts. Describe desired operational outcomes and evidence; Maneuver Control chooses physical tools.
   Initial travel may be authorized at Mission time zero. Later travel becomes
   eligible when the prior planner evidence interval ends; authoritative maneuver
   lifecycle feedback triggers Maneuver Control immediately, without rounding
   planner timing to the periodic heartbeat cadence. Keep observation start and
   duration distinct from travel timing.
   Record planner-derived outcome facts such as location, arrival timing,
   source-event identity, expected evidence, and observation timing. Maneuver
   Control chooses the physical action and its adapter parameters at runtime.
   Continuous sensing needs no maneuver-owned sensing state; represent a
   planner assignment with movement followed by its evidence-ready outcome.
5. Assert that every extracted planner item is represented exactly once. Generate `statechart.json` at the exact returned location and print a compact manifest containing planner-item coverage, order, state/edge counts, and terminal completion.
6. Run the generator. Inspect both authored files and the printed manifest. Repair the same files until their contents and manifest agree with the planner artifact.
7. Call `submit_statechart_draft` with the exact returned `statechart_file_location`. On rejection, use the structured diagnostic to edit, rerun, inspect, and resubmit those same files.
8. Completion requires verifier acceptance and successful `python-statemachine` construction.

## Draft contract

The draft contains exactly `entry_state`, `terminal_states`, `states`, `state_context`, and `transitions`.

- `states` is a non-empty array of unique state-ID strings.
- `entry_state` and every explicit terminal state are declared.
- `state_context` maps every declared state to one arbitrary finite JSON object.
- Every transition contains exactly `event`, `source`, `target`, and `context`.
- Transition events and `(source, target)` pairs are unique. Every transition context is an arbitrary finite JSON object.
- Every state is reachable from entry and can reach a terminal state.

Context vocabulary belongs to this generator. Use nested objects and names that explain meaning, units, planner provenance, desired outcomes, and readiness evidence without relying on state or event names.

Observation confirmation may use both the end of the planner evidence interval
and matching pending sensor evidence. Sensor capture is continuous and is not
started, stopped, or scoped by a physical maneuver.

## Repair boundary

- `workspace_path`: submit the exact path returned by `planner_executor`; keep the generator and draft there.
- `schema`: repair JSON shape, context coverage, references, event uniqueness, reachability, or terminal paths.
- `machine_build`: repair topology that the dynamic `python-statemachine` engine cannot construct.

Every submission is snapshotted. A rejection’s `required_next_action` identifies the same live files to repair; repeating an unchanged rejected draft spends another attempt.
