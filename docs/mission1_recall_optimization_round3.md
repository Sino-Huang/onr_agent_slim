# Mission 1: realistic-range, route-dependent information investigation

## Attempt 12 — broader camera-aware viewpoint family

A report-coverage audit found no missing promised-report signal in completed
Attempts 04/10/11: respectively 45/45, 37/37 and 38/38 report IDs attached to
executed surveillance assignments eventually receive checks. This is not a
claim that every check occurred in its scheduled assignment; early/transit
checks also count. All three detect seven corrupted outcomes at t=299 s, with
three/three/two earlier detections. Artifact: `executed-report-coverage.json`.

Attempt 12 keeps corrected 300 m range, H=45, nominal/+4 s observations and
functional `4cda00e`. It replaces offset-only sampling with the existing
camera-facing pair/group views plus base centres/midpoints and 25 m offsets.
Native preparation: **2,266 views in 151.813 s**. The largest single-view terminal
public-report count stays at 18; no larger geometric coverage bound is claimed.

Completed result: **11/39 (28.21%)**, balanced MSE **0.066891006**, 54 unique
checks (43 clean, 5 omitted, 6 altered), final time 303.5 s. This is a new best
for this corrected 300 m series, not a comparison against the historical 750 m
11/39 run. The initial assignment exactly matches Attempt 10; subsequent
evidence-conditioned routes differ. The extra issue is at event time 295.5 s;
terminal corruption detections stay at seven. No pursuit is executed.

All **17 native revisions** are optimal and independently match primary score,
maneuver count, duration and report route. Maximum/total native solve time:
**9.745/41.926 s**; rollout **217.094 s**. Execution audit passes: continuous
transit/wait/surveillance totals 201/79/23.5 s, with 1/1/9 issues. Independent
audit takes 33.87 s. Artifacts: `attempt-12/{input,evaluation,audit.json,count-state-audit.json}`.
No production code, sensor parameters, evidence contracts or live workflow
changes in this experiment; existing 833-test Agent/101-test replay checkpoint
remains the source verification. All experiment processes are terminal.

Retain Attempt 12 as the realistic-range comparison point. This modest gain
supports examining viewpoint geometry further, not claiming generality or
adequate mission recall. The goal remains active at 12/50, requiring 20/39 for
the recall exit. All 223 completed-replay revisions audited so far match the
independent reference; `ledger-audit-12.json` is the current count audit.

## Count-history reduction checkpoint

### Bounded multi-view implementation checkpoint

#### Attempts 10 and 11 completed

On `4cda00e`, H=45 with the unchanged nominal/+4 s input completes at 303.5 s:
**10/39**, balanced MSE **0.0693578436**, 50 checks (40 clean, 5 omitted, 5
altered). Twenty native revisions are optimal and independently match score,
maneuver count, duration and report route. Maximum solve **3.531 s**, rollout
**122.190 s**. Both execution and count-state audits pass. The final revision
selects multiple views of the 299 s epoch, but total recall ties the old control.
Transit/wait/surveillance seconds: 216.5/67.5/19.5; issues: 1/8/1. No pursuit.

Full Agent non-live suite: **833 passed**, 21 deselected, 277.36 s. Physical
replay tests: **101 passed**, 6.18 s. Artifacts: `multiview-full-agent.xml`,
`attempt-10/{audit,count-state-audit}.json`. All 188 completed-replay revisions
audited so far match the independent reference. `ledger-audit-10.json` passes.

Attempt 11 keeps H=45 and 300 m, offering observation delays 0/1/2/3/4 s rather
than only 0/4 s. Native preparation produces 2,548 views in 66.503 s. The replay
completes at 303.5 s with **9/39**, balanced MSE **0.0840205066**, 50 checks
(41 clean, 4 omitted, 5 altered). Its 18 native revisions match independent
score/maneuver/duration/report-route audits; continuous execution audit passes.
Maximum/total solver time is **17.400/54.935 s**, rollout **254.962 s**.
Transit/wait/surveillance is 219/64.5/20 s, with 1/7/1 issues. This is worse in
recall, estimation and cost than Attempt 10; do not retain the finer sampling as
the preferred configuration. Its artifacts are under `attempt-11/`.

Eleven configurations are now terminal and audited, with 206 independently
audited completed-replay revisions. No jobs remain running. Further timing
sweeps are not justified by this result; next inspect missed planned public
observations and camera candidate coverage. Neither graph correctness nor
more candidate choices guarantees higher realized corrupted-event recall.

Bounded information-aware planning now admits physically reachable visits to
different vessels within the same report epoch. It tracks selected vessel/epoch
batches until their last alternative, preventing nonadjacent report reuse and
double credit for a shared report-owned omission cell. Reports within one
vessel/epoch remain an atomic batch; this is not unrestricted report-subset
scheduling. Counts still forget only future-irrelevant vessels. The independent
oracle uses batch sets and full count vectors; native graph expansion uses bit
masks and reduced counts. Source choices and positive-intermediate arcs remain
unpruned until histories are lifted, when ordinary positive-node dominance is
safe again. No production MiniZinc model or physical command changes.

