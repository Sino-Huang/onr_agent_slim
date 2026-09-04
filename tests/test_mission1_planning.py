from __future__ import annotations

import json
import subprocess
from pathlib import Path

from onr.application.mission1_planning import (
    Mission1ReplanGate,
    build_candidate_dag,
    longest_path_oracle,
    serialize_minizinc_data,
)
from onr.application.reporting_reliability import ReportingReliabilityManager
from onr.contracts.reporting_reliability import ReportingReliabilitySnapshot


NOW = "2026-09-03T00:00:00+10:00"
EXAMPLE_ROOT = Path(
    "conf/skills/hyper/creating-minizinc-problem-files/examples/"
    "event-information-patrol"
)


def _belief(ship_ids: tuple[int, ...]):
    manager = ReportingReliabilityManager("mission-1", ship_ids)
    return manager.snapshot(
        input_event_id="initial", input_revision=0, created_at=NOW
    )


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
    belief = manager.snapshot(
        input_event_id="tick-1", input_revision=1, created_at=NOW
    )
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


def test_replan_gate_exact_boundary_infeasibility_zero_and_explicit_request() -> None:
    gate = Mission1ReplanGate(relative_improvement_threshold=0.10)

    assert not gate.evaluate(10.0, 10.999999, next_assignment_feasible=True).trigger
    boundary = gate.evaluate(10.0, 11.0, next_assignment_feasible=True)
    assert boundary.trigger and boundary.reason == "score_improvement"
    assert gate.evaluate(4.0, 4.0, next_assignment_feasible=False).reason == "next_assignment_infeasible"
    assert gate.evaluate(0.0, 0.1, next_assignment_feasible=True).reason == "positive_route_from_zero"
    assert gate.evaluate(10.0, 9.0, next_assignment_feasible=True, explicit_request=True).reason == "explicit_replan_request"


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
