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

**Completed attempts: 2/30. Best verified public-evidence native recall remains
11/39 (28.21%); the above-50% objective remains active.**

## Remaining work

Observation-window candidates and omission/diminishing-information utility are
not implemented by these first two attempts. Window alternatives must prevent
nonadjacent duplicate report credit; the existing DAG's adjacent disjointness
alone is insufficient once one report can be observed at multiple times. Avoid
claiming an optimal route from a relaxed network that silently double-counts it.
Broader viewpoint/window formulations must also stay within the executor limit.
