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
