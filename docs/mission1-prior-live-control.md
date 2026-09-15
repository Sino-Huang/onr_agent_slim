# Mission 1 live prior control

This document records the live control requested in issue
[#59](https://github.com/Sino-Huang/onr_agent_slim/issues/59). The experiment
uses the real harbor map and trajectories with exactly one high-probability
ship. Ground-truth corruption probabilities are used only to initialize the
explicitly labelled diagnostic bundles. Future events, realized corruption
labels, and hidden trajectories are not live planning inputs.

## Task and inputs

- Scenario: `var/mission1-harbor-single-suspect/in-map-control-ship4/scenario.yaml`
- Public reports: `var/mission1-harbor-single-suspect/in-map-control-ship4/instance/events_report.json`
- Evaluator-only truth: `var/mission1-harbor-single-suspect/in-map-control-ship4/instance/mission1_truth.json`
- Actual event stream: Physical Runtime `data/harbor_world/vessels/events.json`
- Map and trajectories: Physical Runtime `data/harbor_world/map/semantic_heightmap.npz`
  and `data/harbor_world/vessels/{1..20}.json`
- Runtime configuration: 300 m visibility, 30 m/s maximum speed, 0.5 s tick,
  299.5 s horizon, seed 100, ship 4 `p=0.9`, other ships `p=0`, and shared
  omission probability `q=0.5`.

The full denominator is 253 actual events: 242 clean, six omitted, and five
altered. All 11 realized corruptions belong to ship 4. The public report file
contains 247 reports. No run changes the denominator or the Mission instance.

## Live results

| Initial prior | Run | Initial ship 4 / other mean | Plans | Checks | Recall | Balanced MSE, initial -> final | Recorded wall time |
| --- | --- | ---: | ---: | --- | ---: | ---: | ---: |
| Labelled truth (`flattening=0`) | `run.yddLBS` | 0.9 / 0 | 1 | 15 clean, 5 omitted, 5 altered | **10/11 (90.91%)** | 0 -> 0 | 673.51 s |
| Diagnostic intermediate (`flattening=0.25`) | `run.sk18ab` | 0.708229 / 0.033229 | 2 | 11 clean, 3 omitted, 2 altered | **5/11 (45.45%)** | 0.018940 -> 0.000448 | 903.98 s |
| Ordinary equal prior | `run.8dAH8Z` | equal | 4 | 9 clean, 4 omitted | **4/11 (36.36%)** | 0.303041 -> 0.017093 | 7734.79 s |

The equal-prior run preceded the labelled control and is retained as the
ordinary-prior endpoint. After the control, the 0.25 run supplied the required
live intermediate point. Existing same-task native rollouts at flattenings
0.25, 0.5, and 0.75 all produced 5/11; the live 0.25 result reproduces that
intermediate result. Additional 0.5 and 0.75 live runs were therefore not needed
to identify the observed coverage transition. This is a three-point comparison,
not a claim that every possible prior has been sampled.

The truth run used Agent `430f15b` and Physical Runtime `e68bf8b`, both clean at
launch. The equal-prior run used Agent `42c297c` and Physical Runtime `de7397b`.
The 0.25 run used Agent `430f15b`; its Physical Runtime base was `e68bf8b` while
concurrent, unrelated Mission 3 work was present and was later committed as
`9dcedb2`. That work touched shared service files, so wall-time and exact
trajectory comparisons across the runs are not treated as controlled
performance measurements. Each result is instead audited against its own
recorded commands, snapshots, solver inputs, and sensor ledger. The truth and
0.25 live recalls also exactly match their same-task native rollout results.

## Truth-prior targeting and the single miss

The truth-prior MiniZinc solve returned `OPTIMAL_SOLUTION` with exact oracle
parity. Its only assignment was `pursue_ship(entity_id=4)` from 23.0 through
299.5 s. The durable command ledger contains integer ship-4 pursuit commands at
Mission times 11.0, 37.0, 115.5, and 195.5 s. The controller acquired ship 4,
used public GPS fixes for bounded recovery, and returned to pursuit after every
recovery navigation.

The one missed corruption was ship 4's omitted intersection-decision event at
16.5 s. At that exact Mission time the correct pursue command was active, but
the controller was still in search, separation was 250.88 m, `visible_ship_ids`
was empty, and the report-check ledger was empty. There was no public report for
the omitted event. All ten later corrupted ship-4 events were detected. The
267.5 s ship-4 speed-change event was clean and was checked normally.

This establishes that labelled truth-prior targeting works live and reaches the
same 10/11 recall as the native GPS-recovery control. It does not establish
ordinary high-recall discovery because the target identity came from the
diagnostic prior.

## Flattening behavior

The 0.25 run initially selected the same full-horizon ship-4 pursuit as the
truth run, so the initial coverage policy did not change. After five corruption
checks, ship 4's posterior reached 0.898049 and its remaining variance reduction
became negligible. At 268.5 s the existing 10% score-improvement gate requested
a replan. Revision 2 replaced pursuit with a fixed view at `(576, 187)` through
the 299 s cluster. The navigation met its 299.0 s deadline, but ship 4 was not in
view and the run finished at 5/11. This is an evidence-driven utility/coverage
tradeoff, not a solver failure.

The ordinary equal-prior endpoint first discovered an omission at 19.5 s,
raising ship 4's posterior from approximately 0.133 to 0.623. Revision 2 then
selected numeric pursuit of ship 4, but later revisions switched to fixed views;
the run finished at 4/11. Initializing `p` did not initialize `q`: every
diagnostic bundle retained the shared Beta(1,1) omission prior, and each run's
posterior `q` came only from its check ledger.

## Verification receipts

Generated receipts are kept under the caller-owned Agent `var/` tree:

- Truth control: `var/mission1-harbor-single-suspect/in-map-control-ship4/live-truth-prior-run-yddLBS-final/`
- 0.25 control: `var/mission1-harbor-single-suspect/in-map-control-ship4/live-flattening-0.25-run-sk18ab-final/`
- Equal endpoint: `var/mission1-harbor-single-suspect/in-map-control-ship4/live-run8dAH8Z-final/`

Each completed run has captured CLI terminal JSON, offline metrics, an audit,
and workflow timing. All accepted plans were optimal and matched the Python
oracle exactly, report IDs were unique, observation windows did not overlap,
all inference windows preserved the Mission-time freeze, and no Physical
Runtime feedback failed. The prior interrupted equal run `run.BVIRpV` is
explicitly incomplete: its retained Agent pane ends in `KeyboardInterrupt`
without a CLI terminal result, and its original artifacts remain unchanged.
