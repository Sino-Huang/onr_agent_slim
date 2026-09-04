from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from onr.application.mission1_planning import (
    Mission1ReplanGate,
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
            _report("report-2", 7, 12.0, 30.0, 0.0),
            _report("report-3", 7, 14.0, 50.0, 0.0),
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
                _report("report-b", 1, 12.0, 30.0, 0.0),
                _report("report-c", 1, 14.0, 50.0, 0.0),
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
            _report("report-b", 7, 12.0, 30.0, 0.0),
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
            _report("report-b", 7, 12.0, 30.0, 0.0),
        ],
        fov=1.0,
    )
    route = longest_path_oracle(build_candidate_dag(environment, belief))
    candidate = route.candidates[0]
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
