"""Materialize the patrol Statechart at caller-supplied artifact paths."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from generate_statechart import (
    build_statechart,
    decode_planner_output,
    extract_assignments,
)


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(
            "usage: prepare_statechart.py PLANNER_ARTIFACT GENERATOR_PY STATECHART_JSON"
        )
    planner_path, generator_path, statechart_path = map(Path, sys.argv[1:])
    items = extract_assignments(decode_planner_output(planner_path))
    statechart, manifest = build_statechart(items)

    generator_path.parent.mkdir(parents=True, exist_ok=True)
    statechart_path.parent.mkdir(parents=True, exist_ok=True)
    generator_path.write_bytes(
        Path(__file__).with_name("generate_statechart.py").read_bytes()
    )
    statechart_path.write_text(
        json.dumps(statechart, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, separators=(",", ":"), ensure_ascii=False))


if __name__ == "__main__":
    main()
