from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from onr.application.mission1_planning import (
    SCORE_SCALE,
    Mission1ReplanGate,
    _travel_time,
    build_candidate_dag,
    longest_path_oracle,
    public_report_rates,
    serialize_minizinc_data,
)
from onr.application.reporting_reliability import ReportingReliabilityManager
from onr.contracts.fsm import FSMStatus, Statechart
from onr.contracts.reporting_reliability import ReportingReliabilitySnapshot

NOW = "2026-09-03T00:00:00+10:00"
EXAMPLE_ROOT = Path(
    "conf/skills/hyper/creating-minizinc-problem-files/examples/"
    "event-information-patrol"
)


def _belief(ship_ids: tuple[int, ...]):
    manager = ReportingReliabilityManager("mission-1", ship_ids)
    return manager.snapshot(input_event_id="initial", input_revision=0, created_at=NOW)


def _environment(reports: list[dict[str, object]], *, fov: float = 5.0):
    return {
        "mission_id": "mission-1",
        "mission_time_seconds": 0.0,
        "state_version": 0,
        "controlled_vehicle": {
            "entity_id": "drone-1",
            "position": {"x": 0.0, "y": 0.0, "z": -20.0},
            "max_velocity": 10.0,
            "fov_radius": fov,
        },
        "world_model_info": {"event_report_checks": []},
        "static_info": reports,
    }


def _report(report_id: str, ship: int, time_s: float, x: float, y: float):
    return {
        "report_id": report_id,
        "entity_id": ship,
        "time": time_s,
        "position": [x, y, -2.5],
        "event type": "intersection decision",
        "event information": {"decision": "left"},
    }


def test_travel_budget_uses_cardinal_distance_and_ninety_percent_speed() -> None:
    assert _travel_time(0, 0, 511, 318, 30) == pytest.approx(829 / 27)
    assert _travel_time(511, 318, 0, 0, 30) == pytest.approx(829 / 27)
    environment = _environment([_report("too-early", 1, 21, 511, 318)])
    environment["controlled_vehicle"]["max_velocity"] = 30.0
    graph = build_candidate_dag(environment, _belief((1,)))
    assert not graph.candidates


@pytest.mark.parametrize("radius, covered_count", [(100.0, 1), (300.0, 2)])
def test_fixed_view_uses_advertised_sensor_range(radius, covered_count):
    environment = _environment([
        _report("near", 1, 60.0, 0.0, 0.0),
        _report("further", 2, 60.0, 350.0, 0.0),
    ], fov=radius)
    graph = build_candidate_dag(environment, _belief((1, 2)))
    route = longest_path_oracle(graph)
    assert len(route.candidates) == 1
    assert route.candidates[0].mode == "fixed_view"
    assert len(route.covered_report_ids) == covered_count


@pytest.mark.parametrize("event_time", [12.0, 30.0])
def test_midpoint_view_covers_reports_without_reaching_each_ship(event_time):
    environment = _environment([
        _report("a", 1, event_time, 0.0, 0.0),
        _report("b", 2, event_time, 150.0, 0.0),
    ], fov=100.0)
    route = longest_path_oracle(build_candidate_dag(environment, _belief((1, 2))))
    assert set(route.covered_report_ids) == {"a", "b"}
    assert len(route.candidates) == 1
    assert (route.candidates[0].x, route.candidates[0].y) == (75, 0)


def test_distinct_fixed_viewpoints_keep_distinct_stable_identity():
    reports = [_report("a", 1, 30, 0, 0), _report("b", 2, 30, 100, 0)]
    belief = _belief((1, 2))
    graph = build_candidate_dag(_environment(reports, fov=100), belief)
    views = [c for c in graph.candidates if c.mode == "fixed_view" and len(c.report_ids) == 2]
    assert {(c.x, c.y) for c in views} >= {(0, 0), (50, 0), (100, 0)}
    assert len({c.candidate_id for c in views}) == len(views)
    reordered = build_candidate_dag(_environment(list(reversed(reports)), fov=100), belief)
    assert graph == reordered


