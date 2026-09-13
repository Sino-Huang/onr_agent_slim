from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from onr.application.mission1_planning import (
    SCORE_SCALE,
    Mission1ReplanGate,
    build_candidate_dag,
    holding_exposures,
    longest_path_oracle,
    score_holding_exposure,
    serialize_minizinc_data,
)
from onr.contracts.fsm import FSMStatus, Statechart
from tests.test_mission1_planning import EXAMPLE_ROOT, _belief, _environment, _report


def _holding_environment():
    environment = _environment([
        _report("past", 1, 0, 5000, 0), _report("anchor", 1, 10, 0, 0),
        _report("future", 1, 20, 5000, 0),
    ])
    environment.update(event_check_window_seconds=4, fixed_view_dwell_options_s=[.5, 4.5])
    environment["controlled_vehicle"].update(heading_degrees=90, quarter_turn_seconds=.5)
    environment["surveillance_views"] = [{
        "x": 0, "y": 0, "arrival_direction": 0, "report_ids": ["anchor"],
        "holding_intervals": [{"entity_id": 1, "start_s": 10, "end_s": 20}],
    }]
    return environment


def test_holding_reserves_report_lookbacks_and_never_recredits_overlaps():
    environment, belief = _holding_environment(), _belief((1,))
    # Repeated delay rows and forecast intervals must not multiply the rate.
    environment["surveillance_views"] *= 2
    exposures = holding_exposures(environment, belief)[(0, 0, 0)]
    rate = belief.ships[0].expected_omission_probability * .1
    assert exposures == [(1, 10, 16.0, rate)]
    assert score_holding_exposure(exposures, ((10, 20.5),)) == pytest.approx(6 * rate, abs=1e-6)
    assert score_holding_exposure(exposures, ((10, 12.5), (11, 14.5))) == pytest.approx(4 * rate, abs=2e-6)
    assert score_holding_exposure(exposures, ((10, 14.5),), now=12) == pytest.approx(2 * rate, abs=1e-6)
    assert score_holding_exposure(exposures, ((10, 10.5),)) == 0


def test_long_hold_is_a_distinct_reachable_choice_and_zero_value_is_omitted():
    environment, belief = _holding_environment(), _belief((1,))
    graph = build_candidate_dag(environment, belief)
    assert len(graph.candidates) == 2
    assert len({c.candidate_id for c in graph.candidates}) == 2
    route = longest_path_oracle(graph)
    assert route.candidates[0].duration_s == 4.5
    assert route.candidates[0].report_span_s == 0
    assert route.covered_report_ids == ("anchor",)
    environment["surveillance_views"][0]["holding_intervals"] = []
    assert [c.duration_s for c in build_candidate_dag(environment, belief).candidates] == [.5]


def test_holding_windows_match_native_output_and_active_checked_tail(tmp_path):
    environment, belief = _holding_environment(), _belief((1,))
    graph = build_candidate_dag(environment, belief)
    route = longest_path_oracle(graph)
    candidate = route.candidates[0]
    data = tmp_path / "hold.dzn"
    data.write_text(serialize_minizinc_data(graph))
    native = subprocess.run([
        "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc", "--solver", "coin-bc",
        str(EXAMPLE_ROOT / "model.mzn"), str(data),
    ], capture_output=True, text=True, check=True)
    result = json.loads(native.stdout.splitlines()[0])
    assignment = result["assignments"][0]
    assert assignment["candidate_id"] == candidate.candidate_id
    assert result["combined_score"] == round(route.score * SCORE_SCALE)
    assert assignment["parameters"]["scored_observation_windows"] == [{"start": 20, "duration": 9, "time_scale": 2}]
    context = {"candidate_id": candidate.candidate_id, "surveillance_mode": "fixed_view",
               "target_entity_id": None, "target_report_ids": list(candidate.report_ids),
               "observation_window": {"start": {"seconds": 10}, "duration": {"seconds": 4.5}},
               "planner_item": assignment}
    chart = Statechart(mission_id="mission-1", plan_revision=1, mission_snapshot_id="snapshot-1",
                      planning_profile="temporal", entry_state="view", states=("view",),
                      transitions=(), terminal_states=("view",), state_context={"view": context})
    status = FSMStatus(mission_id="mission-1", plan_revision=1, statechart_revision=1,
                       active_state="view", active_state_context=context)
    decision, advisory = Mission1ReplanGate().assess(environment, belief, chart, status)
    assert decision.current_score == advisory.score == route.score
    environment["mission_time_seconds"] = 10.5
    environment["world_model_info"]["event_report_checks"] = [{
        "report_id": "anchor", "entity_id": 1, "checked_at_s": 10,
    }]
    decision, advisory = Mission1ReplanGate().assess(environment, belief, chart, status)
    assert not advisory.candidates
    assert not decision.trigger
    assert decision.current_score == pytest.approx(3.5 * belief.ships[0].expected_omission_probability * .1, abs=1e-6)


