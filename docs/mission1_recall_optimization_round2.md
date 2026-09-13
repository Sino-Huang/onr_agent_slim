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

## Visibility comparisons

The retained 16/39 result detects seven of twenty earlier corrupted outcomes
and nine of nineteen at the shared final epoch. These are evaluator diagnostics,
not target selection inputs. The remaining deficit is not confined to startup.

### 05/06 — Constant altitude (completed; no recall gain)

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

Both finish at **16/39**, with all 58 check IDs/outcomes/timestamps identical to
the 25 m control and the same balanced MSE **0.01790063**. Both execution audits
pass. Each has 44 gate assessments, four optimal/oracle-equal revisions and
28 scheduled fixed-view segments. Replacements: 42/47.5/141 s. Continuous
sensing remains 200 s transit, 67.5 s wait, 32 s surveillance.

| Attempt | Initial candidates / generation seconds | Solver seconds | Rollout / gate seconds |
| --- | --- | --- | --- |
| 05: 50 m | 7,497 / 17.71 | 10.940/9.720/9.718/3.911 | 369.15 / 258.79 |
| 06: 100 m | 7,497 / 18.05 | 11.070/9.826/9.576/4.100 | 379.49 / 266.86 |

Do not promote higher altitude as a demonstrated recall fix. **6/30 renewed
configurations completed; best remains 16/39.**

### 07/08 — Longer range with matched partition

These comparisons vary range to 1,000/1,500 m using the existing 200-cell
partition control (first series Attempt 30: 12/39 at 750 m). Compare against
both that matched control and the overall 16/39 best. Paths:
`preparation/range-1000m-partition200/` and
`preparation/range-1500m-partition200/`. Source/live configuration remains
unchanged. Scratch configuration/coverage diagnostics are path-driven under
Agent `var`; generated scenario paths remain relative. No higher-range recall
gain is claimed before native solving and continuous feedback evaluation.

Effective dataclass comparison confirms only range differs from the matched
750 m/200-cell control. Native preparation creates 1,165/1,345 views in
109.84/162.89 s. The best sampled terminal single-view public coverage grows
from 26 to 30/37 reports. Same-pose four-heading unions grow from 36 to 46/54;
unlike the 100-cell baseline (26-report union), this suggests a possible later
cardinal-sweep experiment. These are sampled public geometry counts, not
timing-feasible sweep plans or truth-based recall bounds. Current delayed-window
arcs conservatively allow only one view per co-timed epoch to prevent report
reuse; do not relax that guard without preserving route-wide uniqueness.

Attempt 07 (1,000 m) completes at **14/39**, balanced MSE **0.06172225**, 94
checks (80 clean, seven omitted, seven altered). This improves on its matched
200-cell/750 m control (12/39) but not the retained 100-cell/750 m best (16/39).
Four native optimal/oracle-equal revisions; initial graph 14,134 candidates,
87.99 s generation. Solves: **29.782/24.539/8.152/4.720 s**, narrowly within the
unchanged executor limit. Rollout **1,530.52 s**, gates **1,335.46 s**, 56
assessments; replacements 45.5/155.5/190.5 s. Sensing: 196 s transit, 49 s wait,
54.5 s surveillance; two/one/eleven detected issues respectively; 34 scheduled
fixed-view segments. Execution audit passes (`attempt-07/audit.json`). Larger
coverage has a substantial computational cost and is not promoted globally.

Attempt 08 (1,500 m) **fails the initial 30-second native executor limit**:
17,614 candidates and 1,659,651 arcs. Candidate validation passes, but no native
optimal plan is returned; the evaluator does not start world/trajectory replay.
There is **no recall result** for this configuration. Failure artifacts and audit:
`attempt-08/evaluation/revision-001/` and `attempt-08/audit.json`. Count the
terminal failure as one genuine configuration, not as a successful rollout.
This was the seventh terminal result while 07 was still running. The solver
deadline and source model remain unchanged.

