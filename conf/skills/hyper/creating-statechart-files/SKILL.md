---
name: creating-statechart-files
description: Apply after planner execution returns an accepted planner-native artifact and exact Statechart workspace paths to author, inspect, submit, and repair schema-flexible execution semantics.
version: '3.3.0'
---

# Creating Statechart Files

## Procedure

1. Treat the exact `planner_native_plan_artifact_reference`, not the tool-call
   transcript, as the input. Keep its returned leading-`/` file-tool path and
   repository-relative execute path distinct.
2. For the Mission 1 event-information patrol, use these checked-in,
   parameterized helpers directly:
   `conf/skills/hyper/creating-statechart-files/examples/event-information-patrol/prepare_statechart.py`
   and
   `conf/skills/hyper/creating-statechart-files/examples/event-information-patrol/inspect_statechart.py`.
   Run
   `python conf/skills/hyper/creating-statechart-files/examples/event-information-patrol/prepare_statechart.py <planner-artifact> <shell-workspace>/generate_statechart.py <shell-workspace>/statechart.json`,
   then
   `python conf/skills/hyper/creating-statechart-files/examples/event-information-patrol/inspect_statechart.py <planner-artifact> <shell-workspace>/statechart.json`.
   Substitute the labeled paths returned by `planner_executor`; shell-quote paths
   containing whitespace. Do not copy or transcribe the helper, inspect the
   MiniZinc JSONL with an ad-hoc query, or author an inspection script. The same
   commands apply to initial planning and replacement revisions.
3. For any other planner shape, inspect and decode the artifact with `jq` or
   standard-library Python. Read
   `examples/event-information-patrol/generate_statechart.py` as a few-shot,
   author a mission-specific generator at the exact returned virtual location,
   and keep planner-output extraction separate from semantic-topology
   construction. Adapt extraction to the observed schema; do not introduce a
   production-owned planner schema.
4. Preserve every planner-selected item’s order, dependencies, parameters, timing, units, and identifiers in self-explanatory state or transition contexts. Describe desired operational outcomes and evidence; Maneuver Control chooses physical tools.
   Initial travel may be authorized at Mission time zero. Later travel becomes
   eligible when the prior planner evidence interval ends; authoritative maneuver
   lifecycle feedback triggers Maneuver Control immediately, without rounding
   planner timing to the periodic heartbeat cadence. Keep observation start and
   duration distinct from travel timing.
   Record candidate identity, `fixed_view` or `pursue_ship` mode, numeric target
   entity where applicable, opaque report identities, observation window, and
   the recall/estimation/omission utility breakdown. Maneuver Control chooses
   `navigate` or `pursue` and its adapter parameters at runtime.
   Continuous sensing needs no maneuver-owned sensing state; represent a
   planner assignment with movement followed by its evidence-ready outcome.
5. Assert that every extracted planner item is represented exactly once. Generate `statechart.json` at the exact returned location and print a compact manifest containing planner-item coverage, order, state/edge counts, and terminal completion.
6. For Mission 1, require both helper manifests to agree and the inspector to
   report `valid: true`. For other planner shapes, run the authored generator and
   inspect both authored files and its manifest. Do not read a large generated
   Statechart into model context. Repair the same files until their contents and
   manifest agree with the planner artifact.
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
