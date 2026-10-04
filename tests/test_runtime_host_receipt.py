"""The terminal receipt (``overview.receipt``) and its owner export (#76 U9)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from onr.contracts.transport import TransportEvent
from onr.runtime_host import create_app
from tests.test_runtime_host_operator_view import _client, _view

EXAMPLES = (
    Path(__file__).resolve().parents[1] / "docs/design/operator-console/contract/v1.5"
)
OWNER = {"Authorization": "Bearer console-secret"}


def _fsm_status(sequence: int, state: str, plan_revision: int) -> dict[str, object]:
    return TransportEvent(
        1,
        f"fsm-status-{sequence}",
        "mission-1",
        sequence,
        "fsm-status",
        {"active_state": state, "plan_revision": plan_revision, "status": "updated"},
    ).to_dict()


def _environment(config, mission_time: float) -> None:
    fake = config.environment_profile.fake
    path = fake.artifact_root / "mission-1" / "environment.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "scene_graph": {
                    "mission_time_seconds": mission_time,
                    "current_maneuver": {"maneuver_id": "m-7", "lifecycle": "active"},
                    "drone": {
                        "position": {"x": 1, "y": 2, "z": -3},
                        "velocity": {"x": 0, "y": 0, "z": 0},
                    },
                },
                "perceptions": [],
            }
        ),
        encoding="utf-8",
    )


def _end(host, status: str, classification: str | None = None) -> None:
    host._transition("run-1", "running")
    host._transition("run-1", status, terminal_classification=classification)


def _audit(host, status: str, failures: list[str]) -> Path:
    path = host._runs_root / "run-1" / "live-acceptance.json"
    path.write_text(
        json.dumps(
            {
                "status": status,
                "mission_mode": "mission1",
                "failures": failures,
                "run_root": str(path.parent),
            }
        ),
        encoding="utf-8",
    )
    return path


def _receipt(client: TestClient) -> dict[str, Any]:
    return _view(client, "overview").json()["overview"]["receipt"]


def test_running_runs_have_no_receipt(tmp_path: Path) -> None:
    client, host, _, _ = _client(tmp_path)
    host._transition("run-1", "running")
    assert "receipt" not in _view(client, "overview").json()["overview"]


def test_succeeded_lifecycle_without_an_audit_artifact_is_not_a_verdict(
    tmp_path: Path,
) -> None:
    client, host, evidence, config = _client(tmp_path)
    evidence.items.extend(
        [_fsm_status(1, "pursue", 2), _fsm_status(2, "mission-complete", 3)]
    )
    _environment(config, 120.5)
    _end(host, "succeeded")

    receipt = _receipt(client)
    assert receipt == {
        "status": "succeeded",
        "classification": None,
        "wall_seconds": 0.0,
        "final": {
            "fsm_state": "mission-complete",
            "plan_revision": 3,
            "mission_time_seconds": 120.5,
            "source": "fsm-status",
        },
        "audit": {
            "status": "not_recorded",
            "path": None,
            "recorded_at": None,
            "mission_mode": None,
            "failures": [],
        },
        "export": {
            "path": str(host._runs_root / "run-1" / "mission-run-receipt.json"),
            "exported_at": None,
        },
    }
    # The receipt never turns the lifecycle into a mission verdict.
    assert "pass" not in json.dumps(receipt["audit"]).lower()


def test_physical_runtime_live_environment_gives_final_mission_time(
    tmp_path: Path,
) -> None:
    """The physical runtime writes ``<mission>/live/latest.json``, not
    ``environment.json``; the receipt and Overview still read it."""

    client, host, evidence, config = _client(tmp_path)
    evidence.items.append(_fsm_status(1, "assignment-1-in-progress", 2))
    fake = config.environment_profile.fake
    assert fake is not None
    path = fake.artifact_root / "mission-1" / "live"
    path.mkdir(parents=True)
    (path / "latest.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "mission_id": "mission-1",
                "mission_time_seconds": 187.0,
                "controlled_vehicle": {
                    "position": {"x": 1190.0, "y": -485.0, "z": -25.0},
                    "speed_mps": 30.0,
                },
                "maneuver_lifecycle": {
                    "action": "pursue",
                    "lifecycle": "active",
                    "maneuver_id": "assignment-pursuit:candidate-1",
                },
                "world_model_info": {},
            }
        ),
        encoding="utf-8",
    )
    _end(host, "cancelled", "cancelled_by_owner")

    overview = _view(client, "overview").json()["overview"]
    assert overview["receipt"]["final"] == {
        "fsm_state": "assignment-1-in-progress",
        "plan_revision": 2,
        "mission_time_seconds": 187.0,
        "source": "fsm-status",
    }
    assert overview["environment"]["mission_time_seconds"] == 187.0
    assert overview["active_maneuver"]["action"] == "pursue"


def test_audit_comes_only_from_the_audit_artifact(tmp_path: Path) -> None:
    client, host, _, _ = _client(tmp_path)
    _end(host, "succeeded")
    _audit(host, "FAIL", ["fsm_not_terminal", "evidence_replan_not_observed"])
    audit = _receipt(client)["audit"]
    assert (audit["status"], audit["path"], audit["failures"]) == (
        "fail",
        "live-acceptance.json",
        ["fsm_not_terminal", "evidence_replan_not_observed"],
    )
    assert audit["mission_mode"] == "mission1"
    assert audit["recorded_at"] is not None

    _audit(host, "PASS", [])
    assert _receipt(client)["audit"]["status"] == "pass"

    (host._runs_root / "run-1" / "live-acceptance.json").write_text("{}")
    assert _receipt(client)["audit"]["status"] == "unreadable"


def test_a_failed_or_cancelled_lifecycle_keeps_its_audit_and_classification(
    tmp_path: Path,
) -> None:
    client, host, evidence, _ = _client(tmp_path)
    evidence.items.append(_fsm_status(1, "pursue", 2))
    _end(host, "failed", "worker_failed")
    _audit(host, "PASS", [])
    receipt = _receipt(client)
    assert (receipt["status"], receipt["classification"]) == ("failed", "worker_failed")
    assert receipt["final"]["fsm_state"] == "pursue"
    assert receipt["final"]["mission_time_seconds"] is None
    assert receipt["audit"]["status"] == "pass"


def test_owner_export_writes_the_receipt_under_the_run_root(tmp_path: Path) -> None:
    client, host, evidence, config = _client(tmp_path)
    evidence.items.append(_fsm_status(1, "mission-complete", 3))
    _environment(config, 60.0)
    run_root = host._runs_root / "run-1"
    url = "/api/v1/mission-runs/run-1/receipt-exports"

    host._transition("run-1", "running")
    busy = client.post(url, json={}, headers=OWNER)
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "mission_run_not_terminal"
    host._transition("run-1", "succeeded")

    assert client.post(url, json={}).status_code == 403
    stranger = {"Authorization": "Bearer someone-else"}
    assert client.post(url, json={}, headers=stranger).status_code == 403
    assert client.post(url, json={"path": "/etc"}, headers=OWNER).status_code == 422
    remote = TestClient(create_app(host=host), client=("192.0.2.7", 50000))
    assert remote.post(url, json={}, headers=OWNER).status_code == 403

    first = client.post(url, json={}, headers=OWNER)
    assert first.status_code == 200
    path = run_root / "mission-run-receipt.json"
    assert first.json() == {
        "mission_run_id": "run-1",
        "path": str(path),
        "exported_at": "2026-08-27T12:00:00+00:00",
        "byte_size": path.stat().st_size,
        "replaced": False,
    }
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["kind"] == "mission_run_receipt"
    assert document["run_root"] == str(run_root)
    assert document["receipt"]["final"]["fsm_state"] == "mission-complete"
    assert document["receipt"]["audit"]["status"] == "not_recorded"
    assert "export" not in document["receipt"]
    assert all((run_root / item["path"]).is_file() for item in document["artifacts"])
    assert all(
        not Path(str(item["path"])).is_absolute() for item in document["artifacts"]
    )
    assert _receipt(client)["export"]["exported_at"] == "2026-08-27T12:00:00+00:00"

    # Exporting again overwrites the same file and says so.
    second = client.post(url, json={}, headers=OWNER)
    assert second.json()["path"] == str(path)
    assert second.json()["replaced"] is True
    assert not list(run_root.glob(".mission-run-receipt.json*"))


def test_export_references_run_root_logs_and_evidence(tmp_path: Path) -> None:
    client, host, _, _ = _client(tmp_path)
    run_root = host._runs_root / "run-1"
    (run_root / "worker.log").write_text("worker\n")
    (run_root / "services" / "physical-runtime.log").write_text("ready\n")
    (run_root / "closed-loop-result.json").write_text("{}\n")
    _audit(host, "PASS", [])
    _end(host, "succeeded")
    client.post("/api/v1/mission-runs/run-1/receipt-exports", json={}, headers=OWNER)
    document = json.loads((run_root / "mission-run-receipt.json").read_text())
    references = {
        (item["role"], item["path"], item["artifact_id"])
        for item in document["artifacts"]
    }
    assert {
        ("closed_loop_result", "closed-loop-result.json", None),
        ("live_demo_audit", "live-acceptance.json", None),
        ("log", "worker.log", "worker-log"),
        ("log", "services/physical-runtime.log", "service-log-physical-runtime"),
    } <= references


def test_contract_examples_match_the_host_shapes(tmp_path: Path) -> None:
    client, host, evidence, config = _client(tmp_path)
    evidence.items.append(_fsm_status(1, "mission-complete", 3))
    _environment(config, 60.0)
    _end(host, "succeeded")
    receipt = _receipt(client)
    example = json.loads(
        (EXAMPLES / "mission-run-operator-overview.succeeded.response.json").read_text()
    )["overview"]["receipt"]
    assert set(receipt) == set(example)
    for key in ("final", "audit", "export"):
        assert set(receipt[key]) == set(example[key]), key

    exported = client.post(
        "/api/v1/mission-runs/run-1/receipt-exports", json={}, headers=OWNER
    ).json()
    response = json.loads(
        (EXAMPLES / "mission-run-receipt-export.response.json").read_text()
    )
    assert set(exported) == set(response)
    document = json.loads(
        (host._runs_root / "run-1" / "mission-run-receipt.json").read_text()
    )
    file_example = json.loads((EXAMPLES / "mission-run-receipt.json").read_text())
    assert set(document) == set(file_example)
    assert set(document["stack"]) == set(file_example["stack"])
