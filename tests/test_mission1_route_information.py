"""Route-dependent score diagnostics; not an integration claim for the planner."""

import importlib.util
import json
import subprocess
from collections import Counter
from itertools import pairwise
from pathlib import Path

import pytest
from test_mission1_planning import _belief, _environment, _report

SPEC = importlib.util.spec_from_file_location(
    "route_information_diagnostic", Path("scripts/inspect_mission1_route_information.py"),
)
diagnostic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostic)


def solution(*reports):
    return {"assignments": [{"parameters": {"report_ids": list(reports)}}]}


def test_first_selected_check_does_not_pay_for_unobserved_schedule():
    environment = _environment([_report(str(i), 1, 10 + i, 0, 0) for i in range(20)])
    row = diagnostic.inspect_route(environment, _belief((1,)), solution("19"))["ships"][0]
    assert row["selected_route_information_credit"] == pytest.approx(.5)
    assert row["scheduled_information_credit"] < .05
    assert row["first_selected_report_time_s"] == 29


def test_full_selected_schedule_preserves_existing_budget():
    environment = _environment([_report(str(i), 1, 10 + i, 0, 0) for i in range(20)])
    result = diagnostic.inspect_route(environment, _belief((1,)), solution(*map(str, range(20))))
    assert result["scheduled_information_credit"] == pytest.approx(result["selected_route_information_credit"])


def test_distinct_vessels_have_independent_information_budgets():
    environment = _environment([_report("a", 1, 10, 0, 0), _report("b", 1, 20, 0, 0),
                                _report("c", 2, 20, 0, 0)])
    repeated = diagnostic.inspect_route(environment, _belief((1, 2)), solution("a", "b"))
    diverse = diagnostic.inspect_route(environment, _belief((1, 2)), solution("a", "c"))
    assert diverse["selected_route_information_credit"] == pytest.approx(1)
    assert repeated["selected_route_information_credit"] < 1


def test_curve_is_monotone_with_diminishing_marginals_and_variance_cap():
    variance, gain = .1, .03
    curve = diagnostic.information_curve(variance, gain, gain, 20)
    increments = [b - a for a, b in pairwise(curve)]
    assert all(a > b > 0 for a, b in pairwise(increments))
    assert curve[-1] * 2 * gain < variance
    assert diagnostic.information_curve(0, 0, 0, 3) == [0, 0, 0, 0]


def test_unavailable_and_repeated_reports_are_not_credited():
    environment = _environment([_report("old", 1, 1, 0, 0), _report("done", 1, 10, 0, 0),
                                _report("next", 1, 20, 0, 0)])
    environment["mission_time_seconds"] = 5
    environment["world_model_info"]["event_report_checks"] = [{"report_id": "done"}]
    for ids in (("old",), ("done",), ("next", "next")):
        with pytest.raises(ValueError):
            diagnostic.inspect_route(environment, _belief((1,)), solution(*ids))
    result = diagnostic.inspect_route(environment, _belief((1,)), solution("next"))
    assert result["ships"][0]["remaining_public_reports"] == 1


def test_native_route_count_counterexample_to_additive_prefix_pruning(tmp_path):
    # Diamond: 0 -> (A: ship1 or B: ship2) -> C: ship1 -> sink.
    # Uniform schedule credit selects A: 20+33+33 > 50+33.
    # Selected-count credit selects B: 50+50 > 20+67.
    paths = ((1, 0, 1), (0, 1, 1))
    uniform = (33, 50, 33)
    base = (20, 0, 0)
    curve = (0, 50, 67)
    additive = lambda path: sum(x * (a + b) for x, a, b in zip(path, base, uniform))
    route_score = lambda path: sum(x * b for x, b in zip(path, base)) + curve[path[0] + path[2]] + curve[path[1]]
    assert max(paths, key=additive) == paths[0]
    assert max(paths, key=route_score) == paths[1]
    model = tmp_path / "route-count-counterexample.mzn"
    model.write_text('''
array[1..3] of var 0..1: chosen;
array[0..2] of int: info = array1d(0..2, [0, 50, 67]);
constraint chosen[1] + chosen[2] = 1;
constraint chosen[3] = 1;
var int: score = 20 * chosen[1] + info[chosen[1] + chosen[3]] + info[chosen[2]];
solve maximize score;
output ["{\\"chosen\\":" ++ show(chosen) ++ ",\\"score\\":" ++ show(score) ++ "}"];
''')
    result = subprocess.run([
        "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc", "--solver", "coin-bc", str(model),
    ], capture_output=True, text=True, check=True, timeout=30)
    native = json.loads(result.stdout.splitlines()[0])
    assert tuple(native["chosen"]) == max(paths, key=route_score)
    assert native["score"] == 100
    assert "==========" in result.stdout


