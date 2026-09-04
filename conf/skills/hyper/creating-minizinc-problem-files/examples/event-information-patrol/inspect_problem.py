"""Validate a generated Mission 1 DZN file and print a compact summary."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast


def _assignments(path: Path) -> dict[str, int | list[object]]:
    values: dict[str, int | list[object]] = {}
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.endswith(";") or " = " not in line:
            raise ValueError(f"invalid DZN assignment on line {line_number}")
        name, encoded = line[:-1].split(" = ", 1)
        if name in values:
            raise ValueError(f"duplicate DZN assignment: {name}")
        values[name] = json.loads(encoded) if encoded.startswith("[") else int(encoded)
    return values


def _integer(values: dict[str, int | list[object]], name: str) -> int:
    value = values.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _array(values: dict[str, int | list[object]], name: str) -> list[object]:
    value = values.get(name)
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def _integer_array(
    values: dict[str, int | list[object]], name: str
) -> list[int]:
    value = _array(values, name)
    if not all(isinstance(item, int) and not isinstance(item, bool) for item in value):
        raise ValueError(f"{name} must contain only integers")
    return cast(list[int], value)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def inspect(path: Path) -> dict[str, object]:
    values = _assignments(path)
    candidate_count = _integer(values, "candidate_count")
    node_count = _integer(values, "node_count")
    arc_count = _integer(values, "arc_count")
    source = _integer(values, "source_node")
    sink = _integer(values, "sink_node")
    report_count = _integer(values, "report_id_count")

    arc_from = _integer_array(values, "arc_from")
    arc_to = _integer_array(values, "arc_to")
    outgoing = _integer_array(values, "outgoing_start")
    incoming = _integer_array(values, "incoming_start")
    incoming_edge = _integer_array(values, "incoming_edge")
    _require(len(arc_from) == len(arc_to) == arc_count, "arc arrays are misaligned")
    _require(
        len(outgoing) == len(incoming) == node_count + 1,
        "adjacency offsets are misaligned",
    )
    _require(
        outgoing[0] == incoming[0] == 1
        and outgoing[-1] == incoming[-1] == arc_count + 1,
        "adjacency offset bounds are invalid",
    )
    _require(
        all(left <= right for left, right in zip(outgoing, outgoing[1:])),
        "outgoing offsets are not nondecreasing",
    )
    _require(
        all(left <= right for left, right in zip(incoming, incoming[1:])),
        "incoming offsets are not nondecreasing",
    )
    for node in range(1, node_count + 1):
        _require(
            all(
                item == node
                for item in arc_from[
                    outgoing[node - 1] - 1 : outgoing[node] - 1
                ]
            ),
            "outgoing index does not match arc_from",
        )
    _require(
        sorted(incoming_edge) == list(range(1, arc_count + 1)),
        "incoming_edge is not an arc permutation",
    )
    for node in range(1, node_count + 1):
        edge_window = incoming_edge[incoming[node - 1] - 1 : incoming[node] - 1]
        _require(
            all(arc_to[edge - 1] == node for edge in edge_window),
            "incoming index does not match arc_to",
        )

    _require(
        all(
            1 <= start < end <= node_count
            for start, end in zip(arc_from, arc_to)
        ),
        "arcs are not forward and in bounds",
    )
    reachable = {source}
    for node in range(source, sink + 1):
        if node in reachable:
            reachable.update(
                end for start, end in zip(arc_from, arc_to) if start == node
            )
    _require(sink in reachable, "candidate DAG has no source-to-sink route")

    candidate_arrays = (
        "candidate_mode",
        "candidate_entity_id",
        "candidate_start",
        "candidate_duration",
        "candidate_x",
        "candidate_y",
        "candidate_score",
        "candidate_recall",
        "candidate_estimation",
        "candidate_omission",
        "candidate_id",
    )
    _require(
        all(len(_array(values, name)) == candidate_count for name in candidate_arrays),
        "candidate arrays are misaligned",
    )
    report_start = _integer_array(values, "candidate_report_start")
    report_ids = _array(values, "candidate_report_id")
    _require(
        len(report_start) == candidate_count + 1
        and report_start[0] == 1
        and report_start[-1] == report_count + 1
        and len(report_ids) == report_count
        and all(left <= right for left, right in zip(report_start, report_start[1:])),
        "candidate report arrays are misaligned",
    )

    return {
        "valid": True,
        "candidate_count": candidate_count,
        "node_count": node_count,
        "arc_count": arc_count,
        "report_id_count": report_count,
        "candidate_arrays_aligned": True,
        "report_arrays_aligned": True,
        "forward_arcs": True,
        "source_to_sink": True,
        "outgoing_index_valid": True,
        "incoming_index_valid": True,
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: inspect_problem.py DATA_DZN")
    print(
        json.dumps(
            inspect(Path(sys.argv[1])), sort_keys=True, separators=(",", ":")
        )
    )


if __name__ == "__main__":
    main()
