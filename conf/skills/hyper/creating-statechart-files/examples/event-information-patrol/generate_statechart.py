"""Few-shot planner-artifact to schema-v2 patrol Statechart generator."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def decode_planner_output(path: Path) -> list[object]:
    """Decode a JSON document or the JSON values embedded in JSONL/stdout."""

    text = path.read_text(encoding="utf-8")
    try:
        return [json.loads(text)]
    except json.JSONDecodeError:
        values = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if not values:
            raise ValueError("planner artifact contains no JSON value")
        return values


def candidate_arrays(value: object) -> list[list[Mapping[str, Any]]]:
    found: list[list[Mapping[str, Any]]] = []
    if isinstance(value, Mapping):
        for child in value.values():
            found.extend(candidate_arrays(child))
    elif isinstance(value, list):
        if value and all(isinstance(item, Mapping) for item in value):
            found.append(value)
        for child in value:
            found.extend(candidate_arrays(child))
    return found


def first(item: Mapping[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        value: Any = item
        for key in path:
            if not isinstance(value, Mapping) or key not in value:
                break
            value = value[key]
        else:
            return value
    raise KeyError(" or ".join(".".join(path) for path in paths))


def extract_assignments(values: list[object]) -> list[dict[str, Any]]:
    """Isolate aliases observed in planner outputs from topology semantics."""

    arrays = [array for value in values for array in candidate_arrays(value)]
    for array in arrays:
        extracted: list[dict[str, Any]] = []
        try:
            for order, raw in enumerate(array, start=1):
                identifier = first(
                    raw,
                    ("maneuver_id",),
                    ("assignment_id",),
                    ("id",),
                    ("identity", "id"),
                )
                start_tick = first(raw, ("start",), ("timing", "start_tick"))
                duration_tick = first(raw, ("duration",), ("timing", "duration_tick"))
                time_scale = first(
                    raw,
                    ("time_scale",),
                    ("timing", "ticks_per_second"),
                    ("parameters", "time_scale"),
                )
                x = first(raw, ("x",), ("target", "x"), ("parameters", "x"))
                y = first(raw, ("y",), ("target", "y"), ("parameters", "y"))
                source_event_index = first(
                    raw, ("source_event_index",), ("parameters", "source_event_index")
                )
                expected_count = first(
                    raw,
                    ("captured_event_count",),
                    ("parameters", "captured_event_count"),
                )
                dependencies = raw.get("dependencies", raw.get("depends_on", []))
                extracted.append(
                    {
                        "order": order,
                        "identifier": str(identifier),
                        "start_tick": int(start_tick),
                        "duration_tick": int(duration_tick),
                        "time_scale": int(time_scale),
                        "x": x,
                        "y": y,
                        "source_event_index": int(source_event_index),
                        "expected_observation_count": int(expected_count),
                        "dependencies": dependencies,
                        "planner_item": dict(raw),
                    }
                )
        except (KeyError, TypeError, ValueError):
            continue
        if extracted:
            return extracted
    raise ValueError("no assignment array matches this generator's extraction aliases")


def scaled_time(tick: int, scale: int) -> dict[str, object]:
    """Preserve DZN ticks and make their seconds interpretation explicit."""

    if tick < 0 or scale <= 0:
        raise ValueError("scaled time requires non-negative ticks and positive scale")
    return {"tick": tick, "ticks_per_second": scale, "seconds": tick / scale}


def build_statechart(
    items: list[dict[str, Any]],
) -> tuple[dict[str, object], dict[str, object]]:
    states = ["patrol-awaiting-first-assignment"]
    state_context: dict[str, dict[str, object]] = {
        states[0]: {"desired_outcome": "patrol is ready to begin"}
    }
    transitions: list[dict[str, object]] = []
    represented: list[str] = []
    source = states[0]
    previous_end_tick: int | None = None

    for item in items:
        identifier = item["identifier"]
        moving = f"assignment-{item['order']}-in-progress"
        observing = f"assignment-{item['order']}-observation-window-active"
        achieved = f"assignment-{item['order']}-outcome-achieved"
        states.extend((moving, observing, achieved))
        shared = {
            "planner_identity": identifier,
            "planner_order": item["order"],
            "dependencies": item["dependencies"],
            "planner_item": item["planner_item"],
        }
        state_context[moving] = {
            **shared,
            "desired_outcome": {
                "kind": "arrive_at_planner_selected_location",
                "location": {"x": item["x"], "y": item["y"]},
            },
            "navigation_adapter_parameters": {
                "x": item["x"],
                "y": item["y"],
                "deadline_time": item["start_tick"] / item["time_scale"],
                "observation_start": item["start_tick"] / item["time_scale"],
                "observation_duration": item["duration_tick"] / item["time_scale"],
                "source_event_index": item["source_event_index"],
                "expected_observation_count": item["expected_observation_count"],
            },
        }
        state_context[observing] = {
            **shared,
            "desired_outcome": {
                "kind": "planner_selected_patrol_observation_complete",
                "location": {"x": item["x"], "y": item["y"]},
            },
            "observation_window": {
                "start": scaled_time(item["start_tick"], item["time_scale"]),
                "duration": scaled_time(item["duration_tick"], item["time_scale"]),
                "source_event_index": item["source_event_index"],
                "expected_observation_count": item["expected_observation_count"],
            },
        }
        state_context[achieved] = {
            **shared,
            "desired_outcome": "the planner-selected observation window is complete",
        }
        if item["order"] == 1:
            state_context[achieved]["hyper_evaluation"] = {
                "kind": "replan",
                "reason": (
                    "Evaluate whether the first completed live observation window "
                    "materially changes the active plan."
                ),
                "send_once": True,
            }
        departure_tick = 0 if previous_end_tick is None else previous_end_tick
        observation_end_tick = item["start_tick"] + item["duration_tick"]
        transitions.extend(
            (
                {
                    "event": f"assignment-{item['order']}-may-begin",
                    "source": source,
                    "target": moving,
                    "context": {
                        "desired_outcome": "begin the next planner-selected assignment",
                        "readiness": {
                            "mission_time_at_or_after": scaled_time(
                                departure_tick, item["time_scale"]
                            ),
                        },
                    },
                },
                {
                    "event": f"assignment-{item['order']}-observation-may-begin",
                    "source": moving,
                    "target": observing,
                    "context": {
                        "desired_outcome": "confirm the selected observation outcome",
                        "readiness": {
                            "live_evidence": "the drone is at the selected location",
                            "not_before": scaled_time(
                                item["start_tick"], item["time_scale"]
                            ),
                        },
                    },
                },
                {
                    "event": f"assignment-{item['order']}-outcome-confirmed",
                    "source": observing,
                    "target": achieved,
                    "context": {
                        "desired_outcome": "confirm the completed observation window",
                        "readiness": {
                            "not_before": scaled_time(
                                observation_end_tick, item["time_scale"]
                            ),
                            "sensed_evidence": {
                                "source_event_index": item["source_event_index"],
                                "expected_observation_count": item[
                                    "expected_observation_count"
                                ],
                            },
                        },
                    },
                },
            )
        )
        represented.append(identifier)
        source = achieved
        previous_end_tick = observation_end_tick

    terminal = "patrol-objective-complete"
    states.append(terminal)
    state_context[terminal] = {
        "desired_outcome": "every planner-selected patrol assignment is complete"
    }
    final_tick = items[-1]["start_tick"] + items[-1]["duration_tick"]
    transitions.append(
        {
            "event": "patrol-objective-may-complete",
            "source": source,
            "target": terminal,
            "context": {
                "desired_outcome": "finish after the final planned dwell interval",
                "readiness": {
                    "mission_time_at_or_after": scaled_time(
                        final_tick, items[-1]["time_scale"]
                    )
                },
            },
        }
    )
    expected = [item["identifier"] for item in items]
    assert represented == expected and len(set(represented)) == len(expected)
    chart = {
        "entry_state": states[0],
        "terminal_states": [terminal],
        "states": states,
        "state_context": state_context,
        "transitions": transitions,
    }
    manifest = {
        "planner_items": len(items),
        "represented_once": represented,
        "planner_order_preserved": represented == expected,
        "states": len(states),
        "edges": len(transitions),
        "terminal_completion": terminal,
    }
    return chart, manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("planner_artifact", type=Path)
    parser.add_argument("--output", type=Path, default=Path("statechart.json"))
    args = parser.parse_args()
    items = extract_assignments(decode_planner_output(args.planner_artifact))
    chart, manifest = build_statechart(items)
    args.output.write_text(
        json.dumps(chart, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, separators=(",", ":"), ensure_ascii=False))


if __name__ == "__main__":
    main()