Attempt 09 tests the already-supported public camera-facing offset-only sampler
at 25 m standoff, with the same 1,500 m range and 200-cell partition. It reduces
the geometry to 523 rows (55.49 s preparation), without selecting a route or
reading hidden events. This is a different sampled candidate family, not a claim
of dominance-preserving pruning. Input: `preparation/range-1500m-partition200/offset25-input/`;
output: `attempt-09/evaluation/`. Completed results follow below.

### 09/10 — Smaller offset-view families (completed; below retained best)

Attempt 09 completes at **12/39**, balanced MSE **0.08451521**, with 91 checks
(79 clean, five omitted, seven altered). Its initial route promises 105 public
checks, compared with 75 in the retained baseline, yet realized issue discovery
is lower. It detects five earlier and seven final-epoch issues, versus seven/nine
in the retained control. Larger public coverage is not a recall guarantee.
All three revisions are optimal/oracle-equal; solves 11.630/3.309/1.181 s,
initial generation 23.61 s, 8,150 candidates. Rollout 423.84 s; gates 353.64 s;
61 assessments, replacements at 165/212 s. Sensing: 141 s transit, 55 s wait,
103.5 s surveillance; one issue while waiting, eleven during surveillance;
21 scheduled fixed-view segments. Execution audit passes (`attempt-09/audit.json`).

Attempt 10 uses the same 25 m offset-only sampler at **1,000 m** range and the
same 200-cell partition. Preparation: 522 rows, 44.75 s. Result **14/39**,
balanced MSE **0.07759304**, 63 checks (49 clean, five omitted, nine altered).
Four optimal/oracle-equal revisions; solves 10.459/4.333/3.519/3.485 s;
initial generation 18.83 s, 7,525 candidates. Rollout **337.06 s**, gates 255.57 s;
44 assessments, replacements at 119/141/151 s. Execution audit passes
(`attempt-10/audit.json`). Neither offset/range combination beats retained
16/39 or its estimation quality; neither is promoted to live inputs.
Attempt 10 sensing is 188.5 s transit, 56.5 s wait, 54.5 s surveillance;
one issue while waiting and thirteen during surveillance; 17 scheduled
fixed-view segments.

Attempt 11 tests **250 m** offset-only standoff at 1,500 m range/200-cell
partition, isolating standoff against Attempt 09. Preparation creates 491 views
in 60.07 s. It finishes at **12/39**, balanced MSE **0.08442416**, with 103 checks
(91 clean, five omitted, seven altered). More clean checks still do not improve
recall. Three native optimal/oracle-equal revisions; 10,840 initial candidates,
49.60 s generation; solves 20.197/4.135/4.025 s. Rollout **787.71 s**, gate work
**709.85 s**, 67 assessments; replacements at 180/180.5 s (first infeasibility,
then score improvement). Sensing: 128 s transit, 39 s wait, 132.5 s surveillance;
one issue in transit and eleven during surveillance; 21 scheduled fixed-view
segments. Execution audit passes (`attempt-11/audit.json`); do not promote.
Input: `preparation/range-1500m-partition200/offset250-input/`; output:
`attempt-11/evaluation/`.

**Eleven configurations are terminal (01–11); best remains 16/39.**
`ledger-audit-11.json` verifies terminal results separately
from concurrent work; the ledger records current completion versus running
attempts. No new production scoring change has been made during these runs.
Further route-wide information-allocation experiments should keep a bounded
budget while addressing the current conservative assumption that unobserved
earlier public reports have already consumed information slots. That remains
a proposed next experiment, not a demonstrated fix.

## Uniform information-budget experiment

