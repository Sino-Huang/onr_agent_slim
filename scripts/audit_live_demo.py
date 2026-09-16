#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path

from onr.application.live_demo_audit import audit_live_demo


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit a terminal model-backed live demo")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument(
        "--mission-mode", choices=("mission2", "mission3", "mission4"), required=True
    )
    parser.add_argument(
        "--mission2-scenario-dir",
        type=Path,
        help="Private Mission 2 scenario used only for terminal metric scoring",
    )
    args = parser.parse_args()
    mission_metrics = None
    if args.mission_mode == "mission2":
        if args.mission2_scenario_dir is None:
            parser.error("Mission 2 audit requires --mission2-scenario-dir")
        from onr_physical_runtime.mission2_evaluation import score_recorded_run

        mission_metrics = score_recorded_run(
            args.mission2_scenario_dir,
            args.run_root / "physical-state/observations",
        )
        (args.run_root / "mission2-metrics.json").write_text(
            json.dumps(mission_metrics, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    audit = audit_live_demo(
        args.run_root,
        args.mission_mode,
        mission_metrics=mission_metrics,
    )
    print(json.dumps(audit, sort_keys=True), flush=True)
    return 0 if audit["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
