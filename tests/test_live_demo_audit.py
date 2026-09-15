from __future__ import annotations

import json
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
