"""Offline evaluator semantics; no hidden truth may influence a solver route."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "ideal_plan_evaluation",
    Path(__file__).parents[1] / "scripts/evaluate_mission1_plan.py",
)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def assignment(start=1, duration=1, mode="fixed_view", entity=None, report="r1"):
    return {
        "start": start,
        "duration": duration,
        "time_scale": 2,
        "surveillance_mode": mode,
        "entity_id": entity,
        "parameters": {"x": 0, "y": 0, "report_ids": [report]},
    }


def test_ideal_visibility_uses_radius_or_perfect_pursuit_without_truth_labels():
    positions = {1: (0, 0), 2: (200, 0), 3: (301, 0)}
    assert evaluation.ideal_visible_ids(assignment(), positions, 100) == [1]
    assert evaluation.ideal_visible_ids(assignment(), positions, 300) == [1, 2]
    assert evaluation.ideal_visible_ids(
        assignment(mode="pursue_ship", entity=2), positions, 110
    ) == [2, 3]


def converter_with_two_corrupted_events():
    # Exercise the actual runtime discrepancy window, pairing and deduplication.
    model = pytest.importorskip("onr_physical_runtime.world_model.model")
    converter = model.HeightmapMultigridConverter.__new__(
        model.HeightmapMultigridConverter
    )
    converter.event_time_interval_s = 4.0
    actual = {
        "entity_id": 1,
        "time": 0.5,
        "event type": "intersection decision",
        "event information": {"decision": "left"},
    }
    converter.ground_truth_events = {1: [actual, {**actual, "time": 1.0}]}
    converter.event_reports = {
        1: [{**actual, "report_id": "r1", "event information": {"decision": "right"}}]
    }
    converter._event_report_pairings = {1: {0: 0}}
    converter._unpaired_report_indices = {1: set()}
    converter.trajectories = {
        1: SimpleNamespace(
            get_traj_point_at_time=lambda now: SimpleNamespace(ned_north=0, ned_east=0)
        )
    }
    converter.detected_discrepancies = {}
    converter.event_report_checks = []
    converter._detected_discrepancy_keys = set()
    converter._checked_event_keys = set()
    return converter


def test_ideal_evaluation_uses_half_open_dwell_and_counts_omissions_once():
    converter = converter_with_two_corrupted_events()
    first = evaluation.score_ideal_plan(
        {"assignments": [assignment()]}, converter, 100, 0.5
    )
    assert first["total_corrupted"] == 2
    assert first["issues_discovered"] == 1  # End=1.0 is excluded.
    extended = evaluation.score_ideal_plan(
        {"assignments": [assignment(duration=3)]}, converter, 100, 0.5
    )
    assert extended["issues_discovered"] == 2
    assert extended["check_outcomes"] == {"altered": 1, "omitted": 1}
    assert extended["recall"] == 1.0


def test_ideal_evaluation_rejects_duplicate_report_assignments():
    with pytest.raises(AssertionError):
        evaluation.score_ideal_plan(
            {"assignments": [assignment(), assignment(start=2)]},
            converter_with_two_corrupted_events(),
            100,
            0.5,
        )
