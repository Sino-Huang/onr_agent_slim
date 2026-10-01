"""Terminal audit for model-backed physical-runtime demo runs."""

from __future__ import annotations

import json
import math

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


def _physical_feedback_records(
    run_root: Path,
) -> list[tuple[Path, Mapping[str, Any]]]:
    return [
        (path, value)
        for path in sorted((run_root / "physical-state" / "feedback").glob("*.json"))
        if isinstance((value := _read(path)), Mapping)
    ]


def _joint34_mission3_audit(
    run_root: Path,
    inspection: Mapping[str, Any],
    environments: list[Mapping[str, Any]],
) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    failures: list[str] = []
    selected = inspection.get("selected_ship_ids")
    selected_ids = list(selected) if isinstance(selected, (list, tuple)) else []
    selected_set = set(selected_ids)
    ships = inspection.get("ships")
    ship_rows = [item for item in ships if isinstance(item, Mapping)] if isinstance(ships, (list, tuple)) else []
    ship_by_id = {item.get("ship_id"): item for item in ship_rows}
    evidence = inspection.get("evidence")
    evidence_rows = [item for item in evidence if isinstance(item, Mapping)] if isinstance(evidence, (list, tuple)) else []

    if not selected_ids:
        failures.append("mission3_selection_missing")
    elif len(selected_ids) < 2 or len(selected_ids) > 3 or len(selected_set) != len(selected_ids):
        failures.append("mission3_roster_size_invalid")
    if set(ship_by_id) != selected_set:
        failures.append("mission3_roster_not_resolved")

    latest_m3 = environments[-1].get("world_model_info", {}).get("mission3", {}) if environments else {}
    perception_source = latest_m3.get("perception_source") if isinstance(latest_m3, Mapping) else None
    if perception_source != "simulated_fixture" or not evidence_rows:
        failures.append("mission3_fixture_evidence_missing")
    if not any(
        isinstance(item.get("world_model_info", {}).get("mission3"), Mapping)
        and item["world_model_info"]["mission3"].get("target_observations")
        for item in environments
    ):
        failures.append("mission3_target_observations_missing")

    latest_time = environments[-1].get("mission_time_seconds") if environments else None
    evidence_by_id: dict[str, Mapping[str, Any]] = {}
    sufficient_stages: set[str] = set()
    sufficient_ship_ids_by_stage: dict[str, set[Any]] = {
        "screening": set(),
        "investigation": set(),
    }
    verdict_values: set[str] = set()
    evidence_valid = True
    for row in evidence_rows:
        evidence_id = row.get("evidence_id")
        acquired_at = row.get("acquired_at_s")
        stage = row.get("stage")
        if (
            not isinstance(evidence_id, str)
            or not evidence_id
            or evidence_id in evidence_by_id
            or row.get("ship_id") not in selected_set
            or isinstance(acquired_at, bool)
            or not isinstance(acquired_at, (int, float))
            or not math.isfinite(float(acquired_at))
            or float(acquired_at) < 0.0
            or (isinstance(latest_time, (int, float)) and float(acquired_at) > float(latest_time))
            or row.get("source") != "simulated"
            or stage not in {"screening", "investigation"}
            or row.get("availability") not in {"usable", "unavailable", "not_ready"}
        ):
            evidence_valid = False
            continue
        evidence_by_id[evidence_id] = row
        if row.get("sufficient") is True and row.get("verdict") in {"normal", "abnormal"}:
            sufficient_stages.add(str(stage))
            sufficient_ship_ids_by_stage[str(stage)].add(row["ship_id"])
            verdict_values.add(str(row["verdict"]))
    if not evidence_valid:
        failures.append("mission3_fixture_evidence_missing")
    if not {"screening", "investigation"} <= sufficient_stages:
        failures.append("mission3_screening_and_investigation_evidence_missing")
    if not {"normal", "abnormal"} <= verdict_values:
        failures.append("mission3_verdict_diversity_missing")

    roster_valid = bool(selected_ids) and set(ship_by_id) == selected_set
    for ship_id in selected_ids:
        ship = ship_by_id.get(ship_id)
        if not isinstance(ship, Mapping):
            roster_valid = False
            continue
        resolution = ship.get("resolution")
        verdict = ship.get("verdict")
        if (
            not isinstance(resolution, Mapping)
            or resolution.get("status") != "resolved"
            or not isinstance(verdict, Mapping)
            or verdict.get("sufficient") is not True
            or verdict.get("value") not in {"normal", "abnormal"}
        ):
            roster_valid = False
            continue
        supporting_ids = verdict.get("supporting_evidence_ids")
        if not isinstance(supporting_ids, (list, tuple)) or not supporting_ids:
            roster_valid = False
            continue
        supporting_evidence = [evidence_by_id.get(str(item)) for item in supporting_ids]
        if not any(
            isinstance(item, Mapping)
            and item.get("ship_id") == ship_id
            and item.get("sufficient") is True
            and item.get("verdict") == verdict.get("value")
            and isinstance(item.get("viewpoint"), Mapping)
            and item["viewpoint"].get("request_id")
            for item in supporting_evidence
        ):
            roster_valid = False
    if not roster_valid:
        failures.append("mission3_roster_not_resolved")

    m3_reports = [
        report
        for path in sorted(
            (run_root / "transport/topics/mission3-agent-reports").glob("missions/*/*.json")
        )
        if (report := _read(path)).get("event_kind") == "mission3-agent-report"
    ]
    report_complete = False
    for event in m3_reports:
        payload = event.get("payload")
        report = payload.get("report") if isinstance(payload, Mapping) else None
        if not isinstance(report, Mapping) or report.get("inspection_complete") is not True:
            continue
        report_ships = report.get("ships")
        report_by_id = {
            item.get("ship_id"): item
            for item in report_ships
            if isinstance(item, Mapping)
        } if isinstance(report_ships, (list, tuple)) else {}
        if set(report_by_id) != selected_set:
            continue
        if all(
            item.get("resolution") == "resolved"
            and item.get("verdict") in {"normal", "abnormal"}
            and any(
                evidence_by_id.get(str(evidence_id), {}).get("ship_id") == ship_id
                and evidence_by_id.get(str(evidence_id), {}).get("sufficient") is True
                and evidence_by_id.get(str(evidence_id), {}).get("verdict") == item.get("verdict")
                for evidence_id in item.get("supporting_evidence_ids", ())
            )
            for ship_id, item in report_by_id.items()
        ):
            report_complete = True
            break
    if not report_complete:
        failures.append("mission3_report_evidence_missing")

    feedback = _physical_feedback_records(run_root)
    if any(
        row.get("feedback_kind") == "protocol_diagnostic" and row.get("fatal") is True
        for _, row in feedback
    ):
        failures.append("physical_runtime_error_recorded")
    commands = [
        value
        for path in sorted((run_root / "physical-state" / "commands").glob("*.json"))
        if isinstance((value := _read(path)), Mapping)
        and isinstance(value.get("intent"), Mapping)
        and value["intent"].get("action") == "investigate"
    ]
    states_by_command: dict[str, set[str]] = {}
    feedback_by_command: dict[str, list[tuple[Path, Mapping[str, Any]]]] = {}
    for path, row in feedback:
        command_id = row.get("command_id")
        if row.get("feedback_kind") != "lifecycle" or not isinstance(command_id, str):
            continue
        states_by_command.setdefault(command_id, set()).add(str(row.get("lifecycle_state")))
        feedback_by_command.setdefault(command_id, []).append((path, row))

    completed_investigations = []
    for command in commands:
        command_id = command.get("command_id")
        parameters = command.get("intent", {}).get("parameters", {})
        entity_id = parameters.get("entity_id") if isinstance(parameters, Mapping) else None
        if (
            isinstance(command_id, str)
            and entity_id in selected_set
            and {"accepted", "completed"} <= states_by_command.get(command_id, set())
            and any(
                row.get("ship_id") == entity_id
                and row.get("stage") == "investigation"
                and row.get("sufficient") is True
                for row in evidence_rows
            )
        ):
            completed_investigations.append((command, command_id, entity_id))
    if not completed_investigations:
        failures.append("mission3_investigation_not_completed")
    screened_ship_ids = sufficient_ship_ids_by_stage["screening"]
    investigated_ship_ids = {entity_id for _, _, entity_id in completed_investigations}
    if not any(
        screening_ship_id != investigation_ship_id
        for screening_ship_id in screened_ship_ids
        for investigation_ship_id in investigated_ship_ids
    ):
        failures.append("mission3_distinct_screening_and_investigation_missing")

    camera_receipts: list[dict[str, Any]] = []
    observed_owners: set[str] = set()
    for command, command_id, entity_id in completed_investigations:
        tracking_samples: list[dict[str, Any]] = []
        reset_receipt: dict[str, Any] | None = None
        for path, row in feedback_by_command[command_id]:
            telemetry = row.get("telemetry")
            synchronization = telemetry.get("synchronization") if isinstance(telemetry, Mapping) else None
            status = synchronization.get("camera_status") if isinstance(synchronization, Mapping) else None
            if not isinstance(status, Mapping):
                continue
            owner = status.get("owner")
            if isinstance(owner, str):
                observed_owners.add(owner)
            intent = status.get("active_intent")
            witness = status.get("last_tracking")
            sequence = intent.get("camera_sequence") if isinstance(intent, Mapping) else None
            body_position = witness.get("body_position_ned") if isinstance(witness, Mapping) else None
            target_position = witness.get("target_position_ned") if isinstance(witness, Mapping) else None
            # Two tracking bases prove the same fact: an entity-tracked intent
            # binds the target by entity_id, while a harbor vessel without an
            # engine actor binding is tracked as an explicit aim point that is
            # re-published from the live estimate — the command identity binds
            # the investigated entity in both cases.
            entity_bound = (
                intent.get("operation") == "track_entity"
                and intent.get("entity_id") == entity_id
                and isinstance(witness, Mapping)
                and witness.get("entity_id") == entity_id
            ) if isinstance(intent, Mapping) else False
            point_bound = (
                intent.get("operation") == "look_at_point"
                and isinstance(witness, Mapping)
                and isinstance(witness.get("point_ned"), (list, tuple))
                and len(witness["point_ned"]) == 3
            ) if isinstance(intent, Mapping) else False
            if (
                row.get("lifecycle_state") in {"active", "accepted"}
                and owner == "runtime"
                and status.get("phase") == "tracking"
                and isinstance(intent, Mapping)
                and intent.get("command_id") == command_id
                and (entity_bound or point_bound)
                and isinstance(sequence, int)
                and status.get("accepted_sequence") == sequence
                and status.get("applied_sequence") == sequence
                and isinstance(witness, Mapping)
                and witness.get("command_id") == command_id
                and isinstance(witness.get("captured_at_s"), (int, float))
                and isinstance(body_position, (list, tuple))
                and len(body_position) == 3
                and isinstance(target_position, (list, tuple))
                and len(target_position) == 3
            ):
                tracking_samples.append(
                    {
                        "feedback_path": str(path.relative_to(run_root)),
                        "mission_time_s": row.get("mission_time_s"),
                        "camera_sequence": sequence,
                        "captured_at_s": witness["captured_at_s"],
                        "body_position_ned": list(body_position),
                        "target_position_ned": list(target_position),
                    }
                )
            progress = row.get("progress")
            reset_sequence = progress.get("camera_reset_sequence") if isinstance(progress, Mapping) else None
            if (
                row.get("lifecycle_state") == "completed"
                and owner == "runtime"
                and status.get("phase") == "default"
                and status.get("reset_verified") is True
                and status.get("active_intent") is None
                and isinstance(reset_sequence, int)
                and status.get("accepted_sequence") == reset_sequence
                and status.get("applied_sequence") == reset_sequence
            ):
                reset_receipt = {
                    "feedback_path": str(path.relative_to(run_root)),
                    "camera_sequence": reset_sequence,
                    "phase": status.get("phase"),
                    "reset_verified": True,
                }
        distinct_times = {sample["captured_at_s"] for sample in tracking_samples}
        distinct_positions = {tuple(sample["body_position_ned"]) for sample in tracking_samples}
        if len(distinct_times) >= 2 and len(distinct_positions) >= 2 and reset_receipt is not None:
            camera_receipts.append(
                {
                    "investigate_command_id": command_id,
                    "target_entity_id": entity_id,
                    "investigate_command_path": str(
                        next(
                            path
                            for path in sorted((run_root / "physical-state" / "commands").glob("*.json"))
                            if _read(path).get("command_id") == command_id
                        ).relative_to(run_root)
                    ),
                    "camera_owner": "runtime",
                    "tracking_samples": tracking_samples,
                    "reset_receipt": reset_receipt,
                }
            )
    if not camera_receipts:
        failures.append("mission3_camera_control_incomplete")
    camera_control = {
        "status": "PASS" if camera_receipts else "FAIL",
        "owners_observed": sorted(observed_owners),
        "receipts": camera_receipts,
        "source_feedback_directory": str((run_root / "physical-state" / "feedback").resolve()),
    }
    inspection_evidence = {
        "perception_source": perception_source,
        "selected_ship_ids": selected_ids,
        "inspection": dict(inspection),
        "reports": m3_reports,
        "investigate_commands": commands,
        "classification_evidence_path": "transport/topics/environment-data/missions/mission%3Ademo/",
        "block_entered": False,
    }
    return failures, inspection_evidence, camera_control


