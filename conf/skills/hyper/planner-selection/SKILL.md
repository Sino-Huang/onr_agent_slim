---
name: planner-selection
description: Apply when deriving Planner Choice from PlanningIntent.
version: '1.5.0'
---

# Planner Selection

## Decision Procedure

1. Read PlanningIntent and its objective and constraints; do not reinterpret the raw MissionInput or operator Mission Intent.
2. Select MiniZinc for temporal optimization, not only conventional timed scheduling. It is required when feasibility or objective value depends on where the drone is at event times, travel along a path, FoV overlap or coverage, time windows or horizons, or maximizing reliability-aware observation utility. Do not reduce the Mission to symbolic inspect/reach goals merely because candidate ships or actions can be named. Set `planner_choice` exactly to `{"planning_profile":"temporal","planner_id":"minizinc"}`.
3. Reserve Fast Downward with PDDL for symbolic state reachability only when timestamps, durations, and path timing do not affect feasibility or objective value. Actions may have preconditions/effects and goals, but their timing, time windows, and FoV or coverage value must not require temporal optimization. Set `planner_choice` exactly to `{"planning_profile":"symbolic","planner_id":"fast-downward"}`.
4. Put flexible planner facts only in `details`. Planner-native files, solver input/output, and verification evidence belong to later translation outputs.
5. Satisfy the PlanningIntent fields completely and include no extra fields. Record the selection with `record_planning_intent`; the rationale must be concise and public.

## Example: ships observation coverage

For the time-indexed ships events in `data/ships_report_and_trajectory_example/ships/events_report.json`, risk-weighted field-of-view coverage is temporal optimization: the drone must travel to positions that observe ships at their event times, and FoV overlap determines weighted coverage. Select MiniZinc even though the ships and inspect actions can be named. Use risk scores from MissionInput or an explicit code-owned derivation.

## Authority Boundaries

- Planner Choice does not authorize changes to raw mission authority or PlanningIntent.
- Preserve the mission objective and all authoritative revisions exactly; never invent objectives, constraints, revision values, or risk scores.
- Plans and Planner Choice remain versioned runtime artifacts and must follow their established publication path.

## Gotchas

- Do not choose temporal merely because maneuvers are ordered; use it when time itself constrains feasibility or optimization value.
- Do not silently switch planner profile to make an invalid specification pass validation.
- A symbolic PlanningIntent selects `fast-downward`; a temporal PlanningIntent selects `minizinc`.
