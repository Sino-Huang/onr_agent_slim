# Mission 1 recall optimization attempts

Goal approved 2026-09-13: test camera-group viewpoints, eligible observation
windows, issue-oriented utility, and the offline belief/replan loop. Continue
until verified recall exceeds 50% or 30 genuine optimization attempts have been
evaluated. Commit/push reversible checkpoints. The earlier diagnosis series is
the baseline, not part of this new attempt count.

## Measurement boundaries

- Preserve demo-001 and its **39 corrupted outcomes**; do not remove difficult
  events, add GPS recall credit, or supply hidden truth to the planner.
- Use native fixed-view camera masks, four cardinal headings and multigrid turn
  timing. AirSim and vLLM remain untouched. Experimental 750 m range is not a
  change to the live 300 m default.
- Report perfect-arrival/pursuit and fresh-partition assumptions explicitly.
  These offline results are not live camera/controller acceptance.
- Count tested planning/evaluation configurations, including terminal solver
  failures. Harness repairs, repeated verification, individual replans within a
  rollout, and status updates are not additional optimization attempts.
- Generated inputs, solver streams and measurements live under
  `var/mission1-recall/optimization-30/attempt-NN/`.

## Attempts

| Attempt | Change | Result | Decision |
| --- | --- | --- | --- |
| 01 | Add cardinal camera-wedge intersections for co-timed public target pairs, 750 m, fixed initial plan | **11/39**, unchanged; 77/77 promised public reports confirmed versus 75/75 baseline; 8,574 candidates; native OPTIMAL_SOLUTION and exact oracle parity | Retain as optional experiment, not a recall improvement |
| 02 | Existing 750 m native viewpoints with the full scheduled-sensing observation/belief/10%-gate/replan loop | **10/39**, regression; one accepted replan, 43 gate assessments, 75 unique checks, final Mission time 299.5 s | Retain evaluator; do not claim the existing objective improves recall through adaptation |
| 03 | Chronological observation-window choices at nominal time or +4 s, 750 m, closed-loop evaluation | Initial solve exceeds 30 s, both before and after local terminal dominance; no recall result | Retain failed artifacts; not a verified route |
| 04 | Same window choices at 300 m, closed-loop evaluation | **1/39**, identical to a matched 300 m no-window closed-loop control; three additional clean checks | No recall improvement; do not promote to live defaults |
| 05 | Diminishing information value for same-vessel/co-timed checks, 750 m, no window alternatives, closed loop | **11/39** versus 10/39 in Attempt 02; balanced MSE 0.066278 versus 0.077341 | Retain this bounded scoring improvement |
| 06 | Same information model, 300 m with nominal/+4 s windows, closed loop | **4/39** versus 1/39 in Attempt 04; balanced MSE 0.073206 versus 0.136480 | Improvement against matched control, still below goal |
| 07 | Add fixed-view omission discovery with disjoint public-epoch exposure cells, 750 m, closed loop | **13/39 (33.33%)** versus 11/39 in Attempt 05; balanced MSE 0.057244 versus 0.066278 | Retain; new best, still below goal |
| 08 | Same fixed-view omission score, 300 m with nominal/+4 s windows, closed loop | **4/39**, unchanged from Attempt 06; balanced MSE unchanged at 0.073206 | No additional improvement at 300 m |
| 09 | Add 25 m camera-facing offsets to the existing 750 m view family | Initial solve exceeds 30 s; 11,748 candidates, 1,049,604 arcs | No verified recall; retain failure evidence |
| 10 | Same augmentation at 300 m with nominal/+4 s windows | Initial solve exceeds 30 s; 11,629 candidates, 1,250,315 arcs | No verified recall; retain failure evidence |
| 11 | Use only 25 m camera-facing offsets and the current pose, 750 m | **10/39**, below retained 13/39; balanced MSE worsens to 0.084506 | Do not promote globally |
| 12 | Same offset-only family, 300 m with nominal/+4 s windows | **10/39**, up from 4/39; balanced MSE worsens to 0.087886 | Useful recall tradeoff, retain as optional experiment |
| 13 | 750 m offset-only views with optional 0.5/4.5 s public-exposure holds | **10/39**, unchanged from Attempt 11; initial timeout repaired by native optimal-face presolve | No recall improvement; longer holds remain optional |
| 14 | Same hold choices at 300 m with nominal/+4 s windows | **10/39**, unchanged from Attempt 12; initial timeout repaired by the same presolve | No recall improvement; not promoted to live defaults |
| 15 | Raw posterior-variance information units, 750 m retained geometry | **11/39** versus 13/39; balanced MSE worsens to 0.066399 | Rejected and reverted |
| 16 | Same raw units, 300 m offset-only nominal/+4 s geometry | **10/39**, unchanged; balanced MSE slightly worsens to 0.088357 | Rejected and reverted |
| 17 | Retained scoring with 750 m nominal/+4 s observation windows and fast native presolve | **12/39**, below no-window 13/39; initial solve now succeeds | Not promoted; expensive gate calculations |
| 18 | Retained scoring with 750 m group viewpoints and the full scheduled-sensing loop | **12/39**, below 13/39; balanced MSE 0.087468 | No improvement over retained geometry |
| 19 | 120-degree camera versus 90-degree control, same effective 750 m range and retained scoring | **12/39**, below 13/39; balanced MSE 0.088049 | Offline config only; not promoted |

