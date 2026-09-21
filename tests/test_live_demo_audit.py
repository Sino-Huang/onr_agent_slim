from __future__ import annotations

import json
import os
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
            "final_fsm_state": "joint34-complete" if mode == "joint34" else "complete",
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
    elif mode == "joint34":
        section = {
            "mission_end_time_s": 10.0,
            "mission4": {
                "status": "completed",
                "reason": "all_found",
                "requests": [{}, {}],
                "observations": [{}],
                "source": "simulated",
            },
            "mission3": {
                "schema_version": 1,
                "selected_ship_ids": [4, 5, 6],
                "target_observations": [
                    {"ship_id": 4, "sampled_at_s": 5.0, "age_s": 0.0}
                ],
                "ships": [
                    {"ship_id": ship_id, "resolution": {"status": "unresolved"}}
                    for ship_id in (4, 5, 6)
                ],
            },
        }
        write(
            root / "mission4-worker-session.json",
            {"history": [{"kind": "accepted"}, {"kind": "accepted"}]},
        )
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
    if mode == "joint34":
        write(
            log / "3.json",
            {
                "source": "fsm-runner",
                "event_kind": "fsm",
                "outcome": "transitioned",
                "details": {"state": "mission3-block"},
            },
        )
        write(
            root
            / "transport/topics/mission3-agent-reports/missions/mission%3Ademo/1.json",
            {"event_kind": "mission3-agent-report", "payload": {"reason": "mission_budget"}},
        )


@pytest.mark.parametrize("mode", ["mission2", "mission3", "mission4", "joint34"])
def test_live_demo_audit_accepts_terminal_mission_receipts(tmp_path: Path, mode: str) -> None:
    run_tree(tmp_path, mode)
    result = audit_live_demo(tmp_path, mode)
    assert result["status"] == "PASS"
    assert json.loads((tmp_path / "live-acceptance.json").read_text())["status"] == "PASS"


def test_joint34_audit_rejects_premature_terminal(tmp_path: Path) -> None:
    run_tree(tmp_path, "joint34")
    result_path = tmp_path / "closed-loop-result.json"
    result = json.loads(result_path.read_text())
    result["simulated_duration_seconds"] = 5.0
    write(result_path, result)
    audit = audit_live_demo(tmp_path, "joint34")
    assert "mission3_stopped_before_budget" in audit["failures"]


def test_live_demo_audit_joint34_requires_block_and_report(tmp_path: Path) -> None:
    run_tree(tmp_path, "joint34")
    oplog = tmp_path / "agent-storage/operational-log/mission:demo/events/3.json"
    oplog.unlink()
    trigger = (
        tmp_path
        / "transport/topics/mission3-agent-reports/missions/mission%3Ademo/1.json"
    )
    trigger.unlink()
    write(
        tmp_path / "transport/topics/hyper-heartbeat-outcomes/missions/mission%3Ademo/1.json",
        {"trigger_identities": ["mission3-gate:mission_budget:report:none"]},
    )
    audit = audit_live_demo(tmp_path, "joint34")
    assert audit["status"] == "FAIL"
    assert set(audit["failures"]) >= {
        "mission3_block_not_entered",
        "mission3_report_evidence_missing",
    }


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


def _evaluator_stub(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def _run_audit_cli(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "ONR_MISSION4_EVALUATOR": str(tmp_path / "evaluator.py"),
    }
    return subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts/audit_live_demo.py"),
            "--run-root",
            str(tmp_path),
            *extra,
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
        env=env,
    )


def test_mission4_live_demo_audit_embeds_answer_metrics(tmp_path: Path) -> None:
    run_tree(tmp_path, "mission4")
    _evaluator_stub(
        tmp_path / "evaluator.py",
        "import json, pathlib, sys\n"
        "run_root = pathlib.Path(sys.argv[sys.argv.index('--run-root') + 1])\n"
        "(run_root / 'mission4-answer-metrics.json').write_text("
        "json.dumps({'tasks': [], 'aggregate': {'tasks_total': 2}}))\n",
    )
    result = _run_audit_cli(
        tmp_path,
        "--mission-mode",
        "mission4",
        "--mission4-answers",
        str(tmp_path / "answers.json"),
    )
    assert result.returncode == 0, result.stderr
    acceptance = json.loads((tmp_path / "live-acceptance.json").read_text())
    assert acceptance["mission4_answer_metrics"] == {
        "tasks": [],
        "aggregate": {"tasks_total": 2},
    }
    assert acceptance["status"] == "PASS"


def test_mission4_live_demo_audit_records_evaluator_error_informationally(
    tmp_path: Path,
) -> None:
    run_tree(tmp_path, "mission4")
    _evaluator_stub(tmp_path / "evaluator.py", "import sys\nsys.exit('boom')\n")
    result = _run_audit_cli(
        tmp_path,
        "--mission-mode",
        "mission4",
        "--mission4-answers",
        str(tmp_path / "answers.json"),
    )
    assert result.returncode == 0, result.stderr
    acceptance = json.loads((tmp_path / "live-acceptance.json").read_text())
    assert "error" in acceptance["mission4_answer_metrics"]
    assert acceptance["status"] == "PASS"


def test_mission4_answers_flag_requires_mission4_mode(tmp_path: Path) -> None:
    run_tree(tmp_path, "mission3")
    result = _run_audit_cli(
        tmp_path,
        "--mission-mode",
        "mission3",
        "--mission4-answers",
        str(tmp_path / "answers.json"),
    )
    assert result.returncode == 2
    assert "--mission4-answers" in result.stderr
