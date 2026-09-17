from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "replay_mission2_replan_gate.py"
SPEC = importlib.util.spec_from_file_location("replay_mission2_replan_gate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
replay = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = replay
SPEC.loader.exec_module(replay)


def _write_event(root: Path, topic: str, event: dict, name: str = "000.json") -> None:
    path = root / "transport" / "topics" / topic / "missions" / "mission" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(event), encoding="utf-8")


def _environment_event(
    *,
    event_id: str,
    sequence: int,
    mission_time: float,
    state_version: int,
    prediction_sequence: int,
    pair: tuple[int, int] | None = (1, 2),
    alert_id: str | None = "run:near_collision:1:2",
    feasible: bool = True,
) -> dict:
    active_pairs = []
    if pair is not None:
        active_pairs.append(
            {
                "ship_ids": list(pair),
                "predicted_contact_at_s": mission_time + 4.0,
                "position": {"x": 0.0, "y": 0.0},
                "probability": 0.5,
            }
        )
    alerts = []
    if alert_id is not None and pair is not None:
        alerts.append({"event_id": alert_id, "ship_ids": list(pair)})
    point = {
        "time_s": mission_time + 1.0,
        "position": {"x": 0.0, "y": 0.0, "z": -25.0},
    }
    trajectories = {
        str(ship): {
            "ready": feasible,
            "sampled_at_s": mission_time,
            "points": [point] if feasible else [],
        }
        for ship in (pair or ())
    }
    payload = {
        "schema_version": 2,
        "mission_id": "mission:test",
        "mission_epoch": "test",
        "mission_time_seconds": mission_time,
        "observation_time_seconds": mission_time,
        "state_version": state_version,
        "controlled_vehicle": {
            "entity_id": "drone-1",
            "position": {"x": 0.0, "y": 0.0, "z": -25.0},
            "max_velocity": 30.0,
            "fov_radius": 300.0,
        },
        "maneuver_lifecycle": None,
        "world_model_info": {
            "mission_mode": "mission2",
            "mission_end_time_s": 20.0,
            "visible_ship_ids": [],
            "perception_predictions": {
                "schema_version": 1,
                "run_id": "run",
                "sequence": prediction_sequence,
                "source": "synthetic",
                "status": "ready",
                "valid_until_s": 20.0,
                "active_pairs": active_pairs,
                "alerts": alerts,
                "trajectories": trajectories,
            },
        },
    }
    return {
        "schema_version": 1,
        "event_id": event_id,
        "mission_id": "mission:test",
        "sequence": sequence,
        "event_kind": "environment_data",
        "payload": payload,
    }


def _fsm_event(event_id: str = "fsm:1") -> dict:
    return {
        "schema_version": 1,
        "event_id": event_id,
        "mission_id": "mission:test",
        "sequence": 1,
        "event_kind": "fsm-status",
        "payload": {
            "schema_version": 1,
            "mission_id": "mission:test",
            "plan_revision": 1,
            "statechart_revision": 1,
            "active_state": "collision-monitoring-active",
            "transition_candidates": [],
            "status": "ready",
            "superseded_plan_revision": None,
            "last_applied_event": None,
            "active_state_context": {},
            "state_entry_revision": 1,
        },
    }


def _snapshot_event(environment_id: str, fsm_id: str = "fsm:1") -> dict:
    references = {
        "plan": "planner-plan:1",
        "environment_data": environment_id,
        "bayesian_belief_snapshot": None,
        "fsm_status": fsm_id,
        "active_maneuver": None,
    }
    return {
        "schema_version": 1,
        "event_id": "snapshot:1",
        "mission_id": "mission:test",
        "sequence": 1,
        "event_kind": "mission-snapshot",
        "payload": {
            "schema_version": 1,
            "mission_id": "mission:test",
            "version": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "plan_revision": 1,
            "plan_reference": "planner-plan:1",
            "environment_data": environment_id,
            "bayesian_belief_snapshot": None,
            "fsm_status": fsm_id,
            "active_maneuver": None,
            "source_revisions": {
                "plan": 1,
                "environment_data": 1,
                "bayesian_belief_snapshot": None,
                "fsm_status": 1,
                "active_maneuver": None,
            },
            "source_references": references,
            "source_health": {
                "plan": "healthy",
                "environment_data": "healthy",
                "bayesian_belief_snapshot": "missing",
                "fsm_status": "healthy",
                "active_maneuver": "missing",
            },
            "source_freshness": {
                "plan": True,
                "environment_data": True,
                "bayesian_belief_snapshot": False,
                "fsm_status": True,
                "active_maneuver": False,
            },
            "missing_sources": ["bayesian_belief_snapshot", "active_maneuver"],
        },
    }