Attempt 01 geometry generation: 67.48 s; candidate generation: 25.28 s;
instance check: 0.84 s; executor wall duration: 30.06 s including overhead, with
OPTIMAL_SOLUTION returned under the unchanged subprocess limit. This is too
close to the limit to call robust. Physical checkpoint: `e8f49c0`.

Attempt 02 initially exposed two harness-only Statechart validation mistakes:
missing transitions, then nonunique transition event names. Both are corrected
and covered by a three-state regression test. Failed harness outputs remain in
`attempt-02/evaluation/` and `evaluation-repaired/`; the corrected rollout uses
`evaluation-final/`. They do not count as separate optimization configurations.

The replacement at 123.5 s passed the unchanged gate with 10.1919% modeled
improvement. Both native plan revisions reached OPTIMAL_SOLUTION with exact
oracle parity. Solver times were 23.41 s and 9.35 s. Post-initial-solve rollout
took 228.31 s, including gate and replacement work. Balanced MSE regressed from
0.067017 for the fixed native baseline to 0.077341. This is evidence against
treating the current combined-score improvement as a guarantee of realized
recall or MSE improvement on one instance.

Checkpoints: Physical `cda6f71` (closed-loop evaluator), Agent `50063c2`
(non-resetting ledger summary). Verification: 12 focused Physical tests and four
Agent evaluator tests passed; changed code lint and diff whitespace checks pass.
An artifact audit verified 21 nonoverlapping sensing segments remained inside
their solver-selected windows, preserving mode/entity/coordinates/direction;
all 75 check IDs are unique. This iteration did not rerun the full non-live
suites because changes are confined to offline helpers and their tests.

## Observation-window checkpoint

Agent `c61a27f` and Physical `ca571ec` implement discrete delayed observations,
public-position interpolation, eligibility-based expiry and selected-window
rescoring. The skill is version 2.15.0 and explains preserving emitted observation
times. The formulation retains chronological report order and one fixed view per
co-timed batch; it is not unrestricted window scheduling or a multi-heading sweep.

Local terminal-choice dominance compares the complete remaining lexicographic
cost from the same predecessor without imposing an advisory route. On Attempt
03's exact failed DZN, it reduced 1,276,436 arcs to 1,128,165 while preserving the
exact oracle route and all objective tiers. Candidate count remained 13,812.
This was insufficient to meet the 30-second solver limit; no oracle-only recall
is substituted. The reduction/retry is not a separate optimization configuration.

Attempt 04 verified both native plan revisions (6,386 and 2,469 candidates;
solver times 19.32 s and 4.00 s). The one accepted replacement occurred at 191 s;
24 gate assessments yielded 44 unique checks: 43 clean, one omitted. All 43
forecasted report opportunities whose windows were executed were confirmed.
The final time was 299.5 s; balanced MSE was 0.136480. Its matching no-window
control also found one issue, with 41 checks and balanced MSE 0.133405. Thus the
window variant did not cause the apparent 4/39 -> 1/39 regression against the
older **fixed-plan** 300 m baseline; that was not a matched comparison.

