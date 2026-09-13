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
connections can differ. Without `surveillance_views`, coverage uses the advertised
radius; actual direction and occlusion can still leave reports unconfirmed.

Offline sensor-aware snapshots can supply `surveillance_views`: public geometry
rows containing integer `x`, `y`, `arrival_direction` (0=east, 1=south, 2=west,
3=north), and visible `report_ids`. These replace radius-only fixed-view coverage;
an empty table means no visible fixed views. The physical-runtime offline helper
computes these rows from native camera/occlusion masks and position-selected
partitions, without reading event truth. Use supplied rows unchanged, never
invent visibility from hidden events. This is not yet a live-feed export or a
replay of navigation-dependent partition migration.

Rows may also supply `observation_delay_s`, bounded by the snapshot's
`observation_window_seconds`, in half-second increments. The offline helper
forecasts delayed positions by interpolating disclosed public report locations,
holding the final disclosed position afterward; these are point forecasts, not
confirmed observations or hidden trajectories. MiniZinc selects among these
sampled observation times. A report expires after its declared eligibility
window, not merely when its nominal event time passes.

This window formulation retains chronological report order and at most one
fixed view per co-timed report batch. Arcs require the next original report epoch
to follow the previous candidate's last original epoch. This prevents reuse of
an older report after an intervening view while keeping integral network flow.
It is not an unrestricted window/set-cover planner or a multi-heading batch scan.
Pursuit rendezvous remains at the first public report time.

Offline snapshots can also offer `fixed_view_dwell_options_s`, including the
short 0.5-second choice, and per-view `holding_intervals` of forecast visibility.
Longer candidates keep the same public anchor reports but occupy their full
selected dwell. They are offered only when additional omission exposure has
positive value; timing arcs reserve the full duration. Incidental public checks
during a longer dwell can update belief, but are not precredited as anchor
reports. This is still sampled observation planning, not continuous patrol.

Rows may additionally supply `gap_observation_windows: [{start_s, end_s}]` and
public `holding_intervals`. These create reachable, positive-value `fixed_view`
choices with no covered public reports. Their recall/information components are
zero; omission value uses the same holding-exposure scorer below. Each window
uses half-second times and at least one second of dwell. Identity includes its
pose, direction, start and duration; no synthetic report IDs are introduced.
The offline helper samples windows inside disclosed inter-report gaps, reserves
the next report's lookback, and forecasts four camera-facing poses. It uses no
hidden events or trajectories. Unsupplied gap windows add no such candidates.

Gap windows retain the graph's temporal ordering. In delayed-window graphs their
start acts as a conservative epoch cutoff: later candidates cannot return to
an older public epoch across the gap node. This may exclude feasible delayed
observations, but prevents nonadjacent report reuse without changing native
network-flow optimization.

Window graphs also remove locally dominated terminal choices: from one
predecessor, a final observation with no later continuation cannot beat a better
final observation under the complete lexicographic cost. Candidate indices stay
unchanged, preserving tie-break meaning; orphaned terminal candidates retain
their initially feasible source arc. This is local dominance, not an imposed
advisory route or a change to mode utility.

When supplied, `controlled_vehicle.quarter_turn_seconds` reserves multigrid Mission
time per 90-degree turn, including navigation and final orientation. A half-turn
costs two ticks. The builder reserves the worse of the two cardinal axis orders;
an unknown post-pursuit heading reserves its worst initial turn. This is
world-model timing, not AirSim yaw calibration.

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

Every covered public report contributes `0.5 * posterior risk`. Information
value is grouped by vessel and original report timestamp. For `n` co-timed checks,
current posterior variance `V`, and one-check expected variance reduction `g`,
the builder uses the moment-based precision approximation:

`G(n) = V * n * g / (V + (n - 1) * g)`

It preserves `G(1)=g`, gives diminishing increments and saturates at `V`.
For a vessel with `N` remaining valid public reports, allocate its full-schedule
budget uniformly: a selected batch of `n` reports earns
`0.5 * (n/N) * G(N) / max_remaining_one_check_gain`. Checked, expired and duplicate
reports are excluded from `N`. Earlier and later reports share equal per-report
credit; unobserved earlier epochs are not treated as consumed measurements.
Unique report credit caps the route's information utility at the vessel's
normalized variance budget, before integer rounding. Separate vessels have
separate budgets. Candidate scoring, the oracle and gate share this allocation
for both modes.

