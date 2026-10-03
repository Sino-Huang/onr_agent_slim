"""Operator-view ``context`` section over a trimmed ``run.a6CqxX`` transport.

The fixture keeps the two newest events of each topic published no later
than Mission Snapshot v397 (plan revision 4, Mission 1 assignment 1 en route).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from onr.runtime_host import context_view


def context_section(transport_root: Path, mission_id: str) -> Any:
    return context_view.context_section(transport_root, mission_id)


FIXTURE = Path(__file__).parent / "fixtures" / "run_a6cqxx_belief_context" / "transport"
MISSION = "mission:demo"


def _stream(transport: Path, topic: str) -> Path:
    return transport / "topics" / topic / "missions" / "mission%3Ademo"


def _publish(
    transport: Path, topic: str, sequence: int, event_kind: str, payload: dict
) -> None:
    event = {
        "event_id": f"{event_kind}:{MISSION}:{sequence}",
        "event_kind": event_kind,
        "mission_id": MISSION,
        "payload": payload,
        "schema_version": 1,
        "sequence": sequence,
    }
    name = f"{sequence:020d}-{event_kind}%3Amission%3Ademo%3A{sequence}.json"
    (_stream(transport, topic) / name).write_text(json.dumps(event), encoding="utf-8")


@pytest.fixture
def transport(tmp_path: Path) -> Path:
    root = tmp_path / "transport"
    shutil.copytree(FIXTURE, root)
    return root


def test_latest_mission_snapshot_reports_source_health() -> None:
    snapshot = context_section(FIXTURE, MISSION)["mission_snapshot"]

    assert snapshot["version"] == 397
    assert snapshot["plan_revision"] == 4
    assert snapshot["sequence"] == 396
    sources = {
        "active_maneuver",
        "bayesian_belief_snapshot",
        "environment_data",
        "fsm_status",
        "plan",
    }
    assert snapshot["source_health"] == dict.fromkeys(sources, "healthy")
    assert snapshot["source_freshness"] == dict.fromkeys(sources, True)
    assert snapshot["source_revisions"]["bayesian_belief_snapshot"] == 5
    assert snapshot["source_revisions"]["fsm_status"] == 54
    assert snapshot["missing_sources"] == []


def test_fsm_status_lists_enabled_transition_candidates() -> None:
    fsm = context_section(FIXTURE, MISSION)["fsm_status"]

    assert fsm["active_state"] == "assignment-1-in-progress"
    assert fsm["plan_revision"] == 4
    assert fsm["sequence"] == 54
    [candidate] = fsm["transition_candidates"]
    assert candidate["event"] == "assignment-1-outcome-confirmed"
    assert candidate["target"] == "assignment-2-in-progress"
    assert candidate["condition"] == "confirm the completed evidence interval"
    assert candidate["readiness"]["not_before"]["seconds"] == 200.5


def test_active_maneuver_reports_progress_and_deadline() -> None:
    maneuver = context_section(FIXTURE, MISSION)["active_maneuver"]

    assert maneuver["action"] == "navigate"
    assert maneuver["status"] == "active"
    assert maneuver["phase"] == "navigate"
    assert (
        maneuver["progress"] is None
    )  # navigate reports distance, not a completion fraction
    assert maneuver["progress_detail"]["remaining_distance_m"] == pytest.approx(
        33.42, abs=0.01
    )
    assert maneuver["deadline_seconds"] == 198.5
    assert maneuver["remaining_seconds"] == pytest.approx(8.0, abs=1e-3)


def test_latest_intent_and_hyper_outcome() -> None:
    context = context_section(FIXTURE, MISSION)

    intent = context["latest_transition_intent"]
    assert intent["status"] == "selected"
    assert intent["source_state"] == "assignment-1-in-progress"
    assert intent["target"] == "assignment-2-in-progress"
    assert intent["sequence"] == 16
    hyper = context["latest_hyper_outcome"]
    assert hyper["disposition"] == "replan"
    assert hyper["trigger_identities"] == [
        "mission1-gate:next_assignment_infeasible:belief-157"
    ]
    assert hyper["sequence"] == 2


def test_newer_event_replaces_cached_latest(transport: Path) -> None:
    first = context_section(transport, MISSION)["fsm_status"]
    assert first["sequence"] == 54

    _publish(
        transport,
        "fsm-status",
        55,
        "fsm-status",
        {
            "active_state": "assignment-2-in-progress",
            "status": "transitioned",
            "plan_revision": 4,
            "statechart_revision": 4,
            "last_applied_event": "assignment-1-outcome-confirmed",
            "transition_candidates": [],
        },
    )
    latest = context_section(transport, MISSION)["fsm_status"]
    assert latest["sequence"] == 55
    assert latest["active_state"] == "assignment-2-in-progress"
    assert latest["transition_candidates"] == []


def test_unreadable_newest_event_falls_back_to_previous(transport: Path) -> None:
    (_stream(transport, "transition-intents") / f"{99:020d}-torn.json").write_text(
        "{", encoding="utf-8"
    )

    assert (
        context_section(transport, MISSION)["latest_transition_intent"]["sequence"]
        == 16
    )


def test_search_area_progress_fraction(transport: Path) -> None:
    _publish(
        transport,
        "environment-data",
        999,
        "environment_data",
        {
            "mission_time_seconds": 50.0,
            "maneuver_lifecycle": {
                "action": "search_area",
                "lifecycle": "active",
                "phase": "sweep",
                "parameters": {"deadline_time": 80.0},
                "progress": {"cleared_cells": 31, "total_cells": 50},
            },
        },
    )
    maneuver = context_section(transport, MISSION)["active_maneuver"]

    assert maneuver["progress"] == pytest.approx(0.62)
    assert maneuver["remaining_seconds"] == 30.0


def test_run_without_transport_has_empty_context(tmp_path: Path) -> None:
    assert context_section(tmp_path / "transport", MISSION) == {
        "mission_snapshot": None,
        "fsm_status": None,
        "active_maneuver": None,
        "latest_transition_intent": None,
        "latest_hyper_outcome": None,
    }


def test_contract_example_matches_section_shape() -> None:
    contract = (
        Path(__file__).parents[1]
        / "docs"
        / "design"
        / "operator-console"
        / "contract"
        / "v1.2"
    )
    example = json.loads(
        (contract / "mission-run-operator-context.response.json").read_text()
    )["context"]
    context = context_section(FIXTURE, MISSION)

    assert set(context) == set(example)
    for key, value in context.items():
        assert set(value) == set(example[key]), key
    assert set(context["fsm_status"]["transition_candidates"][0]) == set(
        example["fsm_status"]["transition_candidates"][0]
    )
