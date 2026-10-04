"""The run history list (``GET /api/v1/mission-runs``, #76 U10)."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from onr.runtime_host import RuntimeHost, create_app
from tests.test_runtime_host_operator_view import MutableEvidence, _config, _ids

EXAMPLES = (
    Path(__file__).resolve().parents[1] / "docs/design/operator-console/contract/v1.5"
)
OWNER = {"Authorization": "Bearer console-secret"}


def _ticking_clock() -> Callable[[], str]:
    """Each call is one second after the previous one."""

    ticks = iter(range(1_000_000))
    origin = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    return lambda: (origin + timedelta(seconds=next(ticks))).isoformat()


def _host(tmp_path: Path, clock: Callable[[], str]) -> tuple[TestClient, RuntimeHost]:
    pending: list[Callable[[], None]] = []
    host = RuntimeHost(
        _config(tmp_path, debug=True),
        clock=clock,
        generate_id=_ids(),
        launch_worker=pending.append,
        evidence_source=MutableEvidence(),
    )
    return TestClient(create_app(host=host), client=("127.0.0.1", 50000)), host


def _launch(client: TestClient, host: RuntimeHost, number: int, final: str) -> str:
    """Activate run-<number> with a secret-bearing intent and end it as ``final``."""

    response = client.post(
        "/api/v1/mission-activations",
        headers=OWNER,
        json={
            "activation_request_id": f"request-{number}",
            "console_session_id": "session-1",
            "mission_intent": f"Survey sector {number} with secret-intent-{number}",
            "source_authority": "operator_console",
        },
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["mission_run_id"]
    host._transition(run_id, "running")
    if final != "running":
        host._transition(
            run_id,
            final,
            terminal_classification="cancelled_by_owner"
            if final == "cancelled"
            else None,
        )
    return run_id


def _history(client: TestClient, query: str = "") -> dict:
    response = client.get(f"/api/v1/mission-runs{query}")
    assert response.status_code == 200, response.text
    return response.json()


def _ids_of(page: dict) -> list[str]:
    return [row["mission_run"]["mission_run_id"] for row in page["mission_runs"]]


def test_pages_are_newest_first_and_before_is_a_stable_cursor(tmp_path: Path) -> None:
    client, host = _host(tmp_path, _ticking_clock())
    for number in range(1, 6):
        _launch(client, host, number, "succeeded")

    first = _history(client, "?limit=2")
    assert _ids_of(first) == ["run-5", "run-4"]
    assert first["next_before"] == "run-4"

    # A newer activation never shifts or repeats an older page.
    _launch(client, host, 6, "running")
    second = _history(client, "?limit=2&before=run-4")
    assert _ids_of(second) == ["run-3", "run-2"]
    third = _history(client, f"?limit=2&before={second['next_before']}")
    assert _ids_of(third) == ["run-1"]
    assert third["next_before"] is None
    assert _ids_of(_history(client))[:2] == ["run-6", "run-5"]
    assert _history(client, "?before=run-1") == {
        "mission_runs": [],
        "next_before": None,
    }


def test_equal_start_times_order_by_run_id(tmp_path: Path) -> None:
    fixed = datetime(2026, 8, 27, 12, 0, tzinfo=UTC).isoformat()
    client, host = _host(tmp_path, lambda: fixed)
    for number in range(1, 4):
        _launch(client, host, number, "failed")
    assert _ids_of(_history(client)) == ["run-3", "run-2", "run-1"]
    assert _ids_of(_history(client, "?limit=1&before=run-3")) == ["run-2"]


def test_rows_carry_the_public_record_toggles_duration_and_root(
    tmp_path: Path,
) -> None:
    client, host = _host(tmp_path, _ticking_clock())
    _launch(client, host, 1, "cancelled")
    _launch(client, host, 2, "running")

    rows = {
        row["mission_run"]["mission_run_id"]: row
        for row in _history(client)["mission_runs"]
    }
    finished = rows["run-1"]
    assert finished["mission_run"] == {
        "mission_id": "mission-1",
        "mission_run_id": "run-1",
        "status": "cancelled",
        "created_at": "2026-08-27T12:00:01+00:00",
        "started_at": "2026-08-27T12:00:02+00:00",
        "finished_at": "2026-08-27T12:00:03+00:00",
        "terminal_classification": "cancelled_by_owner",
        "stack": {"preset_id": "mission1-harbor", "airsim": False, "perception": "off"},
        "terminal_detail": None,
    }
    assert finished["toggles"] == {
        "update_ownership": "coordinator_driven",
        "simulation_limit_seconds": 600.0,
    }
    assert finished["wall_seconds"] == 1.0
    assert finished["run_root_available"] is True
    assert finished["current"] is False
    running = rows["run-2"]
    assert running["mission_run"]["status"] == "running"
    assert running["wall_seconds"] is None
    assert running["current"] is True


def test_history_never_includes_the_mission_intent(tmp_path: Path) -> None:
    client, host = _host(tmp_path, _ticking_clock())
    for number in range(1, 4):
        _launch(client, host, number, "succeeded")
    text = client.get("/api/v1/mission-runs").text
    assert "secret-intent" not in text
    assert "Survey sector" not in text
    assert "mission_intent" not in text
    # The intent stays behind the owner credential of each run.
    intent = client.get("/api/v1/mission-runs/run-1/mission-intent", headers=OWNER)
    assert intent.json()["mission_intent"] == "Survey sector 1 with secret-intent-1"
    stranger = {"Authorization": "Bearer someone-else"}
    assert (
        client.get("/api/v1/mission-runs/run-1/mission-intent", headers=stranger)
    ).status_code == 403


def test_a_historical_run_opens_through_the_per_run_routes(tmp_path: Path) -> None:
    client, host = _host(tmp_path, _ticking_clock())
    _launch(client, host, 1, "succeeded")
    _launch(client, host, 2, "running")
    overview = client.get("/api/v1/mission-runs/run-1/operator-view?section=overview")
    assert overview.status_code == 200
    assert overview.json()["mission_run_id"] == "run-1"
    assert overview.json()["run_status"] == "succeeded"
    stack = client.get("/api/v1/mission-runs/run-1/operator-view?section=stack")
    assert stack.status_code == 200
    # Reading the old run leaves the current one current.
    current = client.get("/api/v1/mission-runs/current").json()["mission_run"]
    assert current["mission_run_id"] == "run-2"


def test_a_missing_run_root_is_marked_and_opening_it_says_so(tmp_path: Path) -> None:
    client, host = _host(tmp_path, _ticking_clock())
    _launch(client, host, 1, "failed")
    _launch(client, host, 2, "succeeded")
    shutil.rmtree(host._runs_root / "run-1")

    rows = {
        row["mission_run"]["mission_run_id"]: row
        for row in _history(client)["mission_runs"]
    }
    assert rows["run-1"]["run_root_available"] is False
    assert rows["run-2"]["run_root_available"] is True

    for path in (
        "/api/v1/mission-runs/run-1/operator-view?section=overview",
        "/api/v1/mission-runs/run-1/world-frame?source=world",
        "/api/v1/mission-runs/run-1/artifacts",
        "/api/v1/mission-runs/run-1/artifacts/worker-log/content",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "run_root_unavailable", path
        assert "Run Root" in response.json()["error"]["message"]
    unknown = client.get("/api/v1/mission-runs/run-9/operator-view?section=overview")
    assert unknown.json()["error"]["code"] == "mission_run_not_found"


def test_history_is_loopback_only_and_its_query_is_strict(tmp_path: Path) -> None:
    client, host = _host(tmp_path, _ticking_clock())
    _launch(client, host, 1, "succeeded")
    remote = TestClient(create_app(host=host), client=("192.0.2.7", 50000))
    assert remote.get("/api/v1/mission-runs").status_code == 403

    for query in ("?limit=0", "?limit=101", "?limit=01", "?limit=x", "?before="):
        response = client.get(f"/api/v1/mission-runs{query}")
        assert response.status_code == 422, query
        assert response.json()["error"]["code"] == "invalid_request"
    assert client.get("/api/v1/mission-runs?cursor=a").status_code == 422
    assert client.get("/api/v1/mission-runs?limit=1&limit=2").status_code == 422
    unknown = client.get("/api/v1/mission-runs?before=run-9")
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "invalid_cursor"
    assert len(_history(client, "?limit=100")["mission_runs"]) == 1


def test_contract_example_matches_the_host_shape(tmp_path: Path) -> None:
    client, host = _host(tmp_path, _ticking_clock())
    _launch(client, host, 1, "succeeded")
    _launch(client, host, 2, "running")
    page = _history(client, "?limit=1")
    example = json.loads((EXAMPLES / "mission-runs.response.json").read_text())
    assert set(page) == set(example)
    row, sample = page["mission_runs"][0], example["mission_runs"][0]
    assert set(row) == set(sample)
    assert set(row["mission_run"]) == set(sample["mission_run"])
    assert set(row["toggles"]) == set(sample["toggles"])
    assert any(not item["run_root_available"] for item in example["mission_runs"])