def _outcome_event(alert_pair: tuple[int, int] = (1, 2)) -> dict:
    return {
        "schema_version": 1,
        "event_id": "outcome:1",
        "mission_id": "mission:test",
        "sequence": 1,
        "event_kind": "hyper-heartbeat-decision",
        "payload": {
            "mission_id": "mission:test",
            "plan_revision": 1,
            "disposition": "replan",
            "evidence_summary": f"synthetic incident {alert_pair}",
            "request_identities": [],
            "trigger_identities": ["mission2-gate:risk_changed:run:1"],
        },
    }


def _build_run(root: Path, *, feasible: bool = True, outcome_pair: tuple[int, int] = (1, 2)) -> Path:
    environment = _environment_event(
        event_id="environment:1",
        sequence=1,
        mission_time=1.0,
        state_version=1,
        prediction_sequence=1,
        pair=(1, 2),
        alert_id="run:near_collision:1:2" if feasible else None,
        feasible=feasible,
    )
    _write_event(root, "environment-data", environment)
    _write_event(root, "mission-snapshots", _snapshot_event(environment["event_id"]))
    _write_event(root, "hyper-heartbeat-outcomes", _outcome_event(outcome_pair))
    identity = root / "transport" / "identity" / "fsm.json"
    identity.parent.mkdir(parents=True, exist_ok=True)
    identity.write_text(json.dumps(_fsm_event()), encoding="utf-8")
    statechart = root / "planner-artifacts" / "revision-001" / "workspace" / "001" / "statechart.json"
    statechart.parent.mkdir(parents=True, exist_ok=True)
    statechart.write_text(
        json.dumps(
            {
                "entry_state": "collision-monitoring-active",
                "terminal_states": ["scenario-recording-end"],
                "states": ["collision-monitoring-active", "scenario-recording-end"],
                "state_context": {},
                "transitions": [],
            }
        ),
        encoding="utf-8",
    )
    (root / "closed-loop-result.json").write_text(
        json.dumps({"tick_count": 1, "hyper_heartbeat_count": 1}), encoding="utf-8"
    )
    return root


def test_reference_resolution_and_trigger_report_schema(tmp_path: Path) -> None:
    report = replay.replay_run(_build_run(tmp_path / "run"))

    assert report["recorded"] == {"ticks": 1, "supervisor_invocations": 1, "replans": 1}
    assert len(report["preserved_replan_incidents"]) == 1
    assert report["missing_replan_incidents"] == []
    trigger = report["replayed"]["triggers"][0]
    assert {
        "mission_time_seconds",
        "prediction_sequence",
        "plan_revision",
        "risk_revision",
        "reason",
        "affected_pairs",
        "fresh_alert_ids",
        "current_candidate_id",
        "advisory_candidate_ids",
        "dedup_signature",
    } <= trigger.keys()
    assert trigger["fresh_alert_ids"] == ["run:near_collision:1:2"]
    assert trigger["dedup_signature"][0] == trigger["plan_revision"]


def test_environment_ordering_and_dedup_uses_latest_duplicate() -> None:
    late = _environment_event(
        event_id="late", sequence=4, mission_time=2.0, state_version=2, prediction_sequence=2
    )
    duplicate_old = _environment_event(
        event_id="old", sequence=1, mission_time=1.0, state_version=1, prediction_sequence=1
    )
    duplicate_new = _environment_event(
        event_id="new", sequence=2, mission_time=1.0, state_version=1, prediction_sequence=1
    )

    ordered = replay.ordered_environment_events([late, duplicate_new, duplicate_old])

    assert [event["event_id"] for event in ordered] == ["new", "late"]


def test_main_exit_status_zero_when_incident_is_preserved(
    tmp_path: Path, capsys
) -> None:
    root = _build_run(tmp_path / "passing")

    assert replay.main(["--run-root", str(root)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert len(report["preserved_replan_incidents"]) == 1
    assert report["missing_replan_incidents"] == []


def test_main_exit_status_nonzero_when_observed_incident_is_missing(
    tmp_path: Path, capsys
) -> None:
    root = _build_run(tmp_path / "missing", feasible=False)

    assert replay.main(["--run-root", str(root)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["preserved_replan_incidents"] == []
    assert len(report["missing_replan_incidents"]) == 1
