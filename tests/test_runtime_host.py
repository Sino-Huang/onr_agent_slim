from __future__ import annotations

import json
import multiprocessing
import os
import signal
import socket
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from typing import Any

import httpx
import uvicorn
from fastapi.testclient import TestClient

import onr.runtime_host.host as runtime_host_module
from onr.runtime import (
    HeartbeatsConfig,
    LLMConfig,
    PlannerConfig,
    PlannersConfig,
    RuntimeConfig,
    ServicesConfig,
    StorageConfig,
    TransportConfig,
)
from onr.runtime_host import (
    RuntimeHost,
    RuntimeWorkerOptions,
    WorkerContext,
    create_app,
)


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        llm=LLMConfig("openai", "http://127.0.0.1:1/v1", "offline", "EMPTY", 0),
        planners=PlannersConfig(
            PlannerConfig(Path(__file__), 1),
            PlannerConfig(Path(__file__), 1, Path(__file__)),
        ),
        heartbeats=HeartbeatsConfig(1, 1),
        transport=TransportConfig("inprocess", tmp_path / "transport"),
        storage=StorageConfig(tmp_path / "storage"),
        services=ServicesConfig("hyper", "maneuver", "context", "fsm", "planner"),
        debug=False,
        agent_name="test-agent",
    )


def _clock() -> str:
    return datetime(2026, 8, 24, 12, 0, tzinfo=UTC).isoformat()


def _ids() -> Callable[[str], str]:
    counts: dict[str, int] = {}

    def generate(kind: str) -> str:
        counts[kind] = counts.get(kind, 0) + 1
        return f"{kind}-{counts[kind]}"

    return generate


def _client(
    tmp_path: Path,
    *,
    worker: Callable[[WorkerContext], None] | None = None,
) -> tuple[TestClient, RuntimeHost, list[Callable[[], None]]]:
    pending: list[Callable[[], None]] = []
    host = RuntimeHost(
        _config(tmp_path),
        clock=_clock,
        generate_id=_ids(),
        worker_entrypoint=worker,
        launch_worker=pending.append,
    )
    return TestClient(create_app(host=host), client=("127.0.0.1", 50000)), host, pending


def _activate(
    client: TestClient,
    *,
    activation_request_id: str = "request-1",
    console_session_id: str = "session-1",
    mission_intent: str = "Survey sector seven",
    source_authority: str = "operator_console",
    credential: str = "console-secret",
) -> Any:
    return client.post(
        "/api/v1/mission-activations",
        headers={"Authorization": f"Bearer {credential}"},
        json={
            "activation_request_id": activation_request_id,
            "console_session_id": console_session_id,
            "mission_intent": mission_intent,
            "source_authority": source_authority,
        },
    )


def _owner_headers(credential: str = "console-secret") -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


def _cancel(
    client: TestClient,
    *,
    mission_run_id: str = "run-1",
    cancellation_request_id: str = "cancel-1",
    credential: str = "console-secret",
) -> Any:
    return client.post(
        f"/api/v1/mission-runs/{mission_run_id}/cancellations",
        headers=_owner_headers(credential),
        json={"cancellation_request_id": cancellation_request_id},
    )


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return
        sleep(0.02)
    raise AssertionError("condition was not reached before timeout")


def _process_tree_worker(context: WorkerContext) -> None:
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os,signal,time;"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                f"open({str(context.run_root.path / 'child.pid')!r},'w').write(str(os.getpid()));"
                "time.sleep(60)"
            ),
        ]
    )
    child.wait()


def _mark_process_execution(path: str) -> None:
    Path(path).write_text("executed", encoding="utf-8")


def test_health_and_empty_current_run_contract(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)

    assert client.get("/api/v1/health").json() == {
        "status": "ok",
        "api_version": {"major": 1, "minor": 3},
    }
    response = client.get("/api/v1/mission-runs/current")
    assert response.status_code == 200
    assert response.json() == {"mission_run": None}


def test_activation_is_queued_and_credential_is_only_a_persisted_verifier(
    tmp_path: Path,
) -> None:
    client, _, pending = _client(tmp_path)

    response = _activate(client)

    assert response.status_code == 202
    assert response.json() == {
        "activation_request_id": "request-1",
        "mission_id": "mission-1",
        "mission_run_id": "run-1",
        "status": "queued",
        "created_at": "2026-08-24T12:00:00+00:00",
    }
    assert len(pending) == 1
    assert client.get("/api/v1/mission-runs/current").json() == {
        "mission_run": {
            "mission_id": "mission-1",
            "mission_run_id": "run-1",
            "status": "queued",
            "created_at": "2026-08-24T12:00:00+00:00",
            "started_at": None,
            "finished_at": None,
            "terminal_classification": None,
            "stack": {"preset_id": "mission1-harbor", "airsim": False, "perception": "off"},
            "terminal_detail": None,
        }
    }
    persisted = json.loads(
        (tmp_path / "storage/runtime-host/state.json").read_text(encoding="utf-8")
    )
    serialized = json.dumps(persisted, sort_keys=True)
    assert "console-secret" not in serialized
    assert persisted["activations"]["request-1"]["credential_verifier"]
    assert persisted["activations"]["request-1"]["console_session_id"] == "session-1"
    assert persisted["activations"]["request-1"]["mission_intent"] == "Survey sector seven"
    assert persisted["activations"]["request-1"]["source_authority"] == "operator_console"
    run = persisted["runs"]["run-1"]
    assert run["console_session_id"] == "session-1"
    assert run["worker_identity"] == "runtime_host.closed_loop_demo"
    run_root = tmp_path / "storage/runtime-host/runs/run-1"
    assert run["run_root"] == str(run_root)
    assert (run_root / "services").is_dir()