The existing optional information-horizon API enables this bounded behavior;
unconfigured full-horizon/live behavior remains unchanged. Both advisory solving
and the gate use the shared builder. The exact 10% gate is unchanged. The small
cardinal-turn fixture now schedules two distinct-vessel reports at the same
epoch and refuses a nonadjacent repeat. A separate same-vessel/epoch report
test protects shared omission-cell ownership. Real MiniZinc matches the fixture.

**121 focused tests pass in 19.79 s**; changed-file Ruff passes excluding the
existing TRY004 rule. Initial actual H=45 public snapshot: 172 lifted candidates,
0.263 s generation, 0.335 s native optimal solve with oracle parity. Artifact:
`multiview-h45-probe/`. Full Agent non-live and Physical replay tests are running;
no full-suite or recall result is claimed at this checkpoint. Next genuine
configuration is Attempt 10: same corrected 300 m input and H=45, changing only
bounded multi-view semantics, compared against Attempt 04.

### Explicit downstream-decision diagnostic

#### Acquisition-aware extension

The helper's `--acquisition-aware` mode evaluates each reachable singleton
fixed-view first observation, then filters the suffix by its actual end time,
position, heading, turn/travel budget, report chronology and already consumed
report. All alternatives share one absolute horizon. Branch probabilities for
this known published report condition on presence: clean `(1-E[p])/(1-E[pq])`
and altered `(E[p]-E[pq])/(1-E[pq])`; omission of that report is impossible.
Branch posteriors use the existing actual-outcome likelihoods. No synthetic
check enters a runtime snapshot. Future rewards retain the diagnostic's existing
public-recall utility, not a newly calibrated full-mission success objective.

| Public snapshot / horizon | Feasible singleton first views | Adaptive versus fixed first choice | Best first choice's downstream decision value |
| --- | ---: | --- | ---: |
| Prior / 90 s | 580 | Same | Approximately zero |
| Prior / 120 s | 1,030 | Same | 0.0301651 |
| Observed evidence at 71 s / 45 s | 285 | Same | Zero |

The prior probes choose the same view of entity 15 at 25–25.5 s; the observed
snapshot chooses entity 18 at 89.5–90 s. These are derived results, not rules.
At H=120 the optimal suffix changes between clean/altered outcomes, but the
acquisition decision does not. Artifacts: `acquisition-prior-h90/`,
`acquisition-prior-h120/`, `acquisition-evidence-h45/`. Both outcome branches of
each selected best acquisition have real MiniZinc score/report-route parity
(six native verifications total); other acquisitions use the Python oracle.

This removes the free-check assumption but remains limited: singleton first
views, no incidental transit checks, no later evidence branching, no estimation
or omission-exposure reward, and no enacted replacement gate. It does not
establish live feasibility or improved recall. The three probes are diagnostics,
not added optimization configurations. Three additional tests cover conditional
presence, turn/chronology/consumed-report filtering and zero future value after
all opportunities have passed. **119 focused tests pass in 19.53 s**; Ruff clean.

Do not promote a per-candidate information bonus on this evidence. The next
coverage change should address the independently reproduced single-view-per-
report-epoch restriction while preserving nonadjacent report uniqueness.
Downstream adaptation remains a possible later extension, not disproven by
these restricted probes. Best remains 10/39 at 300 m; active count 9/50.

`scripts/inspect_mission1_downstream_value.py` now measures a different quantity
from G(n): expected optimal future public-recall utility after a hypothetical
check minus optimal expected utility when the route must be chosen before its
outcome. The existing conditional-grid posterior supplies clean/altered/omitted
branch probabilities and updates, including shared omission-rate coupling.
Public outcome counts reconstruct and verify the posterior; hypothetical checks
exist only in private diagnostic copies and never become runtime evidence.

This is deliberately **not an executable policy or recall configuration**. It
assumes one actual-event check can be supplied at the current time/pose. It does
not price acquisition travel, observation availability, or distinguish the
conditional outcome distribution of a specifically published report. It scores
future public recall only, without estimation or hidden-omission rewards. It
rebuilds graph arcs for that objective rather than retaining pruning justified
only by the omitted rewards. Four-direction timing and existing report-window
uniqueness remain unchanged. It does not enact the 10% replacement gate.

At the initial public snapshot with H=90, all twenty vessels have identical
one-check variance reduction (0.03683335). Yet only entities 2, 9 and 10 have
positive decision value beyond integer-rounding tolerance: respectively
0.0237435, 0.0386535 and 0.0149099 weighted recall units. The remaining seventeen
choose the same route under every outcome. **All sixty branches** have native
MiniZinc score/report-route parity on the 582-candidate graph. Artifacts:
`downstream-prior-h90/`. Entity IDs are diagnostic results, not production rules.

