from __future__ import annotations

import json
import subprocess

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


@pytest.mark.parametrize("options", [[4.5], [.5, .75], [.5, float("inf")]])
def test_invalid_dwell_choices_are_rejected(options):
    environment = _holding_environment()
    environment["fixed_view_dwell_options_s"] = options
    with pytest.raises(ValueError, match="dwell options"):
        build_candidate_dag(environment, _belief((1,)))
