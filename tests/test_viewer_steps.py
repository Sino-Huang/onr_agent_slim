from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPConnection, HTTPResponse
from pathlib import Path
from threading import Thread
from typing import cast
from urllib.parse import quote, urlencode

from onr.adapters.file_transport import FileTransport
from onr.adapters.operational_log import FileOperationalLog
from onr.contracts.transport import TransportEvent
from onr.viewer.server import ViewerHTTPServer, create_server
from onr.viewer.steps import (
    MISSION_CONTENT_WARNING,
    PHASES,
    TEXT_FIELD_LIMIT,
    StepProjection,
)

MISSION_ID = "mission:steps"
STARTED = "2026-08-21T10:00:00+00:00"
FINISHED = "2026-08-21T10:00:01+00:00"


def _llm_invocation(
    *, sequence: int = 3, invocation_id: str = "llm-1"
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "sequence": sequence,
        "invocation_id": invocation_id,
        "parent_id": None,
        "agent_role": "hyper-agent",
        "role": "hyper-agent",
        "kind": "llm",
        "name": "planner_executor",
        "input": {
            "correlation_id": "corr-1",
            "messages": [{"role": "user", "content": "private invocation"}],
        },
        "output": None,
        "error": None,
        "started_at": STARTED,
        "finished_at": FINISHED,
    }


def _tool_invocation() -> dict[str, object]:
    return {
        "schema_version": 1,
        "sequence": 4,
        "invocation_id": "tool-1",
        "parent_id": "llm-1",
        "agent_role": "hyper-agent",
        "role": "hyper-agent",
        "kind": "tool",
        "name": "run_minizinc",
        "input": {"attempt": 2, "model": "workspace/002/model.mzn"},
        "output": {"status": "accepted"},
        "error": None,
        "started_at": "2026-08-21T10:00:00.200000+00:00",
        "finished_at": "2026-08-21T10:00:00.700000+00:00",
    }


def _planning_intent_invocation() -> dict[str, object]:
    return {
        "schema_version": 1,
        "sequence": 2,
        "invocation_id": "planning-intent-tool",
        "parent_id": None,
        "agent_role": "hyper-agent",
        "kind": "tool",
        "name": "record_planning_intent",
        "input": {
            "title": "Account for reported events",
            "objective": "Patrol sector seven and account for every reported event.",
            "sector": "sector-7",
            "constraints": ["Remain within the patrol boundary."],
            "issued_at": "2026-08-21T09:59:00+00:00",
            "source_authority": "operator",
            "details": {
                "mission_pattern": "report_event_accounting_patrol",
                "capture_rule": "Observe each event from within sensor range.",
            },
        },
        "output": {"status": "recorded"},
        "error": None,
        "started_at": STARTED,
        "finished_at": FINISHED,
    }


def _llm_record(*, sequence: int = 3) -> dict[str, object]:
    return {
        "schema_version": 1,
        "role": "hyper-agent",
        "sequence": sequence,
        "request": {
            "messages": [{"role": "user", "content": "private prompt"}],
            "invocation_params": {"api_key": "sk-private"},
        },
        "response_id": "response-1",
        "model": "reasoning-model",
        "status_code": 200,
        "finish_reason": "tool_calls",
        "content": "I will execute planner attempt two.",
        "function_call": None,
        "reasoning": "The second planner attempt satisfies the constraints.",
        "reasoning_content": "Check objective and feasibility.",
        "reasoning_details": [{"type": "summary", "text": "Verified."}],
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "run_minizinc",
                    "arguments": json.dumps({"attempt": 2}),
                },
            }
        ],
    }


def _operational_log() -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_id": "record-1",
        "mission_id": MISSION_ID,
        "sequence": 7,
        "event_time": "2026-08-21T10:00:00.500000+00:00",
        "source": "hyper-agent",
        "event_kind": "planner-execution",
        "outcome": "completed",
        "details": {"correlation_id": "corr-1", "attempt": 2},
    }


def _transport_feedback() -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": "feedback-1",
        "mission_id": MISSION_ID,
        "sequence": 8,
        "event_kind": "maneuver-feedback",
        "payload": {
            "correlation_id": "corr-1",
            "maneuver_id": "maneuver-1",
            "lifecycle": "completed",
        },
    }


def _artifact() -> dict[str, str]:
    return {
        "kind": "model.mzn",
        "ref": "workspace/002/model.mzn",
        "label": "MiniZinc model (attempt 2)",
    }