@pytest.mark.parametrize("encoding", ["lookup", "envelope"])
@pytest.mark.parametrize("compress", ["none", "suffix", "interval"])
def test_benchmark_native_encodings_match_exhaustive_diamond(tmp_path, monkeypatch, encoding, compress):
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    import benchmark_mission1_route_information as benchmark

    model = tmp_path / "probe.mzn"
    data = tmp_path / "data.dzn"
    model.write_text(benchmark.information_model(encoding))
    arcs = [(0, 1), (0, 2), (1, 3), (2, 3), (3, 4)]
    nodes = 5
    if compress != "none":
        compressor = {"suffix": benchmark.compress_arc_suffixes, "interval": benchmark.compress_arc_intervals}[compress]
        nodes, arcs = compressor(3, arcs)
    arrays = {"af": [u for u, _ in arcs], "at": [v for _, v in arcs],
              "ie": [i+1 for i in sorted(range(len(arcs)), key=lambda i: (arcs[i][1], arcs[i][0]))]}
    lines = [f"C=3; A={len(arcs)}; S=2; R=3; K=2; N={nodes};",
             "base=[20,0,0]; rc=[1,2,3]; rs=[1,2,1];",
             "gain=array2d(1..2,0..2,[0,50,67,0,50,67]);"]
    lines.extend(f"{key}={json.dumps(value)};" for key, value in arrays.items())
    for name, counts in (("os", Counter(arrays["af"])), ("ins", Counter(arrays["at"]))):
        offsets = [1]
        for node in range(nodes):
            offsets.append(offsets[-1] + counts[node])
        lines.append(f"{name}=array1d(0..N,{json.dumps(offsets)});")
    data.write_text("\n".join(lines))
    result = subprocess.run([
        "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc", "--solver", "coin-bc",
        str(model), str(data),
    ], capture_output=True, text=True, check=True, timeout=30)
    native = json.loads(result.stdout.splitlines()[0])
    assert native == {"objective": 100, "selected": [2, 3]}
    assert "==========" in result.stdout


@pytest.mark.parametrize("compression", ["suffix", "interval"])
def test_compression_preserves_every_candidate_path(monkeypatch, compression):
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    import benchmark_mission1_route_information as benchmark

    def paths(arcs, count):
        outgoing = {}
        for u, v in arcs:
            outgoing.setdefault(u, []).append(v)
        def walk(node, visited):
            if node == count + 1:
                return {visited}
            return set().union(*(walk(v, visited + ((v,) if 1 <= v <= count else ()))
                                 for v in outgoing.get(node, [])))
        return walk(0, ())

    # Enumerate all edge subsets of a small ordered graph, including no route.
    edges = [(u, v) for u in range(4) for v in range(u + 1, 5)]
    for mask in range(1 << len(edges)):
        arcs = [edge for i, edge in enumerate(edges) if mask & (1 << i)]
        compressor = {"suffix": benchmark.compress_arc_suffixes, "interval": benchmark.compress_arc_intervals}[compression]
        _, compressed = compressor(3, arcs)
        assert paths(arcs, 3) == paths(compressed, 3)