def test_fixed_viewpoint_can_be_reused_at_a_different_report_time():
    environment = _environment([
        _report("earlier", 1, 10, 100, 0),
        _report("later", 2, 11, 200, 0),
    ], fov=150)
    environment["controlled_vehicle"]["max_velocity"] = 20.0
    # The later ship's own position is unreachable by t=11 and the current
    # position cannot see it. The earlier report's viewpoint sees both times.
    assert _travel_time(0, 0, 200, 0, 20) > 11
    graph = build_candidate_dag(environment, _belief((1, 2)))
    assert any(
        c.mode == "fixed_view" and c.report_ids == ("later",) and (c.x, c.y) == (100, 0)
        for c in graph.candidates
    )
    route = longest_path_oracle(graph)
    assert route.covered_report_ids == ("earlier", "later")


def test_fixed_view_coverage_is_checked_after_integer_coordinate_rounding():
    import math

    reports = [_report("a", 1, 30, 0.49, 0), _report("b", 2, 30, 200.49, 0)]
    graph = build_candidate_dag(_environment(reports, fov=100), _belief((1, 2)))
    positions = {r["report_id"]: r["position"][:2] for r in reports}
    for candidate in graph.candidates:
        if candidate.mode == "fixed_view":
            assert candidate.x == round(candidate.x) and candidate.y == round(candidate.y)
            assert all(math.dist((candidate.x, candidate.y), positions[r]) <= 100 for r in candidate.report_ids)


def test_current_view_is_feasible_without_flying_to_a_report():
    environment = _environment([_report("a", 1, 0.5, 80, 0)], fov=100)
    route = longest_path_oracle(build_candidate_dag(environment, _belief((1,))))
    assert route.covered_report_ids == ("a",)
    assert (route.candidates[0].x, route.candidates[0].y) == (0, 0)


@pytest.mark.parametrize(
    "selected_x, now, checked, expected_infeasible",
    [(0, 29, False, False), (100, 29, False, True), (50, 24, True, False)],
)
def test_replan_checks_selected_viewpoint_not_an_alternative(selected_x, now, checked, expected_infeasible):
    environment = _environment([
        _report("a", 1, 30, 0, 0), _report("b", 2, 30, 100, 0),
    ], fov=100)
    belief = _belief((1, 2))
    context = {
        "candidate_id": "selected-viewpoint", "surveillance_mode": "fixed_view",
        "target_entity_id": None, "target_report_ids": ["a", "b"],
        "observation_window": {"start": {"seconds": 30}, "duration": {"seconds": .5}},
        "planner_item": {"parameters": {"x": selected_x, "y": 0}},
    }
    chart = Statechart(
        mission_id="mission-1", plan_revision=1, mission_snapshot_id="snapshot-1",
        planning_profile="temporal", entry_state="active", states=("active",),
        transitions=(), terminal_states=("active",), state_context={"active": context},
    )
    status = FSMStatus(mission_id="mission-1", plan_revision=1, statechart_revision=1,
        active_state="active", active_state_context=context)
    environment["mission_time_seconds"] = now
    if checked:
        environment["world_model_info"]["event_report_checks"] = [{"check_id": "already-checked", "report_id": "a", "outcome": "clean"}]
    decision, _ = Mission1ReplanGate().assess(environment, belief, chart, status)
    assert (decision.reason == "next_assignment_infeasible") == expected_infeasible
    assert decision.trigger == expected_infeasible


def test_midpoint_route_matches_real_minizinc(tmp_path):
    environment = _environment([
        _report("a", 1, 12, 0, 0), _report("b", 2, 12, 150, 0),
    ], fov=100)
    graph = build_candidate_dag(environment, _belief((1, 2)))
    oracle = longest_path_oracle(graph)
    data = tmp_path / "data.dzn"
    data.write_text(serialize_minizinc_data(graph))
    result = subprocess.run([
        "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc", "--solver", "coin-bc",
        str(EXAMPLE_ROOT / "model.mzn"), str(data),
    ], capture_output=True, text=True, check=True)
    solved = json.loads(result.stdout.splitlines()[0])
    assert [a["candidate_id"] for a in solved["assignments"]] == [c.candidate_id for c in oracle.candidates]
    assert solved["combined_score"] == round(oracle.score * SCORE_SCALE)
    assert solved["assignments"][0]["parameters"]["x"] == 75
    assert set(solved["assignments"][0]["parameters"]["report_ids"]) == {"a", "b"}


