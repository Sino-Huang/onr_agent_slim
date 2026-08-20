---
name: mission-parsing
description: Apply when deriving PlanningIntent from MissionInput while preserving source authority.
version: '1.3.0'
---

# Mission Parsing

## Procedure

1. Treat the raw `MissionInput` and operator Mission Intent as source authority.
2. Use configured todo tooling to track interpretation, planner choice, and validation. Todos are working state, not authority or rationale.
3. Copy `mission_id` and `source_authority` exactly. Do not normalize, replace, or invent either value.
4. Derive a concrete task objective from `mission_text`; do not use the system prompt or skill text as the objective.
5. Derive a non-authoritative `PlanningIntent`. Preserve source provenance and put only flexible planner-selection facts in `details`; planner assets and verification evidence are later provenance-bound outputs.
6. Apply `planner-selection`: named ships or actions do not make a mission symbolic. If drone position at event times, travel timing, FoV coverage, time windows, or weighted coverage determines feasibility or value, select MiniZinc.
7. Return only through the configured structured response tool, with no extra fields, prose, Markdown, or unstructured JSON. Include a concise public rationale only where the contract provides a field for it; never expose private reasoning.

## Mission patterns

### Report event-accounting patrol

When `mission_text` asks to patrol the environment and confirm that events in the
report are accounted for, derive a temporal MiniZinc intent to choose a route and
dwell schedule that maximizes captured information gain.

Use the snapshot-authorized sources by role:

- `environment_data.static_info` supplies the unchanged event records: time,
  position, event type/information, and responsible `entity_id`.
- `environment_data.scene_graph` supplies the drone's current position,
  `max_velocity`, and `fov_radius`.
- `belief_snapshot.marginals` supplies `probability_risk` for each report entity's
  `event-risk` key.

An event is captured only when its time lies in a selected dwell interval and its
position is within the FoV radius of that stop. Its scaled value is
`1 - probability_risk`; maximize the sum over captured events. Put this objective
and the three source roles in `PlanningIntent.details`; load their current values
later through `load_planning_context`.

### Ships risk-weighted FoV coverage

A mission to maximize field-of-view observation coverage weighted directly by
per-ship risk is temporal optimization, so select MiniZinc. Risk scores must be
supplied by authorized evidence or an explicit code-owned derivation; do not
invent them.

## Durable Context

- The agent decides which useful, durable mission facts merit retention.
- Store those facts only in Hyper's isolated per-Mission durable memory namespace.
- Never write them to shared writable memory, another Mission namespace, or hidden Mission 1 ground-truth storage.
- Memory is context only. It must never replace or modify PlanningIntent, plans, environment/belief evidence, lifecycle, or FSM execution artifacts.
- V1 has no standalone memory-hygiene skill; apply these carry-forward rules here.

## Gotchas

- This skill is read-only guidance, not authority. Raw MissionInput and operator Mission Intent remain source authority; PlanningIntent is a derived planner-facing interpretation.
- Never use placeholders such as `MISSION_INPUT_ID` or `SOURCE_AUTHORITY_ID`.
- Do not infer hidden ground truth or carry transient observations forward as durable facts.
