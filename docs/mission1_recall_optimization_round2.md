# Mission 1 recall optimization: renewed series

The user requested another optimization series after the first thirty attempts.
Treat this as a fresh limit of thirty genuine configurations, or verified recall
above 50%, starting from the retained **16/39 (41.03%)** result. The first series
and its stopping-condition audit remain intact in `mission1_recall_optimization.md`.

## Baseline and boundaries

- Retained Agent scoring: `cbb2387`; baseline documentation: `193fc4c`.
  Physical helpers: `39ccf2b`. Current code was compared with these checkpoints
  before the new run. User-owned untracked `statechart.json` remains untouched.
- Baseline artifact: Agent `var/mission1-recall/optimization-30/attempt-28/evaluation/summary.json`:
  16/39, balanced MSE 0.01790063, full continuous offline feedback.
- Keep the same 39 corrupted outcomes and source scenario. Hidden truth scores
  evaluation only; it never selects planner targets or weights.
- No AirSim/vLLM startup, live-default changes or physical command-lifecycle
  changes. Retain four headings, 0.9 speed reserve, one MiniZinc model, exact
  report uniqueness and the existing 10% replacement threshold.
- Runtime experiment outputs go under Agent
  `var/mission1-recall/optimization-round-2/`. Tests and evaluator controls are
  not additional optimization attempts. Count only inspected terminal results.

## Experiments

### 01 — Information slots plus delayed windows (completed; rejected)

Combine the retained information-slot score with the existing 750 m nominal/+4 s
public visibility forecasts. The window variant was tested before the scoring
improvement, not with this retained score and continuous feedback.

Input: `optimization-30/attempt-03/input/environment.json`, 1,712 native view
rows, time zero, 750 m range, 30 m/s maximum, 0.5 s quarter turn and four-second
eligibility. Belief: `fixed-runs-integrated-demo-001/case-2/belief.json`.
Output: `optimization-round-2/attempt-01/evaluation/`. Existing helper invocation
uses `--sensing continuous` and default closed-loop planning. No source code was
changed for this comparison. Result: **11/39**, below retained **16/39**; balanced
MSE worsens from 0.017901 to **0.072439**. There are 57 unique checks (46 clean,
five omitted, six altered), 21 scheduled fixed-view sensing segments and no
pursuit. Thirty-four gate assessments produce replacements at 46 and 190.5 s.

All three revisions reach native OPTIMAL_SOLUTION and exact oracle parity.
Initial graph: 13,812 candidates; generation 81.21 s; solver times
28.65/25.35/1.44 s across revisions. Post-initial-solve rollout: **1,091.53 s**,
including **944.32 s** gate work. Sensing spans all 299.5 Mission seconds:
224.5 transit, 44 early waiting and 31 surveillance. Two issues are found while
waiting and nine during surveillance. The execution audit passes (unique
report/check credit, detector windows, continuous sensing, selected windows,
mode and pose, native parity): `attempt-01/audit.json`.

Do not promote this delayed-window combination. Best remains 16/39. This is
**1/30 completed configurations** in the renewed series.

## Public gap-forecast helper checkpoint

Physical **`7ac9867`** implements the forecast preparation portion, not yet
planner selection of report-free windows. Existing planner/model code remained
unchanged throughout Experiment 01.

`prepare_surveillance_views.py --gap-hold-seconds D` adds optional public-only
forecasts. For each vessel's consecutive distinct public timestamps, reserve
the next report's detector lookback and center one half-second-aligned window
in the remaining gap. Clip to current Mission time; skip gaps that cannot fit
the configured capture exposure. D must be a half-second multiple of at least
one second; zero keeps existing preparation behavior.

Interpolate only within disclosed public activity spans. Sample four
camera-facing poses one native grid cell from the midpoint forecast, and query
the same native camera/occlusion masks at every capture tick. Output rows have
empty `report_ids`, `holding_intervals` with real entity IDs, and
`gap_observation_windows: [{start_s, end_s}]`. Internal geometry query IDs are
never persisted as fabricated public reports. The shared Agent holding scorer
will still remove all report-owned lookbacks and deduplicate entity exposure.