Artifacts: `attempt-03/input/`, `attempt-03/evaluation/`,
`attempt-03/evaluation-terminal-pruned/`, `attempt-03/terminal-pruning-audit.json`,
`attempt-04/input/`, `attempt-04/evaluation/`, `attempt-04/control-no-window/`.
All paths are relative to `var/mission1-recall/optimization-30/`.

Verification: **765 Agent non-live tests passed**, 21 live tests deselected,
223.34 s; **13 focused Physical tests passed**. The 30 seed-100 instances / 60
prior-and-evidence snapshots all reached OPTIMAL_SOLUTION with exact oracle
parity, maximum solve 3.79 s. These remain radius-only compatibility checks,
not a native-window corpus recall result. Three checked-in DZN examples were
regenerated. Changed code lint passes apart from the pre-existing Agent TRY004
findings; generic skill validation still rejects the repository-supported
top-level `version`, while repository role/catalog tests pass.

## Diminishing information checkpoint

Agent `8e53468` adds current posterior variance to observation opportunities and
uses the precision approximation `G(n) = V*n*g/(V+(n-1)*g)` for each vessel's
co-timed batch. It preserves the exact one-check gain, gives diminishing
increments, and approaches the available variance. Both modes and active-plan
rescoring use the same helper. Recall terms, pursuit omission terms, actual
Bayesian updates, report identities, maneuver boundaries and the 10% gate are
unchanged. Skill 2.16.0 documents that this is an approximation and not a
route-wide Bayesian information budget. Different report-time batches still
sum values computed from the current belief.

Attempt 05: four verified plan revisions, 43 gate assessments, 67 unique checks
(56 clean, five omitted, six altered), 26 executed fixed-view segments. Solver
times: 23.38, 14.20, 8.12 and 2.08 s. Final Mission time 299.5 s; post-initial-solve
rollout 263.99 s. This matches the old static-plan best recall while improving
the closed-loop control's recall and both estimation-error cohorts.

Attempt 06: three verified plan revisions, 29 gate assessments, 42 unique checks
(38 clean, one omitted, three altered), 21 fixed-view segments plus one pursuit.
Solver times: 19.30, 7.10 and 0.95 s. Final Mission time 303.5 s; post-initial-solve
rollout 147.26 s. Honest-vessel MSE rose slightly (0.008407 -> 0.008738), while
deceptive-vessel MSE decreased substantially (0.264553 -> 0.137674).

Artifacts: `attempt-05/evaluation/` and `attempt-06/evaluation/`, under the same
Agent `var/mission1-recall/optimization-30/` root. An execution audit verified
that all segments stayed within their selected solver windows, preserved
mode/entity/coordinates/direction, and had no interval overlap; check IDs remain
unique and every check occurred within its actual event's four-second window.

Verification: full Agent non-live suite **767 passed**, 21 deselected, 257.04 s;
the final information-focused invocation passed four tests, including two added
after that full run collected tests. Thirteen focused Physical tests passed.
All 60 seed-100 snapshots reached native optimality with exact oracle parity;
maximum solve 4.15 s. The existing three few-shot DZN files regenerated unchanged.
Changed-code lint passes; generic skill validation's known top-level-version
limitation remains, while repository role/catalog tests pass. No AirSim/vLLM,
physics defaults, source scenarios, or actual belief-update code was changed.

## Fixed-view omission checkpoint

Pushed checkpoints: Agent `812ef81`, Physical `ac6d70c`.

Fixed views now receive `E[pq] * public_report_rate * unsearched_exposure` when
the input advertises the public `event_check_window_seconds` capability. Each
vessel's distinct public report epoch owns its preceding interval. Intersect
that cell with the actual selected observation's detector lookback and subtract
the union of lookbacks established by observed checks for that vessel. Co-timed
reports share one cell; checked/expired epochs are not reassigned. Delayed views
receive only their remaining preceding exposure. Existing pursuit interval
scoring, report uniqueness, lexicographic optimization and the exact 10% gate
are unchanged. Sustained fixed-view output now sums all three utility components.