The next implementation shares the existing saturating full-schedule information
budget uniformly across each vessel's `N` remaining public reports. A batch of
`n` earns `0.5 * (n/N) * G(N) / max_remaining_one_check_gain`, with the same
`G(N)=V*N*g/(V+(N-1)*g)`. This preserves total modeled budget while removing
report-order preference. Checked/expired/duplicate reports are excluded from
`N`; a selected subset can still be undervalued. It is not exact Bayesian
lookahead. Actual belief updates, recall/omission formulas, native model,
geometry, exact 10% gate and all execution interfaces stay unchanged.

A new focused test first reproduced the temporal bias: otherwise equivalent
unobserved reports received information values 0.500 and 0.139. The revised
test passes with equal allocation. Budget tests verify additivity across batch
partitions, full-schedule saturation, independent vessel budgets, permutation
invariance and checked/expired/duplicate exclusion. **130 focused Agent tests
pass**, including real native solver/oracle/gate cases. Three few-shot DZN
artifacts were regenerated; prior first mode remains fixed, altered-evidence
replan pursuit, unreachable counterexample fixed. The changed prior example
score is 1.518799; the existing native golden expectation was updated accordingly.

MiniZinc skill **2.21.0** explains the allocation and its limitations in the
existing focused reference; the main workflow is unchanged. Generic Codex skill
validation still rejects the repository-supported `version` key; repository
role tests pass. Changed-file Ruff passes (pre-existing TRY004 excluded).
Checkpoint **`8ce91d5`** is committed and pushed. **795 non-live Agent tests
passed**, 21 live deselected, 313.22 s; **107 Physical helper/replay tests passed**,
7.75 s. **60/60 seed-100 snapshots** reach native optimal with exact oracle route
parity; max solve **2.4001 s**. All thirty altered-evidence snapshots switch
target pursuit from unselected to selected. First mode remains pursuit in both
conditions; this is a radius-only regression corpus, not native-camera recall.
Artifacts: `test-uniform-full.xml`, `test-uniform-physical.xml`,
`uniform-regression-corpus/`.

Attempts 12/13/14 retain respectively the 750 m baseline
(control 16/39), the 300 m offset/window control (10/39), and the 1,500 m
offset control (12/39), changing only information allocation. Inputs are time-zero
public snapshots with empty check ledgers and the same prior. No geometry
regeneration, belief truth, or native model edit is part of these comparisons.

### 12–14 — Uniform budget recall comparisons (completed)

| Attempt / geometry | Recall / matched control | Balanced MSE | Checks: clean/omitted/altered | Initial candidates | Native solve seconds | Rollout / gate seconds |
| --- | --- | --- | --- | --- | --- | --- |
| 12: 750 m baseline | 13/39 / 16/39 | 0.07430925 | 72/6/7 | 7,496 | 11.104 | 305.19 / 259.63 |
| 13: 300 m offset/window | 1/39 / 10/39 | 0.13100216 | 45/0/1 | 6,170 | 9.292/2.580 | 161.57 / 131.25 |
| 14: 1,500 m, 200 cells, offset 25 m | **17/39 / 12/39** | **0.02708237** | 67/7/10 | 8,150 | 12.361/4.208 | 428.49 / 349.43 |

All native revisions are optimal/oracle-equal and all execution audits pass.
Artifacts: `attempt-12/`, `attempt-13/`, `attempt-14/`. Attempt 12 has 53 gate
assessments and no replacements; sensing 153.5 s transit, 85 s wait, 61 s
surveillance; issues one/one/eleven respectively; 32 scheduled fixed-view
segments. Attempt 13 has 29 assessments and one replacement at 165 s; sensing
165 s transit, 34 s wait, 100.5 s surveillance; its only issue is during
surveillance; eleven fixed-view and one pursuit segment. Pursuit is not forced
and its selection alone is not success.

Attempt 14 has 51 assessments and one replacement at 135 s. Sensing:
178 s transit, 52 s wait, 69.5 s surveillance; issues two/two/thirteen;
thirteen scheduled fixed-view segments. It detects eight earlier issues and
nine final-epoch issues. **17/39 (43.59%) is the new highest verified recall**,
but its balanced MSE 0.027082 is worse than the older 16/39 result's 0.017901.
Against its matched wide-camera control, both recall (12→17) and balanced MSE
(0.084515→0.027082) improve.

