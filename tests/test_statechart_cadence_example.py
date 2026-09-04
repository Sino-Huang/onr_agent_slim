from __future__ import annotations

import runpy
from pathlib import Path

from onr.contracts.fsm import Statechart


def test_statechart_example_preserves_evidence_intervals_and_departures() -> None:
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
                    "candidate_id": "first",
                    "surveillance_mode": "fixed_view",
                    "entity_id": None,
                    "start": 20,
                    "duration": 4,
                    "parameters": {
                        "x": 10,
                        "y": 20,
                        "report_ids": ["report-3", "report-4"],
                        "utility": {"recall": 3, "estimation": 4, "combined": 7},
                        "time_scale": 2,
                    },
                },
                {
                    "candidate_id": "second",
                    "surveillance_mode": "pursue_ship",
                    "entity_id": 7,
                    "start": 60,
                    "duration": 2,
                    "parameters": {
                        "x": 30,
                        "y": 40,
                        "report_ids": ["report-7"],
                        "utility": {"recall": 5, "estimation": 2, "combined": 7},
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
        "report_ids": ["report-3", "report-4"],
        "report_check_ledger": "world_model_info.event_report_checks",
    }
    moving = draft["state_context"]["assignment-1-in-progress"]
    assert "navigation_adapter_parameters" not in moving
    assert moving["desired_outcome"] == {
        "kind": "arrive_and_observe_fixed_view",
        "location": {"x": 10, "y": 20},
        "arrival_deadline": {
            "tick": 20,
            "ticks_per_second": 2,
            "seconds": 10.0,
        },
        "visibility_outcome": "target reports are checked from the fixed FoV",
    }
    pursuing = draft["state_context"]["assignment-2-in-progress"]
    assert pursuing["surveillance_mode"] == "pursue_ship"
    assert pursuing["target_entity_id"] == 7
    assert pursuing["desired_outcome"] == {
        "kind": "maintain_moving_entity_visibility",
        "entity_id": 7,
        "evidence_window": pursuing["observation_window"],
        "physical_action": "pursue",
    }
    assert len(draft["states"]) == 6
    assert len(draft["transitions"]) == 5
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