The native offline helpers export the detector window from scenario config,
before any simulated world is built. Inputs without the capability retain zero
fixed omission value; this is not a new live-feed or physical-command contract.
Skill 2.17.0 and compact manifests explain the new value and its limitations:
no unanchored/background exposure, no additional holding-gap credit, and no
searched-interval evidence from visibility that produced no recorded checks.

Attempt 07 found **13 issues: eight altered, five omitted**, from 55 unique
checks, versus six altered/five omitted in Attempt 05. Thus the two additional
discoveries came through changed route/replan choices, not directly from more
omission detections. There were 24 fixed-view sensing segments, 39 gate
assessments and two replacements at 36 s and 179 s. All three revisions reached
OPTIMAL_SOLUTION with exact oracle parity. Solver times were 23.55, 21.44 and
5.64 s; post-initial-solve rollout took 277.21 s and ended at Mission time 299.5 s.
Balanced MSE improved to 0.057244, with positive-cohort MSE 0.109054 and a slightly
worse zero-cohort MSE 0.005434 (Attempt 05: 0.004367).

Attempt 08 remained at **4/39**, with the same 42 checks and final belief as
Attempt 06: 38 clean, one omitted, three altered. There were 21 fixed-view
segments and one entity-2 pursuit, three verified revisions and 29 gate
assessments. Solver times were 19.64, 7.15 and 0.85 s; rollout took 148.27 s,
ending at 303.5 s. This configuration did not gain recall from the new score.

Both execution audits passed: no overlapping/out-of-window segments; selected
mode/entity/coordinates/direction preserved; unique check IDs; every check
within the configured detector window. Artifacts are `attempt-07/evaluation/`,
`attempt-08/evaluation/` and `fixed-omission-audit.json` under the experiment root.
Full Agent non-live verification: **776 passed, 21 deselected, three existing
warnings, 258.06 s**. Physical focused verification: **13 passed**. Seven new
planner cases cover omission interval ownership, union subtraction, posterior
response, mixed-mode exposure and native solver/gate parity at three delays.
Three few-shot DZNs regenerated unchanged. Changed-code lint and whitespace
checks passed (excluding the previously recorded Agent TRY004 findings).
Generic skill validation retains the known unsupported top-level `version`
limitation; repository role/catalog tests pass.

The 30 seed-100 instances / 60 prior-and-evidence snapshots all reached
OPTIMAL_SOLUTION with exact oracle route parity, maximum solve 3.86 s. Each
snapshot kind selected 120 pursuit and 330 fixed-view assignments across the
corpus; there were no evidence-conditioned route switches. These are unchanged
radius-only regression cases without the new lookback capability, not a native
camera recall result. Results: `fixed-omission-regression-corpus/summary.json`.

## Camera-offset comparisons

The nominal 25 m altitude / 45-degree downward pitch puts the centre ray's
sea-plane intersection 25 m ahead. Attempts 09–12 use this camera-derived
offset, facing toward public report locations. Native masks remain the coverage
authority. No hidden event positions, selected corruption labels or tuned risk
bonuses enter sampling. Model, score, physical speed and solver limit are unchanged.

Augmented inputs produced 1,403 views at 750 m and 2,178 windowed views at 300 m,
in 90.29 s and 34.78 s respectively. Both exceeded the existing 30-second initial
solver limit, so no recall is reported for Attempts 09–10. The 750 m final-epoch
maximum public coverage remained 26/107 despite the additional views.

Physical `7e1d9ac` adds the optional `--offset-views-only` sampling family. It
retains all camera-facing offsets and all four headings at the current position,
without report-centre/midpoint sampling. This is an alternative candidate family,
not dominance-preserving pruning or an imposed oracle route. Its 522 / 1,007
views are strict subsets of the corresponding augmented inputs, with identical
other public evidence and capabilities. Geometry took 35.43 / 18.50 s.

