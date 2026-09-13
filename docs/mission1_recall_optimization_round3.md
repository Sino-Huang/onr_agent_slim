# Mission 1: realistic-range, route-dependent information investigation

## Goal and boundaries

Fresh user-authorized limit: **50 genuine optimization configurations**, or
verified recall **at least 50%**. Keep the same 39 corrupted outcomes, so the
recall condition requires 20 detections. Previous series do not count. Baseline
measurements, diagnostic probes and unit tests do not count as optimization
attempts. Status: **active; 0/50 completed optimization configurations**.

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
