# Mission 1 mixed-action reference

## Evidence and candidates

The installed code-owned builder is shared by data materialization, the Python
advisory oracle, and Context Coordination active-plan rescoring. It consumes the
current agent-facing public schedule, vehicle pose/capabilities, cumulative
report-check ledger, and persisted reporting-reliability snapshot. It excludes
expired, checked, duplicate, and unreachable opportunities.

`fixed_view` candidates cover the reports visible from one report-anchored FoV
during the observation dwell. `pursue_ship` candidates are every feasible
contiguous window of at least two consecutive future reports for one numeric
ship ID. A pursuit starts at its first report position/time, follows every
adjacent advertised movement at the vehicle maximum velocity, and ends after
the last report dwell. The builder uses the current FoV and velocity without capability caps.

The public report rate uses the ship's complete valid public schedule, including
reports that are now expired or checked:

`rate_i = (N_i - 1) / (t_last - t_first)`

The rate is zero when the schedule contains fewer than two distinct timestamps.

## Utility and optimization

Every covered public report contributes the existing utility
`0.5 * posterior risk + 0.5 * normalized expected variance reduction`. A pursuit
adds expected hidden-omission yield exactly once for its report interval:

`E[p_i q] * rate_i * (t_last - t_first)`

There is no risk threshold or pursuit bonus. A clustered fixed view can beat a
pursuit by covering more public evidence efficiently; a sparse or unreachable
high-risk schedule can likewise leave `fixed_view` preferable.

`model.mzn` derives each candidate score from the recall, estimation, and
omission arrays and maximizes their route sum. It then minimizes maneuver count
and total surveillance duration lexicographically, using candidate order only
to make otherwise identical optima deterministic. Compatible routes never
repeat a public report. The unit-flow relaxation remains exact because a
directed network incidence matrix has integral vertices.

The compact inspector validates aligned candidate/report/arc arrays, forward
arcs, a source-to-sink route, an incoming-edge permutation, and nondecreasing
CSR offsets. Repeated offsets are valid empty adjacency windows.

## Output interpretation

Each assignment preserves its `fixed_view` or `pursue_ship` mode and reports the
observation window, numeric target entity when present, opaque covered report
IDs, scaled target posterior risk, `E[p_i q]`, public report rate, and recall /
estimation / hidden-omission / combined utility. Interpret risk and rate using
`rate_scale`, utility using `utility.scale`, and time using `time_scale`.

MiniZinc selects the mode. Hyper preserves that mode in the Statechart. Maneuver
Control alone turns `fixed_view` into navigation or calls
`pursue(entity_id=<numeric target>)` for `pursue_ship`.

## Few-shot sequence

All artifacts live beside `model.mzn` under
`examples/event-information-patrol/` and are regenerated with
`generate_data.py ENVIRONMENT_JSON BELIEF_JSON DATA_DZN`.

- `prior-environment.json` + `prior-belief.json` -> `data.dzn`: the first
  advisory action is an efficient `fixed_view` under prior risk.
- `replan-environment.json` + `replan-belief.json` -> `replan-data.dzn`: an
  altered check raises entity 7's posterior and the replacement selects
  `pursue_ship` for its dense reachable future window.
- `counterexample-environment.json` + `counterexample-belief.json` ->
  `counterexample-data.dzn`: entity 7 remains high-risk but its future reports
  are unreachable, so the plan selects `fixed_view`.

These example values are teaching values only. Runtime evidence paths remain
authoritative.