At the observed-evidence snapshot at t=71 with H=45, five vessels have positive
decision value (2, 7, 9, 11, 16); the largest is 0.0287374. This second probe uses
the Python oracle only, on 285 candidates (`downstream-evidence-h45/`). Because
pose, time, evidence and horizon differ, this is not a controlled causal claim
about evidence alone. Both probes leave the 39-outcome recall result unchanged.

Three tests verify positive value when outcome changes the future choice, zero
decision value despite positive variance reduction when no choice exists,
posterior martingale consistency, unchanged source belief, and reconstruction
after mixed evidence. Combined focused suites: **116 passed in 20.01 s**;
changed-file Ruff passes. No production scoring changes, AirSim or live changes.

Next: evaluate obtaining an actual reachable early check and its remaining
decision set, including acquisition cost and outcome availability. Do not add
these diagnostic per-vessel values to every candidate: that would repeatedly
credit the same downstream benefit. Goal remains active at 9/50 configurations,
best 10/39 at 300 m; all probes are terminal.

### Completed H=90 verification replay

On Agent `bd70868`, the formerly failing H=90 configuration completes at
303.5 s with **10/39 recall**, 53 unique checks (43 clean, 4 omitted, 6 altered),
and balanced MSE **0.0825908824**. This ties H=45 recall and slightly improves
its estimation error, but does not establish a new recall best. All 17 native
revisions are optimal; maximum solve time is **22.658 s**, below the unchanged
30 s limit. Total rollout is **370.083 s**. The formerly failing sixth revision
now solves in 21.655 s with 30,854 lifted candidates rather than 113,566.

Continuous execution audit verifies unique credit and detector eligibility.
Transit/wait/surveillance totals are 195.5/67.5/40.5 s; issue counts are 0/1/9.
All twenty scheduled sensing segments are fixed views. Independent full-count
oracle auditing matches primary score, maneuver count, duration and report route
on every revision (56.79 s audit). Artifacts are under
`h90-count-forgetting-replay/`, including both audits and all native revisions.

This is a verification rerun of existing Attempt 02 after a representation-only
repair, **not a tenth distinct optimization configuration**. Preserve the old
timeout evidence. The active count remains 9/50; realistic-range best remains
10/39. No process remains running. This result rules out the expectation that
merely enabling this longer horizon would increase recall. Next investigate
explicit downstream evidence value and within-window multi-view choices.

The bounded information graph now forgets a vessel's count only when that
vessel is absent from the current and all later candidates. Earned prefix
utility is retained, and current-observation counts remain available to price
their marginal information. This changes neither beliefs nor the objective.
The independent full-count oracle is unchanged. Materialized candidate indices
can change, so arbitrary final index tie choices are not promised identical.

Two regression tests failed before this change and pass afterward; the focused
planner/information suites pass **113 tests in 18.74 s**, including native
MiniZinc parity. Changed-file Ruff passes excluding the existing TRY004 rule.
The initial H=90 public-snapshot probe shrinks from 4,892 lifted candidates /
68,176 arcs to **1,837 / 49,293**. Native solving is optimal in **1.567 s**;
primary score remains 3,314,555 with independent full-count oracle agreement.
Artifact: `forget-counts-h90-probe/`. This is not a recall configuration or proof
that the formerly failing later H=90 revision will now solve.

A separate minimal probe confirms the chronological-report restriction rejects
two distinct co-timed reports observed at 100 and 101 seconds with a feasible
0.5-second cardinal turn. Removing that restriction alone is unsafe because it
also protects nonadjacent report uniqueness. Multi-view observation-window
planning remains future work, not implemented in this checkpoint.

## Goal and boundaries

Fresh user-authorized limit: **50 genuine optimization configurations**, or
verified recall **at least 50%**. Keep the same 39 corrupted outcomes, so the
recall condition requires 20 detections. Previous series do not count. Baseline
measurements, diagnostic probes and unit tests do not count as optimization
attempts. Current status: **active; 12/50 completed configurations; best 11/39
(28.21%) at 300 m visibility**. The chronological checkpoints below preserve
their original intermediate counts; the ledger is authoritative.

The user rejected 2,000 m visibility as unrealistic. Primary acceptance uses
the existing configured **300 m** range and 90-degree camera, with corrected
Physical `ef05954` visibility. Do not qualify using the earlier 2,000 m result.
Keep 30 m/s advertised speed, the 0.9 planning reserve, four headings, 0.5 s
quarter turns and the four-second detector window. No AirSim/vLLM, source
scenario, live-default, launcher, physical command or evidence-contract change.
Hidden truth is evaluation-only; no truth-specific targets or weights.

