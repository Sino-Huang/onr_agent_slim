---
name: mission-parsing
description: Apply when deriving PlanningIntent from MissionInput while preserving source authority.
version: '1.2.0'
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

## Example: ships risk-weighted FoV coverage

`data/ships_report_and_trajectory_example/ships/events_report.json` contains
ship event records with `time`, `position`, `event information`, `event type`,
and `entity_id`. A mission to maximize field-of-view observation coverage
weighted by per-ship risk is temporal optimization, so select MiniZinc under
the `planner-selection` rule. Risk scores must be supplied in MissionInput or
obtained by an explicit code-owned derivation; do not invent them.

## Durable Context

- The agent decides which useful, durable mission facts merit retention.
- Store those facts only in Hyper's isolated per-Mission durable memory namespace.
- Never write them to shared writable memory, another Mission namespace, or hidden Mission 1 ground-truth storage.
- Memory is context only. It must never replace or modify PlanningIntent, plans, scene/belief, lifecycle, or FSM execution artifacts.
- V1 has no standalone memory-hygiene skill; apply these carry-forward rules here.

## Gotchas

- This skill is read-only guidance, not authority. Raw MissionInput and operator Mission Intent remain source authority; PlanningIntent is a derived planner-facing interpretation.
- Never use placeholders such as `MISSION_INPUT_ID` or `SOURCE_AUTHORITY_ID`.
- Do not infer hidden ground truth or carry transient observations forward as durable facts.
