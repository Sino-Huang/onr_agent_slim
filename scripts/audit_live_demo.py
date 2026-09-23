#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from onr.application.live_demo_audit import audit_live_demo

_DEFAULT_MISSION4_EVALUATOR = (
    "/data/ccu/sukaih/ONR/onr_physical_runtime/scripts/evaluate_mission4_static.py"
)


def _mission4_answer_metrics(run_root: Path, answers_path: Path) -> dict[str, object]:
    """Run the static Mission 4 evaluator; its metrics are informational only."""
    evaluator = Path(
        os.environ.get("ONR_MISSION4_EVALUATOR", _DEFAULT_MISSION4_EVALUATOR)
    )
    metrics_path = run_root / "mission4-answer-metrics.json"
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(evaluator),
                "--run-root",
                str(run_root),
                "--answers",
                str(answers_path),
            ],
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            return {"error": detail or f"evaluator exited {completed.returncode}"}
        return json.loads(metrics_path.read_text(encoding="utf-8"))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit a terminal model-backed live demo")
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument(
        "--mission-mode",
        choices=("mission2", "mission3", "mission4", "joint24", "joint34"),
        required=True,
    )
    parser.add_argument(
        "--mission2-scenario-dir",
        type=Path,
        help="Private Mission 2 scenario used only for terminal metric scoring",
    )
    parser.add_argument(
        "--mission4-answers",
        type=Path,
        help="Private Mission 4 answer key used only for terminal answer metrics",
    )
    parser.add_argument(
        "--mission4-package",
        type=Path,
        help=(
            "Public Mission 4 package; for joint34 runs it enables the strict "
            "dock-ingress gate over the recorded search_area maneuver"
        ),
    )
    args = parser.parse_args()
    if args.mission4_answers is not None and args.mission_mode not in {"mission4", "joint24", "joint34"}:
        parser.error("--mission4-answers is only valid with --mission-mode mission4, joint24 or joint34")
    mission_metrics = None
    if args.mission_mode in {"mission2", "joint24"}:
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
        mission4_package=args.mission4_package,
    )
    if args.mission4_answers is not None and audit["status"] == "PASS":
        audit = audit_live_demo(
            args.run_root,
            args.mission_mode,
            mission_metrics=mission_metrics,
            mission4_answer_metrics=_mission4_answer_metrics(
                args.run_root, args.mission4_answers
            ),
            mission4_package=args.mission4_package,
        )
    print(json.dumps(audit, sort_keys=True), flush=True)
    return 0 if audit["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
