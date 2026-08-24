from __future__ import annotations

import json
import socket
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from typing import Any

import httpx
import uvicorn
from fastapi.testclient import TestClient

import onr.runtime_host.host as runtime_host_module
from onr.adapters.file_transport import FileTransport
from onr.contracts.hyper_agent import MissionInput
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
    runtime_worker,
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
    return datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc).isoformat()


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
    return TestClient(create_app(host=host)), host, pending


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


def test_health_and_empty_current_run_contract(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)

    assert client.get("/api/v1/health").json() == {
        "status": "ok",
        "api_version": {"major": 1, "minor": 0},
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
    first_client, _, pending = _client(tmp_path)
    assert _activate(first_client).status_code == 202
    pending.pop()()
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
    client = TestClient(create_app(host=host))

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
        raise RuntimeError("secret worker details")

    failing, _, pending = _client(tmp_path / "failure", worker=fail)
    _activate(failing)
    pending.pop()()
    failed = failing.get("/api/v1/mission-runs/current").json()["mission_run"]
    assert failed["status"] == "failed"
    assert failed["terminal_classification"] == "worker_failed"
    assert failed["finished_at"] == "2026-08-24T12:00:00+00:00"
    assert "secret worker details" not in json.dumps(failed)


def test_default_worker_adapts_file_runtime_and_closed_loop_seam(
    tmp_path: Path, monkeypatch: Any
) -> None:
    config = _config(tmp_path)
    config = RuntimeConfig(
        llm=config.llm,
        planners=config.planners,
        heartbeats=config.heartbeats,
        transport=TransportConfig("file", tmp_path / "transport"),
        storage=config.storage,
        services=config.services,
        debug=config.debug,
        agent_name=config.agent_name,
        agents=config.agents,
    )
    observed: dict[str, object] = {}

    class FakeSession:
        def __enter__(self) -> None:
            observed["session_entered"] = True

        def __exit__(self, *_args: object) -> None:
            observed["session_exited"] = True

    class FakeRuntime:
        def __init__(self, selected_config: RuntimeConfig, transport: FileTransport) -> None:
            observed["config"] = selected_config
            observed["transport"] = transport

        def runtime_session(self) -> FakeSession:
            return FakeSession()

    def fake_closed_loop(
        runtime: object,
        mission_input: MissionInput,
        **options: object,
    ) -> object:
        observed["runtime"] = runtime
        observed["mission_input"] = mission_input
        observed["options"] = options
        return object()

    monkeypatch.setattr(runtime_host_module, "RuntimeComposition", FakeRuntime)
    monkeypatch.setattr(runtime_host_module, "run_closed_loop_demo", fake_closed_loop)
    context = WorkerContext(
        config=config,
        mission_id="mission-1",
        mission_run_id="run-1",
        activation_request_id="request-1",
        console_session_id="session-1",
        mission_intent="survey the ridge",
        source_authority="operator_console",
        options=RuntimeWorkerOptions(
            repo_root=tmp_path,
            planner_artifacts=tmp_path / "planner-artifacts",
            recursion_limit=23,
            simulation_limit_seconds=45.0,
        ),
    )

    runtime_worker(context)

    mission = observed["mission_input"]
    assert isinstance(mission, MissionInput)
    assert mission == MissionInput(
        mission_id="mission-1",
        mission_text="survey the ridge",
        source_authority="operator_console",
    )
    transport = observed["transport"]
    assert isinstance(transport, FileTransport)
    assert transport.root == config.transport.root
    assert observed["session_entered"] is True
    assert observed["session_exited"] is True
    assert observed["options"] == {
        "repo_root": tmp_path,
        "planner_artifacts": tmp_path / "planner-artifacts",
        "recursion_limit": 23,
        "simulation_limit_seconds": 45.0,
    }


def test_default_worker_rejects_non_file_transport(tmp_path: Path) -> None:
    context = WorkerContext(
        config=_config(tmp_path),
        mission_id="mission-1",
        mission_run_id="run-1",
        activation_request_id="request-1",
        console_session_id="session-1",
        mission_intent="survey the ridge",
        source_authority="operator_console",
        options=RuntimeWorkerOptions(repo_root=tmp_path),
    )

    try:
        runtime_worker(context)
    except RuntimeError as exc:
        assert str(exc) == "runtime Host worker requires transport.backend=file"
    else:
        raise AssertionError("non-file transport was accepted")


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
                "api_version": {"major": 1, "minor": 0},
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
