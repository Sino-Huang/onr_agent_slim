from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from threading import Event as ThreadEvent
from threading import current_thread
from time import sleep
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

import onr.runtime_host.host as host_module
from onr.adapters.mission_log_summarizer import FileMissionLogSummarizer
from onr.adapters.operational_log import FileOperationalLog
from onr.runtime import HeartbeatsConfig, StorageConfig, TransportConfig
from onr.runtime.config import load_runtime_config
from onr.runtime_host import (
    RuntimeHost,
    RuntimeWorkerOptions,
    WorkerContext,
    create_app,
)
from onr.runtime_host.run_root import RunRoot
from onr.runtime_host.stack import ServiceSpec, StackFailure, StackSupervisor
from onr.runtime_host.world import WorldView
from tests.test_runtime_host import _clock, _config, _ids, _wait_for

BODY = {
    "activation_request_id": "request-1",
    "console_session_id": "session-1",
    "mission_intent": "Survey sector seven",
    "source_authority": "operator_console",
}
HEADERS = {"Authorization": "Bearer console-secret"}


def setup_host(tmp_path, worker=lambda context: None):
    pending = []
    host = RuntimeHost(
        _config(tmp_path),
        clock=_clock,
        generate_id=_ids(),
        worker_entrypoint=worker,
        launch_worker=pending.append,
    )
    client = TestClient(create_app(host=host), client=("127.0.0.1", 50000))
    return host, client, pending


def activate(client, stack=None):
    body = dict(BODY)
    if stack is not None:
        body["stack"] = stack
    return client.post("/api/v1/mission-activations", json=body, headers=HEADERS)


def run_root(host):
    return RunRoot.for_run(host._runs_root, "run-1")


def current_run(host: RuntimeHost) -> dict[str, Any]:
    """The host's Mission Run record; callers assert on a run that exists."""

    record = host.current_run()
    assert record is not None
    return record


