from __future__ import annotations

import json
import runpy
import subprocess
import sys
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
    pursue_confirm = transitions["assignment-2-outcome-confirmed"]
    assert (
        pursue_confirm["context"]["readiness"]["live_evidence"]
        == "the pursued vessel is held in the FoV"
    )
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


def test_statechart_path_helpers_decode_minizinc_jsonl_and_inspect_output(
    tmp_path: Path,
) -> None:
    example = Path(
        "conf/skills/hyper/creating-statechart-files/examples/"
        "event-information-patrol"
    )
    assignments = [
        {
            "candidate_id": "fixed-a",
            "surveillance_mode": "fixed_view",
            "entity_id": None,
            "start": 20,
            "duration": 2,
            "time_scale": 2,
            "parameters": {
                "x": 10,
                "y": 20,
                "report_ids": ["report-a"],
                "utility": {"combined": 4},
            },
        },
        {
            "candidate_id": "pursue-b",
            "surveillance_mode": "pursue_ship",
            "entity_id": 7,
            "start": 30,
            "duration": 4,
            "time_scale": 2,
            "parameters": {
                "x": 30,
                "y": 40,
                "report_ids": ["report-b"],
                "utility": {"combined": 8},
            },
        },
    ]
    planner = tmp_path / "arbitrary planner output.plan"
    planner.write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "type": "solution",
                        "output": {
                            "default": json.dumps({"assignments": assignments})
                        },
                    }
                ),
                json.dumps({"type": "status", "status": "OPTIMAL_SOLUTION"}),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    generator = tmp_path / "arbitrary revision/generate_statechart.py"
    statechart = tmp_path / "arbitrary revision/statechart.json"

    prepared = subprocess.run(
        [
            sys.executable,
            str(example / "prepare_statechart.py"),
            planner,
            generator,
            statechart,
        ],
        capture_output=True,
        check=True,
        text=True,
    )

    assert json.loads(prepared.stdout) == {
        "edges": 5,
        "planner_items": 2,
        "planner_order_preserved": True,
        "represented_once": ["fixed-a", "pursue-b"],
        "states": 6,
        "terminal_completion": "patrol-objective-complete",
    }
    assert generator.read_bytes() == (example / "generate_statechart.py").read_bytes()

    inspected = subprocess.run(
        [sys.executable, str(example / "inspect_statechart.py"), planner, statechart],
        capture_output=True,
        check=True,
        text=True,
    )
    assert json.loads(inspected.stdout) == {
        "context_covers_states": True,
        "planner_items": 2,
        "planner_order_preserved": True,
        "represented_once": True,
        "state_count": 6,
        "terminal_count": 1,
        "transition_count": 5,
        "unique_events": True,
        "unique_state_pairs": True,
        "valid": True,
    }
