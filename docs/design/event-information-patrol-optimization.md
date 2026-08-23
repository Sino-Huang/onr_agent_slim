# Event-information patrol optimization

Date: 2026-08-23

## Conclusion

In the general case, this is best described as a **single-vehicle
prize-collecting covering orienteering problem with temporal observation
windows and variable dwell times**. That is a descriptive hybrid, not a
standard problem name. It combines an open orienteering route, time-window
scheduling, and spatial coverage: the drone visits selected intersections,
while the prizes belong to nearby event observations. It is not a team
orienteering problem unless more than one drone route is introduced.

The current fake-environment instance is substantially easier than that general
classification suggests. Its exact semantics allow every possible stop to be
precomputed as a time-stamped **observation action**. Feasible transitions only
move forward in time, and the rewards of actions on one feasible route cannot
overlap. The remaining optimization is therefore a maximum-weight path in a
directed acyclic graph (DAG), not a generic 253-slot constraint-programming
search.

A local exact experiment enumerated all 895 observation actions and solved the
resulting transition DAG in 0.5 seconds, including data parsing, graph
construction, an unoptimized quadratic transition pass, reconstruction, and
printing. It found information gain 15,221 with 15 stops and 27 captured
events. The four-stop MiniZinc incumbent has gain 11,695 and captures 20 events:
it achieves 76.8% of the optimal gain under the current model, 30.1% less than
the optimum. It is a valid and stop-efficient incumbent, but it is not a strong
solution to the objective actually encoded.

The practical recommendation is to precompute the observation-action DAG and
give MiniZinc a small, sparse maximum-weight-path model. That keeps MiniZinc as
the external planner authority required by [ADR 0009](../adr/0009-external-planner-authority-and-fsm-execution.md),
while eliminating the dense event-by-stop search that caused the current
flattening and optimization failure. A direct in-process DAG solver is useful
as a test oracle; making it the production planner would require an explicit
architecture decision because it would move planning authority into code.

## What problem is represented