Verification: **107 focused Physical tests pass**, 6.47 s, including quantized
gap bounds, epoch deduplication, current-time clipping, disclosed-only position
interpolation, four headings, empty report IDs and loss of forecast visibility.
Changed-file lint and diff checks pass. An actual native geometry preparation
with D=8.5 s creates **391 gap rows**, alongside the unchanged **881 baseline
rows**, in **87.37 s**. Public reports are unchanged and every interval references
a real public entity. Artifacts: `preparation/gap-8s/` and `test-gap-forecast.xml`.
This preparation is not a completed recall attempt or a demonstrated gain.

No Agent planner integration is claimed at this checkpoint: current candidate
construction ignores the new report-free rows. The next step below is required
before evaluating them as an optimization. No AirSim, command lifecycle or live
defaults changed; no new dependencies were installed.

## Next structural experiment: report-free omission observation windows

Inspection confirms a gap: `_fixed_view_candidates` constructs holds only from
nonempty public-report batches. `holding_exposures` already values forecast
exposure outside all public-report lookbacks, and active-plan rescoring already
supports `scored_observation_windows` after anchors are checked. This leaves
between-report omission opportunities underrepresented.

Complete planner integration using the prepared public-only gap forecasts:

1. Use the implemented observation-window sampler inside disclosed inter-report gaps, using public
   position interpolation and native four-heading camera masks. Use configured
   dwell duration and detector lookback; no hidden event times or synthetic
   public report IDs. Store forecasts with existing holding-exposure metadata.
2. Construct positive-value `fixed_view` candidates with empty covered-report
   lists, explicit start/end and existing scored-window metadata. Public recall
   and information components are zero; omission value uses the existing shared
   exposure helper and its ownership/exclusion rules.
3. Preserve native route constraints and report uniqueness. Ensure merged output
   reports actual public report span (or zero when none), rather than treating
   a report-free window start as a fabricated report timestamp.
4. Fix the gate's `report_ids`-only feasibility guard so a future, positive-value
   report-free hold also checks its selected pose/heading deadline. This is
   planner rescoring, not a new controller lifecycle or command.
5. Test empty-report native serialization/output, merged windows, gate score and
   feasibility parity, exclusion/deduplication, and absence of fabricated report
   credit. Use the real solver and observed-only feedback for the comparison.

Forecast preparation is now implemented and tested; candidate/model/gate and
Statechart interpretation integration remain pending. Additional integration
details found by source inspection:

- Gap candidate identity must include its explicit start and dwell, since
  report IDs are empty. Preserve existing identities when the new fields are
  absent. `_candidate` currently requires a nonempty report sequence, so use
  explicit construction or an explicit empty-report branch, never a fake report.
- `_fixed_view_runs` and native JSON output currently infer report span from
  first/last member times. Compute public report span from nonempty members only,
  or zero for a report-free run. Keep scored constituent windows intact.
- The gate already scores holding windows with no remaining anchors, but its
  future-feasibility guard requires `report_ids`. Include positive remaining
  holding value in that guard so the selected pose/heading deadline is checked.
- Preserve temporal/report-epoch ordering across gap nodes; simply bypassing
  epoch ordering for empty report lists can permit nonadjacent report reuse in
  delayed-window graphs. Any conservative gap-time ordering must be disclosed.
- Maneuver's derived transition facts report zero required/unconfirmed IDs for
  an empty list; those facts are not a completion decision. Verify the Statechart
  readiness retains the observation-window end for omission holds, including
  merged holds with public reports. Use existing timed readiness/command fields;
  inspect current skills before adding any instruction.

No recall gain for the structural change is claimed yet.
The existing `fixed_view` navigation/window interface appears sufficient; stop
for review if actual implementation requires a physical lifecycle/evidence-contract
change. Continue to distinguish ideal motion/perfect radius pursuit/fresh
partitions from live perception or controller acceptance.