def test_full_join_builds_nested_step_with_decision_feedback_and_artifact() -> None:
    payload = (
        StepProjection()
        .project(
            MISSION_ID,
            llm_records=[_llm_record()],
            agent_invocations=[_llm_invocation(), _tool_invocation()],
            operational_logs=[_operational_log()],
            transport_events=[_transport_feedback()],
            planner_artifacts=[_artifact()],
            generated_at="2026-08-21T10:01:00+00:00",
        )
        .to_dict()
    )

    assert payload["schema_version"] == 2
    assert payload["phases"] == list(PHASES)
    assert payload["warnings"] == []
    [step] = cast(list[dict[str, object]], payload["steps"])
    assert step["step_id"] == "hyper-agent:3"
    assert step["phase"] == "planner-execution"
    assert step["title"] == "Planner execution (attempt 2)"
    assert step["decision"] == {
        "event_kind": "planner-execution",
        "outcome": "completed",
        "details": {"correlation_id": "corr-1", "attempt": 2},
    }
    assert step["feedback"] == [
        {
            "kind": "maneuver-feedback",
            "payload": {
                "correlation_id": "corr-1",
                "maneuver_id": "maneuver-1",
                "lifecycle": "completed",
            },
        }
    ]
    assert step["artifacts"] == [_artifact()]
    assert step["tool_calls"] == [
        {
            "name": "run_minizinc",
            "args": {"attempt": 2},
            "arguments_text": None,
            "partial": False,
            "result": {"status": "accepted"},
            "error": None,
            "duration_ms": 500,
        }
    ]
    [child] = cast(list[dict[str, object]], step["children"])
    assert child["kind"] == "tool"
    assert child["name"] == "run_minizinc"
    assert child["children"] == []


def test_tool_result_joins_by_tool_call_id_when_parent_is_a_graph_node() -> None:
    # LangGraph records tool runs as siblings of the chat-model run: the
    # tool's parent_run_id is a graph node run that is never recorded, so
    # parent-based correlation fails and only the provider tool-call id joins.
    tool = {
        **_tool_invocation(),
        "parent_id": "langgraph-node-run",
        "output": {
            "content": "accepted",
            "status": "success",
            "tool_call_id": "call-1",
            "type": "tool",
        },
    }
    payload = (
        StepProjection()
        .project(
            MISSION_ID,
            llm_records=[_llm_record()],
            agent_invocations=[_llm_invocation(), tool],
            planner_artifacts=[_artifact()],
            generated_at="2026-08-21T10:01:00+00:00",
        )
        .to_dict()
    )

    steps = cast(list[dict[str, object]], payload["steps"])
    assert len(steps) == 1
    [step] = steps
    assert step["kind"] == "llm"
    [call] = cast(list[dict[str, object]], step["tool_calls"])
    assert call["name"] == "run_minizinc"
    assert call["result"] == {
        "content": "accepted",
        "status": "success",
        "tool_call_id": "call-1",
        "type": "tool",
    }
    assert call["error"] is None
    assert call["duration_ms"] == 500
    [child] = cast(list[dict[str, object]], step["children"])
    assert child["kind"] == "tool"
    assert child["name"] == "run_minizinc"


def test_tool_result_joins_command_shaped_output_by_tool_call_id() -> None:
    # Tools returning LangGraph Commands nest the id under update.messages.
    tool = {
        **_tool_invocation(),
        "parent_id": "langgraph-node-run",
        "output": {
            "update": {
                "messages": [
                    {
                        "content": "Updated todo list",
                        "status": "success",
                        "tool_call_id": "call-1",
                        "type": "tool",
                    }
                ],
                "todos": [],
            }
        },
    }
    payload = (
        StepProjection()
        .project(
            MISSION_ID,
            llm_records=[_llm_record()],
            agent_invocations=[_llm_invocation(), tool],
            planner_artifacts=[_artifact()],
            generated_at="2026-08-21T10:01:00+00:00",
        )
        .to_dict()
    )

    [step] = cast(list[dict[str, object]], payload["steps"])
    [call] = cast(list[dict[str, object]], step["tool_calls"])
    # The join key is read from the raw output; the public result keeps the
    # documented messages redaction.
    assert call["result"] == {"update": {"todos": []}}
    children = cast(list[dict[str, object]], step["children"])
    assert [child["name"] for child in children] == ["run_minizinc"]