Initial Agent checkpoint: `78b1440` (functional scoring `8ce91d5`). Physical:
`ef05954`. User-owned untracked `statechart.json` remains untouched. All runtime
artifacts are under Agent `var/mission1-recall/optimization-round-3/`.

## Corrected-camera baseline: completed, not an optimization attempt

Copy `config/harbor_world.yaml` with the existing path-driven range helper,
retaining its 300 m range and 100-cell partition. Use the same public schedule
and prior as previous comparisons; regenerate native views with 25 m
offset-only sampling and nominal/+4 s observation choices. This matches the
earlier 300 m experimental candidate family, not a new live default.

Preparation: `baseline/input/`, **1,007 views in 66.16 s**. Scenario:
`preparation/range-300m/scenario.yaml`. Prior remains
`fixed-runs-integrated-demo-001/case-2/belief.json`. An initial CLI typo supplied
`0,4` rather than separate `0 4` arguments; parsing rejected it before any
preparation. The corrected invocation completed; neither invocation is an
optimization attempt.

Continuous-feedback replay: **1/39**, balanced MSE **0.13100216**, 46 checks
(45 clean, one altered), two native optimal/oracle-equal revisions. Initial
6,192 candidates; generation 13.75 s; solve 9.232/2.453 s; rollout 192.03 s.
Twenty-nine gate assessments replace the route at 165 s. It executes eleven
fixed-view segments and one pursuit of entity 2. Sensing spans 299.5 s:
165.5 transit, 33.5 early wait, 100.5 surveillance. Its single issue is detected
during surveillance. This confirms the known poor realistic-range combination;
it does not imply all 300 m policies are limited to one detection.

`baseline/audit.json` verifies unique credit, detector eligibility, continuous
sensing, selected poses/modes and native parity against previous-series
Attempt 13. Initial assignments match that old-camera control. No 800 s live
Mission acceptance is claimed; the helper covers the event-schedule route,
using ideal obstacle-free cardinal transit, fresh partitions and perfect-radius
pursuit. These assumptions remain explicitly separate from live perception.

## Verified scoring limitation

Current uniform allocation gives a vessel's selected n reports n/N of its
full remaining schedule's bounded information G(N), where
`G(k) = V*k*g / (V + (k-1)*g)`. Here V is current variance, g is expected
one-check variance reduction and N includes reports the route may never visit.
The selected-route alternative uses G(n) itself, retaining the same existing
normalization and 50% information weight. Both remain additive-precision
approximations, **not exact Bayesian multi-check expectation**.

`scripts/inspect_mission1_route_information.py` compares the two on an existing
native solution using only public environment/belief/report IDs. It does not
optimize a route, observe events or report recall. It rejects repeated or
unavailable report IDs, preserving the production checked/expired filtering.
Outputs must be inside the caller-provided Agent `var/` directory.

The baseline initial route selects 49 reports from ten vessels. Uniform
information credit is **3.073813**, versus **7.221391** for selected-count
credit on exactly that same route. This is a change of scoring interpretation,
not a recall improvement. For a prior vessel with twenty remaining reports,
the first chosen observation gets less than 0.05 under uniform allocation,
versus 0.5 under selected-count allocation. Selecting the entire schedule
recovers the same bounded information total in both models.

Six focused diagnostic tests verify first-check value, full-schedule budget
equality, independent vessel budgets, diminishing marginals and variance cap,
checked/expired/duplicate exclusion, and an actual native COIN-BC counterexample.
In the small diamond graph, additive credit prefers repeated-vessel branch A;
selected-count credit prefers diverse branch B, agreeing with exhaustive
enumeration. The small native fixture initially needed explicit `array1d`
index coercion for its zero-based lookup; corrected tests pass. Combined with
existing Mission 1 planning tests: **89 pass in 15.30 s**.

Production's one-best-prefix recurrence, objective potentials and terminal
dominance are **correct for its existing additive objective**. They are not
generally valid for a history-dependent objective. Changing only candidate
numbers while retaining those proofs would not implement the proposed model.
Early checks' downstream effect on later decisions is a further question:
G(n) alone still does not reward obtaining the same evidence earlier.

## Direct native formulation: solver feasibility probe

`scripts/benchmark_mission1_route_information.py` generates an explicitly
experimental model under caller-provided Agent `var/`. It uses binary candidate
selection, continuous path flow, per-vessel selected report counts and bounded
information lookup tables. It disables terminal dominance inside its own
process, because terminal reward depends on the selected prefix. It requires
strictly positive candidate recall so existing positive-intermediate reduction
continues to increase total reward even when information diminishes.

This script is a numerical benchmark, **not another executable Mission planner**:
Hyper does not invoke it; it publishes no accepted plan or Statechart. It does
not yet implement lexicographic maneuver/duration tie breaking, gate parity or
downstream evidence lookahead. Those remain required before a recall attempt.

