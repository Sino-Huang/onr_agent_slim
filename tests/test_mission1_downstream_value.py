"""Decision value is not interchangeable with uncertainty reduction."""
import importlib.util
from dataclasses import replace
from pathlib import Path

import pytest
from test_mission1_planning import _environment, _report

from onr.application import mission1_planning as planning
from onr.application.reporting_reliability import ReportingReliabilityManager

SPEC = importlib.util.spec_from_file_location(
    "downstream", Path("scripts/inspect_mission1_downstream_value.py"))
diagnostic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostic)
STAMP = "2026-09-13T00:00:00Z"


def fixture():
    manager = ReportingReliabilityManager("test", (1, 2))
    belief = diagnostic.snapshot(manager, STAMP)
    environment = _environment([_report("a", 1, 10, 0, 0), _report("b", 2, 10, 20, 0)], fov=1)
    graph = planning.build_candidate_dag(environment, belief)
    candidates = tuple(next(c for c in graph.candidates if c.report_ids == (r,)) for r in ("a", "b"))
    return planning.CandidateDAG(candidates, ((0, 1), (0, 2), (1, 3), (2, 3)), 0, 3), planning._opportunities(environment, belief), manager


def test_early_check_has_value_when_later_target_can_change():
    graph, opportunities, manager = fixture()
    before = manager.checkpoint().content_sha256
    row = diagnostic.inspect_entity(graph, opportunities, manager, 1, STAMP)
    assert row["decision_value"] > 0.02
    assert row["distinct_branch_routes"] == 2
    branches = {b["outcome"]: b for b in row["branches"]}
    assert branches["clean"]["report_ids"] == ["b"]
    assert branches["altered"]["report_ids"] == ["a"]
    assert sum(b["probability"] for b in row["branches"]) == pytest.approx(1)
    assert sum(b["probability"] * b["posterior_mean"] for b in row["branches"]) == pytest.approx(row["posterior_mean"])
    assert manager.checkpoint().content_sha256 == before


def test_information_without_a_later_choice_has_no_decision_value():
    graph, opportunities, manager = fixture()
    graph = replace(graph, candidates=graph.candidates[:1], arcs=((0, 1), (1, 2)), sink=2)
    row = diagnostic.inspect_entity(graph, opportunities, manager, 1, STAMP)
    assert row["variance_reduction"] > 0
    assert abs(row["decision_value"]) <= row["rounding_tolerance"]
    assert row["distinct_branch_routes"] == 1


def test_reconstructs_shared_omission_posterior_from_public_counts():
    manager = ReportingReliabilityManager("test", (1, 2))
    for index, (entity, outcome) in enumerate(((1, "omitted"), (2, "clean"), (1, "altered"))):
        manager._update_one({"check_id": str(index), "entity_id": entity, "outcome": outcome})
    belief = diagnostic.snapshot(manager, STAMP)
    rebuilt = diagnostic.snapshot(diagnostic.reconstruct(belief), STAMP)
    assert rebuilt.omission.mean == pytest.approx(belief.omission.mean)


def test_published_check_conditions_on_presence_and_cannot_be_omitted():
    _, _, manager = fixture()
    prior, branches = diagnostic.outcome_branches(manager, 1, STAMP, published=True)
    probabilities = {outcome: probability for outcome, probability, _ in branches}
    ship = prior.ships[0]
    assert set(probabilities) == {"clean", "altered"}
    assert sum(probabilities.values()) == pytest.approx(1)
    assert probabilities["altered"] == pytest.approx(
        (ship.mean - ship.expected_omission_probability) / (1 - ship.expected_omission_probability))


def test_acquisition_suffix_reserves_turns_and_excludes_spent_reports():
    graph, _, _ = fixture()
    first = replace(graph.candidates[0], arrival_direction=0)
    later = replace(graph.candidates[1], start_s=11, end_s=11.5, x=first.x, y=first.y,
                    end_x=first.x, end_y=first.y, arrival_direction=1)
    duplicate = replace(later, candidate_id="repeat", report_ids=first.report_ids)
    too_early = replace(later, candidate_id="early", start_s=10.5, end_s=11)
    candidates = (first, too_early, duplicate, later)
    graph = planning.CandidateDAG(candidates, (), 0, 5)
    vehicle = {"max_velocity": 30, "quarter_turn_seconds": 0.5}
    suffix = diagnostic.acquisition_suffix(graph, first, vehicle, True)
    assert suffix.candidates == (later,)
    # Even staying at the same location requires the quarter-turn budget.
    assert diagnostic.acquisition_suffix(graph, first, {**vehicle, "quarter_turn_seconds": 1}, True).candidates == ()


def test_late_acquisition_cannot_collect_past_decision_value():
    graph, opportunities, manager = fixture()
    rows = diagnostic.inspect_acquisitions(graph, opportunities, manager, STAMP,
                                           {"max_velocity": 30}, True)
    assert len(rows) == 2
    assert all(row["suffix_candidates"] == 0 for row in rows)
    assert all(row["decision_value"] == 0 for row in rows)
    assert all(row["adaptive_total"] == row["immediate_recall_utility"] for row in rows)