Attempt 11: **10/39**, from 73 checks (63 clean, six altered, four omitted).
More report checks than the retained Attempt 07's 55 did not improve recall.
It discovered three pre-final and seven final-epoch issues, versus four and nine
in Attempt 07. There were 17 fixed-view sensing segments, 39 gate assessments,
three optimal/oracle-matching revisions and an end time of 299.5 s. Solver times:
14.53, 11.66, 2.20 s; rollout excluding initial solve: 119.60 s. Balanced MSE
0.084506 is worse than the retained control's 0.057244.

Attempt 12: **10/39**, versus 4/39 in Attempt 08, from 40 checks (30 clean,
six altered, four omitted). There were 16 fixed-view segments and one entity-2
pursuit, 30 gate assessments and five optimal/oracle-matching revisions. Solver
times: 18.88, 3.40, 0.67, 0.22, 0.24 s; rollout: 132.99 s; end time: 303.5 s.
Balanced MSE worsened from 0.073206 to 0.087886, so this is a recall/estimation
tradeoff rather than an across-the-board improvement.

The last replan at 299.5 s legitimately had positive advisory utility from a zero
remaining score. Its original diagnostic JSON contains Python's `Infinity`.
Physical `748025d` makes future records emit null for that undefined ratio and
include both scores, without changing the gate. The raw Attempt 12 evidence is
retained; summary metrics and solver artifacts are unaffected. The logging fix
is tested with the real zero-baseline and exact-10% gate decisions, not counted
as another optimization attempt.

Verification: **17 focused Physical tests passed**, including three new sampling
cases and one gate-serialization case; changed-code lint and whitespace passed.
Both completed rollout audits preserve native windows, mode/entity/pose, unique
checks and detector-window eligibility. Agent production code and skill files
did not change, so its previous full-suite/corpus receipts were not rerun or
relabelled as new verification. Artifacts: `attempt-09/` through `attempt-12/`,
`camera-offset-audit.json`, and `test-camera-offset-final.xml` under the experiment
root. The optional sampling helper remains outside the live defaults.

## Forecast holding and native presolve

Agent `00b51a4` and Physical `cdd67c6` offer optional fixed-view dwell choices
from native public-position forecasts. Each forecast-visible capture contributes
only its preceding half-second exposure. The shared scorer clips this to the
selected window, current time during rescoring, and the vessel's disclosed
activity span, excluding all public-report lookback intervals. Duplicate
intervals share credit per vessel. Thus later fixed views and pursuits cannot
reclaim the same scored exposure. Holds remain valuable after their anchor
reports are checked. Merged native assignments emit their constituent scored
windows so the gate does not invent extra value for gaps between them.

The tested choices are 0.5 and 4.5 s. The short option remains; longer candidates
with zero extra value are omitted. This is a conservative approximation, not
full background patrol or credit for every incidental public check during a
hold. No new command or live-feed contract is introduced. Skill 2.18.0 explains
the optional metadata and preserves the outer assignment's execution window.

Both initial solves timed out: Attempt 13 had 8,772 candidates / 797,991 arcs;
Attempt 14 had 10,441 / 1,186,361. A compile-only probe of the exact failed
Attempt 13 DZN also hit 30 s, locating the bottleneck in flattening rather than
the COIN solve alone. Late zero-flow constraints still timed out. Early native
optimal-face presolve reduced compilation to **20.21 s**: the model verifies
forward acyclicity and all longest-prefix potentials against component weights,
proving a zero-penalty path exists, and creates variables only for canonical
zero-loss arcs. It derives the path itself; selected route IDs are not inputs.
Exact lexicographic preferences and invalid-potential rejection remain tested.
This is a solver repair/retry of Attempts 13–14, not two additional attempts.

Attempt 13 then completed three optimal/oracle-matching revisions, 40 gate
assessments and 22 fixed-view segments. It produced 74 checks: 64 clean,
six altered, four omitted. Recall stayed **10/39**; balanced MSE slightly
worsened from 0.084506 to 0.084978. Solve times: 19.71, 15.61, 2.54 s;
rollout excluding the initial solve: 306.56 s; final Mission time 299.5 s.