First full-size probe: 6,192 candidates, **444,856 arcs**, generation 9.84 s;
the native invocation reaches its **30 s timeout**, with no optimal result.
Artifact: `solver-probe/`. This is a formulation feasibility failure, not a
recall result or one of the 50 optimization configurations. A statistics-enabled
repeat also times out at 30 s without emitting compilation statistics. The
separate `--compile-only` control **also times out at 30 s**, proving compilation
alone exceeds the executor budget before any optimization search. Artifacts:
`solver-probe-statistics/` and `compile-probe/`. No process remains running.
Do not reuse additive optimal-face pruning merely to make the changed objective
fast. The positive tiny native test establishes that the selected-count lookup
is supported; the real-size control establishes that this explicit-flow
representation is not yet practical within the existing limit.

Next: establish a tractable route-conditioned formulation, preserve independent
small-graph verification, then integrate the single production model, oracle,
active-plan gate and explainability consistently. Verify a full 300 m replay
before counting an optimization attempt or claiming improvement. Consider a
compact time/choice formulation if explicit arc flow is the bottleneck; no
physical workflow or evidence contract change is implied.

## Equivalent encodings and network compression controls

The next controls keep the same public candidate family and selected-count
primary objective. They are solver feasibility probes, not completed Mission
optimization configurations or recall measurements.

- **Concave envelope**: replace variable-indexed G(k) lookup with linear upper
  bounds on information credit. For a discretely concave table these bounds
  equal G(k) at every integer count. A native assertion rejects a table whose
  rounded increments are not concave; no lower envelope is silently substituted.
- **Shared suffixes**: factor identical successor-list suffixes through
  zero-utility, report-free network hubs. This preserves candidate paths but
  unexpectedly expands this instance because its suffixes rarely coincide.
- **Shared intervals**: factor consecutive successor ranges through a balanced
  tree of candidate leaves. This reduces edges without pruning candidate paths.
- **Compiler control**: retain the direct envelope formulation but disable
  optional MiniZinc flattening optimizations (`-O0`). Production executor flags
  and its 30 s limit remain unchanged.

| Probe directory | Nodes | Edges | Generation seconds | Native result |
| --- | --- | --- | --- | --- |
| `envelope-probe` | 6,194 | 444,856 | 9.93 | 30 s timeout |
| `suffix-envelope-probe` | 334,584 | 662,973 | 10.79 | 30 s timeout |
| `interval-envelope-probe` | 12,386 | 334,017 | 10.84 | 30 s timeout |
| `direct-envelope-o0-probe` | 6,194 | 444,856 | 9.84 | 30 s timeout |

The suffix benchmark was launched before its CLI was consolidated; reproduce
it with `--arc-compression suffix`. Current choices are `none`, `suffix`, and
`interval`. All above use `--information-encoding envelope`; the last additionally
uses `--no-flatten-optimize`. No variant produced an optimal full-size result.
Only the earlier direct compile-only control proves compilation alone exceeds
the limit; do not report a measured compilation/search split for these timeout
runs. The interval reduction is about 25%, insufficient for the full invocation.

Verification: both encodings and all three network representations agree with
exhaustive enumeration on the native diamond fixture. Each compressor also
preserves every candidate path across all 1,024 edge subsets of a five-node
ordered graph. **14 diagnostic tests pass**; combined with existing planner
tests, **97 pass in 17.86 s** (`test-compressed-focused.xml`). Changed-file Ruff
passes. These tests prove the stated small-fixture semantics, not integration
with the live planner or full-size recall. Runtime planner/scorer/gate and
Physical code remain unchanged. No jobs remain running.

Keep the envelope and compression controls as reproducible benchmark options,
not promoted production mechanisms. Next investigate a representation that
avoids expanding the large explicit arc network (for example, choices ordered
by observation epoch with direct travel constraints), while preserving true
route-count scoring, report uniqueness, four headings, exact selected-route
verification, and the eventual lexicographic objective and gate integration.
The goal remains active at **0/50**; no baseline or diagnostic failure is used
to exhaust the user-authorized optimization budget.

## Epoch representation and tractable lookahead checkpoint

The experimental epoch representation carries position, heading, available time
and last public-report epoch between groups of candidate start times. It uses
binary candidate choices, at most one per start epoch, direct travel constraints,
explicit report uniqueness and the same selected-count information envelope.
It avoids constructing the full arc graph. Report epochs must remain on the
existing half-second grid; unsupported off-grid epochs are rejected rather than
silently rounded. This is not production integration or a change of planner
authority. Secondary objective/gate integration remains outstanding.

The MiniZinc turn formula matches Python `_navigation_turns` across all **225**
combinations of north/east movement signs and known/unknown initial/final
directions. Native small-graph tests verify the diverse-information branch when
reachable and its rejection when the same branch is too far away. A separate
returned-route verifier checks the primary score, actual Python navigation
budgets, chronology and report uniqueness. It rejects incorrect scores,
duplicate selections and impossible travel.

