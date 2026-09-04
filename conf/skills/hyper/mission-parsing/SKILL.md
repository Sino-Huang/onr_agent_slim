---
name: mission-parsing
description: Apply when deriving PlanningIntent from MissionInput while preserving source authority.
version: '1.7.0'
---

# Mission Parsing

## Procedure

1. Treat the raw `MissionInput` and operator Mission Intent as source authority.
2. Let `record_planning_intent` derive `mission_id` and `source_authority` from workflow context. Do not add either to the tool call or invent replacements.
3. Derive a concrete task objective from `mission_text`; do not use the system prompt or skill text as the objective.
4. Derive a non-authoritative `PlanningIntent`. Put only flexible planner-selection facts in `details`; use keys such as `mission_pattern`, `capture_rule`, `value_rule`, and `source_roles`. Planner files and verification evidence are later translation outputs.
5. Apply `planner-selection`: named ships or actions do not make a mission symbolic. If drone position at event times, travel timing, FoV coverage, time windows, or weighted coverage determines feasibility or value, select MiniZinc.
6. Call `record_planning_intent` with the objective, planner choice, rationale, details, and concise public reflection. Its acceptance immediately supplies the exact evidence and MiniZinc or PDDL file locations selected by that Planner Choice.

## Mission patterns

### Report event-accounting patrol

When `mission_text` asks to patrol the environment and confirm that events in the
report are accounted for, derive a temporal MiniZinc intent to choose a route and
surveillance route that balances issue-discovery recall with corruption-rate
estimation quality.

Record the operational sources by logical role, without predicting their field names or nesting before `record_planning_intent` returns the current payload:

- event-report evidence supplies event time, position, type/information, and the
  identifier needed to join related evidence;
- vehicle-state evidence supplies the drone's current position and movement and
  sensing capabilities;
- belief evidence supplies the applicable entity-risk estimate.

Resolve those logical roles against the returned environment and belief structures.
Do not put example JSON paths in
`PlanningIntent.details` as if they were a stable environment interface.

An opportunity is covered only once when its time and position are feasible for
a selected fixed view or its vessel is pursued through the evidence window.
Use the code-owned utility: half posterior corruption mean and half normalized
expected posterior variance reduction, plus expected hidden-omission yield for
pursuit. Put the coverage rule, utility rule, mission pattern, and three logical source roles in
`PlanningIntent.details`; keep the objective only in the top-level `objective`
field. Use current values only after the intent is accepted.

### Ships risk-weighted FoV coverage

A mission to maximize field-of-view observation coverage weighted directly by
per-ship risk is temporal optimization, so select MiniZinc. Risk scores must be
supplied by authorized evidence or an explicit code-owned derivation; do not
invent them.

## Gotchas

- Never use placeholders such as `MISSION_INPUT_ID` or `SOURCE_AUTHORITY_ID`.
- Keep `schema_version`, `mission_id`, `source_authority`, `objective`, `rationale`, `planner_choice`, and `details` out of `PlanningIntent.details`; these names are reserved top-level fields.
- Do not infer hidden ground truth or carry transient observations forward as durable facts.
- Use `event_report_checks` once as reliability evidence; never double-count
  its correlated `detected_issues` entries or raw Event Observations.