def test_objective_potentials_shift_every_route_by_the_same_constant():
    graph = build_candidate_dag(_environment([
        _report("a", 1, 10, 0, 0), _report("b", 2, 10, 8, 0),
        _report("c", 3, 20, 15, 0),
    ], fov=5), _belief((1, 2, 3)))
    data = {name: json.loads(value[:-1]) for name, value in (
        line.split(" = ", 1) for line in serialize_minizinc_data(graph).splitlines()
    )}
    potentials = data["node_objective_potential"]
    scores = [a + b + c for a, b, c in zip(data["candidate_recall"], data["candidate_estimation"], data["candidate_omission"])]
    weights = [0] + [
        score * data["maneuver_bound"] * data["duration_bound"] * data["tie_break_bound"]
        - data["duration_bound"] * data["tie_break_bound"]
        - duration * data["tie_break_bound"] - (i + 1)
        for i, (score, duration) in enumerate(zip(scores, data["candidate_duration"]))
    ] + [0]
    outgoing = [[] for _ in potentials]
    for u, v in graph.arcs:
        outgoing[u].append(v)

    priorities = []

    def check_paths(node, original, reduced, loss=0):
        if node == graph.sink:
            assert reduced == original + potentials[graph.source] - potentials[graph.sink]
            priorities.append((original, loss))
            return 1
        return sum(check_paths(v, original + weights[v],
            reduced + weights[v] + potentials[node] - potentials[v],
            loss + int(weights[v] + potentials[node] - potentials[v] < 0)) for v in outgoing[node])

    assert check_paths(graph.source, 0, 0) > 1
    best = max(original for original, _ in priorities)
    assert all((original == best) == (loss == 0) for original, loss in priorities)


def test_large_lexicographic_weights_keep_solver_tie_parity(tmp_path, monkeypatch):
    import onr.application.mission1_planning as planning
    from onr.adapters.minizinc import MiniZincExecutor
    from onr.contracts.planning import PlanningOutcome

    # Minimize the observed ~10^15-objective failure to three tied viewpoints.
    # Scaling units changes neither the public evidence nor the utility ratio.
    monkeypatch.setattr(planning, "SCORE_SCALE", 1_000_000_000_000_000)
    graph = planning.build_candidate_dag(_environment([
        _report("a", 1, 30, 0, 0), _report("b", 2, 30, 100, 0),
    ], fov=100), _belief((1, 2)))
    oracle = planning.longest_path_oracle(graph)
    data = planning.serialize_minizinc_data(graph)
    values = {name: json.loads(value[:-1]) for name, value in (
        line.split(" = ", 1) for line in data.splitlines()
    )}
    assert max(values["node_objective_potential"]) > 2**53
    executor = MiniZincExecutor(
        Path("modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc").resolve(), tmp_path / "solver",
    )
    assets = {"model.mzn": (EXAMPLE_ROOT / "model.mzn").read_bytes(), "data.dzn": data.encode()}
    result = executor.execute(assets, "coin-bc")
    stream = [json.loads(line) for line in result.stdout.splitlines()]
    assert any(row.get("status") == "OPTIMAL_SOLUTION" for row in stream)
    native = json.loads(next(row["output"]["default"] for row in reversed(stream) if row.get("type") == "solution"))
    assert [a["candidate_id"] for a in native["assignments"]] == [c.candidate_id for c in oracle.candidates]
    corrupted = data.replace("node_objective_potential = [0,", "node_objective_potential = [1,", 1)
    # Instance checking validates types; the assert is evaluated on flattening.
    rejected = executor.execute({**assets, "data.dzn": corrupted.encode()}, "coin-bc")
    assert rejected.outcome is PlanningOutcome.ERROR
    # Rejection may occur at the potential assertion or during construction of
    # its zero-cost predecessor set. Neither may produce an accepted plan.
    assert not any(
        row.get("type") == "solution"
        for row in (json.loads(line) for line in rejected.stdout.splitlines())
    )