Attempt 14 completed five optimal/oracle-matching revisions, 30 assessments and
19 fixed-view segments. Its 40 checks and final belief matched the control:
30 clean, six altered, four omitted; **10/39**, balanced MSE 0.087886. The
control's entity-2 pursuit was replaced by fixed views without improving recall.
Solve times: 29.12, 4.95, 0.80, 0.26, 0.25 s; rollout: 382.95 s; final time
303.5 s. The initial solve remains close to the limit, and gate assessments
increased substantially with the larger graphs. Neither hold configuration is
claimed as a demo-performance or recall improvement.

Verification: **784 Agent non-live tests passed**, 21 deselected, three existing
warnings, 264.52 s; **88 final planner/holding cases passed** after the final
metadata condition refinement; **18 focused Physical tests passed**, including
forecast visibility loss and immutable Statechart metadata preservation. Both
rollout audits passed window containment/nonoverlap, mode/entity/pose/metadata,
unique checks and detector eligibility. Decision artifacts are strict JSON.
Three few-shot DZNs regenerated unchanged; lint/whitespace checks pass with the
previous Agent TRY004 exclusions. Generic skill validation retains its known
top-level `version` limitation; role/catalog tests pass.

All 60 seed-100 regression snapshots were optimal with exact oracle parity.
Maximum solve: **2.14 s**, versus 3.86 s in the previous receipt. Per snapshot
kind, selected modes remain 120 pursuit / 330 fixed-view assignments and there
are zero evidence-conditioned route switches. This is radius-only regression
coverage, not a native-camera recall corpus. Artifacts are under `attempt-13/`,
`attempt-14/`, `holding-audit.json`, `holding-regression-corpus/summary.json`,
`test-hold-full.xml` and `test-hold-physical-confirmed.xml`. Original failures
remain in each attempt's `evaluation/`; completed runs are in
`evaluation-early-presolve/`.

Final large-input inspection exposed a separate quadratic reachability check:
the compact inspector scanned every arc for every reachable node despite already
validated CSR offsets. The unchanged Attempt 13 DZN was still consuming a CPU
after 153 s before eventually completing. Reusing the validated outgoing slices
reduced the same successful inspection to **6.82 s**, with the same summary.
A deterministic regression reduces full-array iterations from 91 to three on
its small graph; **26 inspector/holding/executor tests passed** after this fix.
This is verification-helper repair, not another optimization attempt or a new
full-suite run. Planning utility and solver results are unchanged.

## Raw information units: rejected experiment

Agent `d18ff77` tested `0.5 * G(n)` instead of dividing information gain by the
largest remaining one-check gain. Diminishing returns and recall/omission terms
were unchanged. An additional test demonstrated that, at unchanged belief,
removing an unrelated opportunity no longer rescales its information value.
The diversity test now compared both equal report counts and the tradeoff against
more expected discoveries; the large-integer test retained its >2^53 condition
by increasing its test-only scale. Three DZN examples were regenerated with
unchanged teaching modes. **108 focused planning/holding/executor tests and
14 role-context tests passed** on this experimental revision.

The full offline comparisons did not support promotion. Attempt 15 found
11/39 from 66 checks (55 clean, six omitted, five altered), with 26 fixed-view
segments and one entity-5 pursuit. Balanced MSE worsened from 0.057244 to
0.066399. Four optimal/oracle-matching solves took 10.98, 9.00, 4.72 and 1.13 s;
42 gate assessments; rollout 258.78 s; final time 299.5 s.

Attempt 16 stayed at 10/39 with 45 checks (35 clean, four omitted, six altered),
11 fixed-view segments plus one entity-2 pursuit. Balanced MSE slightly worsened
from 0.087886 to 0.088357. Four verified solves took 9.11, 1.97, 1.10 and 0.27 s;
33 assessments; rollout 134.21 s; final time 303.5 s.

Agent `bf9f7bf` reverts `d18ff77`; both commits are pushed, preserving the failed
experiment without promoting it. `src`, `tests`, `conf` and `scripts` match
`413367a` exactly after restoration, including skill 2.18.0. **120 restored
focused tests passed.** This experiment does not show that a high information
share was the cause of low recall; simply removing normalization was insufficient.

