"""Materialize the Mission 1 model and data at caller-supplied output paths."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from generate_data import build_instance
from onr.contracts.reporting_reliability import ReportingReliabilitySnapshot


def main() -> None:
    if len(sys.argv) != 5:
        raise SystemExit(
            "usage: prepare_problem.py ENVIRONMENT_JSON BELIEF_JSON MODEL_MZN DATA_DZN"
        )
    environment_path, belief_path, model_path, data_path = map(Path, sys.argv[1:])
    environment = json.loads(environment_path.read_text(encoding="utf-8"))
    belief = ReportingReliabilitySnapshot.from_dict(
        json.loads(belief_path.read_text(encoding="utf-8"))
    )
    data, manifest = build_instance(environment, belief)

    model_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_bytes(Path(__file__).with_name("model.mzn").read_bytes())
    data_path.write_text(data, encoding="utf-8")
    print(json.dumps(manifest, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
