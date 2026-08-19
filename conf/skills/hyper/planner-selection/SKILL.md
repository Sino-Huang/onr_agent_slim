---
name: planner-selection
description: Apply when producing or adjusting Planner Choice for a validated Mission Specification, using its temporal constraints or symbolic task structure.
version: '1.0.0'
---

# Planner Selection

## Decision Procedure

1. Read the validated Mission Specification and its task structure; do not reinterpret its objective.
2. Select temporal planning when execution depends on durations, ordering in time, a horizon, or other temporal constraints. Set `planner_choice` exactly to `{"planning_profile":"temporal","planner_id":"minizinc"}`.
3. Select symbolic planning when the task is expressed as symbolic state, actions, preconditions/effects, or goal reachability without temporal scheduling. Set `planning_profile` to `symbolic` and `planner_id` to `fast-downward` or null.
4. Satisfy the selected Mission Specification schema completely and include no extra fields.
5. Check that maneuver IDs are unique and semantic, intents are concrete, declared dependencies are acyclic, and every duration or cost is positive.
6. Provide a positive temporal horizon for temporal planning or the required positive symbolic domain revision for symbolic planning.

## Authority Boundaries

- This skill is read-only guidance. Planner Choice does not authorize changes to the Mission Specification.
- Preserve the mission objective and all authoritative revisions exactly; never invent objectives, constraints, or revision values.
- Plans and Planner Choice remain versioned runtime artifacts and must follow their established publication path.

## Gotchas

- Do not choose temporal merely because maneuvers are ordered; use it when time itself constrains feasibility.
- Do not silently switch planner profile to make an invalid specification pass validation.
- `planner_id: null` is permitted only for the symbolic contract; the temporal planner ID is always `minizinc`.
