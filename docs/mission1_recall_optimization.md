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

**Completed attempts: 6/30. Best verified public-evidence native recall remains
11/39 (28.21%); the above-50% objective remains active.**

## Remaining work

The next experiments should address mode-neutral omission discovery. Fixed
views still receive no omission yield, despite the detector's retrospective
window. Any new exposure-based value must avoid counting overlapping intervals
more than once and must use only public schedules, capabilities and observed
checks. Report-owned time intervals are a possible conservative allocation;
no hidden event-time locations or corruption labels may enter planning.

The co-timed information correction is now implemented, but a full route-wide
information budget remains more demanding than a stateless per-candidate score.

Broader/multi-heading window formulations must preserve unique report credit
and stay within the executor limit. No successful >50% result is claimed.
