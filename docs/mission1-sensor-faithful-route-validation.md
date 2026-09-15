# Mission 1 sensor-faithful route validation

Issue [#61](https://github.com/Sino-Huang/onr_agent_slim/issues/61) closes the
route-coverage investigation from #58. This note records the promoted
configuration and the controlled results from 2026-09-15. The primary task is
the unchanged `in-map-control-ship4` harbor instance: 20 ships, 253 actual
events, 247 public reports, and 11 corrupt outcomes (6 omitted and 5 altered).
All production comparisons start from the ordinary equal population prior.

## Promoted planning input

`scripts/prepare_surveillance_views.py` in Physical Runtime generated candidate
views from the public report schedule, current public vehicle state, declared
camera geometry, and ordinary belief only. It sampled all four discrete arrival
directions with the native heightmap-occlusion masks. The primary task produced
619 views; the independent seed-100/ship-5 task produced 629. Generation did not
open Mission truth or vessel trajectories.

The external Environment Profile now has the optional
`mission1_planning_input_path` seam. Physical Runtime loads that document once,
checks the Mission ID, exact public report-ID set, and advertised FoV radius,
then adds only the prepared surveillance and observation fields to
`planning_view()`. The live environment projection remains unmodified and does
not contain `surveillance_views`. The launcher exposes the seam as
`ONR_DEMO_MISSION1_PLANNING_INPUT` for Mission 1 and joint runs. The generic
Environment Profile default remains `null`. For a Mission 1 live-demo launch,
omitting the override now prepares public native-camera views from the selected
time-zero report stream and ordinary prior before Agent starts; an explicit
override still selects the caller's prepared input. Joint mode continues to
require an explicit input when sensor-aware Mission 1 planning is wanted.

This interface was added in Agent commit `db4008d` and Physical Runtime commit
`01600ad`. It does not change the single Mission 1 `model.mzn`, the 10% replan
threshold, the command lifecycle, or planner authority.

## Separate coverage results

These metrics deliberately describe different execution models. In particular,
the scheduled geometric replay is not an upper bound on continuous native or
live sensing because it omits transit, early-wait, and acquisition observations.

| Metric | Sensing and execution model | Before | Sensor-faithful result |
| --- | --- | ---: | ---: |
| Scheduled-window geometric control | Perfect arrival, assigned windows only; cardinal public views; closed-loop belief/gate | n/a | **0/11** (45 clean checks) |
| Native runtime | Native camera, intercept controller, partition migration, public GPS recovery | **6/11** | **10/11** (14 clean, 5 omitted, 5 altered) |
| Full live Agent | Native Physical service, LLM workflow, accepted commands and terminal audit | **4/11** (`run.8dAH8Z`) | **11/11** (`run.XVNubP`; 13 clean, 6 omitted, 5 altered) |

Both the scheduled control and every native/live plan were solved by COIN-BC as
`OPTIMAL_SOLUTION` and matched the exact Python oracle. The zero scheduled result
therefore says that the selected assigned windows contained no corrupt outcome;
it does not say that the executed route had no useful sensor exposure.

The labelled-p diagnostic from #59 already separated cold discovery from
execution: native and live truth-prior controls both reached 10/11. With the new
public views, the ordinary live run first saw ship 4 during transit and checked
the omitted 16.5 s event at 18 s. That evidence caused revision 3 to pursue ship
4, capturing the middle corruptions. At 268.5 s the shared gate rejected an
infeasible continuation. Exact revision 4 selected the public, direction-3 view
at `(543, 71)` for 299--299.5 s. The drone arrived at 293.5 s, met the 299 s
deadline, saw ship 4, and captured the 298.5/299 s cluster. The native run used
the same late view and missed only the omitted 194 s outcome; the live pursuit
timing captured it.

Thus the old native/live gap was not a solver-optimality failure. It combined
cold-discovery route coverage, real continuous camera exposure, pursuit timing,
and late heading selection. Public cardinal masks fixed the coverage/heading
part without leaking the suspect identity or corrupt event times.

## Independent task check

The parameterized seed-100 task has ship 5 as its sole high-p vessel and 12
corrupt outcomes. Its ordinary equal-prior radius-only native baseline found
0/12. The same public-view preparation improved this to **4/12 (33.3%)**, with
16 clean and 4 omitted checks across four exact-oracle revisions. This is a real
cross-task improvement, but it remains below 50% and is not evidence of universal
high recall: ship 5 leaves the global map and the known out-of-map rendezvous
limitation remains. The primary ship-4 task is an in-map controlled fixture, not
a claim that every vessel trajectory is reachable.

## Live receipt and test gates

The final evidence bundle is
`var/mission1-harbor-single-suspect/in-map-control-ship4/live-sensor-views-run-XVNubP-final/`.
It contains the captured CLI result, evidence audit, truth-after-completion
metrics, and JSON/Markdown workflow timing. The audit records:

- terminal state `patrol-objective-complete` at 299.5 s and 600 ticks;
- four optimal/oracle-exact revisions, unique report credit, component parity,
  and nonoverlapping scored windows;
- 24 unique checks matching belief revision 12, no unconfirmed final report IDs,
  no transition-timing issue, and no failed Physical feedback;
- balanced MSE reduced from `0.303041` to `0.007931` (97.38%);
- 1,387.46 s wall time: 604.39 s recorded LLM latency, 49.93 s non-LLM tool
  time, and 733.15 s startup/simulation/coordination/transport time.

Final verification:

- Agent: `pytest -q` -- **941 passed, 22 deselected**.
- Physical focused integration/planning tests -- **67 passed**.
- Physical full non-live gate at integrated commit `faa02f6`:
  **535 passed, 35 skipped, 13 AirSim deselected, 1 failed**. The sole failure is
  the pre-existing
  `test_agent_dependencies_are_isolated_to_optional_agent_package`; it still
  finds the previously introduced core `from onr.` import. Running without the
  marker expression additionally fails the unavailable opt-in AirSim gates, so
  no AirSim result is claimed here.

## Default `demo-001` live reproduction

On 2026-09-15, the unmodified default launcher command
`bash scripts/live_demo_with_wm/herdr_start_live_demo.sh onr` completed the
`demo-001` fixture in `run.VW40wd`. It checked 13 of 234 public reports and
recalled **1/39 corrupt outcomes (2.56%)**. The generated Environment Profile
had no Mission 1 planning input, so its radius-only candidates did not preserve
the native camera direction. A minimized replay confirmed that two
solver-credited reports at `(1046, -116)` were outside the commanded view.

After making the launcher's public-view preparation the Mission 1 default,
`run.e1byuO` completed the same 299.5 s/600-tick task with 46 unique checks:
44 clean, one omitted, and one altered. Both reports from the minimized failure
were checked. Offline evaluator scoring gave **2/39 recall (5.13%)** and reduced
balanced MSE from `0.140416` to `0.113971` (**18.83%**). The result exactly
matched the separate native-camera replay's check/outcome totals. The terminal
transition matched all 19 report IDs required by its final statechart and used
`assessment=satisfied`; belief revision 29 matched the check ledger.

The corrected run took 3,859.89 s wall time (64.33 minutes): 1,932.73 s in 97
recorded LLM calls, 123.01 s in non-overlapping tool calls, and 1,804.15 s in
view preparation, simulation, coordination, transport, and other uninstrumented
work. Relative to the radius-only baseline, wall time increased by 286.10 s and
recorded LLM time by 896.71 s because the wider coverage produced more maneuver
decisions and six rather than four planner revisions.

This recall is not sufficient for a 50% target. An evaluator-only clairvoyant
MILP over all 616 public native-camera views certified an optimistic upper bound
of **16/39 (41.0%)**. Even an omnidirectional execution over the current
candidate set therefore cannot reach 50%; improving the target requires broader
candidate coverage or a changed sensing/task envelope, not prompt tuning.

The strict completed-run audit found one additional route-model defect:
navigation after a pursuit completed at 98.5 s against a 98.0 s deadline. The
graph had assumed that the UAV ended pursuit at the ship's final reported
position, although surveillance can validly hold nearer the pursuit start.
Outgoing pursuit arcs now require the successor to be reachable from both
public endpoint bounds. The regression and the full non-live suite pass
(945 passed, 22 deselected). The completed-run bundle is
`var/mission1-default-live-run-e1byuO-final/`; its diagnostic audit confirms all
other assertions, while the strict audit intentionally retains the observed
late-navigation failure from the pre-fix run.
