# Mission 1 mixed-action reference

## Evidence and candidates

The installed code-owned builder is shared by data materialization, the Python
advisory oracle, and Context Coordination active-plan rescoring. It consumes the
current agent-facing public schedule, vehicle pose/capabilities, cumulative
report-check ledger, and persisted reporting-reliability snapshot. It excludes
expired, checked, and duplicate reports; reachability is checked at each
candidate's actual viewpoint or pursuit rendezvous.

`fixed_view` candidates sample report centres, pair midpoints, and the drone's
current position. Each sampled viewpoint is considered at every visible future
report time, including times unrelated to the reports that supplied that
viewpoint. Each candidate covers the visible reports at one exact timestamp,
followed by half a second of dwell. Later reachable reports remain available
even when a nearby earlier report is unreachable.
A nearby report need not be reachable at its own position to
be observed from a feasible viewpoint. Coordinates are rounded to the solver's
integer metres before coverage is tested. Distinct viewpoints retain distinct
candidate IDs even when they cover the same reports, because their travel
connections can differ. Coverage uses the advertised radius; actual direction
and occlusion can still leave reports unconfirmed.

`pursue_ship` candidates are every feasible
contiguous window of at least two consecutive future reports for one numeric
ship ID. A pursuit starts at its first report position/time, follows every
adjacent advertised movement within the travel budget, and ends after
the last report dwell. The builder budgets cardinal-grid distance
`abs(dx) + abs(dy)` at `0.9 * controlled_vehicle.max_velocity` for initial
reachability, pursuit windows, and route transitions. These feasible arcs are
the timing constraints supplied to MiniZinc; the oracle and replan gate share
the same builder. The physical speed cap is unchanged. The 10% reserve allows
early arrival but does not bound arbitrary obstacle detours; new observations
can still require replanning.

During active-plan rescoring, an executing pursuit may have only one unchecked
report left. The gate treats that tail as continuation, with hidden yield from
the current Mission time to the last remaining report, excluding elapsed time
and final dwell. The two-report minimum still applies to new candidates.

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
and total surveillance duration lexicographically, then minimizes candidate
index sum. Equal index sums are resolved recursively by choosing the smallest
optimal predecessor, back from the sink; the oracle uses the same reverse-path
ordering. Compatible routes never
repeat a public report. The unit-flow relaxation remains exact because a
directed network incidence matrix has integral vertices.

Consecutive selected fixed views at the same coordinates form one sustained
assignment, from the first report time through the last dwell. The objective
counts one maneuver for that run and includes its holding gaps in surveillance
duration. Other viewpoints and pursuits separate runs. Public utility is rounded
per report-time block and summed, identically in the model, oracle and active-plan
rescoring. Holding adds no pursuit-only omission yield.

Integer node potentials first reweight network costs by a route-independent
constant. MiniZinc verifies the longest-prefix potentials against the
component-derived weights, then uses unit penalties for negative reduced-cost
or noncanonical predecessor edges. The unique zero-loss path is a lexicographic
optimum with the deterministic final tie-break; all feasible routes remain
available. This avoids huge floating-point objective
coefficients while preserving the optimal plans and reported utility. Keep the
generated potential array with its paired model and data files.

The compact inspector validates aligned candidate/report/arc arrays, forward
arcs, a source-to-sink route, an incoming-edge permutation, and nondecreasing
CSR offsets. Repeated offsets are valid empty adjacency windows.

## Output interpretation

Each assignment preserves its `fixed_view` or `pursue_ship` mode and reports the
observation window, numeric target entity when present, opaque covered report
IDs, scaled target posterior risk, `E[p_i q]`, public report rate, and recall /
estimation / hidden-omission / combined utility. Interpret risk and rate using
`rate_scale`, utility using `utility.scale`, and time using `time_scale`.
MiniZinc emits sustained fixed-view runs directly, with all their covered report
IDs and summed utility components. Preserve each emitted window as one assignment;
the raw candidate count can exceed the advisory or selected maneuver count.

MiniZinc selects the mode. Hyper preserves that mode in the Statechart. Maneuver
receives the exact selected fixed-view coordinates through the bound
`planner_item.parameters`; retain those coordinates even for a midpoint or
current-position view. Replanning checks the selected viewpoint's reachability,
not an alternative location covering the same reports.
Maneuver
Control alone turns `fixed_view` into navigation or calls
`pursue(entity_id=<numeric target>)` for `pursue_ship`. An unseen pursuit target
may first require navigation to the public first-report rendezvous, and later
public-position-guided reacquisition. Those physical phases remain within the
same pursuit assignment and do not change its mode or evidence window.

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