def test_tool_without_matching_call_id_stays_a_root_step() -> None:
    tool = {
        **_tool_invocation(),
        "parent_id": "langgraph-node-run",
        "output": {"status": "accepted", "tool_call_id": "call-unrelated"},
    }
    payload = (
        StepProjection()
        .project(
            MISSION_ID,
            llm_records=[_llm_record()],
            agent_invocations=[_llm_invocation(), tool],
            planner_artifacts=[_artifact()],
            generated_at="2026-08-21T10:01:00+00:00",
        )
        .to_dict()
    )

    steps = cast(list[dict[str, object]], payload["steps"])
    assert [step["kind"] for step in steps] == ["llm", "tool"]
    [call] = cast(list[dict[str, object]], steps[0]["tool_calls"])
    assert call["result"] is None
    [tool_call] = cast(list[dict[str, object]], steps[1]["tool_calls"])
    assert tool_call["result"] == {
        "status": "accepted",
        "tool_call_id": "call-unrelated",
    }


def test_debug_absent_degrades_to_operational_and_transport_steps() -> None:
    payload = (
        StepProjection()
        .project(
            MISSION_ID,
            operational_logs=[
                {
                    **_operational_log(),
                    "source": "maneuver-control",
                    "event_kind": "heartbeat",
                }
            ],
            transport_events=[
                {
                    **_transport_feedback(),
                    "payload": {"maneuver_id": "maneuver-1", "lifecycle": "completed"},
                }
            ],
            planner_artifacts=[_artifact()],
            generated_at="2026-08-21T10:01:00+00:00",
        )
        .to_dict()
    )

    steps = cast(list[dict[str, object]], payload["steps"])
    warnings = cast(list[str], payload["warnings"])
    assert {step["kind"] for step in steps} == {"decision", "feedback"}
    assert all(step["reasoning"] is None for step in steps)
    assert any("Debug artifacts are unavailable" in warning for warning in warnings)


def test_missing_planner_artifacts_is_a_warning_not_an_error() -> None:
    payload = (
        StepProjection()
        .project(
            MISSION_ID,
            llm_records=[_llm_record()],
            agent_invocations=[_llm_invocation()],
            generated_at="2026-08-21T10:01:00+00:00",
        )
        .to_dict()
    )

    [step] = cast(list[dict[str, object]], payload["steps"])
    assert step["artifacts"] == []
    assert "Planner artifacts are unavailable." in cast(list[str], payload["warnings"])


def test_reasoning_uses_an_explicit_allowlist_and_never_exposes_requests() -> None:
    payload = (
        StepProjection()
        .project(
            MISSION_ID,
            llm_records=[_llm_record()],
            agent_invocations=[_llm_invocation()],
            planner_artifacts=[_artifact()],
            generated_at="2026-08-21T10:01:00+00:00",
        )
        .to_dict()
    )

    rendered = json.dumps(payload)
    [step] = cast(list[dict[str, object]], payload["steps"])
    reasoning = cast(str, step["reasoning"])
    assert "second planner attempt" in reasoning
    assert "Check objective" in reasoning
    assert "Verified" in reasoning
    assert step["content"] == "I will execute planner attempt two."
    assert step["model"] == "reasoning-model"
    assert step["finish_reason"] == "tool_calls"
    assert "private prompt" not in rendered
    assert "sk-private" not in rendered
    assert "request" not in rendered
    assert "invocation_params" not in rendered


def test_v2_partial_record_pairs_by_invocation_and_keeps_draft_arguments_raw() -> None:
    invocation = {
        **_llm_invocation(sequence=8, invocation_id="exact-invocation"),
        "schema_version": 2,
        "finished_at": None,
        "updated_at": FINISHED,
        "completion_state": "live",
        "revision": 1,
    }
    partial = {
        **_llm_record(sequence=1),
        "schema_version": 2,
        "invocation_id": "exact-invocation",
        "content": "x" * (TEXT_FIELD_LIMIT + 5),
        "finish_reason": None,
        "started_at": STARTED,
        "updated_at": FINISHED,
        "finished_at": None,
        "completion_state": "live",
        "revision": 4,
        "error": None,
        "tool_calls": [
            {
                "index": 0,
                "type": "function",
                "function": {
                    "name": "run_minizinc",
                    "arguments": '{"attempt":',
                },
            }
        ],
    }

    payload = (
        StepProjection()
        .project(
            MISSION_ID,
            llm_records=[partial],
            agent_invocations=[invocation],
            planner_artifacts=[_artifact()],
        )
        .to_dict()
    )

    [step] = cast(list[dict[str, object]], payload["steps"])
    assert step["step_id"] == "hyper-agent:8"
    assert step["completion_state"] == "live"
    assert step["updated_at"] == FINISHED
    assert step["duration_ms"] == 1000
    assert step["revision"] == 4
    assert step["status"] == "unknown"
    assert len(cast(str, step["content"])) == TEXT_FIELD_LIMIT
    assert step["truncated"] is True
    assert step["tool_calls"] == [
        {
            "name": "run_minizinc",
            "args": {},
            "arguments_text": '{"attempt":',
            "partial": True,
            "result": None,
            "error": None,
            "duration_ms": None,
        }
    ]

    completed = {
        **partial,
        "content": "done",
        "finish_reason": "tool_calls",
        "finished_at": FINISHED,
        "completion_state": "complete",
        "revision": 5,
        "tool_calls": [
            {
                "index": 0,
                "type": "function",
                "function": {
                    "name": "run_minizinc",
                    "arguments": '{"attempt":2}',
                },
            }
        ],
    }
    completed_step = (
        StepProjection()
        .project(
            MISSION_ID,
            llm_records=[completed],
            agent_invocations=[invocation],
            planner_artifacts=[_artifact()],
        )
        .to_dict()["steps"][0]
    )
    assert completed_step["completion_state"] == "complete"
    assert completed_step["revision"] == 5
    assert completed_step["tool_calls"][0]["args"] == {"attempt": 2}
    assert completed_step["tool_calls"][0]["partial"] is False


