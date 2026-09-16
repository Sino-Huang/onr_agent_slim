from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from onr.application.live_demo_audit import audit_live_demo


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def run_tree(root: Path, mode: str) -> None:
    write(
        root / "closed-loop-result.json",
        {
            "terminal": True,
            "simulated_duration_seconds": 10.0,
            "physical_actions": ["navigate"],
            "feedback_count": 2,
            "plan_revisions": [1, 2],
            "inference_windows": [
                {
                    "evidence_time_seconds": 1.0,
                    "completion_time_seconds": 1.0,
                }
            ],
        },
    )
    if mode == "mission2":
        section = {
            "mission_end_time_s": 10.0,
            "perception_predictions": {"status": "ready", "sequence": 2},
        }
    elif mode == "mission3":
        section = {
            "mission3": {
                "ships": [{"resolution": {"status": "resolved"}}],
                "evidence": [{"source": "simulated"}],
            }
        }
    else:
        section = {
            "mission4": {
                "status": "completed",
                "reason": "all_found",
                "requests": [{}, {}],
                "observations": [{}],
                "source": "simulated",
            }
        }
        write(
            root / "mission4-worker-session.json",
            {"history": [{"kind": "accepted"}, {"kind": "accepted"}]},
        )
    section["mission_mode"] = mode
    stream = root / "transport/topics/environment-data/missions/mission%3Ademo"
    for sequence in (1, 2):
        value = json.loads(json.dumps(section))
        if mode == "mission2":
            value["perception_predictions"]["sequence"] = sequence
        write(
            stream / f"{sequence:020d}-event.json",
            {"event_kind": "environment_data", "payload": {
                "mission_time_seconds": float(sequence),
                "state_version": sequence,
                "world_model_info": value,
            }},
        )
    log = root / "agent-storage/operational-log/mission:demo/events"
    write(
        log / "1.json",
        {"source": "hyper-agent", "event_kind": "workflow", "outcome": "completed"},
    )
    write(
        log / "2.json",
        {"source": "maneuver-control", "event_kind": "heartbeat", "outcome": "completed"},
    )
    write(
        root / "debug/agent/hyper-agent/mission%3Ademo/1.json",
        {"kind": "llm", "completion_state": "complete", "error": None},
    )


@pytest.mark.parametrize("mode", ["mission2", "mission3", "mission4"])
def test_live_demo_audit_accepts_terminal_mission_receipts(tmp_path: Path, mode: str) -> None:
    run_tree(tmp_path, mode)
    result = audit_live_demo(tmp_path, mode)
    assert result["status"] == "PASS"
    assert json.loads((tmp_path / "live-acceptance.json").read_text())["status"] == "PASS"


def test_live_demo_audit_records_failures(tmp_path: Path) -> None:
    run_tree(tmp_path, "mission2")
    result = json.loads((tmp_path / "closed-loop-result.json").read_text())
    result["terminal"] = False
    write(tmp_path / "closed-loop-result.json", result)
    write(
        tmp_path / "transport/subscriptions/context/dead-letter/bad.json",
        {"failed": True},
    )
    audit = audit_live_demo(tmp_path, "mission2")
    assert audit["status"] == "FAIL"
    assert set(audit["failures"]) >= {"fsm_not_terminal", "transport_dead_letter_recorded"}


def test_mission2_live_demo_audit_persists_collision_metrics(tmp_path: Path) -> None:
    run_tree(tmp_path, "mission2")
    scenario = tmp_path / "collision" / "0"
    ships = scenario / "ships"
    ships.mkdir(parents=True)
    for ship_id, start, end, heading in ((1, 0, 20, 0), (2, 30, 10, 180)):
        write(ships / f"{ship_id}.json", {
            "id": ship_id,
            "objectId": ship_id,
            "mesh": {
                "MinBounds": {"X": -100, "Y": -100},
                "MaxBounds": {"X": 100, "Y": 100},
            },
            "pose": [
                [start * 100, 0, 0, heading, 0],
                [end * 100, 0, 0, heading, 10],
            ],
        })
    write(tmp_path / "physical-state/observations/00000001-observation.json", {
        "observation_time_s": 5.0,
        "world_model_info": {
            "mission_mode": "mission2",
            "perception_predictions": {
                "run_id": "video-demo",
                "alerts": [
                    {
                        "ship_ids": [1, 2],
                        "event_type": "near_collision",
                        "published_at_s": 4.0,
                        "predicted_contact_at_s": 7.0,
                    },
                    {
                        "ship_ids": [1, 2],
                        "event_type": "collision",
                        "published_at_s": 5.0,
                        "predicted_contact_at_s": 7.0,
                    },
                ],
            },
        },
    })

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts/audit_live_demo.py"),
            "--run-root",
            str(tmp_path),
            "--mission-mode",
            "mission2",
            "--mission2-scenario-dir",
            str(scenario),
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    metrics = json.loads((tmp_path / "mission2-metrics.json").read_text())
    assert metrics["prediction_run_id"] == "video-demo"
    assert metrics["metrics"]["actual_contact"]["pair_recall"] == 1.0
    assert metrics["metrics"]["actual_contact"]["precision"] == 1.0
    acceptance = json.loads((tmp_path / "live-acceptance.json").read_text())
    assert acceptance["mission_metrics"] == metrics


def test_live_demo_audit_orders_content_addressed_events_by_mission_time(
    tmp_path: Path,
) -> None:
    run_tree(tmp_path, "mission3")
    stream = tmp_path / "transport/topics/environment-data/missions/mission%3Ademo"
    first = stream / "00000000000000000001-event.json"
    second = stream / "00000000000000000002-event.json"
    old = json.loads(first.read_text())
    old["payload"]["world_model_info"]["mission3"]["ships"][0]["resolution"][
        "status"
    ] = "pending"
    first.rename(stream / "z-old.json")
    second.rename(stream / "a-final.json")
    write(stream / "z-old.json", old)

    assert audit_live_demo(tmp_path, "mission3")["status"] == "PASS"