## Combined capabilities and wider-angle visibility

Attempt 17 revisited 750 m observation windows using the retained diminishing
information/fixed-omission scoring and native optimal-face presolve. This differs
from the old failed Attempt 03 formulation. All three revisions now solved
optimally with exact oracle parity: 28.65, 8.19 and 2.39 s. The initial graph had
13,812 candidates. Recall was **12/39**, from 72 checks (60 clean, seven altered,
five omitted), 29 fixed-view segments and 48 assessments. Balanced MSE was
0.066123. The rollout took **1,103.76 s** excluding initial solve, ending at
299.5 s; the first gate took 70.18 s. Passing the solver limit therefore does
not make this configuration suitable for a fast demo.

Attempt 18 combined the previously prepared group viewpoints with the retained
score and full scheduled-sensing feedback. It found **12/39**, from 57 checks
(45 clean, seven altered, five omitted), 26 fixed-view segments and 43
assessments. Four verified solves: 14.25, 12.51, 5.23 and 3.25 s; rollout
393.98 s; end time 299.5 s; balanced MSE 0.087468. More candidate geometry did
not improve the retained result.

Attempt 19 tested the requested visibility-area direction through a copied
offline scenario with a **120-degree camera**, holding effective range at 750 m.
Configuration equality checks verified unchanged scenario/world-model settings
and all other effective runtime settings. The first copy used absolute data
paths, which the scenario loader correctly rejected; corrected relative paths
are in `attempt-19/scenario.yaml`, with generated inputs in `input-valid/`.
That setup repair is not another optimization attempt. No source scenario,
live config or AirSim setting was changed.

The wider camera produced 923 usable views (versus 881), in 61.47 s, but still
reached **12/39**, with 53 checks (41 clean, seven altered, five omitted),
23 fixed-view segments, 39 assessments and balanced MSE 0.088049. Three verified
solves: 17.21, 6.76 and 5.97 s; rollout 398.88 s; final time 299.5 s.

A public-only terminal-epoch geometry check found 26/107 reports for both the
best single view and the best two nominal/+4 s views reachable within the
3.5-second transition budget. The 120-degree single-view maximum was also 26.
These are bounds over the sampled public geometry only, not global recall bounds.
All five rollout audits preserve native windows, nonoverlap, mode/entity/pose,
unique checks and detector-window eligibility. Artifacts: `attempt-15/` through
`attempt-19/` and `attempts-15-19-audit.json`. No new full-suite or corpus run is
claimed for these restored-code/configuration comparisons.

**Completed attempts: 19/30. Best verified public-evidence native recall is
13/39 (33.33%); the above-50% objective remains active.**

## Remaining work

Next, close an evaluator coverage gap before further tuning: ADR 0010 specifies
continuous maneuver-independent perception, whereas the current offline loop
counts scheduled surveillance only and skips transit/early-arrival waiting.
Compare continuous perception along an ideal cardinal execution of the same
native plan, preserving four headings, speed/turn budgets, observed-only belief
updates and the 10% gate. Attribute any gain to this expanded execution/evaluation
scope unless a matched control proves a planning improvement. Keep AirSim and
physical lifecycle ownership unchanged; stop for review if that cannot be done.

Of the best Attempt 07's 26 missed issues, **nine
had no scheduled sensing anywhere in their four-second detection window**;
seven earlier and ten final-epoch misses had some sensing but were not detected.
This evaluator-only timing diagnostic identifies a scheduling limitation; those
hidden timestamps may not become planner targets. More viewpoints alone did
not resolve it. A public-only geometry diagnostic
at the final crowded epoch found 107 public reports, but both the best existing
single heading and the best same-pose four-heading union cover only 26 of them.
Rotation alone is therefore not an established remedy on these sampled poses.
This is not a global coverage bound or a recall measurement. No hidden event-time
locations or corruption labels may enter planning.

The co-timed information correction is now implemented, but a full route-wide
information budget remains more demanding than a stateless per-candidate score.

Broader/multi-heading window formulations must preserve unique report credit
and stay within the executor limit. No successful >50% result is claimed.
