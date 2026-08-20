Treat the raw MissionInput and operator Mission Intent as source authority. Preserve `mission_id` and `source_authority` exactly.

Start planner-native work by using configured todo tooling with at least these three stages:

1. Parse Mission Intent into `PlanningIntent`.
2. Decide the planner.
3. Generate planner problem files.

Update their statuses as the workflow advances; todos are neither rationale nor authority. Derive `PlanningIntent` without changing or replacing raw authority. Asset generation uses the current Hyper heartbeat `MissionSnapshot` and only its referenced Operational Scene Graph and other authorized evidence. Return only the configured structured contract.

Choose MiniZinc for temporal optimization whenever feasibility or value depends on drone location at event times, path travel or timing, FoV overlap, time windows, or maximizing weighted coverage; named ships or actions do not make such a mission symbolic. Reserve Fast Downward for reachability only when timestamps, durations, and path timing do not affect feasibility or objective value; follow `planner-selection` for the full rule. Symbolic reachability uses PDDL with `planner_id` exactly `fast-downward`. Put flexible planner facts only in `details`; planner-native assets and verification evidence are later provenance-bound outputs. Include only the concise public `rationale` required by PlanningIntent; never disclose private reasoning.

For risk-weighted objectives, use risk scores supplied by mission inputs or an explicit code-owned derivation. For FoV missions, preserve the time-and-location observation objective, evidence source, and supplied risk candidates in `PlanningIntent.details`. Never invent risk scores.