def test_merged_holds_preserve_scored_windows_without_crediting_the_gap(tmp_path):
    environment = _holding_environment()
    environment["static_info"] += [
        _report("past2", 2, 0, 5000, 0), _report("anchor2", 2, 30, 0, 0),
        _report("future2", 2, 40, 5000, 0),
    ]
    view = environment["surveillance_views"][0]
    view["report_ids"].append("anchor2")
    view["holding_intervals"].append({"entity_id": 2, "start_s": 30, "end_s": 34})
    belief = _belief((1, 2))
    graph = build_candidate_dag(environment, belief)
    route = longest_path_oracle(graph)
    assert len(route.candidates) == 1
    candidate = route.candidates[0]
    assert candidate.scored_observation_windows == ((10, 14.5), (30, 34.5))
    data = tmp_path / "merged.dzn"
    data.write_text(serialize_minizinc_data(graph))
    native = subprocess.run([
        "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc", "--solver", "coin-bc",
        str(EXAMPLE_ROOT / "model.mzn"), str(data),
    ], capture_output=True, text=True, check=True)
    result = json.loads(native.stdout.splitlines()[0])
    assignment = result["assignments"][0]
    assert assignment["candidate_id"] == candidate.candidate_id
    assert result["combined_score"] == round(route.score * SCORE_SCALE)
    assert assignment["parameters"]["scored_observation_windows"] == [
        {"start": 20, "duration": 9, "time_scale": 2}, {"start": 60, "duration": 9, "time_scale": 2},
    ]
    context = {"candidate_id": candidate.candidate_id, "surveillance_mode": "fixed_view",
               "target_entity_id": None, "target_report_ids": list(candidate.report_ids),
               "observation_window": {"start": {"seconds": 10}, "duration": {"seconds": 24.5}},
               "planner_item": assignment}
    chart = Statechart(mission_id="mission-1", plan_revision=1, mission_snapshot_id="snapshot-1",
                      planning_profile="temporal", entry_state="view", states=("view",),
                      transitions=(), terminal_states=("view",), state_context={"view": context})
    status = FSMStatus(mission_id="mission-1", plan_revision=1, statechart_revision=1,
                       active_state="view", active_state_context=context)
    decision, advisory = Mission1ReplanGate().assess(environment, belief, chart, status)
    assert decision.current_score == advisory.score == route.score


def test_holding_exposure_is_bounded_by_disclosed_activity_span():
    environment, belief = _holding_environment(), _belief((1,))
    environment["surveillance_views"][0]["holding_intervals"] = [{"entity_id": 1, "start_s": 20, "end_s": 24}]
    assert holding_exposures(environment, belief) == {}


