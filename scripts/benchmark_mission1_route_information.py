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
int: C; int: A; int: S; int: R; int: K; int: N;
set of int: CS = 1..C;
array[1..A] of 0..N-1: af;
array[1..A] of 0..N-1: at;
array[0..N] of int: os;
array[0..N] of int: ins;
array[1..A] of int: ie;
array[CS] of int: base;
array[1..R] of CS: rc;
array[1..R] of 1..S: rs;
array[1..S, 0..K] of int: gain;
array[1..A] of var 0.0..1.0: flow;
array[CS] of var 0..1: selected;
constraint forall(n in 0..N-1)(
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

EPOCH_MODEL = r'''
int: C; int: S; int: R; int: K; int: E; int: U;
set of int: CS = 1..C;
array[CS] of int: base;
array[1..R] of CS: rc;
array[1..R] of 1..S: rs;
array[1..R] of 1..U: ru;
array[1..S,0..K] of int: gain;
array[CS] of 1..E: epoch;
array[CS] of float: sx; array[CS] of float: sy;
array[CS] of float: ex; array[CS] of float: ey;
array[CS] of float: start; array[CS] of float: finish;
array[CS] of -1..3: direction;
array[CS] of int: first_report; array[CS] of int: last_report;
float: initial_x; float: initial_y; float: initial_time;
int: initial_direction; float: speed; float: quarter_turn;
bool: chronological;
array[CS] of var 0..1: selected;
array[1..E] of var 0..1: occupied;
array[0..E] of var min(initial_x,min(ex))..max(initial_x,max(ex)): px;
array[0..E] of var min(initial_y,min(ey))..max(initial_y,max(ey)): py;
array[0..E] of var -1..3: facing;
array[0..E] of var initial_time..max(finish): available;
array[0..E] of var min(first_report)-1..max(last_report): previous_report;
constraint px[0]=initial_x /\ py[0]=initial_y /\ facing[0]=initial_direction
 /\ available[0]=initial_time /\ previous_report[0]=min(first_report)-1;
constraint forall(e in 1..E)(
 occupied[e]=sum(c in CS where epoch[c]=e)(selected[c]) /\
 (if occupied[e]=0 then
   px[e]=px[e-1] /\ py[e]=py[e-1] /\ facing[e]=facing[e-1]
   /\ available[e]=available[e-1] /\ previous_report[e]=previous_report[e-1]
 else
   px[e]=sum(c in CS where epoch[c]=e)(ex[c]*selected[c]) /\
   py[e]=sum(c in CS where epoch[c]=e)(ey[c]*selected[c]) /\
   facing[e]=sum(c in CS where epoch[c]=e)(direction[c]*selected[c]) /\
   available[e]=sum(c in CS where epoch[c]=e)(finish[c]*selected[c]) /\
   previous_report[e]=sum(c in CS where epoch[c]=e)(last_report[c]*selected[c])
 endif)
);
function var int: turn(var int: a, var int: b) =
 if a=-1 then 2 else min(abs(a-b),4-abs(a-b)) endif;
function var int: navigation_turns(var float: dx,var float: dy,var int: a,int: b) =
 let {var int: north=if dx>0.0 then 3 else 1 endif;
      var int: east=if dy>0.0 then 0 else 2 endif;} in
 if dx=0.0 /\ dy=0.0 then (if b=-1 then 0 else turn(a,b) endif)
 elseif dx=0.0 then turn(a,east)+(if b=-1 then 0 else turn(east,b) endif)
 elseif dy=0.0 then turn(a,north)+(if b=-1 then 0 else turn(north,b) endif)
 else max(turn(a,north)+1+(if b=-1 then 0 else turn(east,b) endif),
          turn(a,east)+1+(if b=-1 then 0 else turn(north,b) endif)) endif;
constraint forall(c in CS)(selected[c]=1 ->
 available[epoch[c]-1]+(abs(sx[c]-px[epoch[c]-1])+abs(sy[c]-py[epoch[c]-1]))/speed
 +quarter_turn*navigation_turns(sx[c]-px[epoch[c]-1],sy[c]-py[epoch[c]-1],facing[epoch[c]-1],direction[c]) <= start[c]
 /\ (not chronological \/ previous_report[epoch[c]-1]<first_report[c])
);
constraint forall(u in 1..U)(sum(r in 1..R where ru[r]=u)(selected[rc[r]])<=1);
array[1..S] of var 0..K: checks;
constraint forall(s in 1..S)(checks[s]=sum(r in 1..R where rs[r]=s)(selected[rc[r]]));
constraint assert(forall(s in 1..S,k in 2..K)(gain[s,k]-gain[s,k-1]<=gain[s,k-1]-gain[s,k-2]),"nonconcave information");
array[1..S] of var 0..max(gain): information;
constraint forall(s in 1..S,k in 1..K)(information[s]<=gain[s,k-1]+(checks[s]-k+1)*(gain[s,k]-gain[s,k-1]));
var int: objective=sum(c in CS)(base[c]*selected[c])+sum(information);
solve maximize objective;
output ["{\"objective\":" ++ show(objective) ++ ",\"selected\":"
 ++ show([c | c in CS where fix(selected[c])=1]) ++ "}"];
'''


def epoch_data(candidates, environment, by_id):
    epochs_to_encode = [t for c in candidates for t in
                        (c.start_s-c.observation_delay_s,c.start_s-c.observation_delay_s+c.report_span_s)]
    if any(t*planning.TIME_SCALE != round(t*planning.TIME_SCALE) for t in epochs_to_encode):
        raise ValueError("epoch probe requires report epochs on the existing half-second grid")
    starts = sorted({c.start_s for c in candidates})
    epochs = {value: i + 1 for i, value in enumerate(starts)}
    report_indices = {value: i + 1 for i, value in enumerate(sorted(by_id))}
    vehicle = environment["controlled_vehicle"]
    direction = planning._vehicle_direction(vehicle)
    arrays = {
        "epoch": [epochs[c.start_s] for c in candidates],
        "sx": [c.x for c in candidates], "sy": [c.y for c in candidates],
        "ex": [c.end_x for c in candidates], "ey": [c.end_y for c in candidates],
        "start": [c.start_s for c in candidates], "finish": [c.end_s for c in candidates],
        "direction": [c.arrival_direction if c.arrival_direction is not None else -1 for c in candidates],
        "first_report": [round((c.start_s-c.observation_delay_s)*planning.TIME_SCALE) for c in candidates],
        "last_report": [round((c.start_s-c.observation_delay_s+c.report_span_s)*planning.TIME_SCALE) for c in candidates],
        "ru": [report_indices[r] for c in candidates for r in c.report_ids],
    }
    scalars = {"E": len(starts), "U": len(by_id), "initial_x": vehicle["position"]["x"],
               "initial_y": vehicle["position"]["y"], "initial_time": environment["mission_time_seconds"],
               "initial_direction": -1 if direction is None else direction,
               "speed": .9*vehicle["max_velocity"], "quarter_turn": vehicle.get("quarter_turn_seconds",0),
               "chronological": environment.get("observation_window_seconds",0)>0}
    return [f"{name}={json.dumps(value)};" for name,value in {**scalars,**arrays}.items()]


def verify_probe_route(candidates, solution, environment, opportunities, belief):
    """Independently check the returned primary score and actual travel budgets."""
    vehicle = environment["controlled_vehicle"]
    x,y = vehicle["position"]["x"],vehicle["position"]["y"]
    facing = planning._vehicle_direction(vehicle)
    available = environment["mission_time_seconds"]
    previous_report = float("-inf")
    by_id = {o.report_id:o for o in opportunities}
    selected = [candidates[index-1] for index in solution["selected"]]
    assert len(solution["selected"]) == len(set(solution["selected"]))
    reports = []
    for candidate in selected:
        travel = planning._navigation_time(x,y,candidate.x,candidate.y,vehicle["max_velocity"],
                                          facing,candidate.arrival_direction,vehicle.get("quarter_turn_seconds",0))
        assert available+travel <= candidate.start_s+1e-9
        first = candidate.start_s-candidate.observation_delay_s
        if environment.get("observation_window_seconds",0)>0:
            assert first > previous_report+1e-9
        previous_report = first+candidate.report_span_s
        x,y,facing,available = candidate.end_x,candidate.end_y,candidate.arrival_direction,candidate.end_s
        reports.extend(candidate.report_ids)
    assert len(reports) == len(set(reports))
    counts = Counter(by_id[r].entity_id for r in reports)
    normalizer = max(o.estimation for o in opportunities)
    states = {s.entity_id:s for s in belief.ships}
    score = sum(round(c.recall_utility*planning.SCORE_SCALE)+round(c.omission_yield*planning.SCORE_SCALE) for c in selected)
    for ship,count in counts.items():
        state = states[ship]
        score += round(information_curve(state.variance,state.expected_variance_reduction,normalizer,count)[count]*planning.SCORE_SCALE)
    assert score == solution["objective"]
    return {"selected_candidates":len(selected),"selected_reports":len(reports),
            "primary_score":score,"travel_and_unique_credit_verified":True}


def information_model(encoding):
    if encoding == "lookup":
        return MODEL
    return MODEL.replace(
        "var int: objective = sum(c in CS)(base[c]*selected[c]) + sum(s in 1..S)(gain[s,checks[s]]);",
        """constraint assert(forall(s in 1..S, k in 2..K)(
  gain[s,k]-gain[s,k-1] <= gain[s,k-1]-gain[s,k-2]
), "information table is not discretely concave");
array[1..S] of var 0..max(gain): information;
constraint forall(s in 1..S, k in 1..K)(
  information[s] <= gain[s,k-1] + (checks[s]-k+1)*(gain[s,k]-gain[s,k-1])
);
var int: objective = sum(c in CS)(base[c]*selected[c]) + sum(information);""",
    )


def compress_arc_suffixes(candidate_count, arcs):
    """Share identical suffixes of successor lists, preserving candidate paths.

    A hub chooses the first successor or the rest of its immutable list. Hubs
    carry neither reports nor utility. Expanding hubs gives exactly an original
    edge, so no temporal or report-uniqueness relaxation is introduced.
    """
    successors = {}
    for source, target in arcs:
        successors.setdefault(source, []).append(target)
    next_node = candidate_count + 2
    hubs = {}
    compressed = []
    for source, targets in sorted(successors.items()):
        ordered = sorted(targets)
        suffix = ordered[-1]
        for target in reversed(ordered[:-1]):
            key = target, suffix
            if key not in hubs:
                hubs[key] = next_node
                compressed.extend(((next_node, target), (next_node, suffix)))
                next_node += 1
            suffix = hubs[key]
        compressed.append((source, suffix))
    return next_node, tuple(sorted(compressed))


def compress_arc_intervals(candidate_count, arcs):
    """Represent successor ranges by a shared balanced tree of candidate leaves."""
    next_node = candidate_count + 2
    children = {}
    compressed = []
    def tree(low, high):
        nonlocal next_node
        if low == high:
            return low
        node = next_node
        next_node += 1
        middle = (low + high) // 2
        left, right = tree(low, middle), tree(middle + 1, high)
        children[node] = left, right
        compressed.extend(((node, left), (node, right)))
        return node
    root = tree(1, candidate_count + 1)
    def cover(source, first, last, node, low, high):
        if first <= low and high <= last:
            compressed.append((source, node))
            return
        middle = (low + high) // 2
        left, right = children[node]
        if first <= middle:
            cover(source, first, last, left, low, middle)
        if last > middle:
            cover(source, first, last, right, middle + 1, high)
    successors = {}
    for source, target in arcs:
        successors.setdefault(source, []).append(target)
    for source, targets in successors.items():
        ordered = sorted(targets)
        first = last = ordered[0]
        for target in ordered[1:]:
            if target == last + 1:
                last = target
            else:
                cover(source, first, last, root, 1, candidate_count + 1)
                first = last = target
        cover(source, first, last, root, 1, candidate_count + 1)
    return next_node, tuple(sorted(compressed))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("environment", "belief", "minizinc", "agent-var", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--compile-only", action="store_true", help="Isolate flattening cost without solver search")
    parser.add_argument("--information-encoding", choices=("lookup", "envelope"), default="lookup")
    parser.add_argument("--arc-compression", choices=("none", "suffix", "interval"), default="none")
    parser.add_argument("--no-flatten-optimize", action="store_true", help="Measure without MiniZinc's optional flattening optimizations")
    parser.add_argument("--representation", choices=("flow", "epoch"), default="flow")
    parser.add_argument("--horizon-seconds", type=float, help="Experimental epoch lookahead; does not truncate public beliefs or schedules")
    args = parser.parse_args()
    if args.horizon_seconds is not None and args.horizon_seconds <= 0:
        parser.error("horizon must be positive")
    if args.representation == "epoch" and (args.information_encoding != "envelope" or args.arc_compression != "none"):
        parser.error("epoch representation uses envelope information and no arc compression")
    if args.agent_var.resolve().name != "var" or not args.output.resolve().is_relative_to(args.agent_var.resolve()):
        parser.error("output must be inside the caller-provided Agent var directory")
    args.output.mkdir(parents=True, exist_ok=False)
    environment = json.loads(args.environment.read_text())
    belief = ReportingReliabilitySnapshot.from_dict(json.loads(args.belief.read_text()))
    started = time.perf_counter()
    # A terminal's information value depends on the selected prefix. Its old
    # additive score therefore cannot prove local terminal dominance.
    candidates_only = args.representation == "epoch" or args.horizon_seconds is not None
    patched = "_candidate_arcs" if candidates_only else "_prune_terminal_alternatives"
    replacement = (lambda *args: ()) if candidates_only else (lambda candidates, arcs: arcs)
    with patch.object(planning, patched, side_effect=replacement):
        graph = planning.build_candidate_dag(environment, belief)
    if args.horizon_seconds is not None:
        end = environment["mission_time_seconds"] + args.horizon_seconds
        candidates = tuple(c for c in graph.candidates if c.end_s <= end)
        vehicle = environment["controlled_vehicle"]
        arcs = ()
        if args.representation == "flow":
            with patch.object(planning,"_prune_terminal_alternatives",side_effect=lambda candidates,arcs:arcs):
                arcs = planning._candidate_arcs(candidates,vehicle["max_velocity"],vehicle.get("quarter_turn_seconds",0),
                                           environment.get("observation_window_seconds",0)>0)
        graph = planning.CandidateDAG(candidates, arcs, 0, len(candidates)+1)
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
    compression = {"suffix": compress_arc_suffixes, "interval": compress_arc_intervals}
    node_count, arcs = (compression[args.arc_compression](len(graph.candidates), graph.arcs)
                       if args.arc_compression != "none" else (graph.sink + 1, graph.arcs))
    outgoing, incoming = Counter(u for u, _ in arcs), Counter(v for _, v in arcs)
    def offsets(count):
        result = [1]
        for node in range(node_count):
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
    dzn = [f"C={len(graph.candidates)}; A={len(arcs)}; S={len(ships)}; R={len(references)}; K={maximum}; N={node_count};"]
    dzn += [f"{name}={json.dumps(value)};" for name, value in arrays.items()]
    dzn += [f"{name}=array1d(0..N,{json.dumps(offsets(count))});"
            for name, count in (("os", outgoing), ("ins", incoming))]
    dzn += [f"gain=array2d(1..S,0..K,{json.dumps(gains)});"]
    if args.representation == "epoch":
        dzn = [f"C={len(graph.candidates)}; S={len(ships)}; R={len(references)}; K={maximum};"]
        dzn += [f"{name}={json.dumps(arrays[name])};" for name in ("base","rc","rs")]
        dzn += [f"gain=array2d(1..S,0..K,{json.dumps(gains)});"]
        dzn += epoch_data(graph.candidates, environment, by_id)
    model, data = args.output / "probe.mzn", args.output / "data.dzn"
    model.write_text(EPOCH_MODEL if args.representation == "epoch" else information_model(args.information_encoding))
    data.write_text("\n".join(dzn) + "\n")
    result = {"scope": "Solver feasibility probe only; not an executable Mission plan or recall result",
              "compile_only": args.compile_only,
              "representation": args.representation,
              "horizon_seconds": args.horizon_seconds,
              "information_encoding": args.information_encoding,
              "arc_compression": args.arc_compression,
              "flatten_optimization": 0 if args.no_flatten_optimize else 1,
              "uncompressed_arc_count": len(graph.arcs),
              "node_count": node_count,
              "candidate_count": len(graph.candidates), "arc_count": len(arcs),
              "generation_seconds": time.perf_counter() - started}
    started = time.perf_counter()
    command = [str(args.minizinc.resolve()), "--solver", "coin-bc", "--json-stream", "--statistics"]
    if args.no_flatten_optimize:
        command.append("-O0")
    if args.compile_only:
        command += ["--compile", "--verbose-compilation", "--output-base", str((args.output / "compiled").resolve())]
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
    solutions = [json.loads(line) for line in (args.output / "solver.stdout").read_text().splitlines()
                 if json.loads(line).get("type") == "solution"]
    if solutions:
        solution = json.loads(solutions[-1]["output"]["default"])
        result["route_verification"] = verify_probe_route(graph.candidates,solution,environment,opportunities,belief)
    (args.output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
