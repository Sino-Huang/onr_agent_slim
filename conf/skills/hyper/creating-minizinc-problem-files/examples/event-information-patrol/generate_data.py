"""Build the event-information action DAG and write MiniZinc data.

This is a few-shot script for the Hyper Agent to copy into its run workspace.
Adapt only the schema extraction section after inspecting the authorized JSON
with jq. The graph and serialization section is intentionally schema-neutral.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Schema extraction: adapt these functions and current belief values.
# The values below are the belief marginals supplied for the packaged demo.
BELIEF_MARGINALS = [
    {"entity_id": "1", "risk_type": "event-risk", "probability_risk": 0.43896484375},
    {"entity_id": "10", "risk_type": "event-risk", "probability_risk": 0.67919921875},
    {"entity_id": "11", "risk_type": "event-risk", "probability_risk": 0.4091796875},
    {"entity_id": "12", "risk_type": "event-risk", "probability_risk": 0.69384765625},
    {"entity_id": "13", "risk_type": "event-risk", "probability_risk": 0.3701171875},
    {"entity_id": "14", "risk_type": "event-risk", "probability_risk": 0.669921875},
    {"entity_id": "15", "risk_type": "event-risk", "probability_risk": 0.35107421875},
    {"entity_id": "16", "risk_type": "event-risk", "probability_risk": 0.62060546875},
    {"entity_id": "17", "risk_type": "event-risk", "probability_risk": 0.29931640625},
    {"entity_id": "18", "risk_type": "event-risk", "probability_risk": 0.63232421875},
    {"entity_id": "19", "risk_type": "event-risk", "probability_risk": 0.32568359375},
    {"entity_id": "2", "risk_type": "event-risk", "probability_risk": 0.7529296875},
    {"entity_id": "20", "risk_type": "event-risk", "probability_risk": 0.59423828125},
    {"entity_id": "3", "risk_type": "event-risk", "probability_risk": 0.43505859375},
    {"entity_id": "4", "risk_type": "event-risk", "probability_risk": 0.73095703125},
    {"entity_id": "5", "risk_type": "event-risk", "probability_risk": 0.4677734375},
    {"entity_id": "6", "risk_type": "event-risk", "probability_risk": 0.78955078125},
    {"entity_id": "7", "risk_type": "event-risk", "probability_risk": 0.3671875},
    {"entity_id": "8", "risk_type": "event-risk", "probability_risk": 0.6962890625},
    {"entity_id": "9", "risk_type": "event-risk", "probability_risk": 0.3837890625},
]


def extract_events(document: dict[str, Any]) -> list[dict[str, Any]]:
    return document["static_info"]


def extract_event(record: dict[str, Any]) -> tuple[str, str, float, float, float]:
    return (
        str(record["entity_id"]),
        record["event type"],
        float(record["time"]),
        float(record["position"][0]),
        float(record["position"][1]),
    )


def extract_drone(document: dict[str, Any]) -> tuple[float, float, float, float, float]:
    scene = document["scene_graph"]
    drone = next(entity for entity in scene["entities"] if entity["type"] == "drone")
    return (
        float(scene["mission_time_seconds"]),
        float(drone["location"]["x"]),
        float(drone["location"]["y"]),
        float(drone["max_velocity"]),
        float(drone["fov_radius"]),
    )


def extract_risk_by_entity(marginals: list[dict[str, Any]]) -> dict[str, float]:
    return {
        str(marginal["entity_id"]): float(marginal["probability_risk"])
        for marginal in marginals
        if marginal["risk_type"] == "event-risk"
    }


# Stable graph and DZN generation: keep this section unchanged when adapting a schema.
TIME_SCALE = 2
RISK_SCALE = 1000
INTERSECTION_EVENT_TYPE = "intersection decision"


@dataclass(frozen=True)
class Event:
    source_index: int
    entity_id: str
    event_type: str
    time: int
    x: int
    y: int
    gain: int


@dataclass(frozen=True)
class Action:
    x: int
    y: int
    start: int
    end: int
    captured: tuple[int, ...]
    anchor_event: int
    gain: int


def minizinc_round(value: float) -> int:
    """Match MiniZinc round: nearest integer, ties away from zero."""

    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def travel_ticks(ax: int, ay: int, bx: int, by: int, max_velocity: float) -> int:
    distance = math.hypot(bx - ax, by - ay)
    return math.ceil(distance * TIME_SCALE / max_velocity)


def event_rows(
    document: dict[str, Any], risk_by_entity: dict[str, float]
) -> list[Event]:
    rows: list[Event] = []
    for source_index, record in enumerate(extract_events(document), start=1):
        entity_id, event_type, time_s, x_m, y_m = extract_event(record)
        risk = minizinc_round(risk_by_entity[entity_id] * RISK_SCALE)
        rows.append(
            Event(
                source_index,
                entity_id,
                event_type,
                minizinc_round(time_s * TIME_SCALE),
                minizinc_round(x_m),
                minizinc_round(y_m),
                RISK_SCALE - risk,
            )
        )
    return rows


def intersection_candidates(
    document: dict[str, Any], events: list[Event]
) -> list[tuple[float, float]]:
    candidates: list[tuple[float, float]] = []
    raw_events = extract_events(document)
    for raw, event in zip(raw_events, events, strict=True):
        if event.event_type != INTERSECTION_EVENT_TYPE:
            continue
        _, _, _, x, y = extract_event(raw)
        if (x, y) not in candidates:
            candidates.append((x, y))
    return candidates


def observation_actions(
    candidates: list[tuple[float, float]], events: list[Event], fov_radius: float
) -> tuple[list[Action], int]:
    equivalent: dict[tuple[int, int, int, int, tuple[int, ...]], Action] = {}
    raw_action_count = 0
    radius_squared = fov_radius * fov_radius
    for raw_x, raw_y in candidates:
        x = minizinc_round(raw_x)
        y = minizinc_round(raw_y)
        nearby = [
            index
            for index, event in enumerate(events)
            if (event.x - x) ** 2 + (event.y - y) ** 2 <= radius_squared
        ]
        times = sorted({events[index].time for index in nearby})
        for start_index, start in enumerate(times):
            for last in times[start_index:]:
                captured = tuple(
                    index for index in nearby if start <= events[index].time < last + 1
                )
                anchor = min(events[index].source_index for index in captured)
                gain = sum(events[index].gain for index in captured)
                key = (x, y, start, last + 1, captured)
                equivalent.setdefault(
                    key, Action(x, y, start, last + 1, captured, anchor, gain)
                )
                raw_action_count += 1
    actions = sorted(
        equivalent.values(),
        key=lambda action: (
            action.start,
            action.end,
            action.x,
            action.y,
            action.captured,
        ),
    )
    return actions, raw_action_count


def full_graph(
    actions: list[Action],
    drone_start: tuple[int, int, int],
    max_velocity: float,
) -> tuple[list[tuple[int, int]], int, int]:
    source = 1
    sink = len(actions) + 2
    start_time, start_x, start_y = drone_start
    arcs: list[tuple[int, int]] = [(source, sink)]
    for action_index, action in enumerate(actions, start=2):
        if action.start >= start_time + travel_ticks(
            start_x, start_y, action.x, action.y, max_velocity
        ):
            arcs.append((source, action_index))
    for previous_index, previous in enumerate(actions, start=2):
        for next_index, following in enumerate(actions, start=2):
            if following.start >= previous.end + travel_ticks(
                previous.x,
                previous.y,
                following.x,
                following.y,
                max_velocity,
            ):
                arcs.append((previous_index, next_index))
        arcs.append((previous_index, sink))
    return sorted(arcs), source, sink


def transitive_reduction(
    arcs: list[tuple[int, int]], node_count: int, source: int, sink: int
) -> list[tuple[int, int]]:
    outgoing: list[list[int]] = [[] for _ in range(node_count + 1)]
    for start, end in arcs:
        outgoing[start].append(end)

    reachable = [0] * (node_count + 1)
    reduced: list[tuple[int, int]] = []
    for node in range(node_count - 1, 0, -1):
        covered = 0
        for successor in sorted(outgoing[node]):
            successor_bit = 1 << successor
            if covered & successor_bit:
                continue
            reduced.append((node, successor))
            covered |= successor_bit | reachable[successor]
        reachable[node] = covered

    # Keep the valid empty route even though every non-empty source-to-sink route
    # makes this one arc transitively redundant.
    if (source, sink) not in reduced:
        reduced.append((source, sink))
    return sorted(reduced)


def longest_path_oracle(
    actions: list[Action], arcs: list[tuple[int, int]], source: int, sink: int
) -> tuple[int, int, tuple[int, ...]]:
    incoming: list[list[int]] = [[] for _ in range(sink + 1)]
    for start, end in arcs:
        incoming[end].append(start)
    best: list[tuple[int, int, tuple[int, ...]] | None] = [None] * (sink + 1)
    best[source] = (0, 0, ())
    for node in range(source + 1, sink + 1):
        action_index = node - 2
        for previous in incoming[node]:
            prior = best[previous]
            if prior is None:
                continue
            if node == sink:
                candidate = prior
            else:
                candidate = (
                    prior[0] + actions[action_index].gain,
                    prior[1] + 1,
                    prior[2] + (action_index,),
                )
            current = best[node]
            if current is None or (candidate[0], -candidate[1]) > (
                current[0],
                -current[1],
            ):
                best[node] = candidate
    result = best[sink]
    if result is None:
        raise ValueError("the generated graph has no source-to-sink route")
    return result


def dzn_array(values: list[int]) -> str:
    return "[" + ", ".join(str(value) for value in values) + "]"


def one_based_offsets(counts: list[int]) -> list[int]:
    offsets = [1]
    for count in counts:
        offsets.append(offsets[-1] + count)
    return offsets


def serialize_dzn(
    events: list[Event],
    candidates: list[tuple[float, float]],
    actions: list[Action],
    arcs: list[tuple[int, int]],
    source: int,
    sink: int,
) -> str:
    objective_scale = len(candidates) + 1
    arc_cost = [
        (-(actions[end - 2].gain * objective_scale - 1) if 2 <= end < sink else 0)
        for _, end in arcs
    ]
    balance = [1] + [0] * len(actions) + [-1]
    outgoing_counts = [0] * sink
    incoming_counts = [0] * sink
    for start, end in arcs:
        outgoing_counts[start - 1] += 1
        incoming_counts[end - 1] += 1
    incoming_order = sorted(
        range(len(arcs)), key=lambda index: (arcs[index][1], arcs[index][0])
    )
    assignments = {
        "source_event_count": len(events),
        "intersection_count": len(candidates),
        "action_count": len(actions),
        "node_count": sink,
        "arc_count": len(arcs),
        "source_node": source,
        "sink_node": sink,
        "objective_scale": objective_scale,
        "time_scale": TIME_SCALE,
    }
    lines = [f"{name} = {value};" for name, value in assignments.items()]
    arrays = {
        "arc_from": [start for start, _ in arcs],
        "arc_to": [end for _, end in arcs],
        "arc_cost": arc_cost,
        "node_balance": balance,
        "outgoing_start": one_based_offsets(outgoing_counts),
        "incoming_start": one_based_offsets(incoming_counts),
        "incoming_edge": [index + 1 for index in incoming_order],
        "action_x": [action.x for action in actions],
        "action_y": [action.y for action in actions],
        "action_start": [action.start for action in actions],
        "action_end": [action.end for action in actions],
        "action_gain": [action.gain for action in actions],
        "action_anchor_event": [action.anchor_event for action in actions],
        "action_capture_count": [len(action.captured) for action in actions],
    }
    lines.extend(f"{name} = {dzn_array(values)};" for name, values in arrays.items())
    return "\n".join(lines) + "\n"


def build_instance(document: dict[str, Any]) -> tuple[str, dict[str, int]]:
    risk_by_entity = extract_risk_by_entity(BELIEF_MARGINALS)
    events = event_rows(document, risk_by_entity)
    candidates = intersection_candidates(document, events)
    mission_time, drone_x, drone_y, max_velocity, fov_radius = extract_drone(document)
    actions, raw_action_count = observation_actions(candidates, events, fov_radius)
    drone_start = (
        minizinc_round(mission_time * TIME_SCALE),
        minizinc_round(drone_x),
        minizinc_round(drone_y),
    )
    full_arcs, source, sink = full_graph(actions, drone_start, max_velocity)
    reduced_arcs = transitive_reduction(full_arcs, sink, source, sink)
    optimum_gain, optimum_stops, route = longest_path_oracle(
        actions, full_arcs, source, sink
    )
    manifest = {
        "source_events": len(events),
        "intersections": len(candidates),
        "raw_actions": raw_action_count,
        "actions": len(actions),
        "full_arcs": len(full_arcs),
        "reduced_arcs": len(reduced_arcs),
        "longest_route": len(route),
        "optimum_gain": optimum_gain,
        "optimum_stops": optimum_stops,
        "planning_fov_radius_m": fov_radius,
        "planning_max_velocity_mps": max_velocity,
    }
    return serialize_dzn(
        events, candidates, actions, reduced_arcs, source, sink
    ), manifest


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: generate_data.py ENVIRONMENT_JSON DATA_DZN")
    environment_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    document = json.loads(environment_path.read_text(encoding="utf-8"))
    dzn, manifest = build_instance(document)
    output_path.write_text(dzn, encoding="utf-8")
    print(json.dumps(manifest, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
