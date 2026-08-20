---
name: mission-parsing
description: Apply when deriving PlanningIntent from MissionInput while preserving source authority.
version: '1.6.0'
---

# Mission Parsing

## Procedure

1. Treat the raw `MissionInput` and operator Mission Intent as source authority.
2. Copy `mission_id` and `source_authority` exactly. Do not normalize, replace, or invent either value.
3. Derive a concrete task objective from `mission_text`; do not use the system prompt or skill text as the objective.
4. Derive a non-authoritative `PlanningIntent`. Preserve source provenance and put only flexible planner-selection facts in `details`; use keys such as `mission_pattern`, `capture_rule`, `value_rule`, and `source_roles`. Planner assets and verification evidence are later provenance-bound outputs.
5. Apply `planner-selection`: named ships or actions do not make a mission symbolic. If drone position at event times, travel timing, FoV coverage, time windows, or weighted coverage determines feasibility or value, select MiniZinc.
6. Return only through the configured structured response tool, with no extra fields, prose, Markdown, or unstructured JSON. Include a concise public rationale only where the contract provides a field for it; never expose private reasoning.

## Mission patterns

### Report event-accounting patrol

When `mission_text` asks to patrol the environment and confirm that events in the
report are accounted for, derive a temporal MiniZinc intent to choose a route and
dwell schedule that maximizes captured information gain.

Record the snapshot-authorized sources by logical role, without predicting their
field names or nesting before `load_planning_context` returns the current payload:

- event-report evidence supplies event time, position, type/information, and the
  identifier needed to join related evidence;
- vehicle-state evidence supplies the drone's current position and movement and
  sensing capabilities;
- belief evidence supplies the applicable entity-risk estimate.

Resolve those logical roles against the actual environment and belief structures
only after loading the current planning context. Do not put example JSON paths in
`PlanningIntent.details` as if they were a stable environment interface.

An event is captured only when its time lies in a selected dwell interval and its
position is within the FoV radius of that stop. Its scaled value is
`1 - probability_risk`; maximize the sum over captured events. Put the capture
rule, value rule, mission pattern, and three logical source roles in
`PlanningIntent.details`; keep the objective only in the top-level `objective`
field. Load current values later through `load_planning_context`.

### Ships risk-weighted FoV coverage

A mission to maximize field-of-view observation coverage weighted directly by
per-ship risk is temporal optimization, so select MiniZinc. Risk scores must be
supplied by authorized evidence or an explicit code-owned derivation; do not
invent them.

## Gotchas

- Never use placeholders such as `MISSION_INPUT_ID` or `SOURCE_AUTHORITY_ID`.
- Keep `schema_version`, `mission_id`, `source_authority`, `objective`, `rationale`, `planner_choice`, `mission_input_sha256`, and `details` out of `PlanningIntent.details`; these names are reserved top-level fields.
- Do not infer hidden ground truth or carry transient observations forward as durable facts.
