from __future__ import annotations

import runpy
from pathlib import Path

from onr.contracts.fsm import Statechart


def test_statechart_example_preserves_windows_and_immediate_lifecycle_departures() -> (
    None
):
    namespace = runpy.run_path(
        str(
            Path(
                "conf/skills/hyper/creating-statechart-files/examples/"
                "event-information-patrol/generate_statechart.py"
            )
        )
    )
    values = [
        {
            "assignments": [
                {
                    "maneuver_id": "first",
                    "start": 20,
                    "duration": 4,
                    "parameters": {
                        "x": 10,
                        "y": 20,
                        "source_event_index": 3,
                        "captured_event_count": 2,
                        "time_scale": 2,
                    },
                },
                {
                    "maneuver_id": "second",
                    "start": 60,
                    "duration": 2,
                    "parameters": {
                        "x": 30,
                        "y": 40,
                        "source_event_index": 7,
                        "captured_event_count": 1,
                        "time_scale": 2,
                    },
                },
            ]
        }
    ]
    items = namespace["extract_assignments"](values)
    draft, manifest = namespace["build_statechart"](items)

    transitions = {item["event"]: item for item in draft["transitions"]}
    first_departure = transitions["assignment-1-may-begin"]
    second_departure = transitions["assignment-2-may-begin"]
    assert (
        first_departure["context"]["readiness"]["mission_time_at_or_after"]["seconds"]
        == 0
    )
    assert (
        second_departure["context"]["readiness"]["mission_time_at_or_after"]["seconds"]
        == 12
    )

    first_confirm = transitions["assignment-1-outcome-confirmed"]
    assert first_confirm["context"]["readiness"]["not_before"]["seconds"] == 12
    assert first_confirm["context"]["readiness"]["sensed_evidence"] == {
        "source_event_index": 3,
        "expected_observation_count": 2,
    }
    moving = draft["state_context"]["assignment-1-in-progress"]
    assert moving["navigation_adapter_parameters"] == {
        "x": 10,
        "y": 20,
        "deadline_time": 10.0,
        "observation_start": 10.0,
        "observation_duration": 2.0,
        "source_event_index": 3,
        "expected_observation_count": 2,
    }
    assert manifest["represented_once"] == ["first", "second"]

    chart = Statechart.from_dict(
        {
            "schema_version": 2,
            "mission_id": "mission-1",
            "plan_revision": 1,
            "mission_snapshot_id": "mission-1:snapshot:1",
            "planning_profile": "temporal",
            **draft,
        }
    )
    assert chart.terminal_states == ("patrol-objective-complete",)