This is still a conservative public-schedule approximation, not exact
route-conditioned Bayesian lookahead. A selected subset earns its fraction of
the full-schedule budget, which can undervalue a small number of informative
checks. Recompute counts from the current public snapshot on replanning; actual
belief updates remain unchanged and consume observed checks only. Treat the
information cap as modeled utility, not a guarantee of better recall or MSE.

A pursuit adds expected hidden-omission yield exactly once for its report interval:

`E[p_i q] * rate_i * (t_last - t_first)`

When the snapshot advertises `event_check_window_seconds`, fixed views also
receive expected omission-discovery value. Each vessel's distinct public report
epoch owns the interval from its previous public epoch (or Mission time zero).
Intersect this cell with the selected observation's detector lookback, subtract
the union of lookbacks evidenced by that vessel's prior checks, and multiply
the remaining duration by `E[p_i q] * rate_i`. Co-timed reports share one cell;
expired or checked reports retain their ownership so other reports cannot
reclaim it. Delaying a view reduces this preceding-interval exposure. The shared
scorer uses the selected forecast time when rescoring an existing fixed view.

An optional longer dwell adds `E[p_i q] * rate_i * exposure_duration` from public
native-visibility forecasts. Forecast-visible capture ticks certify only their
preceding half-second exposure. Clip to the selected window through its last
capture (one tick before departure), the current Mission time during rescoring,
and the vessel's disclosed activity span. Exclude the union of every public
report's lookback interval, so a future fixed view cannot recredit the same
exposure. Duplicate/overlapping intervals share one credit per vessel. The gate
retains remaining exposure after the anchor report has been checked.

These are public-schedule rate approximations, not hidden-event predictions.
Only explicitly supplied report-free windows receive background observation
value; gaps between selected dwell windows remain uncredited. Visibility without
recorded checks cannot establish a searched interval.
The public capability comes from the offline physical helper's detector config.
Absent that field, fixed omission value is zero; no new live-feed contract or
physical command is introduced. Fixed views have multiple potential vessels,
so their single-target risk/rate output fields remain null; input inspection
lists the per-vessel risks/rates and the configured lookback.

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

Consecutive selected fixed views at the same coordinates and direction form one sustained
assignment, from the first report time through the last dwell. The objective
counts one maneuver for that run and includes its holding gaps in surveillance
duration. Other viewpoints and pursuits separate runs. Public utility is rounded
per report-time block and summed, identically in the model, oracle and active-plan
rescoring. Fixed omission cells and explicitly scored dwell intervals are summed
once; gaps joining the same-view assignments receive no extra omission credit.

Integer node potentials first reweight network costs by a route-independent
constant. MiniZinc verifies the longest-prefix potentials against the
component-derived weights, then uses unit penalties for negative reduced-cost
or noncanonical predecessor edges. The unique zero-loss path is a lexicographic
optimum with the deterministic final tie-break. Native optimal-face presolve
creates flow variables only for zero-loss canonical arcs. The model checks
forward acyclicity and the complete potential recurrence against component
weights, proving a zero-loss path exists; any positive flow on a penalized arc
would be nonoptimal. No selected route IDs are supplied. Early variable
elimination avoids flattening a large LP of provably nonoptimal arcs while
preserving the exact optimum. This avoids huge floating-point objective
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
For delayed views, `parameters.observation_delay` gives the minimum/maximum
sampled delay in the emitted run using its included time scale. `report_span`
still describes original public report times; it can differ from the observation
span. Execute the emitted observation window unchanged. Delay metadata is
explanatory, not an additional physical command parameter.

Longer fixed views also emit `parameters.scored_observation_windows`, including
each selected constituent window of a merged assignment. Preserve this metadata
in `planner_item` for exact rescoring, including after its anchor reports are
checked. Execute the outer assignment window unchanged; these scoring windows
are neither separate maneuvers nor additional physical command parameters.
An empty `report_ids` array is intentional for an omission-search hold, not an
empty plan. Mixed runs retain real report IDs only; report span and delay metadata
refer only to members covering public reports (span zero when none). Preserve
the window-end readiness even when the report list is empty or already checked.

MiniZinc selects the mode. Hyper preserves that mode in the Statechart. Maneuver
receives the exact selected fixed-view coordinates through the bound
`planner_item.parameters`; retain those coordinates even for a midpoint or
current-position view. Replanning checks the selected viewpoint's reachability,
not an alternative location covering the same reports.
When present, preserve `planner_item.parameters.arrival_direction` through the
Statechart and copy it to `navigate`. Complete the requested orientation by the
observation-window start; a different heading is a different fixed view. Omit
the field when the verified plan supplies none. Directions are discrete, not
continuous yaw angles.
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
