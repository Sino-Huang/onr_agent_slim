"""Materialize time-zero public Mission 1 inputs from a live transport."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path

from onr.adapters.file_transport import FileTransport
from onr.application.reporting_reliability import ReportingReliabilityManager


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport-root", type=Path, required=True)
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    transport = FileTransport(args.transport_root)
    update = transport.get_event(f"environment-update:{args.mission_id}:initial")
    if update is None or update.event_kind != "environment-update":
        parser.error("initial environment update is missing")
    environment_event_id = update.payload.get("environment_event_id")
    if not isinstance(environment_event_id, str):
        parser.error("initial update has no environment event")
    event = transport.get_event(environment_event_id)
    if (
        event is None
        or event.event_kind != "environment_data"
        or event.mission_id != args.mission_id
    ):
        parser.error("initial environment data is missing or mismatched")
    environment = event.to_dict()["payload"]
    assert isinstance(environment, dict)
    if environment.get("mission_id") != args.mission_id or environment.get(
        "mission_time_seconds"
    ) != 0.0:
        parser.error("Mission 1 public input must be the time-zero environment")
    reports = environment.get("static_info")
    if not isinstance(reports, (list, tuple)):
        parser.error("initial environment has no public report schedule")
    entity_ids = sorted(
        {
            report.get("entity_id")
            for report in reports
            if isinstance(report, Mapping)
            and type(report.get("entity_id")) is int
            and report["entity_id"] > 0
        }
    )
    if not entity_ids:
        parser.error("public report schedule has no numeric ship IDs")
    created_at = environment.get("mission_epoch")
    if not isinstance(created_at, str):
        parser.error("initial environment has no Mission epoch")
    belief = ReportingReliabilityManager(args.mission_id, entity_ids).snapshot(
        input_event_id=event.event_id,
        input_revision=0,
        created_at=created_at,
    )

    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "environment.json").write_text(
        json.dumps(environment, indent=2) + "\n", encoding="utf-8"
    )
    (args.output / "belief.json").write_text(
        belief.to_canonical_json() + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "mission_id": args.mission_id,
                "report_count": len(reports),
                "ship_count": len(entity_ids),
                "source_environment_event_id": event.event_id,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
