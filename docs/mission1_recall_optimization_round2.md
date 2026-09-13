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

### 01 — Information slots plus delayed windows (running)

Combine the retained information-slot score with the existing 750 m nominal/+4 s
public visibility forecasts. The window variant was tested before the scoring
improvement, not with this retained score and continuous feedback.

Input: `optimization-30/attempt-03/input/environment.json`, 1,712 native view
rows, time zero, 750 m range, 30 m/s maximum, 0.5 s quarter turn and four-second
eligibility. Belief: `fixed-runs-integrated-demo-001/case-2/belief.json`.
Output: `optimization-round-2/attempt-01/evaluation/`. Existing helper invocation
uses `--sensing continuous` and default closed-loop planning. No source code was
changed for this comparison. The initial solve passes; early gate work takes
about 69–71 seconds. Final recall is not yet available.

## Next structural experiment: report-free omission observation windows

Inspection confirms a gap: `_fixed_view_candidates` constructs holds only from
nonempty public-report batches. `holding_exposures` already values forecast
exposure outside all public-report lookbacks, and active-plan rescoring already
supports `scored_observation_windows` after anchors are checked. This leaves
between-report omission opportunities underrepresented.

Implement a bounded public-only gap-window sampling experiment after preserving
the running comparison's code/model consistency:

1. Sample observation windows inside disclosed inter-report gaps, using public
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

No implementation or recall gain for this structural change is claimed yet.
The existing `fixed_view` navigation/window interface appears sufficient; stop
for review if actual implementation requires a physical lifecycle/evidence-contract
change. Continue to distinguish ideal motion/perfect radius pursuit/fresh
partitions from live perception or controller acceptance.
