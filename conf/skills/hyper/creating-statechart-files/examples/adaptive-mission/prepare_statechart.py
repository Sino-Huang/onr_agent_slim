#!/usr/bin/env python
"""Create a Mission 3 or 4 Statechart from verified adaptive-plan artifacts."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def planner_selection(path: Path) -> int:
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        output = value.get("output", {}) if isinstance(value, dict) else {}
        raw = output.get("default") if isinstance(output, dict) else None
        if (
            isinstance(value, dict)
            and value.get("type") == "solution"
            and isinstance(raw, str)
        ):
            selected = json.loads(raw).get("selected_index")
            if isinstance(selected, int) and selected in {0, 1}:
                return selected
    raise ValueError("planner artifact has no adaptive Mission solution")


def create_statechart(mode: str, plan_path: Path, manifest_path: Path) -> dict:
    selected = planner_selection(plan_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    decision = manifest.get("decision")
    if selected != (0 if decision is None else 1):
        raise ValueError("planner selection does not match adaptive decision manifest")
    if mode not in {"mission3", "mission4"}:
        raise ValueError("adaptive Statechart mode must be mission3 or mission4")
    terminal = "inspection-complete" if mode == "mission3" else "search-complete"
    if decision is not None and decision.get("action") == "report":
        return {
            "entry_state": terminal,
            "terminal_states": [terminal],
            "states": [terminal],
            "state_context": {terminal: {"decision": decision}},
            "transitions": [],
        }
    action_state = "inspection-action" if mode == "mission3" else "search-action"
    monitoring = "inspection-monitoring" if mode == "mission3" else "search-monitoring"
    terminal_readiness = (
        {
            "mission_time_at_or_after": {
                "seconds": float(manifest["mission_end_time_s"])
            }
        }
        if mode == "mission3"
        else {"mission4_terminal_outcome": ["all_found", "deadline", "failed"]}
    )
    return {
        "entry_state": action_state,
        "terminal_states": [terminal],
        "states": [action_state, monitoring, terminal],
        "state_context": {
            action_state: {
                "planner_item": decision,
                "desired_outcome": decision,
            },
            monitoring: {
                "desired_outcome": "retain public evidence and await an adaptive replan or terminal result"
            },
            terminal: {
                "desired_outcome": "publish the evidence-backed final Mission result"
            },
        },
        "transitions": [
            {
                "event": f"{action_state}-finished",
                "source": action_state,
                "target": monitoring,
                "context": {
                    "readiness": {"matching_maneuver_lifecycle_terminal": True},
                    "desired_outcome": "the selected physical action has terminal feedback",
                },
            },
            {
                "event": f"{mode}-terminal-evidence",
                "source": monitoring,
                "target": terminal,
                "context": {
                    "readiness": terminal_readiness,
                    "desired_outcome": "the public Mission evidence supports termination",
                },
            },
        ],
    }


def main() -> int:
    if len(sys.argv) != 5:
        raise SystemExit(
            "usage: prepare_statechart.py MISSION_MODE PLAN MANIFEST STATECHART"
        )
    chart = create_statechart(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]))
    output = Path(sys.argv[4])
    output.write_text(json.dumps(chart, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "states": len(chart["states"]),
                "transitions": len(chart["transitions"]),
                "terminal": chart["entry_state"] in chart["terminal_states"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
