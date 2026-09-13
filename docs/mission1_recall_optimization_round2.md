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

Physical **`7ac9867`** implements the forecast preparation portion. Agent
**`bc1ed04`** implements selection and interpretation of report-free windows.
Existing planner/model code remained unchanged throughout Experiment 01.

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

No AirSim, command lifecycle or live defaults changed; no new dependencies were
installed. Preparation alone is not a demonstrated recall gain.

## Report-free omission observation windows: implemented

Inspection confirmed a gap: `_fixed_view_candidates` constructed holds only from
nonempty public-report batches. `holding_exposures` already values forecast
exposure outside all public-report lookbacks, and active-plan rescoring already
supports `scored_observation_windows` after anchors are checked. This leaves
between-report omission opportunities underrepresented.

Agent `bc1ed04` integrates the prepared public-only gap forecasts:

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

Candidate/model/gate and Statechart interpretation integration are implemented
and tested. Important details:

- Gap candidate identity must include its explicit start and dwell, since
  report IDs are empty. Preserve existing identities when the new fields are
  absent. `_candidate` currently requires a nonempty report sequence, so use
  explicit construction or an explicit empty-report branch, never a fake report.
- `_fixed_view_runs` and native JSON output compute public report span and
  observation-delay bounds from nonempty members only, or zero report span for
  a report-free run. Scored constituent windows remain intact.
- The gate scores holding windows with no remaining anchors. Its future-pose
  feasibility guard now includes positive remaining holding value, not just
  `report_ids`, so a future report-free hold must also be reachable.
- Preserve temporal/report-epoch ordering across gap nodes; simply bypassing
  epoch ordering for empty report lists can permit nonadjacent report reuse in
  delayed-window graphs. Any conservative gap-time ordering must be disclosed.
- Maneuver's derived transition facts report zero required/unconfirmed IDs for
  an empty list; those facts are not a completion decision. The existing
  Statechart generator already enforces observation-window end readiness. No
  lifecycle change was needed. Its inspector now detects dropped planner-item
  metadata, including scored windows. MiniZinc skill 2.20.0 and Statechart skill
  3.5.0 explain timed holds without fabricated report IDs or early completion.

Verification: **794 Agent non-live tests passed**, 21 live tests deselected,
295.09 s (`test-gap-full.xml`). **60/60** seed-100 prior/counterfactual snapshots
remain optimal with exact native/oracle candidate-route parity; maximum solve
**2.2762 s** (`gap-regression-corpus/`). This is a radius-only planner regression
corpus, not native-camera recall acceptance. Target-pursuit selection switches
false-to-true in all 30 counterfactual snapshots; first mode remains pursuit in
both conditions. Focused holding tests verify empty global report arrays,
reachability, elapsed exposure, mixed merged metadata, delayed-epoch uniqueness
and Statechart window-end readiness. Generic Codex skill validation still
rejects the repository's supported `version` frontmatter; repository role tests
pass. Changed-code Ruff passes with only the pre-existing TRY004 rule excluded.

### 02 — Eight-second gap exposure (completed; no recall gain)

Add optional 8.5 s report-free holds to the retained 750 m native geometry and
information-slot score. Input: `preparation/gap-8s/environment.json`; output:
`attempt-02/evaluation/`. Initial graph has **7,854 candidates**, including
**358 report-free fixed candidates**, and **477,761 arcs**. All four revisions
are native optimal and exactly match the oracle. Solver times:
12.158/10.823/10.776/4.227 s. Post-initial-solve rollout: **411.32 s**.

Result remains **16/39 (41.03%)**, balanced MSE **0.01790063**. All 58 checks
(42 clean, seven omitted, nine altered) are identical to the control, including
their timestamps. **No gap hold is selected in any revision.** This is not a
recall improvement, despite the additional supported candidate family. Initial
assignments also match the control. Execution audit passes: `attempt-02/audit.json`.
Sensing: 200 s transit, 67.5 s early wait, 32 s surveillance; detected issues:
one/one/fourteen respectively. There are 28 scheduled fixed-view segments, no
pursuit, and 44 gate assessments. Best remains 16/39; **2/30 completed** in this
renewed series.

