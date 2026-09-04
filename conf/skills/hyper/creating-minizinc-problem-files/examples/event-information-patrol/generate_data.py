"""Build Mission 1 MiniZinc data with the code-owned candidate/DAG builder."""

from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path

from onr.application.mission1_planning import (
    build_candidate_dag,
    longest_path_oracle,
    serialize_minizinc_data,
)
from onr.application.reporting_reliability import ReportingReliabilityManager
from onr.contracts.reporting_reliability import ReportingReliabilitySnapshot


def _adapt_environment(document: dict[str, object]) -> dict[str, object]:
    reports = document.get("static_info", [])
    for order, report in enumerate(reports, start=1):
        if "report_id" not in report:
            digest = hashlib.sha256(
                json.dumps(
                    {"public_order": order, "report": report},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:24]
            report["report_id"] = f"report-{digest}"
    if "controlled_vehicle" not in document:
        scene = document["scene_graph"]
        drone = next(entity for entity in scene["entities"] if entity["type"] == "drone")
        document["mission_time_seconds"] = scene["mission_time_seconds"]
        document["controlled_vehicle"] = {
            "position": drone["location"],
            "max_velocity": drone["max_velocity"],
            "fov_radius": drone["fov_radius"],
        }
    document.setdefault("mission_id", "mission-1")
    document.setdefault("world_model_info", {"event_report_checks": []})
    return document


def build_instance(
    document: dict[str, object],
    belief: ReportingReliabilitySnapshot | None = None,
) -> tuple[str, dict[str, object]]:
    environment = _adapt_environment(document)
    if belief is None:
        entity_ids = sorted({int(report["entity_id"]) for report in environment["static_info"]})
        belief = ReportingReliabilityManager(
            str(environment["mission_id"]), entity_ids
        ).snapshot(
            input_event_id="example-prior",
            input_revision=0,
            created_at="2026-09-03T00:00:00+10:00",
        )
    graph = build_candidate_dag(environment, belief)
    route = longest_path_oracle(graph)
    manifest = {
        "candidates": len(graph.candidates),
        "arcs": len(graph.arcs),
        "advisory_score": route.score,
        "advisory_maneuvers": len(route.candidates),
        "advisory_duration_s": route.duration_s,
        "covered_report_count": len(route.covered_report_ids),
        "covered_report_ids": list(route.covered_report_ids),
    }
    return serialize_minizinc_data(graph), manifest


def main() -> None:
    if len(sys.argv) not in {3, 4}:
        raise SystemExit(
            "usage: generate_data.py ENVIRONMENT_JSON BELIEF_JSON DATA_DZN"
        )
    environment = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    belief = None
    output_index = 2
    if len(sys.argv) == 4:
        belief = ReportingReliabilitySnapshot.from_dict(
            json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        )
        output_index = 3
    data, manifest = build_instance(environment, belief)
    Path(sys.argv[output_index]).write_text(data, encoding="utf-8")
    print(json.dumps(manifest, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
