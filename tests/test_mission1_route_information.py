"""Route-dependent score diagnostics; not an integration claim for the planner."""

import importlib.util
import json
import subprocess
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
