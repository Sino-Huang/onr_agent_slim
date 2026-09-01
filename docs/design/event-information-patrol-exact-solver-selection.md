# Exact solver selection for event-information patrol

Date: 2026-08-28

## Recommendation

Keep the observation-action DAG. Do not assume that MiniZinc with COIN-BC is
the fastest way to solve it.

The fastest structurally matched exact algorithm is a longest-path dynamic
program over the DAG's topological order. It is not a heuristic: processing
each vertex and edge once gives an exact result in `O(V + E)` time. The current
generator already implements this recurrence as `longest_path_oracle`.

For the current architecture, make the changes in this order:

1. Preserve MiniZinc as the external planning authority, but replace the
   standard `network_flow_cost` decomposition with flow-conservation
   constraints over precomputed sparse incoming and outgoing arc lists. This
   removes the known flattening pathology without changing
   [ADR 0009](../adr/0009-external-planner-authority-and-fsm-execution.md).
2. Retain COIN-BC for that sparse formulation. The local like-for-like result
   had an 8.06-second median with COIN-BC versus 122.56 seconds with HiGHS.
3. Continue using the direct DAG dynamic program as the independent exact
   oracle. If planning latency still matters enough to make it authoritative,
   adopt it through an explicit amendment to ADR 0009.
4. Use native HiGHS only if future linear coupling constraints invalidate the
   DAG recurrence. Use a dedicated min-cost-flow implementation if the problem
   remains pure flow but gains cycles, multiple supplies, or multiple demands.

Constraint-programming alternatives are out of scope by project direction; no
CP result is used in this assessment or recommendation.

## Implemented path

The checked-in event-information patrol example now implements recommendation
1 and 2: it uses the controlled vehicle's full advertised FoV radius and
maximum velocity, serializes one-based sparse incoming/outgoing incidence
indexes, expands flow conservation only over each node's incident arcs, and
executes through COIN-BC. The temporary 30 m FoV and 20 m/s planning caps have
been removed.

The final checked-in 100 m benchmark generated the 3,089-action instance in
2.33 seconds and solved it in 7.89 seconds end to end. MiniZinc flattening took
4.16 seconds and COIN-BC solving took 2.60 seconds, returning the same optimal
gain of 21,572 and 16 stops with no solver stderr. This remains within the
existing 30-second executor limit without reducing physical capabilities.

## Supplied local measurements

These are project measurements supplied with the investigation, not claims
from the cited sources.

| Planning geometry | Actions | Full compatibility arcs | Reduced arcs |
|---|---:|---:|---:|
| 30 m FoV | 786 | 127,455 | 14,423 |
| 100 m FoV | 3,089 | 1,661,195 | 145,400 |

The uncapped 100 m MiniZinc/COIN-BC run completed with an optimal solution in
165.57 seconds. MiniZinc compilation and flattening consumed 161.74 seconds,
COIN-BC solving consumed 2.57 seconds, and peak RSS was approximately 1.17 GB.
COIN-BC needed no branch-and-bound nodes. The smaller 30 m live run verified
its first planner attempt in approximately 12.5 seconds.

The same full-FoV planning case was then benchmarked through three exact
alternatives:

| Exact execution path | End-to-end time | Relative to original |
|---|---:|---:|
| Original MiniZinc `network_flow_cost` + COIN-BC | 165.57 s | 1.0x |
| Sparse-incidence MiniZinc + HiGHS | 122.56 s | 1.35x faster |
| Sparse-incidence MiniZinc + COIN-BC | 8.06 s median | 20.5x faster |
| Direct action-DAG dynamic program | 2.01 s | 82.4x faster |

The direct DAG run was 4.0 times faster than the ADR-preserving MiniZinc
median. It used approximately 201 MB peak RSS, compared with approximately
1.14 GB for sparse MiniZinc/COIN-BC. The three sparse COIN-BC runs completed in
7.84, 8.06, and 8.11 seconds. These timings establish COIN-BC as the better of the two locally tested
MiniZinc backends for this sparse formulation; they are not general solver
benchmarks.

An earlier repository experiment on the fake-environment instance enumerated
895 actions and solved the action DAG, including unoptimized quadratic graph
construction and output, in approximately 0.5 seconds. See
[Event-information patrol optimization](event-information-patrol-optimization.md).

The original measurements identify translation as the immediate bottleneck.
The sparse follow-up removes that confounder and provides the local backend
decision above.

## Why the current MiniZinc global is expensive

