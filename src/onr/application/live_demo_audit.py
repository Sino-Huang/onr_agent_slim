"""Terminal audit for model-backed physical-runtime demo runs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _environment_events(run_root: Path) -> list[Mapping[str, Any]]:
    stream = run_root / "transport/topics/environment-data/missions/mission%3Ademo"
    events = []
    for path in sorted(stream.glob("*.json")):
        value = _read(path)
        if isinstance(value, Mapping) and value.get("event_kind") == "environment_data":
            events.append(value["payload"])
    return sorted(
        events,
        key=lambda item: (
            float(item.get("mission_time_seconds", -1)),
            int(item.get("state_version", -1)),
        ),
    )


def _operational_records(run_root: Path) -> list[Mapping[str, Any]]:
    root = run_root / "agent-storage/operational-log"
    return [
        value
        for path in root.glob("*/events/*.json")
        if isinstance((value := _read(path)), Mapping)
    ]


def _agent_debug_records(run_root: Path) -> list[Mapping[str, Any]]:
    root = run_root / "debug/agent"
    return [
        value
        for path in root.glob("*/**/[0-9]*.json")
        if isinstance((value := _read(path)), Mapping)
    ]


def _contains_private_fixture_key(value: object) -> bool:
    if isinstance(value, Mapping):
        if {"private_id", "fixture", "target_positions", "ground_truth"} & set(value):
            return True
        return any(_contains_private_fixture_key(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_private_fixture_key(item) for item in value)
    return False


def audit_live_demo(run_root: Path, mission_mode: str) -> dict[str, object]:
    """Return and persist a pass/fail integration audit for one completed run."""
    root = Path(run_root)
    if mission_mode not in {"mission2", "mission3", "mission4"}:
        raise ValueError("live demo audit supports mission2, mission3 or mission4")
    result_path = root / "closed-loop-result.json"
    result = _read(result_path) if result_path.is_file() else None
    environments = _environment_events(root)
    records = _operational_records(root)
    debug_records = _agent_debug_records(root)
    failures: list[str] = []
    if not isinstance(result, Mapping):
        failures.append("closed_loop_result_missing")
    else:
        if result.get("terminal") is not True:
            failures.append("fsm_not_terminal")
        if not result.get("physical_actions") or int(result.get("feedback_count", 0)) < 1:
            failures.append("physical_maneuver_not_observed")
        if len(result.get("plan_revisions", ())) < 2:
            failures.append("evidence_replan_not_observed")
        for window in result.get("inference_windows", ()):
            if window.get("evidence_time_seconds") != window.get("completion_time_seconds"):
                failures.append("mission_time_advanced_during_inference")
                break
    if not environments:
        failures.append("environment_evidence_missing")
    completed_sources = {
        (record.get("source"), record.get("event_kind"))
        for record in records
        if record.get("outcome") in {"completed", "accepted", "verified"}
    }
    if ("hyper-agent", "workflow") not in completed_sources:
        failures.append("hyper_workflow_not_completed")
    if not any(
        record.get("source") == "maneuver-control"
        and record.get("event_kind") in {"heartbeat", "control"}
        and record.get("outcome") in {"completed", "queued"}
        for record in records
    ):
        failures.append("maneuver_model_not_completed")
    if any(record.get("outcome") in {"failed", "error"} for record in records):
        failures.append("operational_error_recorded")
    if not any(record.get("kind") == "llm" for record in debug_records):
        failures.append("model_debug_receipt_missing")
    if any(
        record.get("error") is not None
        or record.get("completion_state") not in {None, "complete"}
        for record in debug_records
    ):
        failures.append("agent_debug_error_recorded")
    if any((root / "transport/subscriptions").glob("**/dead-letter/*.json")):
        failures.append("transport_dead_letter_recorded")

    latest = environments[-1] if environments else {}
    world = latest.get("world_model_info", {}) if isinstance(latest, Mapping) else {}
    if world.get("mission_mode") != mission_mode:
        failures.append("mission_mode_mismatch")
    if mission_mode == "mission2":
        snapshots = [
            item.get("world_model_info", {}).get("perception_predictions", {})
            for item in environments
        ]
        if not any(snapshot.get("status") in {"ready", "partial"} for snapshot in snapshots):
            failures.append("mission2_predictions_never_ready")
        if len({snapshot.get("sequence") for snapshot in snapshots}) < 2:
            failures.append("mission2_prediction_sequence_static")
        mission_end = world.get("mission_end_time_s")
        if (
            isinstance(result, Mapping)
            and isinstance(mission_end, (int, float))
            and float(result.get("simulated_duration_seconds", 0)) < float(mission_end)
        ):
            failures.append("mission2_stopped_before_recording_end")
    elif mission_mode == "mission3":
        inspection = world.get("mission3", {})
        ships = inspection.get("ships", ()) if isinstance(inspection, Mapping) else ()
        if not ships or any(ship.get("resolution", {}).get("status") != "resolved" for ship in ships):
            failures.append("mission3_roster_not_resolved")
        evidence = inspection.get("evidence", ()) if isinstance(inspection, Mapping) else ()
        if not evidence or any(item.get("source") != "simulated" for item in evidence):
            failures.append("mission3_fixture_evidence_missing")
    else:
        search = world.get("mission4", {})
        if search.get("status") != "completed" or search.get("reason") != "all_found":
            failures.append("mission4_not_all_found")
        if len(search.get("requests", ())) < 2:
            failures.append("mission4_requests_missing")
        if not search.get("observations") or search.get("source") != "simulated":
            failures.append("mission4_fixture_evidence_missing")
        worker_path = root / "mission4-worker-session.json"
        worker = _read(worker_path) if worker_path.is_file() else {}
        accepted = sum(item.get("kind") == "accepted" for item in worker.get("history", ()))
        if accepted < 2:
            failures.append("mission4_worker_receipts_missing")
        if _contains_private_fixture_key(latest):
            failures.append("private_fixture_data_exposed")

    audit = {
        "status": "PASS" if not failures else "FAIL",
        "mission_mode": mission_mode,
        "run_root": str(root.resolve()),
        "failures": failures,
        "closed_loop_result": result,
        "environment_event_count": len(environments),
        "operational_record_count": len(records),
        "agent_debug_record_count": len(debug_records),
    }
    (root / "live-acceptance.json").write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return audit


__all__ = ["audit_live_demo"]
