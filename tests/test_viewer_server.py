from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection, HTTPResponse
from pathlib import Path
from threading import Thread
from typing import Iterator

import pytest

import onr.viewer.server as viewer_server
from onr.adapters.bayesian_belief_store import FileBayesianBeliefStore
from onr.adapters.file_transport import FileTransport
from onr.adapters.operational_log import FileOperationalLog
from onr.application.bayesian_belief import (
    BayesianBeliefManager,
    belief_artifact_reference,
)
from onr.contracts.bayesian_belief import (
    BayesianBeliefSnapshot,
    BeliefKey,
    EntityAssociation,
    RiskObservation,
)
from onr.contracts.context_coordination import (
    MissionSnapshot,
    mission_snapshot_to_transport_event,
)
from onr.contracts.fsm import (
    FSMExecutionRecord,
    FSMStatus,
    ManeuverFeedback,
    Statechart,
)
from onr.contracts.hyper_agent import ReplanRequest
from onr.contracts.transport import Command, CommandOutcome, TransportEvent
from onr.ports.mission_log_summarizer import SummaryArtifact
from onr.runtime.config import RuntimeConfig
from onr.runtime.lease import RuntimeLease, RuntimeLeaseStore
from onr.viewer.server import ViewerHTTPServer, create_server


def _config(tmp_path: Path) -> tuple[Path, Path, Path]:
    tool = tmp_path / "planner"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    storage = tmp_path / "storage"
    transport = tmp_path / "transport"
    config = tmp_path / "viewer.yaml"
    config.write_text(
        "\n".join(
            (
                "agent_name: test-agent",
                "debug: true",
                "llm:",
                "  provider: openai",
                "  base_url: http://127.0.0.1:1/v1",
                "  model: offline",
                "  api_key: test-private-key",
                "  temperature: 0",
                "planners:",
                "  temporal:",
                f"    entrypoint: {tool}",
                "    timeout_seconds: 1",
                "  symbolic:",
                f"    entrypoint: {tool}",
                "    timeout_seconds: 1",
                "heartbeats:",
                "  hyper_seconds: 1",
                "  maneuver_seconds: 1",
                "  summary_seconds: 30",
                "transport:",
                "  backend: file",
                f"  root: {transport}",
                "storage:",
                f"  root: {storage}",
                "services:",
                "  hyper_agent: hyper-agent",
                "  maneuver_control: maneuver-control",
                "  context_coordination: context-coordination",
                "  fsm_runner: fsm-runner",
                "  planner: planner",
                "agents:",
                "  hyper_agent:",
                "    output_structure_retry:",
                "      max_retries: 2",
                "  maneuver_control:",
                "    output_structure_retry:",
                "      max_retries: 1",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return config, storage, transport


@contextmanager
def _running_server(
    tmp_path: Path, *, host: str = "127.0.0.1"
) -> Iterator[tuple[ViewerHTTPServer, Path, Path]]:
    config, storage, transport = _config(tmp_path)
    static_root = tmp_path / "web"
    static_root.mkdir()
    (static_root / "index.html").write_text("viewer", encoding="utf-8")
    server = create_server(
        host=host,
        port=0,
        repo_root=tmp_path,
        config_path=config,
        static_root=static_root,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, storage, transport
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _request(
    server: ViewerHTTPServer,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[HTTPResponse, bytes]:
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    connection.request(method, path, headers=headers or {})
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response, body


def _activate(storage: Path) -> RuntimeLeaseStore:
    store = RuntimeLeaseStore(storage / "runtime")
    store.start(session_id="private-session")
    return store


def _store_belief(
    storage: Path, mission_id: str
) -> tuple[FileBayesianBeliefStore, BayesianBeliefSnapshot]:
    manager = BayesianBeliefManager(
        mission_id,
        (BeliefKey("contact-1", "collision"),),
        particle_count=32,
        seed=7,
    )
    snapshot = manager.update(
        RiskObservation(
            event_id="risk-1",
            input_revision=1,
            risk_type="collision",
            associations=(EntityAssociation("contact-1", 1.0),),
            likelihood_given_risk=0.9,
            likelihood_given_safe=0.1,
        ),
        created_at="2026-08-19T12:00:00+00:00",
    )
    store = FileBayesianBeliefStore(storage)
    store.save(snapshot, manager.checkpoint())
    return store, snapshot


def test_idle_get_inspection_is_non_mutating_and_returns_no_trace(
    tmp_path: Path,
) -> None:
    with _running_server(tmp_path) as (server, storage, _):
        runtime_path = storage / "runtime"
        runtime_response, runtime_body = _request(server, "GET", "/api/runtime")
        trace_response, trace_body = _request(
            server, "GET", "/api/trace?mission_id=mission-one"
        )
        assert not runtime_path.exists()

    assert runtime_response.status == 200
    assert json.loads(runtime_body) == {
        "active": False,
        "available": False,
        "status": "unavailable",
    }
    assert trace_response.status == 200
    assert json.loads(trace_body) == {"items": []}
    assert trace_response.getheader("Cache-Control") == "no-store"


def test_persisted_artifacts_without_a_runtime_lease_remain_replayable(
    tmp_path: Path,
) -> None:
    with _running_server(tmp_path) as (server, storage, _):
        FileOperationalLog(storage / "operational-log").emit(
            "mission-historical",
            "hyper-agent",
            "planning-context",
            "completed",
            details={"snapshot_id": "mission-historical:snapshot:1"},
        )

        runtime_response, runtime_body = _request(server, "GET", "/api/runtime")
        trace_response, trace_body = _request(
            server, "GET", "/api/trace?mission_id=mission-historical"
        )

    runtime = json.loads(runtime_body)
    assert runtime_response.status == trace_response.status == 200
    assert runtime == {
        "active": False,
        "available": True,
        "status": "historical",
        "mission_ids": ["mission-historical"],
    }
    assert [item["event_kind"] for item in json.loads(trace_body)["items"]] == [
        "planning-context"
    ]


def test_server_rejects_non_loopback_binding(tmp_path: Path) -> None:
    config, _, _ = _config(tmp_path)
    with pytest.raises(ValueError, match="viewer host must"):
        create_server(host="0.0.0.0", port=0, repo_root=tmp_path, config_path=config)
    with pytest.raises(ValueError, match="viewer host must"):
        create_server(
            host="example.com", port=0, repo_root=tmp_path, config_path=config
        )

    localhost = create_server(
        host="localhost", port=0, repo_root=tmp_path, config_path=config
    )
    try:
        assert localhost.server_address[0] == "127.0.0.1"
    finally:
        localhost.server_close()


def test_request_boundary_rejects_foreign_host_and_origin_before_routes(
    tmp_path: Path,
) -> None:
    with _running_server(tmp_path) as (server, storage, _):
        _activate(storage)
        valid_origin = f"http://{server.listener_authority}"
        valid_response, _ = _request(
            server,
            "GET",
            "/api/runtime",
            headers={"Origin": valid_origin},
        )
        rejected = [
            _request(
                server,
                "GET",
                path,
                headers=headers,
            )
            for path, headers in (
                ("/api/runtime", {"Host": "attacker.example"}),
                ("/", {"Host": "127.0.0.1"}),
                (
                    "/api/trace?mission_id=mission-one",
                    {"Origin": "http://attacker.example"},
                ),
                ("/", {"Origin": f"https://{server.listener_authority}"}),
            )
        ]

    assert valid_response.status == 200
    assert viewer_server._loopback_authority("::1", 14398) == "[::1]:14398"
    assert viewer_server._same_loopback_origin("http://[::1]:14398", "::1", 14398)
    for response, body in rejected:
        assert response.status == 403
        assert json.loads(body) == {"error": "forbidden"}


def test_localhost_listener_accepts_only_its_explicit_alias(tmp_path: Path) -> None:
    with _running_server(tmp_path, host="localhost") as (server, storage, _):
        _activate(storage)
        localhost_authority = f"localhost:{server.server_port}"
        accepted, _ = _request(
            server,
            "GET",
            "/api/runtime",
            headers={
                "Host": localhost_authority,
                "Origin": f"http://{localhost_authority}",
            },
        )
        hostile_host, _ = _request(
            server,
            "GET",
            "/api/runtime",
            headers={"Host": f"attacker.example:{server.server_port}"},
        )
        hostile_origin, _ = _request(
            server,
            "GET",
            "/api/runtime",
            headers={
                "Host": localhost_authority,
                "Origin": f"http://attacker.example:{server.server_port}",
            },
        )

    assert accepted.status == 200
    assert hostile_host.status == hostile_origin.status == 403
    assert viewer_server._authority_variants("localhost", 80) == frozenset(
        {"localhost", "localhost:80"}
    )
    assert viewer_server._authority_variants("::1", 80) == frozenset(
        {"[::1]", "[::1]:80"}
    )


def test_public_mapping_reader_rejects_hostile_files_without_following_links(
    tmp_path: Path,
) -> None:
    valid = tmp_path / "valid.json"
    valid.write_text('{"schema_version":1}', encoding="utf-8")
    shallow = tmp_path / "shallow.json"
    shallow.write_text('{"payload":{"items":[{"status":"ready"}]}}', encoding="utf-8")
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    non_finite = tmp_path / "non-finite.json"
    non_finite.write_text('{"value":NaN}', encoding="utf-8")
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (1024 * 1024 + 1))
    recursive = tmp_path / "recursive.json"
    recursive.write_text(
        '{"value":' + "[" * 1100 + "0" + "]" * 1100 + "}",
        encoding="utf-8",
    )
    linked = tmp_path / "linked-receipt.json"
    linked.symlink_to(valid)
    outside_root = tmp_path / "outside-root"
    outside_root.mkdir()
    (outside_root / "outside.json").write_text('{"schema_version":1}', encoding="utf-8")
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(outside_root, target_is_directory=True)

    assert viewer_server._read_mapping(valid) == {"schema_version": 1}
    assert viewer_server._read_mapping(shallow) == {
        "payload": {"items": [{"status": "ready"}]}
    }
    assert all(
        viewer_server._read_mapping(path) is None
        for path in (duplicate, non_finite, oversized, recursive, linked)
    )
    assert (
        viewer_server._read_mapping(linked_root / "outside.json", root=linked_root)
        is None
    )


def test_symlinked_receipts_directory_is_not_projected(tmp_path: Path) -> None:
    mission_id = "mission-receipt"
    with _running_server(tmp_path) as (server, storage, transport_root):
        _activate(storage)
        transport = FileTransport(transport_root)
        command = Command(
            1,
            "command-linked-receipt",
            "correlation-linked-receipt",
            mission_id,
            "planner",
            "plan",
            {"action": "navigate"},
        )
        transport.send_command(command)
        receipts = transport_root / "receipts"
        outside = tmp_path / "outside-receipts"
        receipts.rename(outside)
        receipts.symlink_to(outside, target_is_directory=True)

        response, body = _request(server, "GET", f"/api/trace?mission_id={mission_id}")

    assert response.status == 200
    kinds = {item["event_kind"] for item in json.loads(body)["items"]}
    assert "command" in kinds
    assert "command-receipt" not in kinds


def test_symlinked_transport_and_storage_parent_trees_are_not_projected(
    tmp_path: Path,
) -> None:
    topic_mission = "mission-outside-topic"
    storage_mission = "mission-outside-storage"
    outside_transport = tmp_path / "outside-transport"
    FileTransport(outside_transport).publish_event(
        "advisory",
        TransportEvent(
            1,
            "outside-topic-event",
            topic_mission,
            0,
            "role-skills-advisory",
            {"operation": "outside-topic"},
        ),
    )
    outside_log = tmp_path / "outside-operational-log"
    FileOperationalLog(outside_log).emit(
        storage_mission,
        "runtime",
        "heartbeat",
        "completed",
        details={"operation": "outside-storage"},
    )

    with _running_server(tmp_path) as (server, storage, transport_root):
        _activate(storage)
        transport_root.mkdir(parents=True, exist_ok=True)
        (transport_root / "topics").symlink_to(
            outside_transport / "topics", target_is_directory=True
        )
        (storage / "operational-log").symlink_to(outside_log, target_is_directory=True)

        topic_response, topic_body = _request(
            server, "GET", f"/api/trace?mission_id={topic_mission}"
        )
        storage_response, storage_body = _request(
            server, "GET", f"/api/trace?mission_id={storage_mission}"
        )

    assert topic_response.status == storage_response.status == 200
    assert json.loads(topic_body) == json.loads(storage_body) == {"items": []}
    assert b"outside-topic" not in topic_body
    assert b"outside-storage" not in storage_body


def test_runtime_lists_missions_and_trace_never_merges_them(tmp_path: Path) -> None:
    with _running_server(tmp_path) as (server, storage, _):
        _activate(storage)
        log = FileOperationalLog(storage / "operational-log")
        log.emit("mission-two", "runtime", "heartbeat", "completed")
        log.emit("mission-one", "runtime", "heartbeat", "completed")
        runtime_response, runtime_body = _request(server, "GET", "/api/runtime")
        one_response, one_body = _request(
            server, "GET", "/api/trace?mission_id=mission-one"
        )
        invalid_response, invalid_body = _request(
            server, "GET", "/api/trace?mission_id=../mission-two"
        )
        absent_response, absent_body = _request(server, "GET", "/api/trace")

    assert runtime_response.status == 200
    assert json.loads(runtime_body)["mission_ids"] == ["mission-one", "mission-two"]
    assert one_response.status == 200
    assert {item["mission_id"] for item in json.loads(one_body)["items"]} == {
        "mission-one"
    }
    assert invalid_response.status == absent_response.status == 200
    assert json.loads(invalid_body) == json.loads(absent_body) == {"items": []}


def test_stopped_run_remains_available_and_replayable(tmp_path: Path) -> None:
    with _running_server(tmp_path) as (server, storage, _):
        store = _activate(storage)
        FileOperationalLog(storage / "operational-log").emit(
            "mission-complete", "runtime", "heartbeat", "completed"
        )
        stopped = store.stop()
        runtime_response, runtime_body = _request(server, "GET", "/api/runtime")
        trace_response, trace_body = _request(
            server, "GET", "/api/trace?mission_id=mission-complete"
        )

    runtime = json.loads(runtime_body)
    assert stopped is not None and stopped.status == "stopped"
    assert runtime_response.status == trace_response.status == 200
    assert runtime["active"] is False
    assert runtime["available"] is True
    assert runtime["status"] == "stopped"
    assert runtime["mission_ids"] == ["mission-complete"]
    assert {item["mission_id"] for item in json.loads(trace_body)["items"]} == {
        "mission-complete"
    }


def test_colon_mission_loads_raw_storage_and_encoded_transport_paths(
    tmp_path: Path,
) -> None:
    mission_id = "mission:alpha"
    with _running_server(tmp_path) as (server, storage, transport_root):
        _activate(storage)
        FileOperationalLog(storage / "operational-log").emit(
            mission_id, "runtime", "heartbeat", "completed"
        )
        summary = SummaryArtifact.create(
            mission_id,
            1,
            1,
            1,
            (),
            "Colon mission public summary",
            created_at="2026-08-19T02:00:00+00:00",
        )
        summary_dir = storage / "summaries" / mission_id
        summary_dir.mkdir(parents=True)
        (summary_dir / "00000000000000000001.json").write_text(
            json.dumps(summary.to_dict()), encoding="utf-8"
        )
        chart = Statechart(
            mission_id=mission_id,
            plan_revision=1,
            mission_snapshot_id="snapshot-colon",
            planning_profile="temporal",
            entry_state="state-0",
            terminal_states=("state-0",),
            states=("state-0",),
            state_context={"state-0": {}},
            transitions=(),
        )
        execution = FSMExecutionRecord(
            mission_id=mission_id,
            plan_revision=1,
            statechart_revision=1,
            active_state="state-0",
        )
        fsm_dir = storage / "fsm" / mission_id
        fsm_dir.mkdir(parents=True)
        (fsm_dir / "statechart.json").write_text(
            chart.to_canonical_json(), encoding="utf-8"
        )
        (fsm_dir / "execution-record.json").write_text(
            execution.to_canonical_json(), encoding="utf-8"
        )
        FileTransport(transport_root).publish_event(
            "advisory",
            TransportEvent(
                1,
                "colon-transport-event",
                mission_id,
                0,
                "role-skills-advisory",
                {"operation": "colon mission"},
            ),
        )

        runtime_response, runtime_body = _request(server, "GET", "/api/runtime")
        trace_response, trace_body = _request(
            server, "GET", "/api/trace?mission_id=mission%3Aalpha"
        )

    assert runtime_response.status == trace_response.status == 200
    assert mission_id in json.loads(runtime_body)["mission_ids"]
    items = json.loads(trace_body)["items"]
    assert items and {item["mission_id"] for item in items} == {mission_id}
    assert {
        "heartbeat",
        "summary",
        "statechart",
        "fsm-execution-record",
        "role-skills-advisory",
    } <= {item["event_kind"] for item in items}
    assert (storage / "operational-log" / mission_id).is_dir()
    assert (storage / "summaries" / mission_id).is_dir()
    assert (storage / "fsm" / mission_id).is_dir()
    assert (
        transport_root / "topics" / "advisory" / "missions" / "mission%3Aalpha"
    ).is_dir()


def test_current_snapshot_references_project_without_raw_typed_identity_conflict(
    tmp_path: Path,
) -> None:
    mission_id = "mission-current-snapshot"
    plan_reference = "planner-plan:mission-current-snapshot:1"
    with _running_server(tmp_path) as (server, storage, transport_root):
        _activate(storage)
        snapshot = MissionSnapshot(
            mission_id,
            1,
            "2026-08-19T03:00:00+00:00",
            plan_revision=1,
            plan_reference=plan_reference,
            source_revisions={"plan": 1},
            source_references={"plan": plan_reference},
            source_health={"plan": "healthy"},
            source_freshness={"plan": True},
        )
        canonical_id = f"mission-snapshot:{mission_id}:1"
        FileTransport(transport_root).publish_event(
            "mission-snapshots",
            mission_snapshot_to_transport_event(
                snapshot,
                event_id=canonical_id,
                sequence=0,
            ),
        )

        response, body = _request(server, "GET", f"/api/trace?mission_id={mission_id}")

    assert response.status == 200
    items = json.loads(body)["items"]
    snapshots = [item for item in items if item["event_kind"] == "mission-snapshot"]
    assert {item["event_id"] for item in snapshots} == {
        canonical_id,
        f"transport:{canonical_id}",
    }
    assert all(item["replay_disposition"] == "normal" for item in snapshots)
    assert all(
        item["payload"]["source_references"]["plan"] == plan_reference
        for item in snapshots
    )
    assert not any(
        item["replay_disposition"] in {"duplicate", "conflict"} for item in items
    )


def test_all_documented_public_artifact_categories_are_projected(
    tmp_path: Path,
) -> None:
    mission_id = "mission-live"
    with _running_server(tmp_path) as (server, storage, transport_root):
        _activate(storage)
        transport = FileTransport(transport_root)
        transport.publish_event(
            "advisory",
            TransportEvent(
                1,
                "advisory-event",
                mission_id,
                0,
                "role-skills-advisory",
                {
                    "role_skills": ["navigation"],
                    "correlation_id": "event-correlation",
                    "parent_id": "parent-event",
                },
            ),
        )
        snapshot = MissionSnapshot(mission_id, 1, "2026-01-01T00:00:01+00:00")
        transport.publish_event(
            "mission-snapshots",
            mission_snapshot_to_transport_event(
                snapshot, event_id="snapshot-wire-event", sequence=0
            ),
        )
        status = FSMStatus(
            mission_id=mission_id,
            plan_revision=1,
            statechart_revision=1,
            active_state="state-0",
        )
        transport.publish_event(
            "fsm-status",
            TransportEvent(
                1, "fsm-status-wire", mission_id, 0, "fsm-status", status.to_dict()
            ),
        )
        feedback = ManeuverFeedback(
            "feedback-1",
            mission_id,
            "maneuver-1",
            "completed",
            {
                "correlation_id": "feedback-correlation",
                "command_id": "command-1",
                "source": "environment",
                "plan_revision": 1,
                "snapshot_id": "snapshot-1",
                "analysis": "private feedback reasoning",
            },
        )
        transport.publish_event(
            "maneuver-feedback",
            TransportEvent(
                1,
                feedback.feedback_id,
                mission_id,
                0,
                feedback.event_kind,
                feedback.to_dict(),
            ),
        )
        replan = ReplanRequest(
            "request-1", mission_id, "new fact", "maneuver-control", 1
        )
        transport.publish_event(
            "replan-requests",
            TransportEvent(
                1, "replan-wire", mission_id, 0, "replan-request", replan.to_dict()
            ),
        )

        command = Command(
            1,
            "command-1",
            "command-correlation",
            mission_id,
            "planner",
            "plan",
            {"action": "navigate", "analysis": "private reasoning"},
        )
        transport.send_command(command)
        transport.publish_outcome(
            CommandOutcome(
                1,
                command.command_id,
                command.correlation_id,
                mission_id,
                "completed",
                {"status": "ready", "result": "public-result", "token": "private"},
            )
        )

        FileOperationalLog(storage / "operational-log").emit(
            mission_id,
            "runtime",
            "heartbeat",
            "completed",
            details={"status": "ready", "api_key": "private"},
        )
        summary = SummaryArtifact.create(
            mission_id,
            1,
            1,
            1,
            (),
            "Public mission progress",
            created_at="2026-01-01T00:00:02+00:00",
        )
        summary_dir = storage / "summaries" / mission_id
        summary_dir.mkdir(parents=True)
        (summary_dir / f"{summary.sequence:020d}.json").write_text(
            json.dumps(summary.to_dict()), encoding="utf-8"
        )

        chart = Statechart(
            mission_id=mission_id,
            plan_revision=1,
            mission_snapshot_id="snapshot-1",
            planning_profile="temporal",
            entry_state="state-0",
            terminal_states=("state-0",),
            states=("state-0",),
            state_context={"state-0": {}},
            transitions=(),
        )
        execution = FSMExecutionRecord(
            mission_id=mission_id,
            plan_revision=1,
            statechart_revision=1,
            active_state="state-0",
        )
        fsm_dir = storage / "fsm" / mission_id
        fsm_dir.mkdir(parents=True)
        (fsm_dir / "statechart.json").write_text(
            chart.to_canonical_json(), encoding="utf-8"
        )
        (fsm_dir / "execution-record.json").write_text(
            execution.to_canonical_json(), encoding="utf-8"
        )

        response, body = _request(server, "GET", f"/api/trace?mission_id={mission_id}")

    payload = json.loads(body)
    assert response.status == 200
    items = payload["items"]
    kinds = {item["event_kind"] for item in items}
    assert {
        "role-skills-advisory",
        "mission-snapshot",
        "fsm-status",
        "maneuver-feedback",
        "replan-request",
        "command",
        "command-receipt",
        "command-outcome",
        "heartbeat",
        "summary",
        "statechart",
        "fsm-execution-record",
    } <= kinds
    by_id = {item["event_id"]: item for item in items}
    assert by_id["advisory-event"]["trace_id"] == "advisory-event"
    assert by_id["advisory-event"]["correlation_id"] == "event-correlation"
    assert by_id["advisory-event"]["parent_id"] == "parent-event"
    assert by_id["command:command-1"]["correlation_id"] == "command-correlation"
    assert by_id["receipt:command-1"]["parent_id"] == "command:command-1"
    assert by_id["outcome:command-1"]["payload"] == {
        "result": "public-result",
        "status": "ready",
    }
    typed_feedback = by_id["feedback:feedback-1"]
    assert typed_feedback["component"] == "environment"
    assert typed_feedback["authority"] == "environment-feedback"
    assert typed_feedback["correlation_id"] == "feedback-correlation"
    assert typed_feedback["parent_id"] == "command:command-1"
    assert typed_feedback["payload"] == {
        "command_id": "command-1",
        "feedback_id": "feedback-1",
        "lifecycle": "completed",
        "maneuver_id": "maneuver-1",
        "plan_revision": 1,
        "snapshot_id": "snapshot-1",
        "source": "environment",
    }
    typed_replan = by_id["replan-request:request-1"]
    assert typed_replan["component"] == "hyper-agent"
    assert typed_replan["authority"] == "hyper-agent"
    assert typed_replan["correlation_id"] == "request-1"
    assert typed_replan["parent_id"] == "replan-wire"
    assert typed_replan["payload"]["reason"] == "new fact"
    assert typed_replan["payload"]["observed_plan_revision"] == 1
    rendered = json.dumps(payload).lower()
    assert "private reasoning" not in rendered
    assert "private feedback reasoning" not in rendered
    assert '"token"' not in rendered


def test_current_belief_snapshot_is_hash_bound_public_evidence(tmp_path: Path) -> None:
    mission_id = "mission-belief"
    with _running_server(tmp_path) as (server, storage, _):
        _activate(storage)
        store, snapshot = _store_belief(storage, mission_id)
        committed = json.loads(
            store.current_path(mission_id).read_text(encoding="utf-8")
        )["generation"]
        partial = (
            store.mission_root(mission_id) / "generations" / f"{committed + 1:020d}"
        )
        partial.mkdir()
        (partial / "private-partial.json").write_text(
            '{"private":"unfinished"}', encoding="utf-8"
        )
        mission_root = store.mission_root(mission_id)

        def tree() -> dict[str, tuple[str, bytes | None]]:
            return {
                str(path.relative_to(mission_root)): (
                    "directory" if path.is_dir() else "file",
                    None if path.is_dir() else path.read_bytes(),
                )
                for path in sorted(mission_root.rglob("*"))
            }

        before = tree()

        response, body = _request(server, "GET", f"/api/trace?mission_id={mission_id}")
        after = tree()

    payload = json.loads(body)
    assert response.status == 200
    assert before == after
    assert (partial / "private-partial.json").is_file()
    [belief] = [
        item for item in payload["items"] if item["event_kind"] == "bayesian-belief"
    ]
    assert belief["component"] == "bayesian-belief"
    assert belief["authority"] == "bayesian-belief-source"
    assert belief["payload"] == {
        "content_sha256": snapshot.content_sha256,
        "input_event_id": "risk-1",
        "input_revision": 1,
        "marginals": [item.to_dict() for item in snapshot.marginals],
        "reference": belief_artifact_reference(mission_id, snapshot.content_sha256),
        "revision": 1,
        "source": "bayesian_belief_snapshot",
    }
    rendered = json.dumps(payload)
    assert "checkpoint" not in rendered
    assert "pending-output" not in rendered
    assert "committed-state" not in rendered
    assert "particles" not in rendered


@pytest.mark.parametrize("corruption", ["pointer-binding", "snapshot-content"])
def test_corrupt_or_unbound_current_belief_is_omitted(
    tmp_path: Path, corruption: str
) -> None:
    mission_id = "mission-corrupt-belief"
    with _running_server(tmp_path) as (server, storage, _):
        _activate(storage)
        store, snapshot = _store_belief(storage, mission_id)
        if corruption == "pointer-binding":
            pointer = json.loads(
                store.current_path(mission_id).read_text(encoding="utf-8")
            )
            pointer["snapshot_sha256"] = "0" * 64
            store.current_path(mission_id).write_text(
                json.dumps(pointer), encoding="utf-8"
            )
        else:
            relative = belief_artifact_reference(
                mission_id, snapshot.content_sha256
            ).partition("#")[0]
            artifact_path = storage / relative
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            artifact["marginals"][0]["probability_risk"] = 0.0
            artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

        response, body = _request(server, "GET", f"/api/trace?mission_id={mission_id}")

    assert response.status == 200
    assert json.loads(body) == {"items": []}


def test_lease_expiry_during_collection_keeps_trace_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _running_server(tmp_path) as (server, storage, _):
        store = _activate(storage)
        FileOperationalLog(storage / "operational-log").emit(
            "mission-one", "runtime", "heartbeat", "completed"
        )
        original = viewer_server._load_public_artifacts

        def expire_after_load(config: RuntimeConfig, mission_id: str | None = None):
            artifacts = original(config, mission_id)
            current = store.inspect()
            assert current is not None
            old = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
            expired = RuntimeLease(
                current.session_id, current.pid, current.started_at, old, "active"
            )
            store.path.write_text(json.dumps(expired.to_dict()), encoding="utf-8")
            return artifacts

        monkeypatch.setattr(viewer_server, "_load_public_artifacts", expire_after_load)
        response, body = _request(server, "GET", "/api/trace?mission_id=mission-one")

    assert response.status == 200
    assert {item["mission_id"] for item in json.loads(body)["items"]} == {"mission-one"}


def test_active_run_stopped_during_collection_keeps_trace_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _running_server(tmp_path) as (server, storage, _):
        store = _activate(storage)
        FileOperationalLog(storage / "operational-log").emit(
            "mission-one", "runtime", "heartbeat", "completed"
        )
        original = viewer_server._load_public_artifacts

        def stop_after_load(config: RuntimeConfig, mission_id: str | None = None):
            artifacts = original(config, mission_id)
            stopped = store.stop()
            assert stopped is not None and stopped.status == "stopped"
            return artifacts

        monkeypatch.setattr(viewer_server, "_load_public_artifacts", stop_after_load)
        response, body = _request(server, "GET", "/api/trace?mission_id=mission-one")

    assert response.status == 200
    assert {item["mission_id"] for item in json.loads(body)["items"]} == {"mission-one"}


def test_lease_replacement_during_projection_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _running_server(tmp_path) as (server, storage, _):
        store = _activate(storage)
        FileOperationalLog(storage / "operational-log").emit(
            "mission-one", "runtime", "heartbeat", "completed"
        )
        original = server.application._projection.project

        def replace_during_projection(records: object):
            items = original(records)  # type: ignore[arg-type]
            now = datetime.now(timezone.utc).isoformat()
            replacement = RuntimeLease("replacement", 999, now, now, "active")
            store.path.write_text(json.dumps(replacement.to_dict()), encoding="utf-8")
            return items

        monkeypatch.setattr(
            server.application._projection, "project", replace_during_projection
        )
        response, body = _request(server, "GET", "/api/trace?mission_id=mission-one")

    assert response.status == 200
    assert json.loads(body) == {"items": []}


def test_static_routes_never_expose_memory_or_traversal_and_send_csp(
    tmp_path: Path,
) -> None:
    with _running_server(tmp_path) as (server, storage, _):
        memory = storage / "mission-memory" / "mission-one" / "role" / "memory"
        memory.mkdir(parents=True)
        (memory / "AGENTS.md").write_text("mission-memory-secret", encoding="utf-8")
        (tmp_path / "secret.txt").write_text("traversal-secret", encoding="utf-8")
        memory_response, memory_body = _request(
            server, "GET", "/mission-memory/mission-one/role/memory/AGENTS.md"
        )
        traversal_response, traversal_body = _request(
            server, "GET", "/%2e%2e/secret.txt"
        )
        index_response, _ = _request(server, "GET", "/")

    assert memory_response.status == traversal_response.status == 404
    assert b"mission-memory-secret" not in memory_body
    assert b"traversal-secret" not in traversal_body
    csp = index_response.getheader("Content-Security-Policy")
    assert csp is not None and "default-src 'self'" in csp


def test_unsupported_methods_return_405(tmp_path: Path) -> None:
    with _running_server(tmp_path) as (server, _, _):
        response, body = _request(server, "POST", "/api/runtime")

    assert response.status == 405
    assert response.getheader("Allow") == "GET, HEAD"
    assert json.loads(body) == {"error": "method_not_allowed"}