def test_activation_resolved_stack_replay_and_conflicting_options(tmp_path):
    host, client, pending = setup_host(tmp_path)
    first = activate(client, {"preset_id": "mission2", "simulation_limit_seconds": 42})
    assert first.status_code == 202
    assert (
        activate(
            client, {"preset_id": "mission2", "simulation_limit_seconds": 42}
        ).json()
        == first.json()
    )
    conflict = activate(
        client, {"preset_id": "mission2", "simulation_limit_seconds": 43}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "activation_request_conflict"
    state = json.loads(host._state_path.read_text())["runs"]["run-1"]
    assert state["stack_options"]["simulation_limit_seconds"] == 42
    assert state["stack"] == {
        "preset_id": "mission2",
        "airsim": False,
        "perception": "off",
    }
    assert len(pending) == 1


@pytest.mark.parametrize(
    "stack",
    [
        {"preset_id": "missing"},
        {"unknown": True},
        {"airsim": "false"},
        {"perception": "yolo"},
        {"preset_id": "mission1-airsim", "airsim": False, "perception": "off"},
        {"simulation_limit_seconds": True},
        {"simulation_limit_seconds": -1},
        {"preset_id": None},
        {"airsim": None},
        [],
        "mission2",
        None,
    ],
)
def test_invalid_stack_objects_are_rejected(tmp_path, stack):
    _, client, _ = setup_host(tmp_path)
    response = client.post(
        "/api/v1/mission-activations", json={**BODY, "stack": stack}, headers=HEADERS
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    "url",
    [
        "/stack/presets?unknown=1",
        "/stack/preflight?airsim=yes",
        "/stack/preflight?preset_id=mission2&preset_id=mission3",
        "/stack/preflight?simulation_limit_seconds=nan",
        "/stack/preflight?unknown=1",
        "/stack/preflight?perception=radar",
        "/stack/preflight?update_ownership=unknown",
        "/mission-runs/run-1/world-frame?source=unknown",
        "/mission-runs/run-1/world-frame?source=world&source=world",
    ],
)
def test_new_routes_reject_invalid_queries(tmp_path, url):
    _, client, _ = setup_host(tmp_path)
    assert client.get("/api/v1" + url).status_code == 422


def test_repeated_body_options_are_invalid(tmp_path):
    _, client, _ = setup_host(tmp_path)
    body = json.dumps(BODY)[:-1] + ',"stack":{"airsim":false,"airsim":true}}'
    response = client.post(
        "/api/v1/mission-activations",
        content=body,
        headers={**HEADERS, "Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_preflight_receives_active_run_and_presets(tmp_path, monkeypatch):
    _, client, _ = setup_host(tmp_path)
    activate(client)

    def preflight(catalog, preset, options, *, repo_root, active_run):
        return {
            "schema_version": 1,
            "launchable": active_run() is None,
            "checks": [
                {"check_id": "active-run", "status": "fail", "detail": active_run()}
            ],
        }

    monkeypatch.setattr(host_module, "run_preflight", preflight)
    assert (
        client.get("/api/v1/stack/presets").json()["presets"][0]["preset_id"]
        == "mission1-harbor"
    )
    response = client.get("/api/v1/stack/preflight?preset_id=mission2").json()
    assert response["launchable"] is False
    assert response["checks"][0]["detail"] == "run-1"


def test_run_local_evidence_and_service_log_byte_ranges(tmp_path):
    host, client, _ = setup_host(tmp_path)
    activate(client)
    root = run_root(host)
    FileOperationalLog(host.config.storage.root / "operational-log").emit(
        "mission-1", "hyper-agent", "planning-intent", "rejected"
    )
    FileOperationalLog(root.operational_log).emit(
        "mission-1", "hyper-agent", "planning-intent", "completed"
    )
    payload = b"x" * (1024 * 1024 + 20) + "éEND".encode()
    root.service_log("perception").write_bytes(payload)
    root.worker_log.write_text("worker output\n")
    observations = client.get("/api/v1/mission-runs/run-1/observations").json()[
        "observations"
    ]
    assert not any("rejected" in json.dumps(item) for item in observations)
    progress = client.get(
        "/api/v1/mission-runs/run-1/operator-view?section=progress"
    ).json()["progress"]
    assert (
        next(item for item in progress["nodes"] if item["node_id"] == "log:1")[
            "outcome"
        ]
        == "completed"
    )
    artifacts = client.get(
        "/api/v1/mission-runs/run-1/operator-view?section=artifacts"
    ).json()["artifacts"]
    assert {item["artifact_id"] for item in artifacts} == {
        "worker-log",
        "service-log-perception",
    }
    response = client.get(
        f"/api/v1/mission-runs/run-1/artifacts/service-log-perception/content?offset={len(payload) - 4}&limit=4"
    )
    content = response.json()
    assert content["classification"] == "service_log"
    assert content["offset"] == len(payload) - 4
    assert content["content"] == "�END"
    assert content["eof"] is True
    remote = TestClient(create_app(host=host), client=("192.0.2.2", 50000))
    assert (
        remote.get(
            "/api/v1/mission-runs/run-1/operator-view?section=artifacts"
        ).status_code
        == 403
    )
    assert (
        remote.get(
            "/api/v1/mission-runs/run-1/artifacts/worker-log/content"
        ).status_code
        == 403
    )
    root.service_log("linked").symlink_to(root.worker_log)
    assert (
        client.get(
            "/api/v1/mission-runs/run-1/artifacts/service-log-linked/content"
        ).status_code
        == 404
    )


def test_stack_failure_is_durable_and_sanitized(tmp_path):
    def fail(context):
        raise StackFailure("perception", "health not ready " + "x" * 600)

    host, client, pending = setup_host(tmp_path, fail)
    activate(client)
    pending.pop()()
    record = client.get("/api/v1/mission-runs/current").json()["mission_run"]
    assert record["status"] == "failed"
    assert record["terminal_classification"] == "stack_failed"
    assert record["terminal_detail"]["service"] == "perception"
    assert len(record["terminal_detail"]["message"]) == 500
    assert "StackFailure" in run_root(host).worker_log.read_text()


def test_operator_new_sections_and_cursor_boundaries(tmp_path):
    host, client, _ = setup_host(tmp_path)
    activate(client, {"preset_id": "mission2"})
    root = run_root(host)
    log = FileOperationalLog(root.operational_log)
    for _ in range(7):
        log.emit("mission-1", "hyper-agent", "planning-intent", "completed")
    prefix = "/api/v1/mission-runs/run-1/operator-view?section="
    first = client.get(prefix + "progress&limit=2").json()
    assert first["has_more"] and first["before_cursor"]
    nodes = {node["node_id"] for node in first["progress"]["nodes"]}
    before = first["before_cursor"]
    while before:
        page = client.get(prefix + "progress&limit=2&before=" + before).json()
        nodes.update(node["node_id"] for node in page["progress"]["nodes"])
        before = page["before_cursor"]
    assert nodes == {"live", *(f"log:{i}" for i in range(1, 8))}
    assert (
        client.get(prefix + "stack&cursor=" + first["next_cursor"]).status_code == 422
    )
    assert (
        client.get(
            prefix
            + "progress&cursor="
            + first["next_cursor"]
            + "&before="
            + first["next_cursor"]
        ).status_code
        == 422
    )
    assert client.get(prefix + "beliefs").json()["beliefs"]["belief_kind"] is None
    assert "mission_snapshot" in client.get(prefix + "context").json()["context"]
    assert client.get(prefix + "stack").json()["stack"]["preset_id"] == "mission2"
    assert client.get(prefix + "world").json()["world"]["state"] is None
    overview = client.get(prefix + "overview").json()["overview"]
    assert overview["counts_by_importance"]["notable"] == 7
    assert "steps" in overview["phase"]


def test_previous_ready_seconds_are_measured_history_of_the_same_preset(tmp_path):
    host, client, _ = setup_host(tmp_path)
    requests = iter(range(1, 10))

    def launch(preset_id):
        response = client.post(
            "/api/v1/mission-activations",
            json={
                **BODY,
                "activation_request_id": f"request-{next(requests)}",
                "stack": {"preset_id": preset_id},
            },
            headers=HEADERS,
        )
        assert response.status_code == 202
        return response.json()["mission_run_id"]

    def write_status(run_id, services):
        RunRoot.for_run(host._runs_root, run_id).stack_status.write_text(
            json.dumps({"preset_id": "x", "toggles": {}, "services": services})
        )

    def service(name, started_at, ready_at):
        return {
            "name": name,
            "required": True,
            "state": "ready" if ready_at else "failed",
            "started_at": started_at,
            "ready_at": ready_at,
        }

    def history(run_id):
        stack = client.get(
            f"/api/v1/mission-runs/{run_id}/operator-view?section=stack"
        ).json()["stack"]
        return {
            item["name"]: item.get("previous_ready_seconds")
            for item in stack["services"]
        }

    first = launch("mission2")
    write_status(
        first,
        [
            service("perception", "2026-08-24T12:00:00Z", "2026-08-24T12:00:27Z"),
            # Started but never ready: nothing was measured.
            service("physical-runtime", "2026-08-24T12:00:27Z", None),
            # The closed loop has no readiness probe.
            service("closed-loop", "2026-08-24T12:00:30Z", "2026-08-24T12:00:30Z"),
        ],
    )
    host._transition(first, "succeeded")
    other = launch("mission1-harbor")
    write_status(
        other,
        [service("perception", "2026-08-24T12:10:00Z", "2026-08-24T12:10:05Z")],
    )
    assert history(other) == {"perception": None}, "no earlier mission1-harbor run"
    host._transition(other, "succeeded")

    third = launch("mission2")
    write_status(
        third,
        [
            service("perception", "2026-08-24T12:20:00Z", None),
            service("physical-runtime", None, None),
            service("closed-loop", None, None),
        ],
    )
    # From the previous mission2 run, not the newer mission1-harbor one.
    assert history(third) == {
        "perception": 27.0,
        "physical-runtime": None,
        "closed-loop": None,
    }


def test_world_route_proxy_etag_and_final_cache(tmp_path):
    host, client, _ = setup_host(tmp_path)
    activate(client)
    root = run_root(host)
    root.stack_plan.write_text(json.dumps({"viewer_port": 5066}))
    frame_bytes = b"\x89PNG\r\n\x1a\nlast-frame"

    def fetch(url, timeout, maximum):
        if url.endswith("/api/state"):
            return json.dumps(
                {
                    "state_version": 4,
                    "mission_time_seconds": 12.5,
                    "flight_state": "hovering",
                    "active_maneuver": None,
                }
            ).encode()
        if url.endswith("/api/frame"):
            return frame_bytes
        raise OSError("camera unavailable")

    view = WorldView(root.path, fetch=fetch)
    host._world_views["run-1"] = view
    response = client.get("/api/v1/mission-runs/run-1/world-frame")
    assert response.content == frame_bytes
    assert response.headers["x-frame-sequence"] == "4"
    assert response.headers["x-mission-time"] == "12.5"
    cached = client.get(
        "/api/v1/mission-runs/run-1/world-frame",
        headers={"If-None-Match": response.headers["etag"]},
    )
    assert cached.status_code == 304 and cached.content == b""
    assert (
        client.get("/api/v1/mission-runs/run-1/world-frame?source=camera_front").json()[
            "error"
        ]["code"]
        == "frame_unavailable"
    )
    view.capture_final()
    host._transition("run-1", "succeeded")

    def reused_port(*args):
        raise AssertionError("terminal run must never fetch a reused viewer port")

    host._world_views["run-1"] = WorldView(root.path, fetch=reused_port)
    assert client.get("/api/v1/mission-runs/run-1/world-frame").content == frame_bytes
    final = client.get("/api/v1/mission-runs/run-1/operator-view?section=world").json()[
        "world"
    ]
    assert final["state"]["state_version"] == 4


def _detached_worker(context):
    script = (
        "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
    )
    child = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    guardian = subprocess.Popen(
        [sys.executable, "-c", script, "--guard-parent"],
        start_new_session=True,
    )
    (context.run_root.path / "guardian.pid").write_text(str(guardian.pid))
    context.run_root.stack_status.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "preset_id": "mission1-harbor",
                "toggles": {},
                "step": {
                    "name": "mission1-surveillance-views",
                    "stage": "post_ready",
                    "started_at": "2026-08-27T14:00:01Z",
                    "timeout_seconds": 600.0,
                },
                "steps": [
                    {
                        "name": "airsim-fixture",
                        "stage": "prepare",
                        "state": "done",
                        "started_at": "2026-08-27T13:59:58Z",
                        "finished_at": "2026-08-27T14:00:01Z",
                        "log_artifact_id": "service-log-airsim-fixture",
                        "timeout_seconds": 600.0,
                    },
                    {
                        "name": "mission1-surveillance-views",
                        "stage": "post_ready",
                        "state": "running",
                        "started_at": "2026-08-27T14:00:01Z",
                        "finished_at": None,
                        "log_artifact_id": "service-log-mission1-surveillance-views",
                        "timeout_seconds": 600.0,
                    },
                ],
                "services": [
                    {
                        "name": "detached",
                        "state": "starting",
                        "importance": "routine",
                        "waiting_for": "the frozen engine",
                        "ready_timeout_seconds": 300.0,
                    },
                    # The supervisor was mid-teardown when the tree was reaped:
                    # the visualizer had stopped, the engine had its SIGTERM.
                    {
                        "name": "airsim-engine",
                        "state": "stopping",
                        "importance": "routine",
                        "started_at": "2026-08-27T13:59:01Z",
                        "stop_requested_at": "2026-08-27T14:00:03Z",
                        "stop_grace_seconds": 30.0,
                    },
                    {
                        "name": "airsim-visualizer",
                        "state": "stopped",
                        "importance": "routine",
                        "stop_requested_at": "2026-08-27T14:00:02Z",
                        "stop_grace_seconds": 10.0,
                        "stop_mode": "graceful",
                        "stopped_at": "2026-08-27T14:00:03Z",
                    },
                ],
                "teardown": {
                    "started_at": "2026-08-27T14:00:02Z",
                    "finished_at": None,
                    "stop_order": ["airsim-visualizer", "airsim-engine"],
                },
            }
        )
    )
    (context.run_root.path / "detached.pid").write_text(str(child.pid))
    sleep(60)