def test_equal_candidate_order_sums_have_one_canonical_solver_route(tmp_path):
    from onr.application.mission1_planning import CandidateDAG

    seed = build_candidate_dag(
        _environment([_report("seed", 1, 10, 0, 0)]), _belief((1,)),
    ).candidates[0]
    candidates = tuple(
        replace(seed, candidate_id=f"c{i}", report_ids=(f"r{i}",),
                start_s=10 if i <= 2 else 20, end_s=10.5 if i <= 2 else 20.5)
        for i in range(1, 5)
    )
    # [1,4] and [2,3] have identical utility, count, duration AND index sum.
    # Resolve the residual tie by smallest optimal predecessor, back from sink.
    graph = CandidateDAG(candidates, ((0, 1), (0, 2), (1, 4), (2, 3), (3, 5), (4, 5)), 0, 5)
    oracle = longest_path_oracle(graph)
    expected = ["c2", "c3"]
    assert [c.candidate_id for c in oracle.candidates] == expected
    data = tmp_path / "data.dzn"
    data.write_text(serialize_minizinc_data(graph))
    result = subprocess.run([
        "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc", "--solver", "coin-bc",
        str(EXAMPLE_ROOT / "model.mzn"), str(data),
    ], capture_output=True, text=True, check=True)
    native = json.loads(result.stdout.splitlines()[0])
    assert [c["candidate_id"] for c in native["assignments"]] == expected
    assert native["combined_score"] == round(oracle.score * SCORE_SCALE)


def test_dag_skips_backward_time_pairs_before_travel_calculation(monkeypatch) -> None:
    import onr.application.mission1_planning as planning

    count = 20
    calls = 0

    def measured_travel(*args):
        nonlocal calls
        calls += 1
        return _travel_time(*args)

    monkeypatch.setattr(planning, "_travel_time", measured_travel)
    graph = build_candidate_dag(
        _environment([
            _report(f"r{i}", i, 10 + 2 * i, i, 0)
            for i in range(1, count + 1)
        ]),
        _belief(tuple(range(1, count + 1))),
    )
    candidate_count = len(graph.candidates)
    assert {r for c in graph.candidates for r in c.report_ids} == {f"r{i}" for i in range(1, count + 1)}
    # One viewpoint admission check per candidate; only forward temporal
    # pairs can require a route travel calculation, including alternate views.
    assert calls <= candidate_count + candidate_count * (candidate_count - 1) // 2
    assert len(longest_path_oracle(graph).candidates) == count


def test_travel_margin_filters_initial_arrival_and_route_transitions() -> None:
    graph = build_candidate_dag(
        _environment(
            [
                _report("at-margin", 1, 10, 90, 0),
                _report("beyond-margin", 2, 10, 91, 0),
                _report("next", 3, 20.5, 181, 0),
            ],
            fov=0.1,
        ),
        _belief((1, 2, 3)),
    )
    by_report = {c.report_ids[0]: i + 1 for i, c in enumerate(graph.candidates)}
    assert "at-margin" in by_report
    assert "beyond-margin" not in by_report
    assert "next" in by_report
    assert (by_report["at-margin"], by_report["next"]) not in graph.arcs


def test_pursuit_rejects_motion_requiring_the_unreserved_maximum_speed() -> None:
    graph = build_candidate_dag(
        _environment(
            [
                _report("a", 1, 10, 10, 0),
                _report("b", 1, 12, 30, 0),
            ],
            fov=1,
        ),
        _belief((1,)),
    )
    assert all(c.mode == "fixed_view" for c in graph.candidates)


def test_fixed_view_wins_for_an_efficient_cluster_without_double_scoring() -> None:
    graph = build_candidate_dag(
        _environment(
            [
                _report("report-a", 1, 10.0, 10.0, 0.0),
                _report("report-b", 2, 10.0, 11.0, 0.0),
            ]
        ),
        _belief((1, 2)),
    )
    result = longest_path_oracle(graph)

    assert len(result.candidates) == 1
    assert result.candidates[0].mode == "fixed_view"
    assert result.candidates[0].report_ids == ("report-a", "report-b")
    assert len(set(result.covered_report_ids)) == len(result.covered_report_ids)