The classical Orienteering Problem (OP) selects a prize-maximizing subset of
locations for a route subject to a travel budget. The original and early exact
papers establish both that definition and its computational difficulty
([Golden, Levy, and Vohra 1987](https://doi.org/10.1002/1520-6750(198706)34:3%3C307::AID-NAV3220340302%3E3.0.CO;2-D),
[Fischetti, Salazar González, and Toth 1998](https://doi.org/10.1287/ijoc.10.2.133)).
The Orienteering Problem with Time Windows (OPTW) adds service durations and
windows in which selected locations may be served
([Kantor and Rosenwein 1992](https://doi.org/10.1057/jors.1992.88)).

This patrol also has a covering-tour feature: a route visits a subset of
candidate sites so that demand at other locations is covered within a distance
threshold. That relationship is the defining distinction of covering-salesman
and covering-tour models
([Current and Schilling 1989](https://doi.org/10.1287/trsc.23.3.208),
[Gendreau, Laporte, and Semet 1997](https://doi.org/10.1287/opre.45.4.568)).
It is related to set orienteering, where visiting one member of a set earns the
set's prize, but the direction of coverage here is different: one visited
intersection can earn several event prizes
([Archetti, Carrabs, and Cerulli 2018](https://doi.org/10.1016/j.ejor.2017.11.009)).

The present model differs from textbook OPTW in four important ways:

- there is one open drone path, with no required return to a depot;
- event observations have fixed timestamps rather than ordinary visit windows;
- a stop's spatial footprint can capture several event observations during one
  variable-length dwell interval; and
- stop count is only a lexicographic tie-break after information gain, not a
  meaningful cost trade-off.

The last point matters when judging the four-stop incumbent. The scalar
objective

```text
information_gain * 106 - used_stop_count
```

means that one unit of information gain is preferred over any possible
reduction in stop count. Fifteen stops are therefore unequivocally better than
four whenever they produce greater gain. If operations genuinely prefer a
shorter plan even at the cost of missed information, the mission needs an
explicit stop, energy, or duration penalty instead of a tie-break.

## Why this instance reduces to a DAG

The reduction follows directly from the current MiniZinc constraints:

1. A used stop begins at the timestamp of a captured event and ends one tick
   after the timestamp of a captured event. For one candidate intersection,
   it is therefore sufficient to enumerate every ordered pair of relevant
   event timestamps and form the half-open interval `[start, last + 1)`.
2. All event profits are positive. Once such an action is selected, an optimum
   captures every event inside its time interval and 30 m footprint; omitting
   one can never improve feasibility or the objective.
3. Successive stops do not overlap. Because events are instantaneous, one
   event cannot belong to two actions on a feasible route. Action rewards are
   consequently additive; no captured-event bitset is needed in the dynamic
   programming state.
4. Add an arc from action `i` to action `j` exactly when `j.start` is at least
   `i.end + travel(i.location, j.location)`. Every arc advances time, so the
   transition graph is acyclic.
5. Give each action weight `106 * captured_gain - 1`, add source arcs only to
   actions reachable from the drone's initial state, and allow termination
   after any action. A maximum-weight source-to-sink path gives the same
   lexicographic objective. For formal equivalence, retain the current limit of
   at most 105 stops; a stop-count dimension in the DAG recurrence is enough.

For the current data, complete enumeration before dominance pruning produces
895 actions from 105 intersections. The measured results were:

| Plan | Information gain | Stops | Captured events | Scalar objective |
|---|---:|---:|---:|---:|
| Four-stop MiniZinc incumbent | 11,695 | 4 | 20 | 1,239,666 |
| Exact action-DAG optimum | 15,221 | 15 | 27 | 1,613,411 |

These are local derived results, not literature benchmarks. The reduction
depends on current semantics. Non-instantaneous or overlapping reward windows,
negative rewards, rewards collectable by more than one compatible action,
multiple coordinated drones, or other cross-action constraints can destroy
additivity and restore the harder covering-orienteering problem.

## Established approaches for the general problem

If future semantics invalidate the DAG reduction, the following are established
starting points:

| Approach | Primary-source evidence | Fit here |
|---|---|---|
| Arc-flow MILP with branch-and-cut | The classical OP branch-and-cut uses valid inequalities, exact and heuristic separation, and incumbent heuristics ([Fischetti et al. 1998](https://doi.org/10.1287/ijoc.10.2.133)). | Natural after generating sparse observation patterns; coverage variables link event prizes to selected patterns. |
| Bounded bidirectional dynamic programming | Decremental state-space relaxation is an exact OPTW method ([Righini and Salani 2009](https://doi.org/10.1016/j.cor.2008.01.003)). | Relevant when route labels need time and visited/reward state. More complex than the current additive DAG. |
| Pulse/labeling with dominance and bounds | A problem-specific pulse framework is exact for OPTW and reports strong benchmark performance ([Duque, Lozano, and Medaglia 2015](https://doi.org/10.1016/j.cor.2014.08.019)). | Useful when observation choices remain time ordered but require resource labels or dominance rules. |
| Branch-and-price | Route-column generation is an established exact approach for team orienteering ([Boussier, Feillet, and Gendreau 2007](https://doi.org/10.1007/s10288-006-0009-1)). | Becomes relevant if the Mission introduces multiple drones and shared event rewards. |
| Constraint programming | An exact TOPTW CP model based on interval variables, global constraints, domain filtering, and custom branching found many best-known solutions and proved some optima ([Gedik et al. 2017](https://doi.org/10.1016/j.cie.2017.03.017)). | Suitable for richer scheduling constraints, but only with a compact action/interval representation. |
| Iterated local search | Insertion and shaking provide fast high-quality TOPTW incumbents, including under short benchmark limits, without optimality proofs ([Vansteenwegen et al. 2009](https://doi.org/10.1016/j.cor.2009.03.008)). | A reasonable fallback for large generalized instances, not needed for the current exact DAG. |

The literature does not imply that every instance with a standard name is
easy. OP and OPTW are NP-hard in general, and papers reporting hundreds of
nodes use specialized formulations, bounds, dominance, cuts, or neighborhood
search—not a symmetric slot for every input event.

## MiniZinc formulation guidance

MiniZinc's own guidance directly explains why the current encoding performs
poorly. It recommends tight bounds, fewer and smaller-domain decision
variables, direct constraints, effective sparse generators, global
constraints, and symmetry breaking where necessary
([Effective Modelling Practices](https://docs.minizinc.dev/en/2.9.1/efficient.html)).
The current formulation instead creates 105 potential stop slots, lets
every slot choose any of 105 intersections and broad start/end times, lets
every one of 253 events choose a slot, and places nested implications and
existentials across the dense slot-event product.

For this repo, the MiniZinc input should contain only precomputed actions and
feasible arcs:

- one Boolean per action and per feasible transition arc;
- a fixed source and sink;
- a directed path through selected actions, expressed either with sparse flow
  conservation or MiniZinc's `dpath` graph global;
- an optional `sum(selected_action) <= 105` equivalence bound; and
- `maximize sum(action_weight[a] * selected_action[a])`.

MiniZinc provides directed-path and network-flow globals over fixed sparse
graphs ([Graph constraints](https://docs.minizinc.dev/en/stable/lib-globals-graph.html)).
For a CP formulation, benchmark Gecode against Chuffed and OR-Tools CP-SAT;
the official solver guide describes Chuffed's lazy-clause generation and
OR-Tools' CP/SAT support, and recommends testing free search where appropriate.
For an explicit linear flow formulation, benchmark the bundled HiGHS and CBC
MIP backends as well. MiniZinc explicitly warns that model efficiency depends
on the backend and search, so solver choice should be measured rather than
assumed ([Solver backends](https://docs.minizinc.dev/en/stable/solvers.html)).

Use compiler and solver statistics to compare FlatZinc size, flattening time,
first-incumbent time, final objective, bound/gap where available, and terminal
status. The IDE supports compilation profiling, which can attribute generated
variables and constraints back to model lines
([MiniZinc IDE](https://docs.minizinc.dev/en/stable/minizinc_ide.html)).

## Time limits and “anytime” results

MiniZinc's `--intermediate` option asks an optimization solver to print
improving incumbents. A time limit bounds the run, but does not change its
algorithm into Anytime A*. These behaviors are part of the official command
line interface
([MiniZinc command line](https://docs.minizinc.dev/en/2.9.0/command_line.html),
[FlatZinc solver interface](https://docs.minizinc.dev/en/stable/fzn-spec.html#command-line-interface-and-standard-options)).

A `solution` message followed by `UNKNOWN` is only a feasible best-so-far plan;
it is not an optimality certificate. `OPTIMAL_SOLUTION` is the relevant final
status for a proven optimum
([MiniZinc JSON stream](https://docs.minizinc.dev/en/2.9.0/json-stream.html)).
An incumbent is operationally “anytime” only in the modest sense that the run
can be stopped and its best feasible result retained. Its quality is unknown
unless the solver supplies a valid bound/gap or it is compared with an exact
result such as the action-DAG optimum above.

## Recommended next model

1. Deterministically enumerate `(intersection, start, end, captured_events)`
   actions from the authorized environment data.
2. Remove duplicate and dominated actions only after proving the dominance
   rule; the current 895-action instance is already small enough without this.
3. Generate the feasible transition DAG and a compact DZN containing action
   metadata, action weights, and sparse arcs.
4. Replace the slot/event MiniZinc model with a maximum-weight `dpath` or binary
   flow model, and retain MiniZinc execution and status as planner authority.
5. Verify the MiniZinc answer against the direct DAG dynamic program in tests,
   then benchmark all bundled applicable solvers under identical limits.
6. Separately confirm that `1000 - event_risk` is the intended Mission value.
   The optimizer is exact only relative to the supplied objective; it cannot
   determine whether preferring lower-risk events is operationally correct.