def _joint34_mission4_audit(
    run_root: Path, search: Mapping[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    failures: list[str] = []
    objectives = search.get("objectives")
    target_ids = set(objectives) if isinstance(objectives, Mapping) else set()
    if not target_ids or not search.get("requests"):
        failures.append("mission4_requests_missing")

    worker_path = run_root / "mission4-worker-session.json"
    worker = _read(worker_path) if worker_path.is_file() else {}
    accepted_receipts: list[dict[str, Any]] = []
    history = worker.get("history", ()) if isinstance(worker, Mapping) else ()
    for entry in history:
        receipt = entry.get("receipt") if isinstance(entry, Mapping) else None
        request = receipt.get("request") if isinstance(receipt, Mapping) else None
        objective = request.get("objective") if isinstance(request, Mapping) else None
        if (
            isinstance(entry, Mapping)
            and entry.get("kind") == "accepted"
            and isinstance(request, Mapping)
            and request.get("operation") == "add"
            and isinstance(objective, Mapping)
            and objective.get("target_id")
        ):
            accepted_receipts.append(
                {"request": dict(request), "receipt": dict(receipt)}
            )
    accepted_target_ids = {
        item["request"]["objective"]["target_id"] for item in accepted_receipts
    }
    if not target_ids <= accepted_target_ids:
        failures.append("mission4_worker_receipts_missing")

    report_events = [
        value
        for path in sorted(
            (run_root / "transport/topics/mission4-agent-reports").glob("missions/*/*.json")
        )
        if isinstance((value := _read(path)), Mapping)
        and value.get("event_kind") == "mission4-agent-report"
    ]
    target_reports: list[dict[str, Any]] = []
    for event in report_events:
        payload = event.get("payload")
        report = payload.get("report") if isinstance(payload, Mapping) else None
        reports = report.get("targets") if isinstance(report, Mapping) else None
        if (
            not isinstance(report, Mapping)
            or report.get("reason") != "all_found"
            or not isinstance(reports, (list, tuple))
        ):
            continue
        by_target = {
            item.get("target_id"): item
            for item in reports
            if isinstance(item, Mapping)
        }
        if set(by_target) >= target_ids and all(
            by_target[target_id].get("status") == "found"
            and isinstance(by_target[target_id].get("match"), Mapping)
            and by_target[target_id]["match"].get("found") is True
            for target_id in target_ids
        ):
            target_reports = [dict(by_target[target_id]) for target_id in sorted(target_ids)]
            break
    if not target_reports:
        failures.append("mission4_target_reports_missing")
    return failures, {
        "status": "PASS" if not failures else "FAIL",
        "active_target_ids": sorted(target_ids),
        "worker_request_receipts": accepted_receipts,
        "target_reports": target_reports,
        "reports_path": "transport/topics/mission4-agent-reports/missions/mission%3Ademo/",
    }


def _contains_private_fixture_key(value: object) -> bool:
    if isinstance(value, Mapping):
        if {"answers", "private_id", "fixture", "target_positions", "ground_truth"} & set(
            value
        ):
            return True
        return any(_contains_private_fixture_key(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_private_fixture_key(item) for item in value)
    return False


def aoi_trajectory_audit(run_root: Path, package_path: Path) -> dict[str, object]:
    """Strict dock-ingress audit of the recorded joint34 ``search_area`` run.

    Feeds recorded public maneuver feedback through the runtime repo's
    geometry-only judge so the video bundle, the no-LLM smoke, and this
    terminal gate share one definition of interior traversal (issue #71).
    """

    from onr_physical_runtime.ingress_audit import (
        DEFAULT_GRID_CELL_M,
        audit_dock_ingress,
        ned_polygon,
        samples_from_feedback,
    )

    def failure(reasons: list[str]) -> dict[str, object]:
        return {"status": "fail", "failures": reasons, "action_ids": []}

    package = _read(Path(package_path))
    dock = package["areas"]["dock"]["polygon"]
    feedback = [
        value
        for path in sorted((run_root / "physical-state" / "feedback").glob("*.json"))
        if isinstance((value := _read(path)), Mapping)
    ]
    accepted_ids = sorted(
        {
            str(row["command_id"])
            for row in feedback
            if row.get("action") == "search_area"
        }
    )
    if not accepted_ids:
        return failure(["no_accepted_search_area_command"])
    rows = [
        row
        for row in feedback
        if row.get("action") == "search_area" and row.get("command_id") in accepted_ids
    ]
    samples = samples_from_feedback(rows, action="search_area")
    coverage = [
        {
            "mission_time_s": float(row["mission_time_s"]),
            "cleared_cells": int(row["progress"]["cleared_cells"]),
            "total_cells": int(row["progress"]["total_cells"]),
        }
        for row in rows
        if isinstance(row.get("progress"), Mapping)
        and "cleared_cells" in row["progress"]
        and "total_cells" in row["progress"]
    ]
    audit = audit_dock_ingress(
        action_polygon=ned_polygon(
            [
                {"x": float(vertex["x"]), "y": float(vertex["y"])}
                for vertex in _first_search_polygon(run_root, accepted_ids)
            ]
        ),
        package_polygon=ned_polygon(dock),
        keep_out_zones=[
            ned_polygon(zone) for zone in (package.get("keep_out_zones") or ())
        ],
        samples=samples,
        coverage=coverage,
        search_completed=any(
            row.get("lifecycle_state") == "completed" for row in rows
        ),
        # Coverage targets include boundary cells whose grid centers can sit
        # up to half a grid cell outside the polygon, and the row-end U-turn
        # overshoots the edge by under a cell of travel; absorb up to one
        # coverage cell of boundary-alignment error while keeping genuine
        # excursions failing the gate.
        containment_tolerance_m=float(DEFAULT_GRID_CELL_M),
    ).to_dict()
    audit["action_ids"] = accepted_ids
    audit["action_polygon"] = _first_search_polygon(run_root, accepted_ids)
    return audit


def _first_search_polygon(run_root: Path, command_ids: list[str]) -> list[object]:
    """The accepted search_area action polygon from the recorded commands."""

    for path in sorted((run_root / "physical-state" / "commands").glob("*.json")):
        command = _read(path)
        if (
            command.get("command_id") in command_ids
            and command.get("intent", {}).get("action") == "search_area"
        ):
            return list(command["intent"]["parameters"]["polygon"])
    raise ValueError("accepted search_area feedback has no recorded command")


def _mission1_perception_audit(
    run_root: Path, environments: list[Mapping[str, Any]], perception: str
) -> tuple[list[str], dict[str, object]]:
    """Prove the Mission 1 evidence came from the external camera producer."""
    failures: list[str] = []
    expected = f"{perception}_camera_perception"
    sources: dict[str, int] = {}
    external_checks = 0
    for item in environments:
        world = item.get("world_model_info", {})
        for ship in world.get("visible_ships", ()):
            source = str(ship.get("source"))
            sources[source] = sources.get(source, 0) + 1
        if world.get("mission1_comparison", {}).get("visibility_source") == "external_camera":
            external_checks = max(external_checks, len(world.get("event_report_checks", ())))
    if sources.get(expected, 0) < 1:
        failures.append("perception_source_not_observed")
    if set(sources) - {expected}:
        failures.append("perception_source_mixed")
    if external_checks < 1:
        failures.append("mission1_external_camera_checks_missing")
    manifests = sorted((run_root / "perception/runs").glob("*/manifest.json"))
    manifest = _read(manifests[-1]) if manifests else {}
    if manifest.get("configuration", {}).get("perception", "ideal") != perception:
        failures.append("perception_producer_mode_mismatch")
    return failures, {
        "expected_source": expected,
        "visible_ship_sources": sources,
        "external_camera_report_checks": external_checks,
        "producer_manifest": str(manifests[-1].resolve()) if manifests else None,
    }


def audit_live_demo(
    run_root: Path,
    mission_mode: str,
    *,
    mission_metrics: Mapping[str, object] | None = None,
    mission4_answer_metrics: Mapping[str, object] | None = None,
    mission4_package: Path | None = None,
    perception: str | None = None,
) -> dict[str, object]:
    """Return and persist a pass/fail integration audit for one completed run."""
    root = Path(run_root)
    if mission_mode not in {"mission1", "mission2", "mission3", "mission4", "joint24", "joint34"}:
        raise ValueError(
            "live demo audit supports mission1, mission2, mission3, mission4, joint24 or joint34"
        )
    if perception not in {None, "yolo", "ideal"} or (perception and mission_mode != "mission1"):
        raise ValueError("perception audit is Mission 1 only and must be yolo or ideal")
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
    if world.get("mission_mode", "mission1") != mission_mode:
        failures.append("mission_mode_mismatch")
    perception_evidence: dict[str, object] | None = None
    if perception is not None:
        perception_failures, perception_evidence = _mission1_perception_audit(
            root, environments, perception
        )
        failures.extend(perception_failures)
    if mission_mode == "mission1":
        if not world.get("event_report_checks"):
            failures.append("mission1_report_checks_missing")
    elif mission_mode in {"mission2", "joint24"}:
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
    elif mission_mode == "joint34":
        if isinstance(result, Mapping):
            if result.get("final_fsm_state") != "joint34-complete":
                failures.append("joint34_terminal_state_mismatch")
            mission_end = world.get("mission_end_time_s")
            if isinstance(mission_end, (int, float)) and float(
                result.get("simulated_duration_seconds", 0)
            ) < mission_end:
                failures.append("mission3_stopped_before_budget")
        inspection = world.get("mission3", {})
        if not isinstance(inspection, Mapping):
            inspection = {}
        m3_failures, mission3_evidence, camera_control = _joint34_mission3_audit(
            root, inspection, environments
        )
        failures.extend(m3_failures)
        mission3_block_entered = any(
            record.get("source") == "fsm-runner"
            and isinstance(record.get("details"), Mapping)
            and record["details"].get("state") == "mission3-block"
            for record in records
        )
        if not mission3_block_entered:
            failures.append("mission3_block_not_entered")
        mission3_evidence["block_entered"] = mission3_block_entered
    if mission_mode in {"mission4", "joint24", "joint34"}:
        search = world.get("mission4", {})
        if not isinstance(search, Mapping):
            search = {}
        all_found = search.get("status") == "completed" and search.get("reason") == "all_found"
        terminal = all_found
        if not terminal and mission_mode == "joint24":
            reports = root / "transport/topics/mission4-agent-reports"
            terminal = any(
                _read(path).get("event_kind") == "mission4-agent-report"
                for path in sorted(reports.glob("missions/*/*.json"))
            )
        if not terminal:
            failures.append("mission4_not_all_found")
        if not search.get("observations") or search.get("source") != "simulated":
            failures.append("mission4_fixture_evidence_missing")
        if mission_mode == "joint34":
            m4_failures, mission4_evidence = _joint34_mission4_audit(root, search)
            failures.extend(m4_failures)
        else:
            if len(search.get("requests", ())) < 2:
                failures.append("mission4_requests_missing")
            worker_path = root / "mission4-worker-session.json"
            worker = _read(worker_path) if worker_path.is_file() else {}
            accepted = sum(item.get("kind") == "accepted" for item in worker.get("history", ()))
            if accepted < 2:
                failures.append("mission4_worker_receipts_missing")
        if _contains_private_fixture_key(latest):
            failures.append("private_fixture_data_exposed")

    ingress: dict[str, object] | None = None
    if mission_mode == "joint34":
        if mission4_package is None:
            ingress = {"status": "fail", "failures": ["mission4_package_missing"], "action_ids": []}
            failures.append("m4_dock_ingress_gate_missing")
        else:
            ingress = aoi_trajectory_audit(root, Path(mission4_package))
            if ingress.get("status") != "pass":
                failures.append("m4_dock_ingress_failed")

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
    if mission_mode == "joint34":
        camera_receipt_path = root / "mission3-camera-control-receipts.json"
        camera_control["receipt_path"] = str(camera_receipt_path.resolve())
        camera_receipt_path.write_text(
            json.dumps(camera_control, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        audit["mission3_evidence"] = mission3_evidence
        audit["camera_control"] = camera_control
        audit["mission4_evidence"] = mission4_evidence
        if ingress is not None:
            audit["aoi_trajectory"] = ingress
    if perception_evidence is not None:
        audit["perception_evidence"] = perception_evidence
    if mission_metrics is not None:
        audit["mission_metrics"] = dict(mission_metrics)
    if mission4_answer_metrics is not None:
        audit["mission4_answer_metrics"] = dict(mission4_answer_metrics)
    (root / "live-acceptance.json").write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return audit


__all__ = ["audit_live_demo"]