def test_mission_content_uses_planning_intent_tool_before_snapshot_fallback() -> None:
    mission, warnings = StepProjection().mission_content(
        operational_logs=[
            {
                **_operational_log(),
                "event_kind": "planning-intent",
                "details": {"planning_intent_sha256": "a" * 64},
            }
        ],
        agent_invocations=[_planning_intent_invocation()],
        transport_events=[
            {
                "schema_version": 1,
                "event_id": "snapshot-1",
                "mission_id": MISSION_ID,
                "sequence": 1,
                "event_kind": "mission-snapshot",
                "payload": {"objective": "Fallback snapshot objective."},
            }
        ],
    )

    assert warnings == ()
    assert mission == {
        "title": "Account for reported events",
        "objective": "Patrol sector seven and account for every reported event.",
        "constraints": ["Remain within the patrol boundary."],
        "sector": "sector-7",
        "issued_at": "2026-08-21T09:59:00+00:00",
        "source_authority": "operator",
        "mission_pattern": "report_event_accounting_patrol",
        "capture_rule": "Observe each event from within sensor range.",
    }


def test_run_overview_uses_null_and_warning_when_mission_content_is_absent() -> None:
    projection = StepProjection()
    mission, mission_warnings = projection.mission_content(
        operational_logs=[_operational_log()],
        agent_invocations=[_llm_invocation()],
        transport_events=[
            {
                "schema_version": 1,
                "event_id": "snapshot-1",
                "mission_id": MISSION_ID,
                "sequence": 1,
                "event_kind": "mission-snapshot",
                "payload": {"plan_revision": 1, "fsm_status": None},
            }
        ],
    )
    view = projection.project(
        MISSION_ID,
        planner_artifacts=[_artifact()],
        generated_at="2026-08-21T10:01:00+00:00",
    )
    payload = projection.overview(
        view,
        mission=mission,
        warnings=mission_warnings,
    ).to_dict()

    assert payload["mission"] is None
    assert MISSION_CONTENT_WARNING in cast(list[str], payload["warnings"])


def _config(tmp_path: Path) -> tuple[Path, Path, Path]:
    tool = tmp_path / "planner"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    storage = tmp_path / "var" / "storage"
    transport = tmp_path / "var" / "transport"
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
    tmp_path: Path,
) -> Iterator[tuple[ViewerHTTPServer, Path, Path]]:
    config, storage, transport = _config(tmp_path)
    static_root = tmp_path / "web"
    static_root.mkdir()
    (static_root / "index.html").write_text("viewer", encoding="utf-8")
    server = create_server(
        host="127.0.0.1",
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
    server: ViewerHTTPServer, method: str, path: str
) -> tuple[HTTPResponse, bytes]:
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    connection.request(method, path)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response, body


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _server_llm_record() -> dict[str, object]:
    record = _llm_record(sequence=1)
    record.pop("role")
    record.pop("sequence")
    return record


def _server_invocation() -> dict[str, object]:
    invocation = _llm_invocation(sequence=1)
    invocation.pop("role")
    return invocation