def test_large_graph_inspection_does_not_rescan_all_arcs_per_node(tmp_path, monkeypatch):
    import importlib.util

    graph = build_candidate_dag(_environment([
        _report(str(i), 1, 10 + i * 2, i, 0) for i in range(8)
    ]), _belief((1,)))
    data = tmp_path / "inspection.dzn"
    data.write_text(serialize_minizinc_data(graph))
    spec = importlib.util.spec_from_file_location("holding_inspector", EXAMPLE_ROOT / "inspect_problem.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    values = module._assignments(data)

    class CountedArcs(list):
        iterations = 0

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    targets = CountedArcs(values["arc_to"])
    values["arc_to"] = targets
    monkeypatch.setattr(module, "_assignments", lambda path: values)
    assert module.inspect(data)["valid"]
    # Type checking, bounds checking and oracle construction are linear passes;
    # reachability must use its already-validated CSR windows, not V*E scans.
    assert targets.iterations <= 3


@pytest.mark.parametrize("options", [[4.5], [.5, .75], [.5, float("inf")]])
def test_invalid_dwell_choices_are_rejected(options):
    environment = _holding_environment()
    environment["fixed_view_dwell_options_s"] = options
    with pytest.raises(ValueError, match="dwell options"):
        build_candidate_dag(environment, _belief((1,)))


def _gap_environment():
    environment = _holding_environment()
    environment.pop("fixed_view_dwell_options_s")
    view = environment["surveillance_views"][0]
    view["report_ids"] = []
    view["gap_observation_windows"] = [
        {"start_s": 11, "end_s": 13.5}, {"start_s": 14, "end_s": 16.5},
    ]
    return environment


def _native_gap_plan(environment, belief, tmp_path):
    graph = build_candidate_dag(environment, belief)
    route = longest_path_oracle(graph)
    data = tmp_path / "gap.dzn"
    data.write_text(serialize_minizinc_data(graph))
    native = subprocess.run([
        "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc", "--solver", "coin-bc",
        "--json-stream", str(EXAMPLE_ROOT / "model.mzn"), str(data),
    ], capture_output=True, text=True, check=True)
    stream = [json.loads(line) for line in native.stdout.splitlines()]
    assert any(row.get("status") == "OPTIMAL_SOLUTION" for row in stream)
    result = json.loads(next(row for row in stream if row["type"] == "solution")["output"]["default"])
    assert result["combined_score"] == round(route.score * SCORE_SCALE)
    assert [a["candidate_id"] for a in result["assignments"]] == [c.candidate_id for c in route.candidates]
    inspected = subprocess.run(["python", str(EXAMPLE_ROOT / "inspect_problem.py"), str(data)],
                               capture_output=True, text=True, check=True)
    inspection = json.loads(inspected.stdout)
    assert inspection["valid"]
    assert inspection["report_free_fixed_candidates"] == sum(not c.report_ids for c in graph.candidates)
    statechart_helpers = Path("conf/skills/hyper/creating-statechart-files/examples/event-information-patrol")
    planner_path, chart_path = tmp_path / "native.json", tmp_path / "statechart.json"
    planner_path.write_text(json.dumps(result))
    subprocess.run(["python", str(statechart_helpers / "prepare_statechart.py"), str(planner_path),
                    str(tmp_path / "generate_statechart.py"), str(chart_path)],
                   capture_output=True, text=True, check=True)
    inspected_chart = subprocess.run(["python", str(statechart_helpers / "inspect_statechart.py"),
                                     str(planner_path), str(chart_path)],
                                    capture_output=True, text=True, check=True)
    assert json.loads(inspected_chart.stdout)["valid"]
    chart = json.loads(chart_path.read_text())
    for candidate in route.candidates:
        name, context = next((n, c) for n, c in chart["state_context"].items()
                             if c.get("candidate_id") == candidate.candidate_id)
        outgoing = next(t for t in chart["transitions"] if t["source"] == name)
        assert outgoing["context"]["readiness"]["not_before"]["seconds"] == candidate.end_s
        assert context["planner_item"]["parameters"]["scored_observation_windows"]
    context["planner_item"]["parameters"].pop("scored_observation_windows")
    chart_path.write_text(json.dumps(chart))
    rejected = subprocess.run(["python", str(statechart_helpers / "inspect_statechart.py"),
                               str(planner_path), str(chart_path)], capture_output=True, text=True, check=False)
    assert rejected.returncode != 0
    assert "planner item metadata differs" in rejected.stderr
    return graph, route, result["assignments"][0]


def test_gap_only_native_route_has_no_fabricated_reports_and_shared_gate_score(tmp_path):
    environment, belief = _gap_environment(), _belief((1,))
    environment["surveillance_views"] *= 2  # Repeated rows must not duplicate windows/exposure.
    environment["surveillance_views"].append({**environment["surveillance_views"][0], "x": 0.0, "y": 0.0})
    graph, route, assignment = _native_gap_plan(environment, belief, tmp_path)
    assert len(graph.candidates) == len({c.candidate_id for c in graph.candidates}) == 2
    assert all(c.recall_utility == c.estimation_utility == 0 for c in graph.candidates)
    assert route.covered_report_ids == ()
    assert assignment["parameters"]["report_ids"] == []
    assert assignment["parameters"]["report_span"] == 0
    assert "observation_delay" not in assignment["parameters"]
    assert assignment["parameters"]["scored_observation_windows"] == [
        {"start": 22, "duration": 5, "time_scale": 2},
        {"start": 28, "duration": 5, "time_scale": 2},
    ]
    context = {"candidate_id": assignment["candidate_id"], "surveillance_mode": "fixed_view",
               "target_entity_id": None, "target_report_ids": [], "planner_item": assignment,
               "observation_window": {"start": {"seconds": 11}, "duration": {"seconds": 5.5}}}
    chart = Statechart(mission_id="mission-1", plan_revision=1, mission_snapshot_id="snapshot-1",
                      planning_profile="temporal", entry_state="view", states=("view",),
                      transitions=(), terminal_states=("view",), state_context={"view": context})
    status = FSMStatus(mission_id="mission-1", plan_revision=1, statechart_revision=1,
                       active_state="view", active_state_context=context)
    gate = Mission1ReplanGate()
    decision, advisory = gate.assess(environment, belief, chart, status)
    assert decision.current_score == advisory.score == route.score
    assert not decision.trigger
    environment["controlled_vehicle"]["position"]["x"] = 5000
    decision, _ = gate.assess(environment, belief, chart, status)
    assert decision.trigger and decision.reason == "next_assignment_infeasible"
    environment["controlled_vehicle"]["position"]["x"] = 0
    environment["mission_time_seconds"] = 11.5
    decision, _ = gate.assess(environment, belief, chart, status)
    rate = belief.ships[0].expected_omission_probability * .1
    assert decision.current_score == pytest.approx(3.5 * rate, abs=2e-6)
    assert not decision.trigger


def test_gap_candidates_exclude_expired_unreachable_and_reserved_exposure():
    environment, belief = _gap_environment(), _belief((1,))
    view = environment["surveillance_views"][0]
    view["gap_observation_windows"].append({"start_s": 17, "end_s": 19.5})
    assert len(build_candidate_dag(environment, belief).candidates) == 2
    environment["mission_time_seconds"] = 13.5
    assert len(build_candidate_dag(environment, belief).candidates) == 1
    environment["controlled_vehicle"]["position"]["x"] = 5000
    assert not build_candidate_dag(environment, belief).candidates


def test_mixed_gap_and_report_run_preserves_real_report_span_and_delay(tmp_path):
    environment = _gap_environment()
    environment["observation_window_seconds"] = 4
    environment["static_info"] += [_report("other-start", 2, 0, 5000, 0), _report("other-end", 2, 40, 5000, 0)]
    gap = environment["surveillance_views"][0]
    gap["holding_intervals"] = [{"entity_id": 2, "start_s": 1, "end_s": 36}]
    gap["gap_observation_windows"] = [{"start_s": 2, "end_s": 4.5}, {"start_s": 16, "end_s": 18.5}]
    environment["surveillance_views"].append({
        "x": 0, "y": 0, "arrival_direction": 0, "report_ids": ["anchor"], "observation_delay_s": 4,
    })
    graph, route, assignment = _native_gap_plan(environment, _belief((1, 2)), tmp_path)
    assert len(graph.candidates) == 3
    assert len(route.candidates) == 1
    assert route.candidates[0].report_span_s == assignment["parameters"]["report_span"] == 0
    assert assignment["parameters"]["report_ids"] == ["anchor"]
    assert assignment["parameters"]["observation_delay"] == {"minimum": 8, "maximum": 8, "time_scale": 2}
    assert assignment["start"] == 4 and assignment["duration"] == 33


@pytest.mark.parametrize("window", [{"start_s": 1, "end_s": 1.5}, {"start_s": .25, "end_s": 2}])
def test_gap_windows_require_quantized_positive_capture_exposure(window):
    environment = _gap_environment()
    environment["surveillance_views"][0]["gap_observation_windows"] = [window]
    with pytest.raises(ValueError, match="gap windows"):
        build_candidate_dag(environment, _belief((1,)))


def test_gap_nodes_keep_delayed_report_epoch_order_and_report_uniqueness():
    environment = _environment([
        *[_report(name, 1, 20, 0, 0) for name in ("a", "b", "c")],
        _report("other-start", 2, 0, 5000, 0), _report("other-end", 2, 50, 5000, 0),
    ])
    environment.update(observation_window_seconds=4, event_check_window_seconds=4)
    environment["controlled_vehicle"].update(heading_degrees=90, quarter_turn_seconds=.5)
    environment["surveillance_views"] = [
        {"x": 0, "y": 0, "arrival_direction": 0, "report_ids": ["a", "b"]},
        {"x": 0, "y": 0, "arrival_direction": 0, "report_ids": ["b", "c"], "observation_delay_s": 4},
        {"x": 0, "y": 0, "arrival_direction": 0, "report_ids": [],
         "gap_observation_windows": [{"start_s": 21, "end_s": 22.5}],
         "holding_intervals": [{"entity_id": 2, "start_s": 21, "end_s": 22}]},
    ]
    graph = build_candidate_dag(environment, _belief((1, 2)))
    gap = next(i for i, c in enumerate(graph.candidates, 1) if not c.report_ids)
    late = next(i for i, c in enumerate(graph.candidates, 1) if c.start_s == 24)
    assert (gap, late) not in graph.arcs

    def visit(node, ids):
        if node == graph.sink:
            assert len(ids) == len(set(ids))
            return
        for source, target in graph.arcs:
            if source == node:
                extra = () if target == graph.sink else graph.candidates[target - 1].report_ids
                visit(target, ids + extra)

    visit(graph.source, ())


def test_gap_windows_require_visibility_forecasts_and_detector_lookback():
    environment = _gap_environment()
    environment["event_check_window_seconds"] = 0
    with pytest.raises(ValueError, match="detector lookback"):
        build_candidate_dag(environment, _belief((1,)))
    environment["event_check_window_seconds"] = 4
    environment["surveillance_views"][0].pop("holding_intervals")
    with pytest.raises(ValueError, match="gap windows require"):
        build_candidate_dag(environment, _belief((1,)))
