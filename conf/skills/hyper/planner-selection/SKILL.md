---
name: planner-selection
description: Apply when deriving Planner Choice from an opt-in PlanningIntent or the legacy MissionSpec runtime mode.
version: '1.2.0'
---

# Planner Selection

## Decision Procedure

1. Read the configured planning representation and task structure; do not reinterpret the raw MissionInput or operator Mission Intent.
2. Use configured todo tooling to track interpretation, planner choice, and validation. Todos do not supply rationale or authority.
3. Select MiniZinc for temporal optimization, not only conventional timed scheduling. It is required when feasibility or objective value depends on where the drone is at event times, travel along a path, FoV overlap or coverage, time windows or horizons, or maximizing weighted coverage or information gain. Do not reduce the task to symbolic inspect/reach goals merely because candidate ships or actions can be named. Set `planner_choice` exactly to `{"planning_profile":"temporal","planner_id":"minizinc"}` when that is the configured contract.
4. Reserve Fast Downward with PDDL for symbolic state reachability only when timestamps, durations, and path timing do not affect feasibility or objective value. Actions may have preconditions/effects and goals, but their timing, time windows, and FoV or coverage value must not require temporal optimization. For opt-in `PlanningIntent`, set `planning_profile` to `symbolic` and `planner_id` exactly to `fast-downward`. `planner_id: null` is only legacy `SymbolicMissionSpec` compatibility where required.
5. Put flexible planner facts only in `details`. Do not put planner-native assets, solver input/output, or verification evidence in PlanningIntent; those are later PlanningRecord outputs.
6. Satisfy the configured schema completely and include no extra fields. Return only the configured structured contract; any contract-supported rationale must be concise and public, never private reasoning.
7. Check that maneuver IDs are unique and semantic, intents are concrete, declared dependencies are acyclic, and every duration or cost is positive.
8. Provide a positive temporal horizon for temporal planning or the required positive symbolic domain revision for symbolic planning.

## Example: ships observation coverage

For the time-indexed ships events in `data/ships_report_and_trajectory_example/ships/events_report.json`, risk-weighted field-of-view coverage is temporal optimization: the drone must travel to positions that observe ships at their event times, and FoV overlap determines weighted coverage. Select MiniZinc even though the ships and inspect actions can be named. Use risk scores from MissionInput or an explicit code-owned derivation.

## Authority Boundaries

- This skill is read-only guidance. Planner Choice does not authorize changes to raw mission authority, PlanningIntent, or the legacy MissionSpec.
- Preserve the mission objective and all authoritative revisions exactly; never invent objectives, constraints, revision values, or risk scores.
- Plans, Planner Choice, and later PlanningRecords remain versioned runtime artifacts and must follow their established publication path.

## Gotchas

- Do not choose temporal merely because maneuvers are ordered; use it when time itself constrains feasibility or optimization value.
- Do not silently switch planner profile to make an invalid specification pass validation.
- `planner_id: null` is permitted only for legacy `SymbolicMissionSpec` compatibility; an opt-in PlanningIntent symbolic choice is `fast-downward` and the temporal planner ID is always `minizinc`.
