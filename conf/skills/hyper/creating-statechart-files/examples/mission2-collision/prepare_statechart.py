#!/usr/bin/env python
"""Create a Mission 2 Statechart from verified planner and candidate artifacts."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def planner_selection(path: Path) -> dict[str, object]:
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict) or value.get("type") != "solution":
            continue
        output = value.get("output", {})
        if isinstance(output, dict):
            raw = output.get("default")
            if isinstance(raw, str):
                decoded = json.loads(raw)
                if isinstance(decoded, dict):
                    return decoded
    raise ValueError("planner artifact has no Mission 2 solution")


def create_statechart(plan_path: Path, candidates_path: Path) -> dict[str, object]:
    selected = planner_selection(plan_path)
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    rows = candidates["candidates"]
    indexes = selected.get("selected_indices", [])
    if not isinstance(indexes, list) or any(
        not isinstance(index, int) or not 1 <= index <= len(rows) for index in indexes
    ):
        raise ValueError("planner selected an invalid Mission 2 candidate index")
    chosen = [rows[index - 1] for index in indexes]
    mission_end = float(candidates["mission_end_time_s"])
    states = [f"risk-observation-{index + 1}" for index in range(len(chosen))]
    states.extend(("collision-monitoring-active", "scenario-recording-end"))
    contexts: dict[str, object] = {}
    transitions = []
    for index, candidate in enumerate(chosen):
        state = states[index]
        contexts[state] = {
            "candidate_id": candidate["candidate_id"],
            "surveillance_mode": candidate["mode"],
            "target_entity_id": candidate["target_entity_id"],
            "risk_pair": candidate["ship_ids"],
            "prediction_run_id": candidate["prediction_run_id"],
            "prediction_sequence": candidate["prediction_sequence"],
            "observation_window": {
                "start_s": candidate["start_s"],
                "end_s": candidate["end_s"],
            },
            "desired_outcome": {
                "location": {
                    "x": candidate["x"],
                    "y": candidate["y"],
                    "z": candidate["z"],
                },
                "arrival_deadline": {"seconds": candidate["end_s"]},
                "entity_id": candidate["target_entity_id"],
            },
            "planner_item": candidate,
        }
        target = (
            states[index + 1]
            if index + 1 < len(chosen)
            else "collision-monitoring-active"
        )
        transitions.append(
            {
                "event": f"risk-observation-{index + 1}-complete",
                "source": state,
                "target": target,
                "context": {
                    "readiness": {
                        "mission_time_at_or_after": {"seconds": candidate["end_s"]}
                    },
                    "desired_outcome": "observation window completed with current public evidence",
                },
            }
        )
    contexts["collision-monitoring-active"] = {
        "desired_outcome": "continue collision monitoring and replan when prediction evidence changes",
        "next_evidence_time_s": selected["monitor_until_s"],
        "mission_end_time_s": mission_end,
    }
    contexts["scenario-recording-end"] = {
        "desired_outcome": "report collision-monitoring results after the recording ends"
    }
    transitions.append(
        {
            "event": "scenario-recording-ended",
            "source": "collision-monitoring-active",
            "target": "scenario-recording-end",
            "context": {
                "readiness": {"mission_time_at_or_after": {"seconds": mission_end}},
                "desired_outcome": "the authoritative scenario recording has ended",
            },
        }
    )
    return {
        "entry_state": states[0],
        "terminal_states": ["scenario-recording-end"],
        "states": states,
        "state_context": contexts,
        "transitions": transitions,
    }


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("usage: prepare_statechart.py PLAN CANDIDATES STATECHART")
    chart = create_statechart(Path(sys.argv[1]), Path(sys.argv[2]))
    output = Path(sys.argv[3])
    output.write_text(json.dumps(chart, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"states": len(chart["states"]), "transitions": len(chart["transitions"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