@pytest.mark.parametrize("speed,expected", [(10.0, {"objective":100,"selected":[2,3]}),
                                            (.5, {"objective":87,"selected":[1,3]})])
def test_epoch_model_information_and_travel(tmp_path, monkeypatch, speed, expected):
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    import benchmark_mission1_route_information as benchmark
    model, data = tmp_path / "epoch.mzn", tmp_path / "epoch.dzn"
    model.write_text(benchmark.EPOCH_MODEL)
    data.write_text('''
C=3; S=2; R=3; K=2; E=2; U=3;
base=[20,0,0]; rc=[1,2,3]; rs=[1,2,1]; ru=[1,2,3];
gain=array2d(1..2,0..2,[0,50,67,0,50,67]);
epoch=[1,1,2]; sx=[0.0,10.0,0.0]; sy=[0.0,0.0,0.0];
ex=[0.0,10.0,0.0]; ey=[0.0,0.0,0.0];
start=[10.0,10.0,30.0]; finish=[10.5,10.5,30.5];
direction=[3,3,3]; first_report=[20,20,60]; last_report=[20,20,60];
initial_x=0.0; initial_y=0.0; initial_time=0.0; initial_direction=3;
quarter_turn=0.5; chronological=true;
''' + f"speed={speed};")
    result = subprocess.run([
        "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc", "--solver", "coin-bc", str(model), str(data),
    ], capture_output=True, text=True, check=True, timeout=30)
    assert json.loads(result.stdout.splitlines()[0]) == expected
    assert "==========" in result.stdout


def test_epoch_turn_formula_matches_python_all_discrete_cases(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    import benchmark_mission1_route_information as benchmark

    from onr.application.mission1_planning import _navigation_turns
    cases = [(x,y,a,b) for x in (-1,0,1) for y in (-1,0,1) for a in (-1,0,1,2,3) for b in (-1,0,1,2,3)]
    expected = [_navigation_turns(None if x==0 else (3 if x>0 else 1),
                                 None if y==0 else (0 if y>0 else 2),
                                 None if a==-1 else a,None if b==-1 else b) for x,y,a,b in cases]
    functions = benchmark.EPOCH_MODEL.split("function var int: turn",1)[1].split("constraint forall(c in CS)",1)[0]
    expressions = [f"navigation_turns({float(x)},{float(y)},{a},{b})" for x,y,a,b in cases]
    model = tmp_path / "turns.mzn"
    model.write_text("function var int: turn"+functions+"\narray[1..225] of var int: values=["
                     +",".join(expressions)+"]; solve satisfy; output [show(values)];")
    result = subprocess.run([
        "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc", "--solver", "coin-bc", str(model),
    ], capture_output=True, text=True, check=True, timeout=30)
    assert json.loads(result.stdout.splitlines()[0]) == expected


def test_probe_verifier_rejects_wrong_score_duplicate_and_unreachable_routes(monkeypatch):
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    import benchmark_mission1_route_information as benchmark

    from onr.application.mission1_planning import _opportunities, build_candidate_dag
    environment = _environment([_report("a",1,10,0,0)])
    belief = _belief((1,))
    candidates = build_candidate_dag(environment,belief).candidates
    opportunities = _opportunities(environment,belief)
    c = candidates[0]
    score = round(c.recall_utility*1_000_000)+round(c.omission_yield*1_000_000)+500_000
    solution = {"selected":[1],"objective":score}
    assert benchmark.verify_probe_route(candidates,solution,environment,opportunities,belief)["travel_and_unique_credit_verified"]
    for bad in ({"selected":[1],"objective":score+1},{"selected":[1,1],"objective":score}):
        with pytest.raises(AssertionError):
            benchmark.verify_probe_route(candidates,bad,environment,opportunities,belief)
    environment["controlled_vehicle"]["position"]["x"]=10_000
    with pytest.raises(AssertionError):
        benchmark.verify_probe_route(candidates,solution,environment,opportunities,belief)
