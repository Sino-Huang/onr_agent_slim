"""Compare a generated patrol Statechart with its planner-native artifact."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path

from generate_statechart import decode_planner_output, extract_assignments


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def inspect(planner_path: Path, statechart_path: Path) -> dict[str, object]:
    items = extract_assignments(decode_planner_output(planner_path))
    chart = json.loads(statechart_path.read_text(encoding="utf-8"))
    _require(isinstance(chart, Mapping), "Statechart must be a JSON object")
    _require(
        set(chart)
        == {"entry_state", "terminal_states", "states", "state_context", "transitions"},
        "Statechart has unknown or missing fields",
    )
    states = chart["states"]
    contexts = chart["state_context"]
    transitions = chart["transitions"]
    terminals = chart["terminal_states"]
    _require(
        isinstance(states, list) and all(isinstance(item, str) for item in states),
        "states must be strings",
    )
    _require(len(states) == len(set(states)), "states are not unique")
    _require(
        isinstance(contexts, Mapping) and set(contexts) == set(states),
        "state context does not cover states",
    )
    _require(isinstance(transitions, list), "transitions must be an array")
    _require(
        all(
            isinstance(item, Mapping)
            and set(item) == {"event", "source", "target", "context"}
            for item in transitions
        ),
        "transition fields are invalid",
    )
    events = [item["event"] for item in transitions]
    pairs = [(item["source"], item["target"]) for item in transitions]
    _require(len(events) == len(set(events)), "transition events are not unique")
    _require(len(pairs) == len(set(pairs)), "transition state pairs are not unique")
    _require(
        isinstance(terminals, list)
        and terminals
        and all(item in states for item in terminals),
        "terminal states are invalid",
    )

    moving_contexts = [
        context
        for context in contexts.values()
        if isinstance(context, Mapping)
        and isinstance(context.get("desired_outcome"), Mapping)
        and context["desired_outcome"].get("kind")
        in {"arrive_and_observe_fixed_view", "maintain_moving_entity_visibility"}
    ]
    represented = [context.get("candidate_id") for context in moving_contexts]
    expected = [item["identifier"] for item in items]
    _require(represented == expected, "planner assignment order or coverage differs")
    _require(len(represented) == len(set(represented)), "planner item is repeated")

    return {
        "valid": True,
        "planner_items": len(items),
        "represented_once": True,
        "planner_order_preserved": True,
        "state_count": len(states),
        "transition_count": len(transitions),
        "terminal_count": len(terminals),
        "context_covers_states": True,
        "unique_events": True,
        "unique_state_pairs": True,
    }


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(
            "usage: inspect_statechart.py PLANNER_ARTIFACT STATECHART_JSON"
        )
    result = inspect(Path(sys.argv[1]), Path(sys.argv[2]))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