### 03/04 — Shorter/longer gap exposure (completed; rejected)

Change only public gap dwell to 4.5 s or 16.5 s. Preparation:
`preparation/gap-4s/` (415 extra rows, 85.94 s) and `preparation/gap-16s/`
(332 extra rows, 79.38 s). Outputs and passing audits are under `attempt-03/`
and `attempt-04/`.

| Attempt | Recall | Balanced MSE | Checks: clean/omitted/altered | Initial candidates | Solver seconds, all four revisions optimal/parity | Rollout / gate seconds |
| --- | --- | --- | --- | --- | --- | --- |
| 03: 4.5 s | 15/39 | 0.06065219 | 41/6/9 | 7,879 | 12.292/11.200/10.599/4.478 | 421.20 / 300.88 |
| 04: 16.5 s | 13/39 | 0.07384530 | 62/6/7 | 7,788 | 11.936/10.879/10.703/0.893 | 403.99 / 297.99 |

Attempt 03 selects gap holds in revisions 1 and 3 but replaces them before
execution. Their inclusion affects the planned route and later feedback; it
does not produce direct gap detections. Replacements occur at 42/47.5/137.5 s.
Continuous sensing: 202.5 s transit, 52.5 s wait, 44.5 s surveillance; one issue
in transit, fourteen during surveillance; 22 scheduled fixed-view segments.

Attempt 04 executes one report-free hold at 231.5–248 s; **no checks occur in
that hold**. It selects the hold after evidence, not in the prior route.
Replacements occur at 42/47.5/249.5 s. Sensing: 169 s transit, 61 s wait,
69.5 s surveillance; one issue in transit and twelve during surveillance;
30 scheduled fixed-view segments. More total checks do not imply better recall
or estimation. Neither gap duration should be promoted to live inputs. These
optional candidates remain an exercised planning capability, not an achieved
optimization. **4/30 renewed configurations completed; best remains 16/39.**

## In-progress visibility comparisons

The retained 16/39 result detects seven of twenty earlier corrupted outcomes
and nine of nineteen at the shared final epoch. These are evaluator diagnostics,
not target selection inputs. The remaining deficit is not confined to startup.

Attempts 05/06 test constant altitude 50/100 m versus the 25 m baseline, with
750 m range and no gap holds. Both public vehicle altitude and copied scenario
initial altitude change together; effective config comparison verifies all
other fields/data and the public schedule remain identical. Native masks are
regenerated, not reused. Geometry preparation takes 62.54/62.31 s and produces
881 rows each. Masks differ from baseline, but the largest sampled public
terminal batch remains 26/107 reports. This is not a global recall bound.
Artifacts: `preparation/altitude-50m/`, `preparation/altitude-100m/` and
`attempt-05/`, `attempt-06/`. These assume the drone starts at the given constant
height; they do not validate a climb maneuver or modify live altitude.

Next preparations vary range to 1,000/1,500 m using the existing 200-cell
partition control (first series Attempt 30: 12/39 at 750 m). Compare against
both that matched control and the overall 16/39 best. Paths:
`preparation/range-1000m-partition200/` and
`preparation/range-1500m-partition200/`. Source/live configuration remains
unchanged. Scratch configuration/coverage diagnostics are path-driven under
Agent `var`; generated scenario paths remain relative. No higher-range recall
gain is claimed before native solving and continuous feedback evaluation.

No recall gain for the structural change is claimed.
The existing `fixed_view` navigation/window interface appears sufficient; stop
for review if actual implementation requires a physical lifecycle/evidence-contract
change. Continue to distinguish ideal motion/perfect radius pursuit/fresh
partitions from live perception or controller acceptance.