Full epoch data generation takes **0.27 s**, versus roughly ten seconds for the
full flow graph, but its native invocation still exceeds 30 s. Epoch lookahead
controls at 30/60/90 seconds contain 56/284/582 candidates respectively and all
also time out. The **30 s epoch compile-only** control completes in **0.97 s**
(compiler reports about 0.91 s and 20 MB), establishing that its small-window
failure is search cost, unlike the original full-flow compilation bottleneck.
Artifacts: `epoch-probe/`, `epoch-horizon-{30,60,90}-probe/`,
`epoch-horizon-30-compile/`. Do not promote this epoch formulation.

The matched flow model **does** solve the 30-second window, initially in 0.27 s.
The helper was then changed to build candidates first and construct arcs only
for the selected time window, avoiding needless full-graph generation. All
public reports and belief inputs remain available; only candidate finish times
are bounded. Terminal additive dominance remains disabled for route-dependent
scoring. Results with the direct bounded build:

| Horizon | Candidates / arcs | Generation seconds | Native seconds | Selected reports | Primary score |
| --- | --- | --- | --- | --- | --- |
| 30 s | 56 / 134 | 0.237 | 0.316 | 2 | 1.143677 |
| 60 s | 284 / 6,285 | 0.265 | 0.669 | 5 | 2.522260 |
| 90 s | 582 / 15,298 | 0.359 | 1.017 | 7 | 3.314555 |

All three are **OPTIMAL_SOLUTION** and their returned routes pass the independent
score/travel/unique-credit verifier. Direct lookup (without the envelope) at
90 s also reaches the same optimum **3.314555** in **1.267 s**. This avoids
needing a concavity assumption for tables affected by integer rounding.
Artifacts: `flow-horizon-{30,60,90}-direct-build/` and
`flow-horizon-90-lookup/`. These are primary-objective lookahead probes, not
full-Mission recall measurements or executable plans accepted by Hyper.

A posterior-conditioned control at Mission time 165 s with a 150-second horizon
contains 2,474 candidates and 128,847 arcs and still times out at 30 s
(`flow-posterior-horizon-150/`). Tractability is demonstrated only for the
measured windows, not every future state. **101 focused tests pass in 17.61 s**
(`test-horizon-focused.xml`), including 18 diagnostic tests. Ruff passes.

Next integrate the working bounded route-count formulation into the single
production model, restore lexicographic maneuver/duration preferences and
independent oracle/gate consistency, and measure a complete continuous-feedback
receding-horizon run. Prevent premature termination when a lookahead route ends
before the public schedule. Do not alter physical lifecycle/evidence contracts
to obtain that behavior without review. G(n) fixes selected-route information
allocation but is still not explicit downstream Bayesian decision value.
The 300 m baseline remains **1/39**; the goal remains active at **0/50** genuine
optimization configurations. No process remains running.

## Shared count-state integration checkpoint

Selected-count curves and integer per-vessel tables now live in
`onr.application.mission1_planning`. Route assignment information is the
difference between rounded cumulative budgets before/after each observation,
so credits telescope exactly even when adjacent fixed views merge. Duplicate
or unavailable report credit is rejected. Recall and omission components are
unchanged. The diagnostic and numerical benchmark reuse this shared curve.

An independent count-labelled path reference retains one best prefix per
original candidate **and observation-count vector**. It preserves primary
utility, maneuver count and surveillance duration. A regression demonstrates
why a weaker prefix at a report-free merge can win after a later observation.
Its 30/60 s real-case primary optima match native solves; reference times are
0.003/0.085 s.

`expand_information_states` converts reachable count histories into an ordinary
DAG. At each lifted node the marginal information is fixed; the **unchanged
production `model.mzn`** can therefore use its existing additive optimization
proof and lexicographic preferences correctly. Count-state IDs are deterministic
candidate identities, not target-specific weights or integrity features. State
expansion changes the final arbitrary candidate-index tie ordering; native
assignment parity is checked against the lifted graph, while the independent
count-labelled reference checks the semantic objective.

| Lookahead | Base / lifted candidates | Lifted arcs | Generation / native solve seconds | Independent reference seconds |
| --- | --- | --- | --- | --- |
| 60 s | 284 / 974 | 12,508 | 0.402 / 0.921 | 0.088 |
| 90 s | 582 / 4,892 | 68,176 | 1.360 / 2.319 | 0.479 |

Both are optimal, retain primary scores 2.522260/3.314555, and pass native
assignment/mode/entity/window/report/component parity plus independent travel,
unique-credit and primary-reference checks. Artifacts: `lifted-horizon-60/`,
`lifted-horizon-90/`. This is the route-count representation to integrate; no
new production model is necessary. It is not a full-Mission optimality claim.