This gain is **configuration-dependent**: uniform allocation regresses sharply
in the two other geometries. Retain `8ce91d5` plus Attempt 14's input/scenario as
the current recall-best experimental combination, and preserve `70b12dc` (the
previous information-slot implementation) plus first-series Attempt 28 as the
16/39, lower-MSE checkpoint. This is not a universally better scoring policy or
live perception acceptance. No source scenario or live configuration file was
changed. Fourteen renewed configurations are terminal; the >50% goal remains
unmet. Three additional detections would give 20/39 (51.28%).

### 15–17 — Wider horizontal view (completed; no new best)

Use the current uniform scoring checkpoint throughout. Attempt 15 changes the
750 m baseline FoV from 90 to 120 degrees, reusing the existing prepared
first-series `attempt-19/input-valid/` and its scenario; compare with Attempt 12.
Attempt 16 changes the new recall-best 1,500 m offset view to 120 degrees;
compare with Attempt 14. Attempt 17 reduces that 120-degree offset-view range to
1,000 m, allowing a range comparison against Attempt 16.

Prepared inputs: `preparation/fov120-range1500-partition200/input/` (523 views,
55.66 s) and `preparation/fov120-range1000-partition200/input/` (523 views,
44.73 s). Effective config/public-source equality checks verify only camera FoV
differs from each respective 90-degree range control. Native visibility rows are
regenerated; all other sensor and flight settings, including four headings,
stay unchanged. Outputs and passing execution audits: `attempt-15/` through
`attempt-17/`.

| Attempt | Recall / matched control | Balanced MSE | Checks: clean/omitted/altered | Initial candidates | Native solve seconds | Rollout / gate seconds |
| --- | --- | --- | --- | --- | --- | --- |
| 15: 750 m / 120 degrees | 13/39 / 13/39 | 0.07428903 | 73/6/7 | 9,564 | 17.974 | 521.46 / 473.57 |
| 16: 1,500 m offset / 120 degrees | 16/39 / 17/39 | 0.03090396 | 62/6/10 | 10,219 | 17.247/5.703 | 632.77 / 550.10 |
| 17: 1,000 m offset / 120 degrees | 14/39 / 16/39 | 0.07749663 | 54/5/9 | 9,448 | 16.108/5.144/4.095 | 539.84 / 459.79 |

All six revisions reach native optimal and exact oracle parity. Attempt 15 has
53 assessments and no replacement; 16 has 53 assessments and replacement at
142 s; 17 has 48 assessments and replacements at 135/165 s. Sensing seconds
(transit/wait/surveillance): 15 = 152.5/75/72, 16 = 179/49/71.5,
17 = 184.5/45/70. Detected issues by those phases: 1/0/12, 1/2/13, 1/1/12.
Scheduled fixed-view segments: 29/14/14; no pursuit. Initial generation:
34.41/38.25/33.96 s. The wider FoV does not improve the current recall-best
configuration; keep the 90-degree Attempt 14 as the best at 17/39. Seventeen
renewed configurations are terminal; the goal remains active.

### 18–19 — Range and standoff controls (completed)

Attempt 18 increases only the current recall-best profile's range from 1,500 to
2,000 m, retaining 90-degree FoV, 200-cell partitions, uniform scoring and 25 m
offset-only sampling. Prepared native input: `preparation/range-2000m-partition200/input/`,
523 rows in 62.83 s. Attempt 19 uses the same 1,500 m setup with 250 m rather
than 25 m standoff; its geometry already exists under
`preparation/range-1500m-partition200/offset250-input/`. The earlier standoff
comparison used the preceding information-slot score, not current uniform
allocation. Compare both with Attempt 14. Outputs are `attempt-18/` and
`attempt-19/`; both execution audits pass.
No source/configuration defaults or production scoring change accompanies them.