@pytest.mark.parametrize(
    ("recover", "legacy_record"), [(False, False), (True, False), (True, True)]
)
def test_owned_detached_child_reaped_but_unrelated_process_survives(
    tmp_path, recover, legacy_record
):
    config = _config(tmp_path)
    host = RuntimeHost(
        config, clock=_clock, generate_id=_ids(), worker_entrypoint=_detached_worker
    )
    client = TestClient(create_app(host=host), client=("127.0.0.1", 50000))
    outsider = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(60)"],
        start_new_session=True,
        env={**os.environ, "ONR_RUNTIME_HOST_WORKER_TOKEN": "unrelated"},
    )
    child_pid = None
    guardian_pid = None
    try:
        activate(client)
        marker = run_root(host).path / "detached.pid"
        _wait_for(marker.exists)
        child_pid = int(marker.read_text())
        guardian_pid = int((run_root(host).path / "guardian.pid").read_text())
        reader = host
        if recover:
            if legacy_record:
                state = json.loads(host._state_path.read_text())
                del state["runs"]["run-1"]["run_root"]
                host._state_path.write_text(json.dumps(state))
            reconstructed = RuntimeHost(
                config,
                clock=_clock,
                generate_id=_ids(),
                worker_entrypoint=_detached_worker,
            )
            _wait_for(
                lambda: current_run(reconstructed)["terminal_classification"]
                == "host_interrupted",
                timeout=15,
            )
            record = current_run(reconstructed)
            assert record["terminal_classification"] == "host_interrupted"
            reader = reconstructed
        else:
            response = client.post(
                "/api/v1/mission-runs/run-1/cancellations",
                headers=HEADERS,
                json={"cancellation_request_id": "cancel-1"},
            )
            assert response.status_code == 202
            assert current_run(host)["status"] == "cancelled"
        _wait_for(
            lambda: (
                not Path(f"/proc/{child_pid}").exists()
                or Path(f"/proc/{child_pid}/stat")
                .read_text()
                .split(") ", 1)[1]
                .startswith("Z")
            )
        )
        assert outsider.poll() is None
        # A stack reaped mid-startup must not keep showing what it waited for.
        status = json.loads(run_root(host).stack_status.read_text())
        assert "step" not in status
        # Finished prep steps keep their history; a reaped one ends stopped.
        assert [(step["name"], step["state"]) for step in status["steps"]] == [
            ("airsim-fixture", "done"),
            ("mission1-surveillance-views", "stopped"),
        ]
        assert status["steps"][1]["finished_at"] is None
        # A forced reap never claims a graceful stop: what was still running
        # or stopping ends `forced` at the time the Host verified the exit; a
        # service the supervisor had already stopped keeps its recorded mode.
        reaped_at = status["updated_at"]
        assert status["services"] == [
            {
                "name": "detached",
                "state": "stopped",
                "importance": "routine",
                "ready_timeout_seconds": 300.0,
                "stop_mode": "forced",
                "stopped_at": reaped_at,
            },
            {
                "name": "airsim-engine",
                "state": "stopped",
                "importance": "routine",
                "started_at": "2026-08-27T13:59:01Z",
                "stop_requested_at": "2026-08-27T14:00:03Z",
                "stop_grace_seconds": 30.0,
                "stop_mode": "forced",
                "stopped_at": reaped_at,
            },
            {
                "name": "airsim-visualizer",
                "state": "stopped",
                "importance": "routine",
                "stop_requested_at": "2026-08-27T14:00:02Z",
                "stop_grace_seconds": 10.0,
                "stop_mode": "graceful",
                "stopped_at": "2026-08-27T14:00:03Z",
            },
        ]
        assert status["teardown"] == {
            "started_at": "2026-08-27T14:00:02Z",
            "finished_at": reaped_at,
            "stop_order": ["airsim-visualizer", "airsim-engine"],
        }
        # The receipt: the worker is stopped; the engine log reported no
        # restoration (the guardian is a stand-in here), so it stays unknown.
        receipt = (
            TestClient(create_app(host=reader), client=("127.0.0.1", 50000))
            .get("/api/v1/mission-runs/run-1/operator-view?section=stack")
            .json()["stack"]["teardown"]
        )
        assert (receipt["worker"], receipt["harbor_config"]) == (
            "stopped",
            {"state": "unknown", "reported_by": None},
        )
        assert (
            not Path(f"/proc/{guardian_pid}/stat")
            .read_text()
            .split(") ", 1)[1]
            .startswith("Z")
        )
    finally:
        if guardian_pid:
            try:
                os.killpg(guardian_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if child_pid:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        outsider.kill()
        outsider.wait()
        for handle in host._workers.values():
            try:
                os.killpg(handle.identity.process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
            handle.join(timeout=2)


def stub_stack(monkeypatch, context):
    root = context.run_root.create()
    config = replace(
        context.config,
        transport=TransportConfig("file", root.transport),
        storage=StorageConfig(root.agent_storage),
        heartbeats=HeartbeatsConfig(1, 1, 3600),
    )
    plan = SimpleNamespace(
        request=SimpleNamespace(toggles=SimpleNamespace(airsim=False)),
        agent_config=root.agent_params,
        closed_loop=SimpleNamespace(
            planner_artifacts=root.planner_artifacts, simulation_limit_seconds=42
        ),
    )
    monkeypatch.setattr(host_module, "plan_mission_run", lambda **kwargs: plan)
    monkeypatch.setattr(
        host_module, "load_runtime_config", lambda *args, **kwargs: config
    )

    class Supervisor:
        def __init__(self, plan):
            self.stopped = False

        def start(self):
            pass

        def begin_closed_loop(self):
            pass

        def end_closed_loop(self, *, failed, cancelled=False):
            pass

        def poll(self):
            return None

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(host_module, "StackSupervisor", Supervisor)
    return root, config


def test_runtime_worker_flushes_run_local_summaries_result_and_final_frame(
    tmp_path, monkeypatch
):
    context = WorkerContext(
        config=_config(tmp_path),
        mission_id="mission-1",
        mission_run_id="run-1",
        activation_request_id="request-1",
        console_session_id="session-1",
        mission_intent="Survey sector seven",
        source_authority="operator_console",
        options=RuntimeWorkerOptions(repo_root=Path.cwd()),
        run_root=RunRoot(tmp_path / "run"),
    )
    root, _ = stub_stack(monkeypatch, context)
    # Read the real config, whose planning file does not exist until stack prep.
    physical = root.path / "physical-runtime"
    package = physical / "src/onr_physical_runtime/agent/__init__.py"
    package.parent.mkdir(parents=True)
    package.write_text("")
    planning_input = root.path / "mission1-planning-input/planning.json"
    profile = yaml.safe_load(Path("conf/environment_physical.yaml").read_text())
    profile["external"]["runtime_repository"] = str(physical)
    profile["external"]["planning_artifact_root"] = str(root.environment_artifacts)
    profile["external"]["mission1_planning_input_path"] = str(planning_input)
    root.environment_profile.write_text(yaml.safe_dump(profile))
    values = yaml.safe_load(Path("conf/onr_agent_params.yaml").read_text())
    values["environment_profile"] = str(root.environment_profile)
    values["transport"] = {"backend": "file", "root": str(root.transport)}
    values["storage"]["root"] = str(root.agent_storage)
    values["storage"]["planner_artifacts"] = str(root.planner_artifacts)
    values["heartbeats"]["summary_seconds"] = 3600
    for planner in values["planners"].values():
        planner["entrypoint"] = sys.executable
        if "validator_entrypoint" in planner:
            planner["validator_entrypoint"] = sys.executable
    root.agent_params.write_text(yaml.safe_dump(values))

    def prepare_inputs(self):
        planning_input.parent.mkdir(parents=True)
        planning_input.write_text("{}")

    monkeypatch.setattr(host_module.StackSupervisor, "start", prepare_inputs)
    monkeypatch.setattr(host_module, "load_runtime_config", load_runtime_config)
    model = SimpleNamespace(
        invoke=lambda *args, **kwargs: SimpleNamespace(
            content="Hyper planned; FSM started."
        )
    )
    monkeypatch.setattr(
        host_module.RuntimeComposition,
        "create_chat_model",
        lambda self, **kwargs: model,
    )

    def loop(runtime, mission, **options):
        assert runtime.config.storage.root == root.agent_storage
        log = FileOperationalLog(runtime.config.storage.root / "operational-log")
        log.emit(mission.mission_id, "hyper-agent", "planning-intent", "completed")
        log.emit(mission.mission_id, "fsm-runner", "fsm", "initialized")
        return SimpleNamespace(
            to_dict=lambda: {"mission_id": mission.mission_id, "status": "completed"}
        )

    monkeypatch.setattr(host_module, "run_closed_loop_demo", loop)
    monkeypatch.setattr(
        host_module.WorldView,
        "capture_final",
        lambda self: (
            root.latest_world_frame.parent.mkdir(parents=True, exist_ok=True)
            or root.latest_world_frame.write_bytes(b"final")
        ),
    )
    host_module.runtime_worker(context)
    assert json.loads(root.closed_loop_result.read_text())["status"] == "completed"
    artifact = json.loads(
        next(
            (root.agent_storage / "summaries" / "mission-1").glob("*.json")
        ).read_text()
    )
    assert (artifact["input_start_sequence"], artifact["input_end_sequence"]) == (1, 2)
    assert artifact["summary"] == "Hyper planned; FSM started."
    assert root.latest_world_frame.read_bytes() == b"final"


def test_service_crash_interrupts_inflight_closed_loop(tmp_path, monkeypatch):
    context = WorkerContext(
        config=_config(tmp_path),
        mission_id="mission-1",
        mission_run_id="run-1",
        activation_request_id="request-1",
        console_session_id="session-1",
        mission_intent="Survey sector seven",
        source_authority="operator_console",
        options=RuntimeWorkerOptions(repo_root=Path.cwd()),
        run_root=RunRoot(tmp_path / "run"),
    )
    root, _ = stub_stack(monkeypatch, context)

    class Crashed(host_module.StackSupervisor):
        def poll(self):
            return StackFailure("physical-runtime", "exited with status 7")

        def stop(self):
            root.worker_log.write_text("stopped")

    monkeypatch.setattr(host_module, "StackSupervisor", Crashed)
    monkeypatch.setattr(
        host_module.RuntimeComposition,
        "create_chat_model",
        lambda self, **kwargs: object(),
    )
    monkeypatch.setattr(
        host_module, "run_closed_loop_demo", lambda *args, **kwargs: sleep(30)
    )
    with pytest.raises(StackFailure, match="physical-runtime"):
        host_module.runtime_worker(context)
    assert root.worker_log.read_text() == "stopped"
    assert not root.closed_loop_result.exists()


def test_summary_arrival_emits_reparented_nodes_through_cursor(tmp_path):
    host, client, _ = setup_host(tmp_path)
    activate(client)
    root = run_root(host)
    log = FileOperationalLog(root.operational_log)
    for _ in range(3):
        log.emit("mission-1", "hyper-agent", "planning-intent", "completed")
    url = "/api/v1/mission-runs/run-1/operator-view?section=progress"
    first = client.get(url).json()
    assert {
        node["parent_id"]
        for node in first["progress"]["nodes"]
        if node["level"] == "record"
    } == {"live"}
    model = SimpleNamespace(invoke=lambda *args, **kwargs: "Mission intent accepted.")
    FileMissionLogSummarizer(log, root.agent_storage, model).heartbeat("mission-1")
    changed = client.get(url + "&limit=2&cursor=" + first["next_cursor"]).json()
    updates = changed["progress"]["nodes"]
    while changed["has_more"]:
        changed = client.get(url + "&limit=2&cursor=" + changed["next_cursor"]).json()
        updates.extend(changed["progress"]["nodes"])
    by_id = {node["node_id"]: node for node in updates}
    assert set(by_id) == {"summary:1", "live", "log:1", "log:2", "log:3"}
    assert by_id["summary:1"]["authoritative"] is False
    assert {by_id[f"log:{i}"]["parent_id"] for i in range(1, 4)} == {"summary:1"}
    assert by_id["live"]["child_count"] == 0


@pytest.mark.parametrize("cancel", [False, True])
def test_rejection_detail_precedes_blocked_summary_flush_and_keeps_ownership(
    tmp_path,
    monkeypatch,
    cancel,
):
    config = _config(tmp_path)
    root = RunRoot(config.storage.root / "runtime-host" / "runs" / "run-1").create()
    context = WorkerContext(
        config=config,
        mission_id="mission-1",
        mission_run_id="run-1",
        activation_request_id="request-1",
        console_session_id="session-1",
        mission_intent="buy me a coffee",
        source_authority="operator_console",
        options=RuntimeWorkerOptions(repo_root=Path.cwd()),
        run_root=root,
    )
    stub_stack(monkeypatch, context)
    # A killed Event.wait() consumer can strand multiprocessing notify_all().
    # Filesystem barriers remain releasable after the worker is forcibly reaped.
    release_summary = root.path / "release-summary"
    summary_started = root.path / "summary-started"

    class BlockedSummaryModel:
        def invoke(self, *args, **kwargs):
            summary_started.touch()
            _wait_for(release_summary.exists, timeout=30)
            return SimpleNamespace(content="Mission intent rejected.")

    monkeypatch.setattr(
        host_module.RuntimeComposition,
        "create_chat_model",
        lambda self, **kwargs: BlockedSummaryModel(),
    )

    def reject(runtime, mission, **options):
        FileOperationalLog(runtime.config.storage.root / "operational-log").emit(
            mission.mission_id,
            "hyper-agent",
            "planning-intent",
            "rejected",
        )
        raise host_module.MissionRejectedError("Coffee is a personal errand.")

    monkeypatch.setattr(host_module, "run_closed_loop_demo", reject)
    host = RuntimeHost(config, clock=_clock, generate_id=_ids())
    client = TestClient(create_app(host=host), client=("127.0.0.1", 50000))
    try:
        activate(client)
        _wait_for(summary_started.exists)
        current = client.get("/api/v1/mission-runs/current").json()["mission_run"]
        assert current["status"] == "running"
        assert current["terminal_detail"] == {
            "kind": "mission_rejected",
            "stage": "intent",
            "reason": "Coffee is a personal errand.",
        }
        second = client.post(
            "/api/v1/mission-activations",
            json={**BODY, "activation_request_id": "request-2"},
            headers=HEADERS,
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "mission_run_active"
        if cancel:
            response = client.post(
                "/api/v1/mission-runs/run-1/cancellations",
                headers=HEADERS,
                json={"cancellation_request_id": "cancel-1"},
            )
            assert response.status_code == 202
            assert current_run(host)["status"] == "cancelled"
            assert current_run(host)["terminal_detail"] is None
        else:
            release_summary.touch()
            _wait_for(lambda: current_run(host)["status"] == "failed")
            assert current_run(host)["terminal_classification"] == "mission_rejected"
            assert (
                current_run(host)["terminal_detail"]["reason"]
                == "Coffee is a personal errand."
            )
    finally:
        release_summary.touch()
        for handle in host._workers.values():
            try:
                os.killpg(handle.identity.process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
            handle.join(timeout=2)


def test_preflight_explains_supported_type_but_unsupported_combination(tmp_path):
    _, client, _ = setup_host(tmp_path)
    response = client.get(
        "/api/v1/stack/preflight?preset_id=mission1-harbor&airsim=true&perception=ideal"
    )
    assert response.status_code == 200
    assert response.json()["launchable"] is False
    assert response.json()["checks"][0]["check_id"] == "toggles"


@pytest.mark.parametrize("outcome", ["completed", "rejected", "cancelled"])
def test_final_monitor_crash_and_late_signals_reap_owned_services(
    tmp_path, monkeypatch, outcome
):
    host, client, pending = setup_host(tmp_path, worker=host_module.runtime_worker)
    root = run_root(host).create()
    context = WorkerContext(
        config=host.config,
        mission_id="mission-1",
        mission_run_id="run-1",
        activation_request_id="request-1",
        console_session_id="session-1",
        mission_intent=BODY["mission_intent"],
        source_authority="operator_console",
        options=RuntimeWorkerOptions(repo_root=Path.cwd()),
        run_root=root,
    )
    stub_stack(monkeypatch, context)
    monitor_entered = ThreadEvent()
    release_monitor = ThreadEvent()
    supervisors = []
    plan = SimpleNamespace(
        run_root=root,
        agent_config=root.agent_params,
        request=SimpleNamespace(
            preset_id="mission1-harbor",
            toggles=SimpleNamespace(payload=dict, airsim=False),
        ),
        services=tuple(
            ServiceSpec(
                name=name,
                argv=(sys.executable, "-c", "import time; time.sleep(60)"),
                env={},
                cwd=root.path,
                readiness=(),
                timeout_seconds=5,
                stop_grace_seconds=5,
            )
            for name in ("engine", "physical-runtime")
        ),
        prepare=(),
        post_ready=(),
        closed_loop=SimpleNamespace(
            planner_artifacts=root.planner_artifacts, simulation_limit_seconds=42
        ),
    )
    monkeypatch.setattr(host_module, "plan_mission_run", lambda **kwargs: plan)

    class FinalCycleSupervisor(StackSupervisor):
        def __init__(self, plan):
            super().__init__(plan)
            supervisors.append(self)
            self.late_signal_sent = False

        def poll(self):
            if current_thread().name == "stack-monitor":
                monitor_entered.set()
                assert release_monitor.wait(5)
            return super().poll()

        def stop(self):
            if release_monitor.is_set() and not self.late_signal_sent:
                self.late_signal_sent = True
                # Deliver real signals exactly where an interrupt used to leave
                # the engine alive and prevent the next run's port preflight.
                real_kill(os.getpid(), signal.SIGUSR1)
                real_kill(os.getpid(), signal.SIGTERM)
                real_kill(os.getpid(), signal.SIGTERM)
            super().stop()

    monkeypatch.setattr(host_module, "StackSupervisor", FinalCycleSupervisor)
    monkeypatch.setattr(
        host_module,
        "RuntimeComposition",
        lambda *args: SimpleNamespace(
            create_chat_model=lambda: object(),
            mission_session=lambda *args, **kwargs: nullcontext(),
        ),
    )
    real_kill = os.kill

    def delayed_monitor_signal(pid, sig):
        # Collect the failure now; deliver its notification at teardown below.
        if current_thread().name != "stack-monitor":
            real_kill(pid, sig)

    monkeypatch.setattr(host_module.os, "kill", delayed_monitor_signal)

    def finish_mission(*args, **kwargs):
        assert monitor_entered.wait(5)
        crashed = supervisors[0]._processes["physical-runtime"]
        crashed.kill()
        crashed.wait(timeout=5)
        release_monitor.set()
        if outcome == "rejected":
            raise host_module.MissionRejectedError("Outside the mission scope.")
        if outcome == "cancelled":
            with host._state_guard():
                state = host._load_state()
                state["runs"]["run-1"]["cancellation_requested"] = True
                host._save_state(state)
            raise host_module._WorkerCancelled
        return SimpleNamespace(to_dict=lambda: {"completed": True})

    monkeypatch.setattr(host_module, "run_closed_loop_demo", finish_mission)
    monkeypatch.setattr(host_module.WorldView, "capture_final", lambda self: None)
    try:
        assert activate(client).status_code == 202
        pending.pop()()
        if outcome == "cancelled":
            host._transition("run-1", "cancelled")
        record = current_run(host)
        assert record["terminal_classification"] == {
            "completed": "stack_failed",
            "rejected": "mission_rejected",
            "cancelled": "cancelled_by_owner",
        }[outcome]
        if outcome == "completed":
            assert record["terminal_detail"]["service"] == "physical-runtime"
        assert not root.closed_loop_result.exists()
        services = json.loads(root.stack_status.read_text())["services"]
        for service in services:
            if service["pid"] is not None:
                assert not Path(f"/proc/{service['pid']}").exists()
        assert {s["name"]: s["state"] for s in services} == {
            "engine": "stopped",
            "physical-runtime": "failed",
            "closed-loop": "failed",
        }
    finally:
        release_monitor.set()
        for supervisor in supervisors:
            supervisor.stop()


def test_old_terminal_views_reload_after_bounded_cache_eviction(
    tmp_path, monkeypatch
):
    import onr.runtime_host.world as world_module

    host, client, pending = setup_host(tmp_path)
    expected = {}
    for number in range(1, 4):
        response = client.post(
            "/api/v1/mission-activations",
            json={**BODY, "activation_request_id": f"request-{number}"},
            headers=HEADERS,
        )
        assert response.status_code == 202
        run_id = response.json()["mission_run_id"]
        mission_id = response.json()["mission_id"]
        root = RunRoot.for_run(host._runs_root, run_id)
        if number > 1:
            previous_id = f"run-{number - 1}"
            assert previous_id not in host._run_observations
            assert previous_id not in host._operator_projection._runs
            assert previous_id not in host._world_views
        FileOperationalLog(root.operational_log).emit(
            mission_id, "hyper-agent", "planning-intent", "completed"
        )
        root.stack_plan.write_text(json.dumps({"viewer_port": 5066}))
        frame = b"\x89PNG\r\n\x1a\n" + run_id.encode()

        def fetch(url, timeout, maximum, *, version=number, data=frame):
            if url.endswith("/api/state"):
                return json.dumps(
                    {"state_version": version, "mission_time_seconds": version * 10}
                ).encode()
            if url.endswith("/api/frame"):
                return data
            raise OSError("camera unavailable")

        view = WorldView(root.path, fetch=fetch)
        host._world_views[run_id] = view
        prefix = f"/api/v1/mission-runs/{run_id}"
        assert client.get(prefix + "/world-frame").content == frame
        view.capture_final()
        progress = client.get(prefix + "/operator-view?section=progress").json()
        expected[run_id] = (
            frame,
            [
                node
                for node in progress["progress"]["nodes"]
                if node["level"] == "record"
            ],
        )
        pending.pop()()

    def foreign_viewer(*args):
        raise AssertionError("historical view contacted a reused viewer port")

    monkeypatch.setattr(world_module, "_fetch", foreign_viewer)
    current_observations = host._run_observations["run-3"]
    current_projection = host._operator_projection._runs["run-3"]
    current_world = host._world_views["run-3"]
    for run_id in ("run-1", "run-2", "run-1"):
        prefix = f"/api/v1/mission-runs/{run_id}"
        progress = client.get(prefix + "/operator-view?section=progress").json()
        assert [
            node for node in progress["progress"]["nodes"] if node["level"] == "record"
        ] == expected[run_id][1]
        assert client.get(prefix + "/world-frame").content == expected[run_id][0]
        world = client.get(prefix + "/operator-view?section=world").json()["world"]
        assert world["state"]["state_version"] == int(run_id[-1])
        assert host._run_observations["run-3"] is current_observations
        assert host._operator_projection._runs["run-3"] is current_projection
        assert host._world_views["run-3"] is current_world
        retained = {"run-3", run_id}
        assert set(host._run_observations) <= retained
        assert set(host._operator_projection._runs) <= retained
        assert set(host._world_views) <= retained


def test_legacy_run_root_migration_preserves_historical_narrative(tmp_path):
    host, client, pending = setup_host(tmp_path)
    activate(client)
    pending[0]()
    historical = run_root(host)
    host._narrative_record("run-1").publish_available(
        text="The recorded patrol completed before this Host upgrade.",
        generated_at=_clock(),
        source_watermark=7,
        terminal=True,
    )
    state = json.loads(host._state_path.read_text())
    del state["runs"]["run-1"]["run_root"]
    del state["runs"]["run-1"]["stack"]
    del state["runs"]["run-1"]["terminal_detail"]
    host._state_path.write_text(json.dumps(state))
    host.close()
    reconstructed = RuntimeHost(
        _config(tmp_path),
        clock=_clock,
        generate_id=_ids(),
        runs_root=tmp_path / "new-canonical-runs",
        launch_worker=lambda worker: None,
    )
    try:
        migrated_client = TestClient(
            create_app(host=reconstructed), client=("127.0.0.1", 50000)
        )
        response = migrated_client.get(
            "/api/v1/mission-runs/run-1/operator-view?section=progress"
        )
        assert response.status_code == 200
        narrative = response.json()["progress"]["narrative"]
        assert narrative["status"] == "available"
        assert narrative["text"] == (
            "The recorded patrol completed before this Host upgrade."
        )
        record = current_run(reconstructed)
        assert record["status"] == "succeeded"
        assert record["stack"] is None
        stored = json.loads(reconstructed._state_path.read_text())
        assert stored["runs"]["run-1"]["run_root"] == str(historical.path)
    finally:
        reconstructed.close()