MiniZinc translates a model and its data to FlatZinc before invoking a solver;
fixed comprehensions and `forall` expressions are evaluated or unrolled during
that process ([MiniZinc flattening documentation](https://docs.minizinc.dev/en/stable/flattening.html)).

The standard-library definition of `fzn_network_flow_cost` computes the
weighted cost and, for every node, forms outgoing and incoming sums by filtering
the complete arc set ([MiniZinc standard-library source](https://raw.githubusercontent.com/MiniZinc/libminizinc/master/share/minizinc/std/fzn_network_flow_cost.mzn)).
For a backend using this decomposition, it follows that the compiler examines
a node-by-arc product to build the sparse balance equations. This is an
inference from the official source and flattening semantics, and is consistent
with the supplied 161.74-second flattening versus 2.57-second solve receipt.

Changing only `--solver coin-bc` to another backend does
not remove that front-end work. A sparse MiniZinc formulation should instead
serialize each node's incident arc indices once so each arc participates in
only its two endpoint balance equations.

## Exact alternatives

| Approach | Exact for the current problem? | Avoids current flattening? | Assessment |
|---|---|---|---|
| MiniZinc `network_flow_cost` + COIN-BC | Yes | No | Current baseline. The supplied run shows solver search is not the bottleneck. |
| Sparse MiniZinc flow + COIN-BC | Yes | Avoids the node-by-all-arcs expansion | Recommended under ADR 0009; 8.06-second median locally. |
| Sparse MiniZinc flow + HiGHS | Yes | Avoids the node-by-all-arcs expansion | Exact, but 122.56 seconds locally and therefore not selected. |
| Native HiGHS LP/MIP | Yes | Yes | Useful when additional linear constraints break the simple recurrence; still a generic optimizer. |
| Dedicated min-cost flow | Yes | Yes | Natural if the graph later gains cycles or general supplies/demands while remaining pure flow. |
| Direct longest-path DAG dynamic program | Yes | Yes | Best fit and fastest locally at 2.01 seconds: one topological pass, `O(V + E)`. Requires an ADR change before becoming production authority. |

MiniZinc officially describes COIN-BC and other MIP backends as solvers for the
flattened linear model ([MiniZinc solver backends](https://docs.minizinc.dev/en/stable/solvers.html)).
COIN-OR describes CBC as a callable branch-and-cut MIP library
([CBC introduction](https://coin-or.github.io/Cbc/intro.html)). These
capabilities do not make branch-and-cut necessary for a DAG.

HiGHS can receive a constraint matrix directly in compressed sparse row or
column form and can solve either LP or MIP models
([HiGHS C API](https://ergo-code.github.io/HiGHS/stable/interfaces/c_api/));
its Python interface can also construct and optimize linear models directly
([HiGHS Python modelling](https://ergo-code.github.io/HiGHS/stable/interfaces/python/model-py/)).
This bypasses MiniZinc flattening but does not beat the DAG recurrence's
algorithmic specialization.

For a pure network formulation, OR-Tools exposes `SimpleMinCostFlow` directly
in Python, C++, Java, and C# and returns an optimal flow status
([OR-Tools minimum-cost flow](https://developers.google.com/optimization/flow/mincostflow)).
That is a sound exact middle ground, but the present single-source,
single-sink DAG is simpler still.

## Why the direct DAG method is exact

Each action has an additive weight combining information gain and the stop
tie-break. Each feasible transition goes forward in time, so the action graph
is acyclic. A path into an action can therefore be optimized solely from the
best already-computed path into each predecessor; no future decision can alter
the reward of that prefix. Recording the winning predecessor reconstructs the
same source-to-sink plan represented by one unit of network flow.

MIT's official algorithm notes specify topological ordering followed by one
pass that relaxes each outgoing edge, taking `Theta(V + E)` time
([MIT OpenCourseWare, Lecture 17](https://ocw.mit.edu/courses/6-006-introduction-to-algorithms-spring-2008/de8d43b3546789a5dc677ae5c5915ff0_lec17.pdf)).
Boost's official graph documentation likewise implements weighted shortest
paths on DAGs and exposes custom comparison and combination operations
([Boost DAG shortest paths](https://www.boost.org/doc/libs/latest/libs/graph/doc/html/graph/algorithms/shortest_paths/dag_shortest_paths.html)).
Negating weights or reversing the comparison gives longest path with the same
complexity.

This reduction must be revisited if rewards overlap across selected actions,
actions consume shared resources not encoded by time, multiple vehicles share
event rewards, or another cross-path constraint makes the value of a prefix
depend on choices outside that prefix. In those cases, native HiGHS or another
general exact formulation becomes appropriate.

## Acceptance gates before changing authority

Use identical 30 m and 100 m generated graphs and record:

- graph-generation, flattening, and solve time separately;
- peak RSS;
- terminal optimality status;
- information gain, stop count, and reconstructed action sequence; and
- parity between direct DP, sparse MiniZinc/COIN-BC, and sparse
  MiniZinc/HiGHS across deterministic generated cases.

Only a terminal optimum with an identical objective and valid reconstructed
route counts as parity. Backend speed should be decided from this benchmark,
not from general solver descriptions.