Attempt 18 finishes at **12/39**, balanced MSE **0.08419880**, with 125 checks
(113 clean, five omitted, seven altered). Its sampled terminal public maximum
increased from 40 to 48, but issue discovery regressed against 17/39. Two native
optimal/oracle-equal revisions; initial 8,261 candidates, generation 23.72 s;
solves 12.081/4.443 s. Rollout/gate **463.58/386.61 s**, 70 assessments,
replacement 142 s. Sensing transit/wait/surveillance: 139.5/62.5/97.5 s;
issues 0/1/11; 23 scheduled fixed-view segments.

Attempt 19 finishes at **12/39**, balanced MSE **0.08442230**, 110 checks
(98 clean, five omitted, seven altered). One optimal/oracle-equal revision;
10,840 candidates, generation 48.94 s, solve 19.602 s. Rollout/gate
**746.11/689.17 s**, 66 assessments, no replacement. Sensing:
138.5/46/115 s; issues 1/0/11; 19 fixed-view segments. Neither control is
promoted over the historical 17/39 combination.

### 20–21 — Faster ideal execution (completed; rejected)

Both compare with Attempt 14, changing only maximum speed in the copied public
vehicle state and scenario: 45/60 rather than 30 m/s. Effective dataclass and
full public-input equality checks verify that all other fields, including
native visibility rows, are unchanged. Planner reachability keeps the 0.9
reserve; the offline cardinal transit uses the advertised cap. This is not
AirSim speed calibration or a change to live velocity configuration.

| Attempt | Recall | Balanced MSE | Checks: clean/omitted/altered | Native solve seconds | Rollout / gate seconds |
| --- | --- | --- | --- | --- | --- |
| 20: 45 m/s | 15/39 | 0.06203272 | 73/6/9 | 13.617/11.921/5.577/4.966 | 441.79 / 327.12 |
| 21: 60 m/s | 16/39 | 0.04534478 | 79/7/9 | 14.208/12.595 | 414.34 / 324.98 |

All six revisions are optimal/oracle-equal; execution audits pass. Initial
candidates/generation: 8,353/20.20 s and 8,523/18.36 s. Replacements:
42.5/130/142 s and 42 s; assessments 54/59. Sensing transit/wait/surveillance:
115.5/33.5/150.5 s and 116/36.5/147 s; issues 2/0/13 and 2/0/14.
Scheduled modes: fifteen/eighteen fixed segments respectively and one pursuit
each. Pursuit uses the disclosed perfect-radius evaluation assumption; selecting
it does not establish higher recall. Inputs: `preparation/speed-45mps/` and
`preparation/speed-60mps/`; outputs/audits: `attempt-20/`, `attempt-21/`.

### 22–25 — Broad camera and delayed-window controls (completed)

Attempts 22/23 use 150-degree FoV at 1,500/2,000 m, retaining uniform scoring,
200-cell partitions and 25 m offset-only views. Each preparation produces 523
view rows in 54.42/63.42 s; sampled terminal coverage is 46/49 public reports.
Attempts 24/25 retain the 1,500 m/90-degree camera but sample fixed observations
only at +2/+4 seconds. The four-second discrepancy window is unchanged. The
public-only forecast interpolates disclosed locations and holds the last one
after the final public report, as in earlier window experiments. Preparation:
521/500 rows, 57.94/54.64 s. These are distinct timing choices, not additional
sensor history or hidden event targets.