def test_steps_run_and_artifact_endpoints(tmp_path: Path) -> None:
    with _running_server(tmp_path) as (server, storage, transport_root):
        mission_name = quote(MISSION_ID, safe="._-")
        _write(
            storage.parent
            / "debug"
            / "agent"
            / "hyper-agent"
            / mission_name
            / "00000000000000000001.json",
            _server_invocation(),
        )
        _write(
            storage.parent
            / "debug"
            / "agent"
            / "hyper-agent"
            / mission_name
            / "00000000000000000002.json",
            _planning_intent_invocation(),
        )
        _write(
            storage.parent
            / "debug"
            / "llm"
            / "hyper-agent"
            / mission_name
            / "00000000000000000001.json",
            _server_llm_record(),
        )
        FileOperationalLog(storage / "operational-log").emit(
            MISSION_ID,
            "hyper-agent",
            "planner-execution",
            "completed",
            details={"attempt": 2, "correlation_id": "corr-1"},
        )
        FileTransport(transport_root).publish_event(
            "maneuver-feedback",
            TransportEvent(
                1,
                "feedback-endpoint",
                MISSION_ID,
                0,
                "maneuver-feedback",
                {
                    "schema_version": 1,
                    "feedback_id": "feedback-endpoint",
                    "mission_id": MISSION_ID,
                    "maneuver_id": "maneuver-1",
                    "lifecycle": "completed",
                    "payload": {"correlation_id": "corr-1"},
                },
            ),
        )
        model = (
            tmp_path / "var" / "planner-artifacts" / "workspace" / "002" / "model.mzn"
        )
        model.parent.mkdir(parents=True)
        model.write_text("solve satisfy;\n", encoding="utf-8")
        statechart = (
            tmp_path
            / "var"
            / "planner-artifacts"
            / "statechart-attempts"
            / "001"
            / "accepted-statechart.json"
        )
        statechart.parent.mkdir(parents=True)
        statechart.write_text('{"accepted":true}\n', encoding="utf-8")

        encoded_mission = quote(MISSION_ID, safe="")
        steps_response, steps_body = _request(
            server, "GET", f"/api/steps?mission_id={encoded_mission}"
        )
        run_response, run_body = _request(
            server, "GET", f"/api/run?mission_id={encoded_mission}"
        )
        artifact_response, artifact_body = _request(
            server,
            "GET",
            "/api/artifact?"
            + urlencode(
                {
                    "mission_id": MISSION_ID,
                    "ref": "workspace/002/model.mzn",
                }
            ),
        )
        json_response, json_body = _request(
            server,
            "GET",
            "/api/artifact?"
            + urlencode(
                {
                    "mission_id": MISSION_ID,
                    "ref": "statechart-attempts/001/accepted-statechart.json",
                }
            ),
        )

    steps = json.loads(steps_body)
    run = json.loads(run_body)
    assert (
        steps_response.status == run_response.status == artifact_response.status == 200
    )
    assert steps["mission_id"] == MISSION_ID
    assert steps["steps"][0]["reasoning"].startswith("The second planner attempt")
    assert steps["steps"][0]["tool_calls"][0]["name"] == "run_minizinc"
    assert run["mission_id"] == MISSION_ID
    assert run["mission"] == {
        "title": "Account for reported events",
        "objective": "Patrol sector seven and account for every reported event.",
        "constraints": ["Remain within the patrol boundary."],
        "sector": "sector-7",
        "issued_at": "2026-08-21T09:59:00+00:00",
        "source_authority": "operator",
        "mission_pattern": "report_event_accounting_patrol",
        "capture_rule": "Observe each event from within sensor range.",
    }
    assert run["status"] == "complete"
    assert run["aggregates"]["llm_call_count"] == 1
    assert run["aggregates"]["planner_attempts"] == 1
    assert run["aggregates"]["statechart_attempts"] == 1
    assert {item["kind"] for item in run["artifacts_index"]} == {
        "model.mzn",
        "accepted-statechart.json",
    }
    assert artifact_response.getheader("Content-Type") == "text/plain; charset=utf-8"
    assert artifact_body == b"solve satisfy;\n"
    assert json_response.status == 200
    assert json_response.getheader("Content-Type") == "application/json"
    assert json.loads(json_body) == {"accepted": True}


def test_artifact_endpoint_rejects_path_confinement_escape(tmp_path: Path) -> None:
    outside = tmp_path / "model.mzn"
    outside.write_text("private outside artifact", encoding="utf-8")
    with _running_server(tmp_path) as (server, _, _):
        response, body = _request(
            server,
            "GET",
            "/api/artifact?"
            + urlencode({"mission_id": MISSION_ID, "ref": "../model.mzn"}),
        )

    assert response.status == 404
    assert json.loads(body) == {"error": "not_found"}
    assert b"private outside artifact" not in body