def test_pursuit_wins_for_a_dense_risky_ship_and_checked_reports_are_excluded() -> None:
    manager = ReportingReliabilityManager("mission-1", (7,))
    manager.update_checks(
        (
            {
                "check_id": "altered-1",
                "report_id": "old-report",
                "entity_id": 7,
                "event_time_s": 1.0,
                "checked_at_s": 1.0,
                "outcome": "altered",
            },
        ),
        input_event_id="tick-1",
        input_revision=1,
        created_at=NOW,
    )
    belief = manager.snapshot(input_event_id="tick-1", input_revision=1, created_at=NOW)
    environment = _environment(
        [
            _report("old-report", 7, 2.0, 1.0, 0.0),
            _report("report-1", 7, 10.0, 10.0, 0.0),
            _report("report-2", 7, 12.0, 28.0, 0.0),
            _report("report-3", 7, 14.0, 46.0, 0.0),
        ],
        fov=1.0,
    )
    environment["world_model_info"] = {
        "event_report_checks": [
            {
                "check_id": "old-check",
                "report_id": "old-report",
                "entity_id": 7,
                "event_time_s": 2.0,
                "checked_at_s": 2.0,
                "outcome": "clean",
            }
        ]
    }

    graph = build_candidate_dag(environment, belief)
    result = longest_path_oracle(graph)

    assert result.candidates[0].mode == "pursue_ship"
    assert result.candidates[0].entity_id == 7
    assert "old-report" not in result.covered_report_ids
    assert result.candidates[0].omission_yield > 0.0


def test_pursuit_candidates_are_every_reachable_contiguous_window() -> None:
    graph = build_candidate_dag(
        _environment(
            [
                _report("report-a", 1, 10.0, 10.0, 0.0),
                _report("report-b", 1, 12.0, 28.0, 0.0),
                _report("report-c", 1, 14.0, 46.0, 0.0),
                _report("report-d", 1, 16.0, 100.0, 0.0),
            ],
            fov=1.0,
        ),
        _belief((1,)),
    )

    windows = {
        candidate.report_ids
        for candidate in graph.candidates
        if candidate.mode == "pursue_ship"
    }

    assert windows == {
        ("report-a", "report-b"),
        ("report-a", "report-b", "report-c"),
        ("report-b", "report-c"),
    }


def test_report_rate_uses_complete_valid_schedule_and_zero_for_one_timestamp() -> None:
    belief = _belief((1, 2))
    environment = _environment(
        [
            _report("expired", 1, -10.0, 0.0, 0.0),
            _report("checked", 1, 10.0, 10.0, 0.0),
            _report("unreachable", 1, 30.0, 1000.0, 0.0),
            _report("duplicate", 1, 40.0, 0.0, 0.0),
            _report("duplicate", 1, 50.0, 0.0, 0.0),
            _report("only-time", 2, 12.0, 0.0, 0.0),
            _report("same-time", 2, 12.0, 1.0, 0.0),
        ]
    )
    environment["world_model_info"] = {
        "event_report_checks": [{"report_id": "checked"}]
    }

    rates = public_report_rates(environment, belief)

    assert rates[1] == pytest.approx(3.0 / 50.0)
    assert rates[2] == 0.0


def test_pursuit_omission_yield_uses_joint_risk_rate_and_report_span() -> None:
    belief = _belief((7,))
    environment = _environment(
        [
            _report("past", 7, -2.0, 0.0, 0.0),
            _report("report-a", 7, 10.0, 10.0, 0.0),
            _report("report-b", 7, 14.0, 30.0, 0.0),
        ],
        fov=1.0,
    )

    graph = build_candidate_dag(environment, belief)
    pursuit = next(
        candidate for candidate in graph.candidates if candidate.mode == "pursue_ship"
    )
    ship = belief.ships[0]

    assert pursuit.public_report_rate == pytest.approx(2.0 / 16.0)
    assert pursuit.report_span_s == 4.0
    assert pursuit.omission_yield == pytest.approx(
        ship.expected_omission_probability * (2.0 / 16.0) * 4.0
    )
    assert pursuit.duration_s == 4.5


