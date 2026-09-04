from __future__ import annotations

import json
import runpy
from pathlib import Path
from typing import Any, cast

SCRIPT = Path("scripts/evaluate_mission1_planning_corpus.py")
MINIZINC = Path("modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc")


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_path_driven_corpus_evaluation_sanitizes_and_matches_solver(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(SCRIPT))
    evaluate_corpus = cast(Any, namespace["evaluate_corpus"])
    benchmark = tmp_path / "arbitrary-benchmark"
    instance = benchmark / "tier/case-001"
    reports = [
        {
            "report_id": f"report-{index}",
            "source_event_index": index,
            "entity_id": 4,
            "time": float(index * 10),
            "position": [float(index * 10), 0.0, -2.5],
            "event type": "intersection decision",
            "event information": {"decision": "left"},
        }
        for index in range(1, 5)
    ]
    _write(
        benchmark / "benchmark_manifest.json",
        {
            "instances": [
                {
                    "instance_id": "case-001",
                    "instance_directory": "tier/case-001",
                    "difficulty": "tier",
                }
            ]
        },
    )
    _write(instance / "events_report.json", reports)
    _write(
        instance / "mission1_truth.json",
        {"ship_corruption_probabilities": {"4": 0.75}},
    )

    output = tmp_path / "agent-var-output"
    summary = evaluate_corpus(
        benchmark,
        output,
        minizinc=MINIZINC.resolve(),
        timeout_seconds=30.0,
    )

    assert summary["instance_count"] == 1
    assert summary["snapshot_count"] == 2
    assert summary["all_optimal"] is True
    assert summary["all_oracle_minizinc_match"] is True
    assert summary["exact_candidate_id_route_match_count"] == 2
    for environment_path in (output / "snapshots/case-001").glob("*/environment.json"):
        environment = json.loads(environment_path.read_text(encoding="utf-8"))
        assert "source_event_index" not in json.dumps(environment["static_info"])
    summary_text = (output / "summary.json").read_text(encoding="utf-8")
    assert "ship_corruption_probabilities" not in summary_text
    assert "counterfactual_truth_probability_diagnostic" in summary_text