`build_candidate_dag(..., information_horizon_seconds=H)` now bounds candidate
finish times and lifts count histories. Public schedules remain complete for
rate and belief normalization. Additive terminal dominance is disabled in this
mode; intermediate insertion is pruning-safe only with positive recall/omission
base reward, not merely standalone information. A saturated-information test
preserves the shorter route when an extra observation adds zero information.
Without H, existing behavior remains unchanged during controlled evaluation.

`Mission1ReplanGate(information_horizon_seconds=H)` builds the same advisory
graph and rescores current remaining information jointly across unique covered
reports, after its existing visibility/time filtering. It retains the exact 10%
comparison and reachability checks. Tests verify equality with the new planner
score, bounded report admission, unchanged full public schedule and invalid
horizon rejection. Agent focused suites: 110 pass before the additional
saturation test; the final route-information suite has 28 tests.

The Physical offline evaluator accepts `--information-horizon-seconds H`, passes
it to both solving and the gate, and adds a gate checkpoint when the final
assignment ends even without a new check. That checkpoint is not an automatic
replacement or a new physical lifecycle action. If planning stops before the
last public epoch plus one sensing tick, `public_schedule_complete` is false;
do not use such a partial result as full-run acceptance. The helper persists
the explicit planning context beside each native solution. **101 Physical
replay tests pass in 5.27 s**, including boundary-checkpoint cases. Production
host/launcher/default configuration, physical commands and evidence contracts
remain unchanged; live adoption is not claimed.

Next genuine configurations: H=60 and H=90 with the fixed 300 m baseline input,
same prior, corrected camera and continuous-feedback evaluator. Commit the
shared implementation before running them, preserve all terminal evidence, and
count a configuration only when its real replay/solver result is inspected.

## First four genuine configurations: terminal and audited

Functional checkpoints **Agent `9afbffc` / Physical `b5fba2b`** are committed
and pushed. All use the same corrected 300 m camera, baseline offset-only
nominal/+4 s input, public prior, 30 m/s maximum and continuous feedback.
Only the information horizon differs; no visibility or score-weight change.

| Attempt / horizon | Recall | Balanced MSE | Checks: clean/omitted/altered | Native revisions | Maximum / total successful solve seconds | Rollout / gate seconds |
| --- | --- | --- | --- | --- | --- | --- |
| 01 / 60 s | 5/39 | 0.12795602 | 36/1/4 | 16 | 9.247 / 26.798 | 134.91 / 12.96 |
| 02 / 90 s | Not evaluated | Not evaluated | Partial run only | 5 optimal, sixth timed out | Sixth reaches 30 s limit | Incomplete |
| 03 / 30 s | 0/39 | 0.16098488 | 32/0/0 | 19 | 1.461 / 7.290 | 75.45 / 4.62 |
| 04 / 45 s | **10/39** | **0.08304908** | 44/4/6 | 21 | 2.958 / 17.986 | 102.78 / 9.35 |

Successful runs reach the public schedule's end: 303.5/299.5/303.5 s for
01/03/04. All **56 revisions** are native optimal with full lifted-graph
assignment/mode/entity/window/component parity. Independent count-labelled
reference audits additionally match primary score, maneuver count and duration
for every revision, and happen to choose the same report routes in all cases.
Reference audit times: 8.03/3.69/6.65 s. The path-driven scratch auditor is
`var/mission1-recall/optimization-round-3/audit-count-states.py`.

All three execution audits pass: no duplicate public/check credit, detector
windows respected, continuous sensing maintained and selected poses/modes
preserved. Sensing transit/wait/surveillance seconds: 01 = 237.5/53.5/12.5;
03 = 173/97/29.5; 04 = 176/94.5/33. Detected issues by those phases:
01 = 1/3/1; 03 = 0/0/0; 04 = 1/0/9. Scheduled fixed-view segments:
19/18/23; no pursuit in these completed runs. More replans or faster solving
do not by themselves imply better recall, as the 30-second regression shows.

Attempt 02 fails after its gate assessment at Mission 71 s. Its sixth materialized
graph contains **113,566 count states and 1,082,392 arcs**; the native executor
reports `solver timed out after 30.0 seconds`. Five preceding revisions are
optimal. Preserve `attempt-02/evaluation/revision-006/solver/` and `audit.json`;
there is no completed recall summary, and no zero or inferred recall is assigned.
This is one genuine failed optimization configuration, not a diagnostic probe.

Latest full non-live suites pass: **823 Agent tests** (21 live excluded,
290.67 s) and **463 Physical tests** (13 live-AirSim excluded, 220.44 s).
Artifacts: `test-bounded-full-agent.xml`, `test-bounded-full-physical.xml`.
Expected test warnings are not live AirSim activity. No dependency or live
configuration changes accompany these results.