def test_clean_evidence_can_change_pursuit_preference_to_fixed_view() -> None:
    environment = _environment(
        [
            _report("report-a", 7, 10.0, 10.0, 0.0),
            _report("report-cluster", 8, 10.0, 11.0, 0.0),
            _report("report-b", 7, 12.0, 28.0, 0.0),
        ]
    )
    manager = ReportingReliabilityManager("mission-1", (7, 8))
    prior = manager.snapshot(input_event_id="prior", input_revision=0, created_at=NOW)
    assert (
        longest_path_oracle(build_candidate_dag(environment, prior)).candidates[0].mode
        == "pursue_ship"
    )

    manager.update_checks(
        (
            {
                "check_id": "clean-7",
                "report_id": "past-7",
                "entity_id": 7,
                "event_time_s": -1.0,
                "checked_at_s": 0.0,
                "outcome": "clean",
            },
        ),
        input_event_id="clean",
        input_revision=1,
        created_at=NOW,
    )
    clean = manager.snapshot(input_event_id="clean", input_revision=1, created_at=NOW)
    route = longest_path_oracle(build_candidate_dag(environment, clean))

    assert clean.ships[0].mean < prior.ships[0].mean
    assert route.candidates[0].mode == "fixed_view"


def test_replan_gate_exact_boundary_infeasibility_zero_and_explicit_request() -> None:
    gate = Mission1ReplanGate(relative_improvement_threshold=0.10)

    assert not gate.evaluate(10.0, 10.999999, next_assignment_feasible=True).trigger
    boundary = gate.evaluate(10.0, 11.0, next_assignment_feasible=True)
    assert boundary.trigger and boundary.reason == "score_improvement"
    assert (
        gate.evaluate(4.0, 4.0, next_assignment_feasible=False).reason
        == "next_assignment_infeasible"
    )
    assert (
        gate.evaluate(0.0, 0.1, next_assignment_feasible=True).reason
        == "positive_route_from_zero"
    )
    assert (
        gate.evaluate(
            10.0, 9.0, next_assignment_feasible=True, explicit_request=True
        ).reason
        == "explicit_replan_request"
    )


