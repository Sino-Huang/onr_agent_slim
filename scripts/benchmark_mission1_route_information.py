"""Experimental native solver benchmark; never publishes an executable plan.

Optimize selected-count information on the public candidate graph to measure
whether a direct mixed-integer formulation is practical. This intentionally
does NOT claim full planner parity: secondary maneuver/duration objectives,
active-plan rescoring and downstream evidence lookahead are not integrated.
The production model and its authority remain unchanged.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from inspect_mission1_route_information import information_curve

from onr.application import mission1_planning as planning
from onr.contracts.reporting_reliability import ReportingReliabilitySnapshot

MODEL = r'''
int: C; int: A; int: S; int: R; int: K;
set of int: CS = 1..C;
array[1..A] of 0..C+1: af;
array[1..A] of 0..C+1: at;
array[0..C+2] of int: os;
array[0..C+2] of int: ins;
array[1..A] of int: ie;
array[CS] of int: base;
array[1..R] of CS: rc;
array[1..R] of 1..S: rs;
array[1..S, 0..K] of int: gain;
array[1..A] of var 0.0..1.0: flow;
array[CS] of var 0..1: selected;
constraint forall(n in 0..C+1)(
  sum(e in os[n]..os[n+1]-1)(flow[e])
  - sum(p in ins[n]..ins[n+1]-1)(flow[ie[p]])
  = if n = 0 then 1 elseif n = C+1 then -1 else 0 endif
);
constraint forall(c in CS)(
  selected[c] = sum(p in ins[c]..ins[c+1]-1)(flow[ie[p]])
);
array[1..S] of var 0..K: checks;
constraint forall(s in 1..S)(checks[s] = sum(r in 1..R where rs[r] = s)(selected[rc[r]]));
var int: objective = sum(c in CS)(base[c]*selected[c]) + sum(s in 1..S)(gain[s,checks[s]]);
solve maximize objective;
output ["{\"objective\":" ++ show(objective) ++ ",\"selected\":"
        ++ show([c | c in CS where fix(selected[c])=1]) ++ "}"];
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("environment", "belief", "minizinc", "agent-var", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--compile-only", action="store_true", help="Isolate flattening cost without solver search")
    args = parser.parse_args()
    if args.agent_var.resolve().name != "var" or not args.output.resolve().is_relative_to(args.agent_var.resolve()):
        parser.error("output must be inside the caller-provided Agent var directory")
    args.output.mkdir(parents=True, exist_ok=False)
    environment = json.loads(args.environment.read_text())
    belief = ReportingReliabilitySnapshot.from_dict(json.loads(args.belief.read_text()))
    started = time.perf_counter()
    # A terminal's information value depends on the selected prefix. Its old
    # additive score therefore cannot prove local terminal dominance.
    with patch.object(planning, "_prune_terminal_alternatives", side_effect=lambda candidates, arcs: arcs):
        graph = planning.build_candidate_dag(environment, belief)
    # Positive recall makes the builder's positive-intermediate reduction safe
    # for this probe: insertion increases base reward even as information shrinks.
    if any(c.recall_utility <= 0 for c in graph.candidates):
        raise ValueError("probe requires positive candidate recall for intermediate reduction")
    opportunities = planning._opportunities(environment, belief)
    by_id = {o.report_id: o for o in opportunities}
    ships = sorted({o.entity_id for o in opportunities})
    ship_index = {ship: i + 1 for i, ship in enumerate(ships)}
    counts = Counter(o.entity_id for o in opportunities)
    maximum = max(counts.values())
    normalizer = max(o.estimation for o in opportunities)
    states = {s.entity_id: s for s in belief.ships}
    gains = []
    for ship in ships:
        state = states[ship]
        gains.extend(round(v * planning.SCORE_SCALE) for v in information_curve(
            state.variance, state.expected_variance_reduction, normalizer, maximum,
        ))
    arcs = graph.arcs
    outgoing, incoming = Counter(u for u, _ in arcs), Counter(v for _, v in arcs)
    def offsets(count):
        result = [1]
        for node in range(graph.sink + 1):
            result.append(result[-1] + count[node])
        return result
    references = [(i + 1, ship_index[by_id[r].entity_id]) for i, c in enumerate(graph.candidates)
                  for r in c.report_ids]
    arrays = {
        "af": [u for u, _ in arcs], "at": [v for _, v in arcs],
        "ie": [i + 1 for i in sorted(range(len(arcs)), key=lambda i: (arcs[i][1], arcs[i][0]))],
        "base": [round(c.recall_utility * planning.SCORE_SCALE)
                 + round(c.omission_yield * planning.SCORE_SCALE) for c in graph.candidates],
        "rc": [c for c, _ in references], "rs": [s for _, s in references],
    }
    dzn = [f"C={len(graph.candidates)}; A={len(arcs)}; S={len(ships)}; R={len(references)}; K={maximum};"]
    dzn += [f"{name}={json.dumps(value)};" for name, value in arrays.items()]
    dzn += [f"{name}=array1d(0..C+2,{json.dumps(offsets(count))});"
            for name, count in (("os", outgoing), ("ins", incoming))]
    dzn += [f"gain=array2d(1..S,0..K,{json.dumps(gains)});"]
    model, data = args.output / "probe.mzn", args.output / "data.dzn"
    model.write_text(MODEL)
    data.write_text("\n".join(dzn) + "\n")
    result = {"scope": "Solver feasibility probe only; not an executable Mission plan or recall result",
              "compile_only": args.compile_only,
              "candidate_count": len(graph.candidates), "arc_count": len(arcs),
              "generation_seconds": time.perf_counter() - started}
    started = time.perf_counter()
    command = [str(args.minizinc.resolve()), "--solver", "coin-bc", "--json-stream", "--statistics"]
    if args.compile_only:
        command += ["--compile", "--output-base", str((args.output / "compiled").resolve())]
    command += [str(model.resolve()), str(data.resolve())]
    with (args.output / "solver.stdout").open("w") as stdout, (args.output / "solver.stderr").open("w") as stderr:
        try:
            process = subprocess.run(command, stdout=stdout, stderr=stderr,
                                     timeout=args.timeout_seconds, check=False)
            result.update(returncode=process.returncode, timed_out=False)
        except subprocess.TimeoutExpired:
            result.update(returncode=None, timed_out=True)
    result["solver_seconds"] = time.perf_counter() - started
    result["optimal"] = any(json.loads(line).get("status") == "OPTIMAL_SOLUTION"
                            for line in (args.output / "solver.stdout").read_text().splitlines())
    (args.output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