Four of the fresh fifty configurations are complete. **Current-series best is
10/39 (25.64%)**, versus its matched uniform-scoring 300 m baseline's 1/39.
It does not exceed the historical old-camera 300 m information-slot result of
10/39, and remains far below the required 20/39. Do not compare it as a gain
over the earlier unrealistic 2,000 m experiment or claim generality from one
scenario. Attempts 05/06/07 (40/50/55 s) are now running to test nearby horizons
with identical code and geometry. Keep the goal active and the issue open.

### 05–07 — Nearby horizons (completed; no new best)

These retain the same source checkpoints, corrected camera and baseline native
views. Compare with the 45-second Attempt 04, not a different sensor profile.

| Attempt / horizon | Recall | Balanced MSE | Checks: clean/omitted/altered | Native revisions | Maximum / total solve seconds | Rollout / gate seconds |
| --- | --- | --- | --- | --- | --- | --- |
| 05 / 40 s | 10/39 | 0.08356133 | 30/4/6 | 19 | 1.715 / 10.133 | 91.28 / 6.18 |
| 06 / 50 s | 10/39 | 0.08304908 | 44/4/6 | 21 | 4.568 / 24.128 | 122.24 / 11.72 |
| 07 / 55 s | 5/39 | 0.12797510 | 36/1/4 | 18 | 3.805 / 20.747 | 120.89 / 10.14 |

All three complete the public schedule at 303.5 s. Execution audits and
independent count-state audits pass for every revision. Across all six completed
replays, **114 native revisions** match the independent semantic lexicographic
reference and report routes. The separate 90-second failure remains recorded,
not assigned a recall value. `ledger-audit-07.json` verifies seven terminal
configurations; baselines and numerical probes remain excluded.

Sensing transit/wait/surveillance: 05 = 213.5/59/31 s, 06 = 201/76.5/26 s,
07 = 245/45/13.5 s. Issue counts by those phases: 0/0/10, 0/1/9 and 0/5/0.
Scheduled fixed-view segments: 20/22/19; no pursuit. Solver/gate durations are
nested inside rollout; concurrent-run timings are observations, not isolated
hardware benchmarks. The 40-second case ties recall with fewer clean checks
but slightly worse estimation; the 50-second case adds no recall/MSE gain.
Keep 45 seconds as the current experimental control, not a universal optimum.

Next configurations 08/09 retain H=45 and 300 m range, adding respectively
the already-supported 4.5-second public gap holds and 0.5/4.5-second fixed-view
dwell choices. Native preparation and replay are chained under each attempt's
directory. No new scoring weights, fabricated report targets or longer sensor
range are introduced. Preparation alone does not count as an optimization
attempt. Both are in progress; no result is claimed yet.

### 08/09 — Extra observation windows (completed; rejected)

Both retain H=45, 300 m range and source checkpoints `9afbffc`/`b5fba2b`.
08 adds 415 native public gap rows (1,422 total, preparation 111.95 s);
09 regenerates 1,007 rows with 0.5/4.5 s dwell choices (66.74 s).

| Attempt | Recall / control | Balanced MSE | Checks: clean/omitted/altered | Revisions | Maximum / total solve seconds | Rollout / gate seconds |
| --- | --- | --- | --- | --- | --- | --- |
| 08: gap holds | 4/39 / 10/39 | 0.12794667 | 35/1/3 | 18 | 1.741 / 9.564 | 96.98 / 6.47 |
| 09: long dwell choice | 4/39 / 10/39 | 0.12831211 | 37/1/3 | 19 | 5.055 / 26.911 | 142.61 / 19.00 |

Both reach 303.5 s and complete the public schedule. Execution and independent
count-state audits pass. Across all eight completed replays, **151 revisions**
match the independent semantic lexicographic reference and report routes.
The single 90-second native failure is separate. `ledger-audit-09.json` verifies
nine terminal configurations, and no jobs remain running.

08 selects three pure report-free assignments across its plan revisions, but
none executes as a surveillance segment. All seventeen executed surveillance
segments last 0.5 s. 09 executes 0.5 s and 4.5 s surveillance segments. Neither
case detects an issue during scheduled surveillance: each finds one in transit
and three during early waiting. Total transit/wait/surveillance seconds:
08 = 248.5/46.5/8.5; 09 = 239.5/37/27. More offered dwell/exposure did not
increase realized recall; do not promote either variant over the control.

An evaluator-only raw source comparison also rules out one hypothesis for this
dataset: all 234 published reports retain their paired true event's position
and timestamp; twenty alter event attributes. Private source indices were used
only to align this diagnostic and never enter planner inputs. This does **not**
validate coordinate transforms or GPS freshness. Further work should examine
early evidence and graph growth rather than assume more range, more holds or
another tiny horizon adjustment will solve the deficit. The goal remains active;
no >=50% result or live adoption is claimed.