def test_replan_gate_rescores_active_pursuit_with_shared_components() -> None:
    manager = ReportingReliabilityManager("mission-1", (7,))
    manager.update_checks(
        (
            {
                "check_id": "altered-7",
                "report_id": "past-7",
                "entity_id": 7,
                "event_time_s": -1.0,
                "checked_at_s": 0.0,
                "outcome": "altered",
            },
        ),
        input_event_id="altered",
        input_revision=1,
        created_at=NOW,
    )
    belief = manager.snapshot(
        input_event_id="altered", input_revision=1, created_at=NOW
    )
    environment = _environment(
        [
            _report("report-a", 7, 10.0, 10.0, 0.0),
            _report("report-b", 7, 12.0, 26.0, 0.0),
        ],
        fov=1.0,
    )
    route = longest_path_oracle(build_candidate_dag(environment, belief))
    candidate = route.candidates[0]
    assert candidate.mode == "pursue_ship"
    context = {
        "candidate_id": candidate.candidate_id,
        "surveillance_mode": candidate.mode,
        "target_entity_id": candidate.entity_id,
        "target_report_ids": list(candidate.report_ids),
        "observation_window": {
            "start": {"seconds": candidate.start_s},
            "duration": {"seconds": candidate.duration_s},
        },
    }
    chart = Statechart(
        mission_id="mission-1",
        plan_revision=1,
        mission_snapshot_id="mission-1:snapshot:1",
        planning_profile="temporal",
        entry_state="active",
        states=("active",),
        transitions=(),
        terminal_states=("active",),
        state_context={"active": context},
    )
    status = FSMStatus(
        mission_id="mission-1",
        plan_revision=1,
        statechart_revision=1,
        active_state="active",
        active_state_context=context,
    )

    decision, advisory = Mission1ReplanGate().assess(environment, belief, chart, status)

    assert decision.current_score == advisory.score
    assert not decision.trigger

    # Acquired before the scheduled window: reaching the report's exact
    # position is no longer a prerequisite for an already-visible target.
    environment["mission_time_seconds"] = 9.0
    environment["controlled_vehicle"]["fov_radius"] = 20.0
    environment["maneuver_lifecycle"] = {
        "action": "pursue", "lifecycle": "active", "phase": "pursuit",
        "plan_revision": 1, "parameters": {"entity_id": 7}, "start_time": 8,
    }
    environment["world_model_info"]["visible_ship_ids"] = [7]
    early, _ = Mission1ReplanGate().assess(environment, belief, chart, status)
    assert not early.trigger
    # Retain both future reports, but do not score omission yield before the
    # assigned observation window starts.
    assert early.current_score == decision.current_score
    for change in (
        {"lifecycle": "accepted"}, {"action": "navigate"},
        {"parameters": {"entity_id": 8}},
    ):
        inactive_environment = {
            **environment,
            "maneuver_lifecycle": {**environment["maneuver_lifecycle"], **change},
        }
        not_acquired, _ = Mission1ReplanGate().assess(inactive_environment, belief, chart, status)
        assert not_acquired.reason == "next_assignment_infeasible"
    environment["world_model_info"]["visible_ship_ids"] = []
    flicker, _ = Mission1ReplanGate().assess(environment, belief, chart, status)
    assert not flicker.trigger  # Tracking controller bridges brief camera loss.
    environment["maneuver_lifecycle"]["phase"] = "search"
    unseen, _ = Mission1ReplanGate().assess(environment, belief, chart, status)
    assert unseen.reason == "next_assignment_infeasible"
    environment["world_model_info"]["visible_ship_ids"] = [7]
    acquired_now, _ = Mission1ReplanGate().assess(environment, belief, chart, status)
    assert not acquired_now.trigger  # Camera reacquired before phase publication.
    environment["maneuver_lifecycle"]["phase"] = "pursuit"
    retained, _ = Mission1ReplanGate().assess(
        environment, belief, replace(chart, plan_revision=2),
        replace(status, plan_revision=2, statechart_revision=2),
    )
    assert not retained.trigger  # An accepted replan need not replace the same pursuit.

    # One unchecked report remains in an already executing pursuit. It is
    # still useful, although it cannot be admitted as a new two-report window.
    environment["mission_time_seconds"] = 11.0
    environment["controlled_vehicle"]["position"] = {"x": 21.0, "y": 0.0, "z": -20.0}
    decision, remaining_route = Mission1ReplanGate().assess(
        environment, belief, chart, status
    )
    assert decision.reason != "next_assignment_infeasible"
    assert remaining_route.candidates[0].mode == "fixed_view"
    remaining_yield = (
        round(belief.ships[0].expected_omission_probability * 0.5 * 1.0 * SCORE_SCALE)
        / SCORE_SCALE
    )
    assert decision.current_score == pytest.approx(
        remaining_route.score + remaining_yield
    )
    not_active, _ = Mission1ReplanGate().assess(
        environment, belief, chart, replace(status, active_state_context={})
    )
    assert not_active.reason == "next_assignment_infeasible"

    environment["world_model_info"]["event_report_checks"] = [
        {"report_id": "report-b"}
    ]
    checked, _ = Mission1ReplanGate().assess(environment, belief, chart, status)
    assert checked.current_score == 0.0
    assert not checked.trigger