def test_owner_can_read_exact_mission_intent_and_authorization_failure_is_safe(
    tmp_path: Path,
) -> None:
    client, _, _ = _client(tmp_path)
    assert _activate(client, mission_intent="Hold the ridge.\nReport obstacles.").status_code == 202

    response = client.get(
        "/api/v1/mission-runs/run-1/mission-intent",
        headers=_owner_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {
        "mission_run_id": "run-1",
        "mission_intent": "Hold the ridge.\nReport obstacles.",
        "source_authority": "operator_console",
    }
    expected = {
        "error": {
            "code": "authorization_failed",
            "message": "request is not authorized",
        }
    }
    for run_id, headers in (
        ("run-1", {}),
        ("run-1", _owner_headers("wrong-secret")),
        ("stale-run", _owner_headers()),
    ):
        denied = client.get(
            f"/api/v1/mission-runs/{run_id}/mission-intent", headers=headers
        )
        assert denied.status_code == 403
        assert denied.json() == expected
        assert "Hold the ridge" not in denied.text


def test_queued_cancellation_is_durable_idempotent_and_prevents_worker_launch(
    tmp_path: Path,
) -> None:
    observed: list[str] = []
    client, _, pending = _client(
        tmp_path, worker=lambda _context: observed.append("launched")
    )
    assert _activate(client).status_code == 202

    accepted = _cancel(client)
    replay = _cancel(client)

    assert accepted.status_code == replay.status_code == 202
    assert replay.json() == accepted.json()
    assert accepted.json() == {
        "mission_run_id": "run-1",
        "cancellation_request_id": "cancel-1",
        "disposition": "cancellation_requested",
        "status": "queued",
        "requested_at": "2026-08-24T12:00:00+00:00",
    }
    pending.pop()()
    assert observed == []
    run = client.get("/api/v1/mission-runs/current").json()["mission_run"]
    assert run["status"] == "cancelled"
    assert run["terminal_classification"] == "cancelled_by_owner"
    persisted = json.loads(
        (tmp_path / "storage/runtime-host/state.json").read_text(encoding="utf-8")
    )
    assert persisted["cancellations"]["cancel-1"]["response"] == accepted.json()
    assert persisted["runs"]["run-1"]["cancellation_requested"] is True
    assert "console-secret" not in json.dumps(persisted)

    second = _activate(
        client,
        activation_request_id="request-2",
        mission_intent="Hold position",
    )
    assert second.status_code == 202
    conflict = _cancel(client, mission_run_id="run-2")
    assert conflict.status_code == 409
    assert conflict.json() == {
        "error": {
            "code": "cancellation_request_conflict",
            "message": "cancellation_request_id was reused for a different cancellation request",
        }
    }


def test_cancellation_authorization_failure_does_not_reveal_run_or_request(
    tmp_path: Path,
) -> None:
    client, _, _ = _client(tmp_path)
    assert _activate(client).status_code == 202
    expected = {
        "error": {
            "code": "authorization_failed",
            "message": "request is not authorized",
        }
    }

    for run_id, credential in (("run-1", "wrong-secret"), ("stale-run", "console-secret")):
        denied = _cancel(client, mission_run_id=run_id, credential=credential)
        assert denied.status_code == 403
        assert denied.json() == expected


def test_running_cancellation_terminates_owned_tree_but_not_environment_process(
    tmp_path: Path,
) -> None:
    grandchild_pid_path = tmp_path / "storage/runtime-host/runs/run-1/child.pid"
    environment_ready_path = tmp_path / "environment-command.ready"
    environment_process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os,time;"
                f"open({str(environment_ready_path)!r},'w').write(str(os.getpid()));"
                "time.sleep(60)"
            ),
        ],
        start_new_session=True,
    )
    host = RuntimeHost(
        _config(tmp_path),
        clock=_clock,
        generate_id=_ids(),
        worker_entrypoint=_process_tree_worker,
        worker_options=RuntimeWorkerOptions(
            repo_root=tmp_path,
        ),
    )
    client = TestClient(create_app(host=host), client=("127.0.0.1", 50000))

    try:
        _wait_for(environment_ready_path.is_file)
        environment_pid = int(environment_ready_path.read_text(encoding="utf-8"))
        assert environment_pid == environment_process.pid
        assert _activate(client).status_code == 202
        _wait_for(
            lambda: client.get("/api/v1/mission-runs/current").json()["mission_run"][
                "status"
            ]
            == "running"
            and grandchild_pid_path.is_file()
        )
        state = json.loads(
            (tmp_path / "storage/runtime-host/state.json").read_text(encoding="utf-8")
        )
        worker_pid = int(state["runs"]["run-1"]["worker_pid"])
        grandchild_pid = int(grandchild_pid_path.read_text(encoding="utf-8"))
        assert state["runs"]["run-1"]["worker_launch_state"] == "group_ready"
        assert state["runs"]["run-1"]["worker_process_group_id"] == worker_pid
        assert state["runs"]["run-1"]["worker_start_time"]

        response = _cancel(client)

        assert response.status_code == 202
        run = client.get("/api/v1/mission-runs/current").json()["mission_run"]
        assert run["status"] == "cancelled"
        assert run["terminal_classification"] == "cancelled_by_owner"
        for pid in (worker_pid, grandchild_pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError(f"owned process {pid} survived cancellation")
        assert environment_process.poll() is None
        os.kill(environment_pid, 0)
    finally:
        environment_process.terminate()
        environment_process.wait(timeout=5)


def test_awaiting_human_decision_cancellation_terminates_live_owned_tree(
    tmp_path: Path,
) -> None:
    grandchild_pid_path = tmp_path / "storage/runtime-host/runs/run-1/child.pid"
    host = RuntimeHost(
        _config(tmp_path),
        clock=_clock,
        generate_id=_ids(),
        worker_entrypoint=_process_tree_worker,
        worker_options=RuntimeWorkerOptions(
            repo_root=tmp_path,
        ),
    )
    client = TestClient(create_app(host=host), client=("127.0.0.1", 50000))

    assert _activate(client).status_code == 202
    _wait_for(
        lambda: client.get("/api/v1/mission-runs/current").json()["mission_run"]["status"]
        == "running"
        and grandchild_pid_path.is_file()
    )
    with host._state_guard():
        state = host._load_state()
        run = state["runs"]["run-1"]
        worker_pid = int(run["worker_pid"])
        run["status"] = "awaiting_human_decision"
        host._save_state(state)
    grandchild_pid = int(grandchild_pid_path.read_text(encoding="utf-8"))

    response = _cancel(client)

    assert response.status_code == 202
    run = host.current_run()
    assert run is not None
    assert run["status"] == "cancelled"
    assert run["terminal_classification"] == "cancelled_by_owner"
    for pid in (worker_pid, grandchild_pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError(f"owned awaiting process {pid} survived cancellation")


def test_cancellation_during_launcher_registration_prevents_worker_execution(
    tmp_path: Path,
) -> None:
    launcher_entered = Event()
    release_launcher = Event()
    worker_executed = Event()
    callback_finished = Event()

    def launch(callback: Callable[[], None]) -> None:
        def invoke() -> None:
            callback()
            callback_finished.set()

        Thread(target=invoke, daemon=True).start()
        launcher_entered.set()
        assert release_launcher.wait(timeout=5)

    host = RuntimeHost(
        _config(tmp_path),
        clock=_clock,
        generate_id=_ids(),
        worker_entrypoint=lambda _context: worker_executed.set(),
        launch_worker=launch,
    )
    client = TestClient(create_app(host=host), client=("127.0.0.1", 50000))
    activation_thread = Thread(target=lambda: _activate(client), daemon=True)
    activation_thread.start()
    assert launcher_entered.wait(timeout=5)

    cancellation_thread = Thread(target=lambda: _cancel(client), daemon=True)
    cancellation_thread.start()
    _wait_for(
        lambda: json.loads(
            (tmp_path / "storage/runtime-host/state.json").read_text(encoding="utf-8")
        )["runs"]["run-1"]["cancellation_requested"]
        is True
    )
    release_launcher.set()
    activation_thread.join(timeout=5)
    cancellation_thread.join(timeout=5)
    assert callback_finished.wait(timeout=5)

    assert worker_executed.is_set() is False
    run = host.current_run()
    assert run is not None
    assert run["status"] == "cancelled"


def test_unreleased_process_worker_self_exits_after_startup_timeout(
    tmp_path: Path,
) -> None:
    executed_path = tmp_path / "executed"
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    process = multiprocessing.Process(
        target=runtime_host_module._process_group_entrypoint,
        args=(
            lambda: _mark_process_execution(str(executed_path)),
            ready,
            release,
            "orphan-token",
            0.2,
        ),
    )
    process.start()
    assert ready.wait(timeout=2)

    process.join(timeout=3)

    assert process.is_alive() is False
    assert process.exitcode == 0
    assert executed_path.exists() is False


def test_awaiting_human_decision_cancellation_prevents_worker_continuation(
    tmp_path: Path,
) -> None:
    worker_executed = Event()
    client, host, pending = _client(
        tmp_path, worker=lambda _context: worker_executed.set()
    )
    assert _activate(client).status_code == 202
    with host._state_guard():
        state = host._load_state()
        state["runs"]["run-1"]["status"] = "awaiting_human_decision"
        host._save_state(state)

    assert _cancel(client).status_code == 202
    pending.pop()()

    assert worker_executed.is_set() is False
    run = host.current_run()
    assert run is not None
    assert run["status"] == "cancelled"


def test_terminal_worker_result_wins_cancellation_race(tmp_path: Path) -> None:
    worker_started = Event()
    release_worker = Event()

    def worker(_context: WorkerContext) -> None:
        worker_started.set()
        assert release_worker.wait(timeout=5)

    client, host, pending = _client(tmp_path, worker=worker)
    assert _activate(client).status_code == 202
    worker_thread = Thread(target=pending.pop(), daemon=True)
    worker_thread.start()
    assert worker_started.wait(timeout=5)
    release_worker.set()
    worker_thread.join(timeout=5)
    run = host.current_run()
    assert run is not None
    assert run["status"] == "succeeded"

    accepted = _cancel(client)

    assert accepted.status_code == 202
    run = host.current_run()
    assert run is not None
    assert run["status"] == "succeeded"
    assert run["terminal_classification"] is None


def test_cancellation_persisted_before_worker_exit_wins_terminal_race(
    tmp_path: Path,
) -> None:
    worker_started = Event()
    release_worker = Event()

    def worker(_context: WorkerContext) -> None:
        worker_started.set()
        assert release_worker.wait(timeout=5)

    client, host, pending = _client(tmp_path, worker=worker)
    assert _activate(client).status_code == 202
    worker_thread = Thread(target=pending.pop(), daemon=True)
    worker_thread.start()
    assert worker_started.wait(timeout=5)

    assert _cancel(client).status_code == 202
    release_worker.set()
    worker_thread.join(timeout=5)

    run = host.current_run()
    assert run is not None
    assert run["status"] == "cancelled"
    assert run["terminal_classification"] == "cancelled_by_owner"


def test_cancellation_timeout_reconciles_after_tree_exit_and_replay_rechecks(
    tmp_path: Path, monkeypatch: Any
) -> None:
    tree_exists = Event()
    tree_exists.set()

    class FakeWorker:
        @property
        def identity(self) -> runtime_host_module.WorkerIdentity:
            return runtime_host_module.WorkerIdentity(
                99123, 99123, "fake-start", 99123, "fake-token"
            )

        def join(self, timeout: float | None = None) -> None:
            del timeout

        def release(self) -> None:
            pass

    pending: list[Callable[[], None]] = []

    def launch(callback: Callable[[], None]) -> FakeWorker:
        pending.append(callback)
        return FakeWorker()

    host = RuntimeHost(
        _config(tmp_path),
        clock=_clock,
        generate_id=_ids(),
        worker_entrypoint=lambda _context: None,
        launch_worker=launch,
    )
    client = TestClient(create_app(host=host), client=("127.0.0.1", 50000))
    monkeypatch.setattr(
        runtime_host_module.WorkerIdentity,
        "is_owned",
        lambda _identity: True,
    )
    monkeypatch.setattr(host, "_signal_process_group", lambda *_args: None)
    monkeypatch.setattr(
        host, "_process_group_exists", lambda _process_group_id: tree_exists.is_set()
    )
    monkeypatch.setattr(host, "_wait_for_owned_tree", lambda *_args, **_kwargs: None)
    assert _activate(client).status_code == 202
    host._transition("run-1", "running")

    accepted = _cancel(client)
    replay = _cancel(client)
    second_replay = _cancel(client)

    assert accepted.status_code == replay.status_code == second_replay.status_code == 202
    assert replay.json() == accepted.json()
    assert second_replay.json() == accepted.json()
    assert len(host._reconcilers) == 1
    run = host.current_run()
    assert run is not None
    assert run["status"] == "running"
    tree_exists.clear()
    _wait_for(lambda: host.current_run()["status"] == "cancelled")  # type: ignore[index]
    _wait_for(lambda: host._reconcilers == {})


def test_cancellation_does_not_escalate_after_worker_identity_is_lost(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ownership_checks = iter((True, False))
    signals: list[int] = []

    class FakeWorker:
        @property
        def identity(self) -> runtime_host_module.WorkerIdentity:
            return runtime_host_module.WorkerIdentity(
                99124, 99124, "reused-start", 99124, "reused-token"
            )

        def join(self, timeout: float | None = None) -> None:
            del timeout

        def release(self) -> None:
            pass

    pending: list[Callable[[], None]] = []

    def launch(callback: Callable[[], None]) -> FakeWorker:
        pending.append(callback)
        return FakeWorker()

    host = RuntimeHost(
        _config(tmp_path),
        clock=_clock,
        generate_id=_ids(),
        worker_entrypoint=lambda _context: None,
        launch_worker=launch,
    )
    client = TestClient(create_app(host=host), client=("127.0.0.1", 50000))
    monkeypatch.setattr(
        runtime_host_module.WorkerIdentity,
        "is_owned",
        lambda _identity: next(ownership_checks),
    )
    monkeypatch.setattr(host, "_process_group_exists", lambda _pgid: True)
    monkeypatch.setattr(
        host,
        "_signal_process_group",
        lambda _pgid, selected_signal: signals.append(selected_signal),
    )
    monkeypatch.setattr(host, "_wait_for_owned_tree", lambda *_args, **_kwargs: None)
    assert _activate(client).status_code == 202
    host._transition("run-1", "running")

    response = _cancel(client)

    assert response.status_code == 202
    assert signal.SIGKILL not in signals
    assert host._reconcilers == {}
    run = host.current_run()
    assert run is not None
    assert run["status"] == "failed"
    assert run["terminal_classification"] == "host_interrupted"


def test_cancellation_does_not_signal_when_worker_identity_is_already_lost(
    tmp_path: Path, monkeypatch: Any
) -> None:
    signals: list[int] = []

    class FakeWorker:
        @property
        def identity(self) -> runtime_host_module.WorkerIdentity:
            return runtime_host_module.WorkerIdentity(
                99125, 99125, "lost-start", 99125, "lost-token"
            )

        def join(self, timeout: float | None = None) -> None:
            del timeout

        def release(self) -> None:
            pass

    def launch(_callback: Callable[[], None]) -> FakeWorker:
        return FakeWorker()

    host = RuntimeHost(
        _config(tmp_path),
        clock=_clock,
        generate_id=_ids(),
        worker_entrypoint=lambda _context: None,
        launch_worker=launch,
    )
    client = TestClient(create_app(host=host), client=("127.0.0.1", 50000))
    monkeypatch.setattr(
        runtime_host_module.WorkerIdentity,
        "is_owned",
        lambda _identity: False,
    )
    monkeypatch.setattr(host, "_process_group_exists", lambda _pgid: True)
    monkeypatch.setattr(
        host,
        "_signal_process_group",
        lambda _pgid, selected_signal: signals.append(selected_signal),
    )
    assert _activate(client).status_code == 202
    host._transition("run-1", "running")

    assert _cancel(client).status_code == 202

    assert signals == []
    run = host.current_run()
    assert run is not None
    assert run["status"] == "failed"
    assert run["terminal_classification"] == "host_interrupted"


def test_recovery_terminates_owned_group_after_leader_exits(
    tmp_path: Path,
) -> None:
    token = "recovery-owned-token"
    child_pid_path = tmp_path / "surviving-child.pid"
    child_script = (
        "import os,signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"open({str(child_pid_path)!r},'w').write(str(os.getpid()));"
        "time.sleep(60)"
    )
    script = f"import subprocess,sys;subprocess.Popen([sys.executable,'-c',{child_script!r}])"
    environment = dict(os.environ)
    environment[runtime_host_module._WORKER_OWNERSHIP_ENV] = token
    leader = subprocess.Popen(
        [sys.executable, "-c", script],
        start_new_session=True,
        env=environment,
    )
    leader_pid = leader.pid
    leader.wait(timeout=5)
    _wait_for(child_pid_path.is_file)
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    try:
        client, host, pending = _client(tmp_path)
        assert _activate(client).status_code == 202
        pending.clear()
        with host._state_guard():
            state = host._load_state()
            run = state["runs"]["run-1"]
            run["status"] = "running"
            run["worker_launch_state"] = "group_ready"
            run["worker_pid"] = leader_pid
            run["worker_process_group_id"] = leader_pid
            run["worker_session_id"] = leader_pid
            run["worker_start_time"] = "leader-exited"
            run["worker_ownership_token"] = token
            host._save_state(state)

        reconstructed = RuntimeHost(
            _config(tmp_path), clock=_clock, generate_id=_ids(), launch_worker=pending.append
        )
        _wait_for(lambda: reconstructed.current_run()["status"] == "failed")  # type: ignore[index]

        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("owned surviving descendant was not terminated")
        run = reconstructed.current_run()
        assert run is not None
        assert run["terminal_classification"] == "host_interrupted"
    finally:
        try:
            os.killpg(leader_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_reconstruction_does_not_claim_cancelled_without_registered_tree_exit(
    tmp_path: Path,
) -> None:
    client, _, pending = _client(tmp_path, worker=lambda _context: None)
    assert _activate(client).status_code == 202
    assert _cancel(client).status_code == 202

    reconstructed, _, reconstructed_pending = _client(tmp_path)

    run = reconstructed.get("/api/v1/mission-runs/current").json()["mission_run"]
    assert run["status"] == "failed"
    assert run["terminal_classification"] == "host_interrupted"
    assert reconstructed_pending == []
    pending.pop()()
    assert reconstructed.get("/api/v1/mission-runs/current").json()["mission_run"] == run


def test_reconstruction_recovers_cancelled_after_owned_group_already_exited(
    tmp_path: Path,
) -> None:
    client, host, pending = _client(tmp_path)
    assert _activate(client).status_code == 202
    assert _cancel(client).status_code == 202
    pending.clear()
    absent_pid = 2_000_000_000
    with host._state_guard():
        state = host._load_state()
        runtime_host_module.WorkerIdentity(
            absent_pid,
            absent_pid,
            "exited-worker-start",
            absent_pid,
            "exited-worker-token",
        ).persist(state["runs"]["run-1"])
        host._save_state(state)

    reconstructed = RuntimeHost(
        _config(tmp_path),
        clock=_clock,
        generate_id=_ids(),
        launch_worker=pending.append,
    )

    run = reconstructed.current_run()
    assert run is not None
    assert run["status"] == "cancelled"
    assert run["terminal_classification"] == "cancelled_by_owner"


def test_reconstruction_keeps_unverifiable_cancelled_identity_interrupted(
    tmp_path: Path,
) -> None:
    client, host, pending = _client(tmp_path)
    assert _activate(client).status_code == 202
    assert _cancel(client).status_code == 202
    pending.clear()
    with host._state_guard():
        state = host._load_state()
        run = state["runs"]["run-1"]
        run["worker_launch_state"] = "group_ready"
        run["worker_pid"] = 2_000_000_000
        run["worker_process_group_id"] = 2_000_000_000
        run["worker_session_id"] = 2_000_000_000
        run["worker_start_time"] = "missing-token"
        run.pop("worker_ownership_token", None)
        host._save_state(state)

    reconstructed = RuntimeHost(
        _config(tmp_path),
        clock=_clock,
        generate_id=_ids(),
        launch_worker=pending.append,
    )

    run = reconstructed.current_run()
    assert run is not None
    assert run["status"] == "failed"
    assert run["terminal_classification"] == "host_interrupted"


def test_rust_documented_wire_schema_is_directly_compatible(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)
    rust_request = {
        "activation_request_id": "req-rust-1",
        "console_session_id": "session-rust-1",
        "mission_intent": "survey the ridge",
        "source_authority": "operator_console",
    }

    response = client.post(
        "/api/v1/mission-activations",
        headers={"Authorization": "Bearer cred-rust-1"},
        json=rust_request,
    )

    assert response.status_code == 202
    assert set(response.json()) == {
        "activation_request_id",
        "mission_id",
        "mission_run_id",
        "status",
        "created_at",
    }


def test_activation_requires_bearer_and_has_stable_strict_422_errors(
    tmp_path: Path,
) -> None:
    client, _, _ = _client(tmp_path)

    for headers, body in (
        (
            {},
            {
                "activation_request_id": "request-1",
                "console_session_id": "session-1",
                "mission_intent": "mission",
                "source_authority": "operator_console",
            },
        ),
        (
            {"Authorization": "Basic abc"},
            {
                "activation_request_id": "request-1",
                "console_session_id": "session-1",
                "mission_intent": "mission",
                "source_authority": "operator_console",
            },
        ),
        (
            {"Authorization": "Bearer secret"},
            {
                "activation_request_id": "request-1",
                "console_session_id": "session-1",
                "mission_intent": "mission",
                "source_authority": "operator_console",
                "extra": True,
            },
        ),
        (
            {"Authorization": "Bearer secret"},
            {
                "activation_request_id": "",
                "console_session_id": "session-1",
                "mission_intent": "mission",
                "source_authority": "operator_console",
            },
        ),
    ):
        response = client.post(
            "/api/v1/mission-activations", headers=headers, json=body
        )
        assert response.status_code == 422
        assert response.json() == {
            "error": {
                "code": "invalid_request",
                "message": "request body or authorization is invalid",
            }
        }


def test_idempotency_survives_reconstruction_and_detects_conflicts(
    tmp_path: Path,
) -> None:
    first_client, _, _ = _client(tmp_path)
    original = _activate(first_client)
    reconstructed_client, _, pending = _client(tmp_path)

    replay = _activate(reconstructed_client)
    changed_body = _activate(reconstructed_client, mission_intent="Different mission")
    changed_session = _activate(reconstructed_client, console_session_id="session-2")
    changed_authority = _activate(reconstructed_client, source_authority="other")
    changed_credential = _activate(reconstructed_client, credential="other-secret")

    assert replay.status_code == 202
    assert replay.json() == original.json()
    assert pending == []
    for conflict in (changed_body, changed_session, changed_authority):
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "activation_request_conflict"
    assert changed_credential.status_code == 409
    assert changed_credential.json()["error"]["code"] == (
        "console_session_credential_conflict"
    )


def test_console_session_credential_binding_survives_terminal_run_and_restart(
    tmp_path: Path,
) -> None:
    first_client, _, pending = _client(tmp_path, worker=lambda _context: None)
    assert _activate(first_client).status_code == 202
    pending.pop()()
    assert first_client.get("/api/v1/mission-runs/current").json()["mission_run"][
        "status"
    ] == "succeeded"
    reconstructed_client, _, _ = _client(tmp_path)

    response = _activate(
        reconstructed_client,
        activation_request_id="request-2",
        mission_intent="Hold position",
        credential="different-secret",
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "console_session_credential_conflict"
    persisted = json.loads(
        (tmp_path / "storage/runtime-host/state.json").read_text(encoding="utf-8")
    )
    assert set(persisted["session_verifiers"]) == {"session-1"}
    assert "different-secret" not in json.dumps(persisted)


def test_mission_intent_whitespace_is_preserved_for_idempotency(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)

    original = _activate(client, mission_intent="  survey the ridge\n")
    replay = _activate(client, mission_intent="  survey the ridge\n")
    trimmed = _activate(client, mission_intent="survey the ridge")

    assert original.status_code == replay.status_code == 202
    assert replay.json() == original.json()
    assert trimmed.status_code == 409
    assert trimmed.json()["error"]["code"] == "activation_request_conflict"
    persisted = json.loads(
        (tmp_path / "storage/runtime-host/state.json").read_text(encoding="utf-8")
    )
    assert persisted["activations"]["request-1"]["mission_intent"] == (
        "  survey the ridge\n"
    )


def test_blank_mission_intent_remains_invalid_without_normalizing_body(
    tmp_path: Path,
) -> None:
    client, _, _ = _client(tmp_path)

    response = _activate(client, mission_intent=" \n\t ")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_exactly_one_non_terminal_run_is_allowed(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)
    assert _activate(client).status_code == 202

    response = _activate(client, activation_request_id="request-2")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mission_run_active"


def test_awaiting_human_decision_is_a_nonterminal_run_status(tmp_path: Path) -> None:
    client, host, _ = _client(tmp_path)
    assert _activate(client).status_code == 202
    with host._lock:
        state = host._load_state()
        state["runs"]["run-1"]["status"] = "awaiting_human_decision"
        host._save_state(state)

    response = _activate(client, activation_request_id="request-2")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "mission_run_active"


def test_worker_launcher_failure_is_durable_and_idempotently_replayable(
    tmp_path: Path,
) -> None:
    def fail_to_start(_callback: Callable[[], None]) -> None:
        raise OSError("cannot start process")

    host = RuntimeHost(
        _config(tmp_path),
        clock=_clock,
        generate_id=_ids(),
        worker_entrypoint=lambda _context: None,
        launch_worker=fail_to_start,
    )
    client = TestClient(create_app(host=host), client=("127.0.0.1", 50000))

    accepted = _activate(client)
    replay = _activate(client)
    run = client.get("/api/v1/mission-runs/current").json()["mission_run"]

    assert accepted.status_code == replay.status_code == 202
    assert replay.json() == accepted.json()
    assert accepted.json()["status"] == "queued"
    assert run["status"] == "failed"
    assert run["terminal_classification"] == "worker_start_failed"
    assert run["finished_at"] == "2026-08-24T12:00:00+00:00"


def test_worker_lifecycle_records_success_and_failure(tmp_path: Path) -> None:
    observed_statuses: list[str] = []
    successful_host: RuntimeHost

    def succeed(_context: WorkerContext) -> None:
        current = successful_host.current_run()
        assert current is not None
        observed_statuses.append(str(current["status"]))

    successful, successful_host, pending = _client(
        tmp_path / "success", worker=succeed
    )
    accepted = _activate(successful)
    pending.pop()()
    succeeded = successful.get("/api/v1/mission-runs/current").json()["mission_run"]
    assert accepted.json()["status"] == "queued"
    assert observed_statuses == ["running"]
    assert succeeded["status"] == "succeeded"
    assert succeeded["started_at"] == "2026-08-24T12:00:00+00:00"
    assert succeeded["finished_at"] == "2026-08-24T12:00:00+00:00"
    assert succeeded["terminal_classification"] is None

    def fail(_context: WorkerContext) -> None:
        raise RuntimeError(
            "external environment has no planning data\n"
            "credential Bearer secret-worker-token " + "x" * 1000
        )

    failing, _, pending = _client(tmp_path / "failure", worker=fail)
    _activate(failing)
    pending.pop()()
    failed = failing.get("/api/v1/mission-runs/current").json()["mission_run"]
    assert failed["status"] == "failed"
    assert failed["terminal_classification"] == "worker_failed"
    assert failed["finished_at"] == "2026-08-24T12:00:00+00:00"
    detail = failed["terminal_detail"]
    assert set(detail) == {"kind", "stage", "error_type", "message"}
    assert detail["kind"] == "worker_failed"
    assert detail["stage"] == "worker"
    assert detail["error_type"] == "RuntimeError"
    assert detail["message"].startswith("external environment has no planning data ")
    assert len(detail["message"]) <= 500
    assert "\n" not in detail["message"]
    assert "secret-worker-token" not in json.dumps(failed)
    worker_log = (
        tmp_path / "failure/storage/runtime-host/runs/run-1/worker.log"
    ).read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in worker_log
    assert "RuntimeError: external environment has no planning data" in worker_log


def test_closed_loop_failure_reports_its_stage(tmp_path: Path) -> None:
    def run_closed_loop_demo() -> None:
        raise ValueError("environment planning view has no static_info evidence")

    client, _, pending = _client(tmp_path, worker=lambda _context: run_closed_loop_demo())
    _activate(client)
    pending.pop()()

    detail = client.get("/api/v1/mission-runs/current").json()["mission_run"][
        "terminal_detail"
    ]
    assert detail == {
        "kind": "worker_failed",
        "stage": "closed_loop",
        "error_type": "ValueError",
        "message": "environment planning view has no static_info evidence",
    }


def _reject_coffee(_context: WorkerContext) -> None:
    raise runtime_host_module.MissionRejectedError(
        "'buy me a coffee' is a personal errand, not a bounded operational objective."
    )


def test_mission_rejection_in_worker_process_reaches_current_run(
    tmp_path: Path,
) -> None:
    host = RuntimeHost(
        _config(tmp_path),
        clock=_clock,
        generate_id=_ids(),
        worker_entrypoint=_reject_coffee,
    )
    client = TestClient(create_app(host=host), client=("127.0.0.1", 50000))
    _activate(client, mission_intent="buy me a coffee")

    def current() -> dict[str, Any]:
        return client.get("/api/v1/mission-runs/current").json()["mission_run"]

    _wait_for(lambda: current()["status"] == "failed", timeout=30.0)
    run = current()
    contract = json.loads(
        Path(
            "docs/design/operator-console/contract/v1.2/"
            "mission-runs.current.rejected.response.json"
        ).read_text(encoding="utf-8")
    )["mission_run"]
    assert set(run) == set(contract)
    assert run["terminal_classification"] == contract["terminal_classification"]
    assert run["terminal_detail"] == {
        "kind": "mission_rejected",
        "stage": "intent",
        "reason": (
            "'buy me a coffee' is a personal errand, not a bounded operational "
            "objective."
        ),
    }
    assert set(run["terminal_detail"]) == set(contract["terminal_detail"])
    assert run["stack"] == {"preset_id": "mission1-harbor", "airsim": False, "perception": "off"}
    worker_log = Path(
        json.loads((tmp_path / "storage/runtime-host/state.json").read_text())[
            "runs"
        ]["run-1"]["run_root"]
    ) / "worker.log"
    assert "mission rejected at intent: 'buy me a coffee'" in worker_log.read_text()










def test_reconstruction_marks_non_terminal_run_interrupted(tmp_path: Path) -> None:
    client, _, pending = _client(tmp_path)
    _activate(client)
    pending.pop()  # Simulate a process ending before its queued worker started.

    reconstructed_client, _, _ = _client(tmp_path)
    run = reconstructed_client.get("/api/v1/mission-runs/current").json()["mission_run"]

    assert run["status"] == "failed"
    assert run["started_at"] is None
    assert run["finished_at"] == "2026-08-24T12:00:00+00:00"
    assert run["terminal_classification"] == "host_interrupted"


def test_ephemeral_loopback_server_exercises_real_http_and_durable_lifecycle(
    tmp_path: Path,
) -> None:
    pending: list[Callable[[], None]] = []
    host = RuntimeHost(
        _config(tmp_path),
        clock=_clock,
        generate_id=_ids(),
        worker_entrypoint=lambda _context: None,
        launch_worker=pending.append,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(host=host),
            host="127.0.0.1",
            port=0,
            log_level="error",
        )
    )
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = int(listener.getsockname()[1])
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = monotonic() + 5
    while not server.started and monotonic() < deadline:
        sleep(0.01)
    assert server.started

    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2) as client:
            assert client.get("/api/v1/health").json() == {
                "status": "ok",
                "api_version": {"major": 1, "minor": 3},
            }
            accepted = client.post(
                "/api/v1/mission-activations",
                headers={"Authorization": "Bearer network-secret"},
                json={
                    "activation_request_id": "network-request-1",
                    "console_session_id": "network-session-1",
                    "mission_intent": "survey the network ridge",
                    "source_authority": "operator_console",
                },
            )
            assert accepted.status_code == 202
            assert client.get("/api/v1/mission-runs/current").json()["mission_run"][
                "status"
            ] == "queued"
            pending.pop()()
            assert client.get("/api/v1/mission-runs/current").json()["mission_run"][
                "status"
            ] == "succeeded"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()

    state_path = tmp_path / "storage/runtime-host/state.json"
    assert state_path.is_file()
    assert "network-secret" not in state_path.read_text(encoding="utf-8")