| Attempt | Recall / control | Balanced MSE | Checks: clean/omitted/altered | Native solve seconds | Rollout / gate seconds |
| --- | --- | --- | --- | --- | --- |
| 22: 1,500 m / 150 degrees | 16/39 / 17/39 | 0.06875735 | 107/7/9 | 20.460 | 811.71 / 748.01 |
| 23: 2,000 m / 150 degrees | 16/39 / 12/39 | 0.05297181 | 102/6/10 | 19.870/5.236 | 797.05 / 712.88 |
| 24: +2 seconds | 13/39 / 17/39 | 0.07382598 | 67/4/9 | 10.209/3.088/2.053/0.267 | 490.81 / 416.67 |
| 25: +4 seconds | 15/39 / 17/39 | 0.06570064 | 104/6/9 | 9.354/1.384/0.299 | 411.93 / 345.74 |

All ten revisions are optimal/oracle-equal; all execution audits pass. Initial
candidates/generation: 11,038/47.70 s, 11,164/47.08 s, 8,136/25.32 s,
7,796/21.51 s. Assessments: 68/65/58/64. Replacements: none; 165 s;
149/187/282.5 s; 210.5/299.5 s. Final times: 299.5/299.5/301.5/303.5 s.
Sensing transit/wait/surveillance: 141.5/41.5/116.5, 177/36.5/86,
151/49.5/101, 158/54/91.5 s. Issues by those phases: 1/0/15, 1/1/14,
0/11/2, 2/10/3. Continuous early-wait detections remain credited once, even
when they arrive before the selected delayed observation. Scheduled fixed-view
segments: 21/18/18/26; no pursuit. Outputs/audits: `attempt-22/`–`attempt-25/`.

**Twenty-five configurations are terminal**, verified by `ledger-audit-25.json`.
Historical best remains 17/39. These trials use Physical `7ac9867`'s camera;
the camera correction below has not yet received a recall measurement.

## Native camera ray-sampling defect and correction

Physical **`ef05954`** fixes a reproduced false-visibility defect. Native triangle
generation traced `camera_width // 5` rays: only 128 for the configured 640-column
camera. A flat-water test at 1,500 m range/10 m grid spacing found **138 of 451**
unoccluded interior grid cells invisible. A minimized one-cell test also failed
without actor creation or partition clipping (4.81 s).

Diagnostic controls held geometry fixed: wider sampling produced zero missing
cells, while disabling occlusion still left 134 missing cells. This isolates
angular undersampling rather than terrain LOS or coordinate conversion. The fix
traces every configured column, retaining the existing 100-ray minimum. It
introduces no camera parameter, FoV/range increase, evidence schema, hidden
input or physical command change. The original probe now has **0/451 missing**
cells in both heightmap-occlusion and lightweight modes. It remains a finite-ray
approximation, not exact continuous geometry or live perception validation.

Verification: **150 focused Physical tests pass**, including 26 camera tests
across four directions, 750/1,500/2,000 m ranges and 90/150-degree FoV, plus real
occlusion and out-of-view exclusions. Full non-live suites: **447 Physical tests
pass** (13 live-AirSim tests excluded, 206.35 s), **795 Agent tests pass**
(21 live tests excluded, 271.26 s). Artifacts: `test-ray-sampling-red.xml`,
`test-ray-sampling-focused.xml`, `test-ray-physical-full.xml`,
`test-ray-agent-full.xml`, `ray-sampling-probe*`. No AirSim started. Existing
Agent `8ce91d5` score/native model and its 60-snapshot corpus remain unchanged.

Regenerate native planning visibility before evaluating this correction; old
tables reflect the undersampled camera. Preparations for 26/27 are underway:
`attempt-26/input/` matches the historical 1,500 m/200-cell/offset25 best, while
`attempt-27/input/` matches the uniform-score 750 m/100-cell full-sampler control.
Neither is a completed optimization attempt or a proven recall gain yet.

No recall gain for the structural change is claimed.
The existing `fixed_view` navigation/window interface appears sufficient; stop
for review if actual implementation requires a physical lifecycle/evidence-contract
change. Continue to distinguish ideal motion/perfect radius pursuit/fresh
partitions from live perception or controller acceptance.
