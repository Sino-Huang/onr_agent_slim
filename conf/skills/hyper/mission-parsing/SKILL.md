---
name: mission-parsing
description: Apply when deriving PlanningIntent from MissionInput while preserving source authority.
version: '1.12.0'
---

# Mission Parsing

## Procedure

1. Treat the raw `MissionInput` and operator Mission Intent as source authority.
2. Let `record_planning_intent` derive `mission_id` and `source_authority` from workflow context. Do not add either to the tool call or invent replacements.
3. Derive a concrete task objective from `mission_text`; do not use the system prompt or skill text as the objective.
4. Derive a non-authoritative `PlanningIntent`. Pass `details` as a JSON object,
   not serialized text. Put only flexible planner-selection facts there; use keys
   such as `mission_pattern`, `capture_rule`, `value_rule`, and `source_roles`.
   Planner files and verification evidence are later translation outputs.
5. When Mission Intent supplies prior knowledge, pass a versioned `prior_knowledge` object to `record_planning_intent`. Preserve qualitative claims and stated bounds exactly. The active belief service owns numeric prior derivation.
6. Apply `planner-selection`: named ships or actions do not make a mission symbolic. If drone position at event times, travel timing, FoV coverage, time windows, or weighted coverage determines feasibility or value, select MiniZinc.
7. Call `record_planning_intent` with the objective, planner choice, rationale, details, prior knowledge (use `null` when Mission Intent supplies none), and concise public reflection. Its acceptance immediately supplies the exact evidence and MiniZinc or PDDL file locations selected by that Planner Choice.

## Out-of-scope Mission Intent

A Mission is a bounded operational objective the controlled vehicles and
sensors in the supplied environment can execute (patrol, monitor, survey,
inspection, search, or rescue, with its constraints and desired outcome).
When the operator Mission Intent is not such an objective — personal errands,
general knowledge questions, or requests outside the environment's capabilities
— do not derive or record a PlanningIntent. Call `reject_mission_intent` once
with a concise public reason, then return the final result with outcome
`mission_rejected`. Rejection is terminal and available only before
`record_planning_intent` is accepted.

In scope: "Patrol and confirm every reported event is accounted for."
Out of scope: "buy me a coffee", "what is the weather today".

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

If Mission Intent says exactly one entity is anomalous and supplies a NED
area/time priority, record `schema_version: 1`, `belief_kind:
reporting_reliability`, and these general claims:

- `hypothesis_cardinality` with `hypothesis: anomalous_entity` and `count: 1`;
- `spatiotemporal_priority` with the stated Mission-time and north/east bounds.

Keep entity IDs and numeric Bayesian probabilities out unless Mission Intent
states them.

Use this exact claim shape, replacing only values stated by Mission Intent:

```json
{
  "schema_version": 1,
  "belief_kind": "reporting_reliability",
  "claims": [
    {
      "claim_kind": "hypothesis_cardinality",
      "parameters": {"hypothesis": "anomalous_entity", "count": 1}
    },
    {
      "claim_kind": "spatiotemporal_priority",
      "parameters": {
        "start_time_s": 160,
        "end_time_s": 300,
        "north_min_m": 800,
        "north_max_m": 1450,
        "east_min_m": -700,
        "east_max_m": -300
      }
    }
  ]
}
```

### Ships risk-weighted FoV coverage

A mission to maximize field-of-view observation coverage weighted directly by
per-ship risk is temporal optimization, so select MiniZinc. Risk scores must be
supplied by authorized evidence or an explicit code-owned derivation; do not
invent them.

### Selected-fleet attached-object inspection

A Mission 3 inspection is a temporal adaptive-tour problem. Select MiniZinc and
use `world_model_info.mission3.selected_ship_ids` as the fixed task roster. The
runtime has already normalized the Mission-description ID/area filters; visible
incidental ships never expand it. Use public target estimates for travel and
`ships[]` evidence state for variable service time. Record a capture rule that
only a producer's explicit sufficient normal/abnormal verdict resolves a ship,
and a value rule that prioritizes resolving the remaining roster within the
recording/budget limit. Mission 3 has no reporting-reliability belief input.

### Worker-requested ground-team assistance

A Mission 4 search is a temporal adaptive-search problem. Select MiniZinc and
use only the accepted objectives, allowed areas, coverage, observations and
accumulated match evidence in `world_model_info.mission4`. Objectives locate a
person in distress or locate/identify an animal for a ground team at a stated
position. Preserve unresolved objectives when a new request revision arrives.
Plan additional useful views while match or location uncertainty remains, and
end only when the public evidence supports `all_found` or an explicit
deadline/failure outcome. Mission 4 has no reporting-reliability belief input;
the object-search belief represented in its public planning evidence owns found
decisions.

## Gotchas

- Never use placeholders such as `MISSION_INPUT_ID` or `SOURCE_AUTHORITY_ID`.
- Keep `schema_version`, `mission_id`, `source_authority`, `objective`, `rationale`, `planner_choice`, `details`, and `prior_knowledge` out of `PlanningIntent.details`; these names are reserved top-level fields.
- Do not infer hidden ground truth or carry transient observations forward as durable facts.
- Use `event_report_checks` once as reliability evidence; never double-count
  its correlated `detected_issues` entries or raw Event Observations.
