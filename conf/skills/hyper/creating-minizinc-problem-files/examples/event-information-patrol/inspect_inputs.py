"""Print a compact Mission 1 planning-input summary from caller-supplied paths."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path


def _object(path: Path) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def summarize(
    environment: Mapping[str, object], belief: Mapping[str, object]
) -> dict[str, object]:
    reports = environment.get("static_info")
    if not isinstance(reports, list):
        raise ValueError("environment static_info must be an array")
    world = environment.get("world_model_info")
    checks = world.get("event_report_checks") if isinstance(world, Mapping) else None
    if not isinstance(checks, list):
        raise ValueError("environment event_report_checks must be an array")
    ships = belief.get("ships")
    if not isinstance(ships, list):
        raise ValueError("belief ships must be an array")
    return {
        "mission_id": environment.get("mission_id"),
        "mission_time_seconds": environment.get("mission_time_seconds"),
        "public_report_count": len(reports),
        "report_check_count": len(checks),
        "belief_kind": belief.get("belief_kind"),
        "belief_revision": belief.get("belief_revision"),
        "belief_input_revision": belief.get("input_revision"),
        "belief_ship_ids": [
            ship.get("entity_id") for ship in ships if isinstance(ship, Mapping)
        ],
    }


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: inspect_inputs.py ENVIRONMENT_JSON BELIEF_JSON")
    summary = summarize(_object(Path(sys.argv[1])), _object(Path(sys.argv[2])))
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
