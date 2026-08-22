from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection, HTTPResponse
from pathlib import Path
from threading import Thread
from urllib.parse import quote

import pytest

import onr.viewer.server as viewer_server
from onr.runtime.lease import RuntimeLease, RuntimeLeaseStore
from onr.viewer.server import ViewerHTTPServer, create_server

_EMPTY = {
    "enabled": False,
    "profiles": [],
    "invocations": [],
    "conversations": [],
}


def _config(tmp_path: Path, *, debug: bool = True) -> tuple[Path, Path]:
    tool = tmp_path / "planner"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    storage = tmp_path / "storage"
    config = tmp_path / "viewer.yaml"
    config.write_text(
        "\n".join(
            (
                "agent_name: test-agent",
                f"debug: {'true' if debug else 'false'}",
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
                "  backend: inprocess",
                f"  root: {tmp_path / 'transport'}",
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
    return config, storage


@contextmanager
def _running_server(
    tmp_path: Path, *, debug: bool = True
) -> Iterator[tuple[ViewerHTTPServer, Path]]:
    config, storage = _config(tmp_path, debug=debug)
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
        yield server, storage
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
    store.start(session_id="debug-session")
    return store


def _mission_root(
    storage: Path,
    mission_id: str,
    *,
    role: str = "hyper-agent",
    legacy: bool = False,
) -> Path:
    root = storage.parent / "debug" / "agent"
    if not legacy:
        root /= role
    root /= quote(mission_id, safe="._-")
    (root / "profiles").mkdir(parents=True)
    return root


def _llm_root(storage: Path, mission_id: str, role: str) -> Path:
    root = storage.parent / "debug" / "llm" / role / quote(mission_id, safe="._-")
    root.mkdir(parents=True)
    return root


def _profile(role: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "agent_role": role,
        "skills": [{"name": "navigation", "version": "1.2", "path": "skills/nav.md"}],
        "tools": ["planner", "telemetry"],
    }


def _invocation(
    sequence: int, invocation_id: str, *, role: str = "hyper-agent"
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "sequence": sequence,
        "invocation_id": invocation_id,
        "parent_id": None,
        "agent_role": role,
        "kind": "llm",
        "name": "reason",
        "input": {"messages": [{"role": "user", "content": "private input"}]},
        "output": {"choices": [{"text": "private output", "score": 0.75}]},
        "error": None,
        "started_at": "2026-08-19T01:00:00+00:00",
        "finished_at": "2026-08-19T01:00:01+00:00",
    }


def _llm_artifact() -> dict[str, object]:
    return {
        "schema_version": 1,
        "request": {
            "model": "reasoning-model",
            "messages": [{"role": "user", "content": "private prompt"}],
        },
        "response_id": "chatcmpl-1",
        "model": "reasoning-model",
        "status_code": 200,
        "finish_reason": "stop",
        "content": "private answer",
        "function_call": None,
        "reasoning": "provider reasoning",
        "reasoning_content": "provider reasoning content",
        "reasoning_details": [{"type": "summary", "text": "provider detail"}],
        "tool_calls": [{"id": "call-1", "type": "function"}],
    }


def _llm_artifact_v2() -> dict[str, object]:
    return {
        **_llm_artifact(),
        "schema_version": 2,
        "sequence": 1,
        "invocation_id": "invocation-live",
        "status_code": 200,
        "finish_reason": None,
        "content": "growing",
        "reasoning": "thinking",
        "reasoning_content": "",
        "tool_calls": [
            {
                "index": 0,
                "type": "function",
                "function": {"name": "inspect", "arguments": '{"path":'},
            }
        ],
        "error": None,
        "started_at": "2026-08-19T01:00:00+00:00",
        "updated_at": "2026-08-19T01:00:01+00:00",
        "finished_at": None,
        "completion_state": "live",
        "revision": 3,
    }


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_active_debug_is_mission_scoped_sorted_and_preserves_raw_json(
    tmp_path: Path,
) -> None:
    with _running_server(tmp_path) as (server, storage):
        _activate(storage)
        selected = _mission_root(storage, "mission:one")
        other = _mission_root(storage, "mission-two")
        _write(selected / "profiles" / "z.json", _profile("planner"))
        _write(selected / "profiles" / "a.json", _profile("hyper-agent"))
        _write(selected / "20.json", _invocation(2, "invocation-b"))
        _write(selected / "10.json", _invocation(1, "invocation-a"))
        _write(other / "01.json", _invocation(1, "other-mission"))
        _write(
            _llm_root(storage, "mission:one", "hyper-agent")
            / "00000000000000000001.json",
            _llm_artifact(),
        )

        response, body = _request(server, "GET", "/api/debug?mission_id=mission%3Aone")

    payload = json.loads(body)
    assert response.status == 200
    assert payload["enabled"] is True
    assert [item["agent_role"] for item in payload["profiles"]] == [
        "hyper-agent",
        "planner",
    ]
    assert [item["invocation_id"] for item in payload["invocations"]] == [
        "invocation-a",
        "invocation-b",
    ]
    assert payload["invocations"][0]["input"] == {
        "messages": [{"role": "user", "content": "private input"}]
    }
    assert payload["invocations"][0]["output"] == {
        "choices": [{"text": "private output", "score": 0.75}]
    }
    assert all(item["role"] == "hyper-agent" for item in payload["profiles"])
    assert all(item["role"] == "hyper-agent" for item in payload["invocations"])
    assert payload["conversations"] == [
        {
            "role": "hyper-agent",
            "sequence": 1,
            "response_id": "chatcmpl-1",
            "model": "reasoning-model",
            "request": {
                "model": "reasoning-model",
                "messages": [{"role": "user", "content": "private prompt"}],
            },
            "input": [{"role": "user", "content": "private prompt"}],
            "reasoning": "provider reasoning",
            "reasoning_content": "provider reasoning content",
            "reasoning_details": [{"type": "summary", "text": "provider detail"}],
            "output": {
                "content": "private answer",
                "function_call": None,
                "tool_calls": [{"id": "call-1", "type": "function"}],
            },
            "content": "private answer",
            "function_call": None,
            "tool_calls": [{"id": "call-1", "type": "function"}],
            "finish_reason": "stop",
            "status_code": 200,
        }
    ]
    assert "other-mission" not in json.dumps(payload)


@pytest.mark.parametrize("terminal_status", ["stopped", "stale"])
def test_terminal_lease_keeps_debug_visible(
    tmp_path: Path, terminal_status: str
) -> None:
    with _running_server(tmp_path) as (server, storage):
        store = _activate(storage)
        mission_root = _mission_root(storage, "mission-complete")
        _write(mission_root / "01.json", _invocation(1, "completed"))
        if terminal_status == "stopped":
            store.stop()
        else:
            current = store.inspect()
            assert current is not None
            old = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
            store.path.write_text(
                json.dumps(
                    RuntimeLease(
                        current.session_id,
                        current.pid,
                        current.started_at,
                        old,
                        "active",
                    ).to_dict()
                ),
                encoding="utf-8",
            )

        response, body = _request(
            server, "GET", "/api/debug?mission_id=mission-complete"
        )

    assert response.status == 200
    assert json.loads(body)["invocations"][0]["invocation_id"] == "completed"
    assert json.loads(body)["enabled"] is True


def test_debug_artifacts_without_a_runtime_lease_remain_visible(tmp_path: Path) -> None:
    with _running_server(tmp_path) as (server, storage):
        mission_root = _mission_root(storage, "mission-historical")
        _write(
            mission_root / "profiles" / "hyper-agent.json",
            _profile("hyper-agent"),
        )
        _write(
            mission_root / "01.json",
            {
                **_invocation(1, "tool-call"),
                "kind": "tool",
                "name": "load_planning_context",
            },
        )
        _write(
            _llm_root(storage, "mission-historical", "hyper-agent") / "01.json",
            _llm_artifact(),
        )

        response, body = _request(
            server, "GET", "/api/debug?mission_id=mission-historical"
        )

    payload = json.loads(body)
    assert response.status == 200
    assert payload["enabled"] is True
    assert [item["name"] for item in payload["invocations"]] == [
        "load_planning_context"
    ]
    assert len(payload["profiles"]) == len(payload["conversations"]) == 1


def test_debug_disabled_returns_safe_empty_even_with_artifacts(tmp_path: Path) -> None:
    with _running_server(tmp_path, debug=False) as (server, storage):
        _activate(storage)
        mission_root = _mission_root(storage, "mission-one")
        _write(mission_root / "01.json", _invocation(1, "hidden"))
        response, body = _request(server, "GET", "/api/debug?mission_id=mission-one")

    assert response.status == 200
    assert json.loads(body) == _EMPTY


def test_raw_reasoning_endpoint_rejects_foreign_host_and_origin(tmp_path: Path) -> None:
    with _running_server(tmp_path) as (server, storage):
        _activate(storage)
        _write(
            _llm_root(storage, "mission-one", "hyper-agent") / "01.json",
            _llm_artifact(),
        )
        host_response, host_body = _request(
            server,
            "GET",
            "/api/debug?mission_id=mission-one",
            headers={"Host": "attacker.example"},
        )
        origin_response, origin_body = _request(
            server,
            "GET",
            "/api/debug?mission_id=mission-one",
            headers={"Origin": "http://attacker.example"},
        )

    assert host_response.status == origin_response.status == 403
    assert json.loads(host_body) == json.loads(origin_body) == {"error": "forbidden"}
    assert b"provider reasoning" not in host_body + origin_body


def test_malformed_oversized_and_symlinked_artifacts_are_ignored(
    tmp_path: Path,
) -> None:
    with _running_server(tmp_path) as (server, storage):
        _activate(storage)
        mission_root = _mission_root(storage, "mission-one")
        _write(mission_root / "valid.json", _invocation(1, "valid"))
        (mission_root / "malformed.json").write_text("{", encoding="utf-8")
        _write(
            mission_root / "wrong-schema.json",
            {**_invocation(2, "wrong"), "schema_version": 2},
        )
        (mission_root / "oversized.json").write_bytes(b" " * (1024 * 1024 + 1))
        outside = tmp_path / "outside.json"
        _write(outside, _invocation(3, "symlinked"))
        (mission_root / "linked.json").symlink_to(outside)
        _write(
            mission_root / "profiles" / "invalid.json",
            {**_profile("invalid"), "tools": [7]},
        )
        llm_root = _llm_root(storage, "mission-one", "hyper-agent")
        _write(llm_root / "00000000000000000001.json", _llm_artifact())
        (llm_root / "00000000000000000002.json").write_text("{", encoding="utf-8")
        _write(
            llm_root / "00000000000000000003.json",
            {**_llm_artifact(), "unknown": True},
        )
        (llm_root / "00000000000000000004.json").write_bytes(b" " * (1024 * 1024 + 1))
        outside_llm = tmp_path / "outside-llm.json"
        _write(outside_llm, _llm_artifact())
        (llm_root / "00000000000000000005.json").symlink_to(outside_llm)

        response, body = _request(server, "GET", "/api/debug?mission_id=mission-one")

    payload = json.loads(body)
    assert response.status == 200
    assert payload["enabled"] is True
    assert payload["profiles"] == []
    assert payload["invocations"] == [
        {"role": "hyper-agent", **_invocation(1, "valid")}
    ]
    assert len(payload["conversations"]) == 1
    assert payload["conversations"][0]["reasoning"] == "provider reasoning"


def test_v2_live_records_are_loaded_with_revision_and_exact_invocation_id(
    tmp_path: Path,
) -> None:
    with _running_server(tmp_path) as (server, storage):
        root = _mission_root(storage, "mission-live")
        invocation = {
            **_invocation(1, "invocation-live"),
            "schema_version": 2,
            "output": None,
            "finished_at": None,
            "updated_at": "2026-08-19T01:00:01+00:00",
            "completion_state": "live",
            "revision": 1,
        }
        _write(root / "01.json", invocation)
        _write(
            _llm_root(storage, "mission-live", "hyper-agent") / "01.json",
            _llm_artifact_v2(),
        )

        response, body = _request(server, "GET", "/api/debug?mission_id=mission-live")

    payload = json.loads(body)
    assert response.status == 200
    assert payload["invocations"][0]["completion_state"] == "live"
    [conversation] = payload["conversations"]
    assert conversation["invocation_id"] == "invocation-live"
    assert conversation["completion_state"] == "live"
    assert conversation["revision"] == 3
    assert conversation["tool_calls"][0]["function"]["arguments"] == '{"path":'


def test_canonical_and_legacy_agent_layouts_merge_without_duplicates(
    tmp_path: Path,
) -> None:
    with _running_server(tmp_path) as (server, storage):
        _activate(storage)
        canonical = _mission_root(storage, "mission-one", role="maneuver-control")
        legacy = _mission_root(storage, "mission-one", legacy=True)
        duplicate = _invocation(1, "shared", role="maneuver-control")
        _write(
            canonical / "profiles" / "maneuver.json",
            _profile("maneuver-control"),
        )
        _write(legacy / "profiles" / "hyper.json", _profile("hyper-agent"))
        _write(canonical / "01.json", duplicate)
        _write(legacy / "01.json", duplicate)
        _write(legacy / "02.json", _invocation(2, "legacy-hyper"))
        _write(
            _llm_root(storage, "mission-one", "hyper-agent") / "01.json",
            _llm_artifact(),
        )
        _write(
            _llm_root(storage, "mission-one", "maneuver-control") / "01.json",
            {**_llm_artifact(), "response_id": "chatcmpl-maneuver"},
        )

        _, all_body = _request(server, "GET", "/api/debug?mission_id=mission-one")
        _, filtered_body = _request(
            server,
            "GET",
            "/api/debug?mission_id=mission-one&role=hyper-agent",
        )

    all_payload = json.loads(all_body)
    assert [item["invocation_id"] for item in all_payload["invocations"]] == [
        "legacy-hyper",
        "shared",
    ]
    assert [item["role"] for item in all_payload["invocations"]] == [
        "hyper-agent",
        "maneuver-control",
    ]
    filtered = json.loads(filtered_body)
    assert filtered["enabled"] is True
    assert [item["invocation_id"] for item in filtered["invocations"]] == [
        "legacy-hyper"
    ]
    assert [item["role"] for item in filtered["profiles"]] == ["hyper-agent"]
    assert [item["role"] for item in filtered["conversations"]] == ["hyper-agent"]


def test_lease_replacement_during_collection_returns_safe_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _running_server(tmp_path) as (server, storage):
        store = _activate(storage)
        mission_root = _mission_root(storage, "mission-one")
        _write(mission_root / "01.json", _invocation(1, "hidden-after-race"))
        original = viewer_server.load_debug_artifacts

        def replace_after_load(
            storage_root: Path, mission_id: str, *, role: str | None = None
        ):
            result = original(storage_root, mission_id, role=role)
            now = datetime.now(UTC).isoformat()
            replacement = RuntimeLease("replacement", 999, now, now, "active")
            store.path.write_text(json.dumps(replacement.to_dict()), encoding="utf-8")
            return result

        monkeypatch.setattr(viewer_server, "load_debug_artifacts", replace_after_load)
        response, body = _request(server, "GET", "/api/debug?mission_id=mission-one")

    assert response.status == 200
    assert json.loads(body) == _EMPTY


def test_debug_endpoint_headers_head_and_query_validation(tmp_path: Path) -> None:
    with _running_server(tmp_path) as (server, storage):
        _activate(storage)
        _mission_root(storage, "mission-one")
        get_response, get_body = _request(
            server, "GET", "/api/debug?mission_id=mission-one"
        )
        head_response, head_body = _request(
            server, "HEAD", "/api/debug?mission_id=mission-one"
        )
        invalid_bodies = [
            _request(server, "GET", path)[1]
            for path in (
                "/api/debug",
                "/api/debug?mission_id=",
                "/api/debug?mission_id=one&mission_id=two",
                "/api/debug?mission_id=mission-one&extra=value",
                "/api/debug?mission_id=..%2Fmission-two",
                "/api/debug?mission_id=mission-one&role=unknown",
                "/api/debug?mission_id=mission-one&role=hyper-agent&role=runtime",
            )
        ]

    assert get_response.status == head_response.status == 200
    assert get_response.getheader("Content-Type") == "application/json; charset=utf-8"
    assert get_response.getheader("Cache-Control") == "no-store"
    assert get_response.getheader("X-Content-Type-Options") == "nosniff"
    assert json.loads(get_body) == {
        "enabled": True,
        "profiles": [],
        "invocations": [],
        "conversations": [],
    }
    assert head_response.getheader("Content-Length") == str(len(get_body))
    assert head_body == b""
    assert all(json.loads(body) == _EMPTY for body in invalid_bodies)