def test_minizinc_and_advisory_oracle_select_the_same_candidate_route(
    tmp_path: Path,
) -> None:
    graph = build_candidate_dag(
        _environment(
            [
                _report("report-a", 1, 10.0, 10.0, 0.0),
                _report("report-b", 2, 10.0, 11.0, 0.0),
            ]
        ),
        _belief((1, 2)),
    )
    oracle = longest_path_oracle(graph)
    data = tmp_path / "data.dzn"
    data.write_text(serialize_minizinc_data(graph), encoding="utf-8")
    result = subprocess.run(
        [
            "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc",
            "--solver",
            "coin-bc",
            "conf/skills/hyper/creating-minizinc-problem-files/examples/event-information-patrol/model.mzn",
            str(data),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    solved = json.loads(result.stdout.splitlines()[0])

    assert [item["candidate_id"] for item in solved["assignments"]] == [
        item.candidate_id for item in oracle.candidates
    ]
    assert solved["combined_score"] == round(oracle.score * solved["score_scale"])


def test_replan_example_rematerializes_current_evidence_with_solver_parity() -> None:
    environment = json.loads(
        (EXAMPLE_ROOT / "replan-environment.json").read_text(encoding="utf-8")
    )
    belief = ReportingReliabilitySnapshot.from_dict(
        json.loads((EXAMPLE_ROOT / "replan-belief.json").read_text(encoding="utf-8"))
    )
    graph = build_candidate_dag(environment, belief)
    oracle = longest_path_oracle(graph)
    data = (EXAMPLE_ROOT / "replan-data.dzn").read_text(encoding="utf-8")

    assert data == serialize_minizinc_data(graph)
    assert "report-past-altered" not in data
    assert "report-past-clean" not in data
    assert "report-checked" not in data
    assert oracle.candidates[0].mode == "pursue_ship"
    assert oracle.candidates[0].entity_id == 7

    result = subprocess.run(
        [
            "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc",
            "--solver",
            "coin-bc",
            str(EXAMPLE_ROOT / "model.mzn"),
            str(EXAMPLE_ROOT / "replan-data.dzn"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    solved = json.loads(result.stdout.splitlines()[0])

    assert [item["candidate_id"] for item in solved["assignments"]] == [
        item.candidate_id for item in oracle.candidates
    ]
    assert solved["combined_score"] == round(oracle.score * solved["score_scale"])
    assignment = solved["assignments"][0]
    candidate = oracle.candidates[0]
    assert assignment["surveillance_mode"] == candidate.mode
    assert assignment["entity_id"] == candidate.entity_id
    assert assignment["parameters"]["target_posterior_risk"] == round(
        candidate.target_posterior_risk * solved["score_scale"]
    )
    assert assignment["parameters"]["public_report_rate"] == round(
        candidate.public_report_rate * solved["score_scale"]
    )
    assert assignment["parameters"]["utility"]["combined"] == sum(
        assignment["parameters"]["utility"][name]
        for name in ("recall", "estimation", "omission_yield")
    )


@pytest.mark.parametrize(
    ("prefix", "expected_mode"),
    (("prior", "fixed_view"), ("counterexample", "fixed_view")),
)
def test_checked_in_prior_and_counterexample_are_deterministic_and_solve(
    prefix: str, expected_mode: str
) -> None:
    data_name = "data.dzn" if prefix == "prior" else f"{prefix}-data.dzn"
    environment = json.loads(
        (EXAMPLE_ROOT / f"{prefix}-environment.json").read_text(encoding="utf-8")
    )
    belief = ReportingReliabilitySnapshot.from_dict(
        json.loads((EXAMPLE_ROOT / f"{prefix}-belief.json").read_text(encoding="utf-8"))
    )
    graph = build_candidate_dag(environment, belief)
    oracle = longest_path_oracle(graph)

    assert (EXAMPLE_ROOT / data_name).read_text(
        encoding="utf-8"
    ) == serialize_minizinc_data(graph)
    assert oracle.candidates[0].mode == expected_mode

    result = subprocess.run(
        [
            "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc",
            "--solver",
            "coin-bc",
            str(EXAMPLE_ROOT / "model.mzn"),
            str(EXAMPLE_ROOT / data_name),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    solved = json.loads(result.stdout.splitlines()[0])

    assert solved["assignments"][0]["surveillance_mode"] == expected_mode
    assert [item["candidate_id"] for item in solved["assignments"]] == [
        item.candidate_id for item in oracle.candidates
    ]
    assert solved["combined_score"] == round(oracle.score * solved["score_scale"])
