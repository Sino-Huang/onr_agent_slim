from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from urllib.parse import quote
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from onr.contracts.transport import TransportEvent
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
from onr.runtime.agent_debug import AgentDebugRecorder
from onr.runtime.config import DEFAULT_ENVIRONMENT_PROFILE
from onr.runtime_host import RuntimeHost, create_app


class MutableEvidence:
    def __init__(self) -> None:
        self.items: list[Mapping[str, object]] = []

    def records(self, mission_id: str) -> Iterable[Mapping[str, object]]:
        assert mission_id == "mission-1"
        return list(self.items)


def _config(tmp_path: Path, *, debug: bool) -> RuntimeConfig:
    environment = replace(
        DEFAULT_ENVIRONMENT_PROFILE,
        fake=replace(
            DEFAULT_ENVIRONMENT_PROFILE.fake,
            artifact_root=tmp_path / "environment",
        ),
    )
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
        debug=debug,
        agent_name="test-agent",
        environment_profile=environment,
    )


def _clock() -> str:
    return datetime(2026, 8, 27, 12, 0, tzinfo=UTC).isoformat()


def _ids() -> Callable[[str], str]:
    counts: dict[str, int] = {}

    def generate(kind: str) -> str:
        counts[kind] = counts.get(kind, 0) + 1
        return f"{kind}-{counts[kind]}"

    return generate


def _client(
    tmp_path: Path, *, debug: bool = True
) -> tuple[TestClient, RuntimeHost, MutableEvidence, RuntimeConfig]:
    config = _config(tmp_path, debug=debug)
    evidence = MutableEvidence()
    pending: list[Callable[[], None]] = []
    host = RuntimeHost(
        config,
        clock=_clock,
        generate_id=_ids(),
        launch_worker=pending.append,
        evidence_source=evidence,
    )
    client = TestClient(create_app(host=host))
    response = client.post(
        "/api/v1/mission-activations",
        headers={"Authorization": "Bearer console-secret"},
        json={
            "activation_request_id": "request-1",
            "console_session_id": "session-1",
            "mission_intent": "Survey sector seven",
            "source_authority": "operator_console",
        },
    )
    assert response.status_code == 202
    return client, host, evidence, config


def _view(client: TestClient, section: str, query: str = ""):
    separator = "&" if query else ""
    return client.get(
        f"/api/v1/mission-runs/run-1/operator-view?section={section}{separator}{query}"
    )


def _operational(sequence: int, event_kind: str, *, source: str = "hyper-agent"):
    return {
        "schema_version": 1,
        "record_id": f"record-{sequence}",
        "mission_id": "mission-1",
        "sequence": sequence,
        "event_time": f"2026-08-27T12:00:{sequence:02d}+00:00",
        "source": source,
        "event_kind": event_kind,
        "outcome": "completed",
        "details": {"attempt": sequence},
    }


def _invocation(
    sequence: int,
    invocation_id: str,
    *,
    role: str,
    kind: str,
    name: str,
    completion_state: str = "complete",
    revision: int = 2,
    output: object = None,
    error: object = None,
) -> dict[str, object]:
    live = completion_state == "live"
    return {
        "schema_version": 2,
        "sequence": sequence,
        "invocation_id": invocation_id,
        "parent_id": None,
        "agent_role": role,
        "kind": kind,
        "name": name,
        "input": {"target": "sector-7", "attempt": sequence},
        "output": output,
        "error": error,
        "started_at": "2026-08-27T12:00:00+00:00",
        "finished_at": None if live else "2026-08-27T12:00:02+00:00",
        "completion_state": completion_state,
        "updated_at": (
            "2026-08-27T12:00:01+00:00" if live else "2026-08-27T12:00:02+00:00"
        ),
        "revision": revision,
    }


def _llm(invocation_id: str, *, role: str = "hyper-agent") -> dict[str, object]:
    del role
    return {
        "schema_version": 2,
        "sequence": 1,
        "invocation_id": invocation_id,
        "request": {"messages": [{"role": "user", "content": "private prompt"}]},
        "response_id": "response-1",
        "model": "reasoning-model",
        "status_code": 200,
        "finish_reason": "stop",
        "content": "Selected the next operational action.",
        "function_call": None,
        "reasoning": "Compare the current evidence and choose the safe transition.",
        "reasoning_content": None,
        "reasoning_details": [{"type": "summary", "text": "evidence compared"}],
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "inspect", "arguments": '{"target":"sector-7"}'},
            }
        ],
        "error": None,
        "started_at": "2026-08-27T12:00:00+00:00",
        "updated_at": "2026-08-27T12:00:02+00:00",
        "finished_at": "2026-08-27T12:00:02+00:00",
        "completion_state": "complete",
        "revision": 2,
    }


def _write_debug(
    config: RuntimeConfig,
    *,
    role: str,
    sequence: int,
    invocation: Mapping[str, object],
    llm: Mapping[str, object] | None = None,
) -> Path:
    mission_name = quote("mission-1", safe="._-")
    root = config.storage.root.parent / "debug" / "agent" / role / mission_name
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{sequence:020d}.json"
    path.write_text(json.dumps(invocation), encoding="utf-8")
    if llm is not None:
        llm_root = config.storage.root.parent / "debug" / "llm" / role / mission_name
        llm_root.mkdir(parents=True, exist_ok=True)
        (llm_root / f"{sequence:020d}.json").write_text(
            json.dumps(llm), encoding="utf-8"
        )
    return path


def _publish_public_artifact(config: RuntimeConfig) -> None:
    content = b"public report\n"
    digest = hashlib.sha256(content).hexdigest()
    root = config.storage.root / "artifact-inbox" / "run-1" / "report"
    root.mkdir(parents=True)
    (root / "report.txt").write_bytes(content)
    (root / "artifact.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_id": "report",
                "mission_id": "mission-1",
                "mission_run_id": "run-1",
                "kind": "report",
                "media_type": "text/plain",
                "display": {"title": "Public report", "summary": "Published"},
                "published_at": "2026-08-27T12:00:05+00:00",
                "content": {
                    "path": "report.txt",
                    "byte_size": len(content),
                    "content_digest": f"sha256:{digest}",
                },
            }
        ),
        encoding="utf-8",
    )


def test_v1_1_overview_contract_unknown_runs_and_strict_query_validation(
    tmp_path: Path,
) -> None:
    client, _, evidence, _ = _client(tmp_path)
    evidence.items.append(_operational(1, "planning-intent"))

    assert client.get("/api/v1/health").json() == {
        "status": "ok",
        "api_version": {"major": 1, "minor": 1},
    }
    response = _view(client, "overview")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "schema_version",
        "mission_id",
        "mission_run_id",
        "run_status",
        "section",
        "debug",
        "next_cursor",
        "before_cursor",
        "has_more",
        "overview",
    }
    assert body["section"] == "overview"
    assert body["overview"]["authority"] == "Runtime Host Mission Run Record"
    assert body["overview"]["hitl"] == {"status": "none", "requires_action": False}
    assert body["overview"]["recent_events"][0]["event_kind"] == "planning-intent"

    missing = client.get("/api/v1/mission-runs/missing/operator-view?section=overview")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "mission_run_not_found"

    invalid_paths = [
        "/api/v1/mission-runs/run-1/operator-view",
        "/api/v1/mission-runs/run-1/operator-view?section=unknown",
        "/api/v1/mission-runs/run-1/operator-view?section=overview&extra=1",
        "/api/v1/mission-runs/run-1/operator-view?section=overview&section=agents",
        "/api/v1/mission-runs/run-1/operator-view?section=overview&limit=101",
        "/api/v1/mission-runs/run-1/operator-view?section=overview&limit=01",
        "/api/v1/mission-runs/run-1/operator-view?section=overview&raw=false",
        "/api/v1/mission-runs/run-1/operator-view?section=agents&cursor=a&before=b",
    ]
    for path in invalid_paths:
        invalid = client.get(path)
        assert invalid.status_code == 422
        assert invalid.json() == {
            "error": {
                "code": "invalid_request",
                "message": "operator-view query is invalid",
            }
        }


def test_agents_expose_debug_reasoning_tool_payloads_and_update_stable_identity(
    tmp_path: Path,
) -> None:
    client, _, evidence, config = _client(tmp_path)
    evidence.items.extend(
        [
            _operational(1, "planning-intent"),
            _operational(2, "maneuver-handoff", source="maneuver-control"),
        ]
    )
    _write_debug(
        config,
        role="hyper-agent",
        sequence=1,
        invocation=_invocation(
            1,
            "hyper-llm-1",
            role="hyper-agent",
            kind="llm",
            name="planner_executor",
        ),
        llm=_llm("hyper-llm-1"),
    )
    tool_path = _write_debug(
        config,
        role="maneuver-control",
        sequence=1,
        invocation=_invocation(
            1,
            "maneuver-tool-1",
            role="maneuver-control",
            kind="tool",
            name="inspect_environment",
            completion_state="live",
            revision=1,
        ),
    )

    first = _view(client, "agents").json()
    hyper = next(
        item for item in first["agents"] if item["invocation_id"] == "hyper-llm-1"
    )
    assert hyper["recorded_debug_reasoning"] == {
        "label": "Recorded Debug Reasoning",
        "authority": "non-authoritative",
        "disposition": "available",
        "content": "Compare the current evidence and choose the safe transition.\n\n"
        '[{"text":"evidence compared","type":"summary"}]',
    }
    assert hyper["content"] == "Selected the next operational action."
    assert hyper["tool_calls"][0]["name"] == "inspect"
    assert hyper["tool_calls"][0]["args"] == {"target": "sector-7"}

    maneuver = next(
        item for item in first["agents"] if item["invocation_id"] == "maneuver-tool-1"
    )
    assert maneuver["completion_state"] == "live"
    stable_id = maneuver["stable_id"]
    cursor = first["next_cursor"]

    tool_path.write_text(
        json.dumps(
            _invocation(
                1,
                "maneuver-tool-1",
                role="maneuver-control",
                kind="tool",
                name="inspect_environment",
                completion_state="error",
                revision=2,
                error={"type": "RuntimeError", "message": "sensor failed"},
            )
        ),
        encoding="utf-8",
    )
    update = _view(client, "agents", f"cursor={cursor}").json()
    changed = next(
        item for item in update["agents"] if item["invocation_id"] == "maneuver-tool-1"
    )
    assert changed["stable_id"] == stable_id
    assert changed["completion_state"] == "error"
    assert changed["tool_calls"][0]["error"] == {
        "type": "RuntimeError",
        "message": "sensor failed",
    }
    assert _view(client, "agents", "cursor=bad").status_code == 422


def test_debug_disabled_keeps_high_level_agent_progress_with_explicit_disposition(
    tmp_path: Path,
) -> None:
    client, _, evidence, config = _client(tmp_path, debug=False)
    evidence.items.append(_operational(1, "planner-execution"))
    _write_debug(
        config,
        role="hyper-agent",
        sequence=1,
        invocation=_invocation(
            1,
            "private-invocation",
            role="hyper-agent",
            kind="llm",
            name="private-model",
        ),
        llm=_llm("private-invocation"),
    )

    body = _view(client, "agents").json()
    assert body["debug"]["disposition"] == "debug_evidence_unavailable"
    assert [item["kind"] for item in body["agents"]] == ["progress"]
    serialized = json.dumps(body)
    assert "Compare the current evidence" not in serialized
    assert "private-invocation" not in serialized


def test_environment_filters_noise_raw_toggle_and_preserves_latest_state(
    tmp_path: Path,
) -> None:
    client, _, evidence, config = _client(tmp_path)
    evidence.items.extend(
        [
            _operational(1, "hyper-heartbeat"),
            _operational(2, "planner-execution"),
            TransportEvent(
                1,
                "belief-1",
                "mission-1",
                3,
                "belief.updated",
                {"revision": 2, "source": "bayesian_belief_snapshot"},
            ).to_dict(),
        ]
    )
    environment_file = (
        config.environment_profile.fake.artifact_root / "mission-1" / "environment.json"
    )
    environment_file.parent.mkdir(parents=True)
    environment_file.write_text(
        json.dumps(
            {
                "scene_graph": {
                    "mission_time_seconds": 12.5,
                    "current_maneuver": {
                        "maneuver_id": "maneuver-7",
                        "lifecycle": "active",
                    },
                    "drone": {
                        "position": {"x": 10, "y": 20, "z": -30},
                        "velocity": {"x": 1, "y": 0, "z": 0},
                    },
                },
                "perceptions": [{"event_id": "perception-1"}],
            }
        ),
        encoding="utf-8",
    )

    filtered = _view(client, "environment").json()["environment"]
    assert filtered["raw"] is False
    assert filtered["position"] == {"x": 10, "y": 20, "z": -30}
    assert filtered["velocity"] == {"x": 1, "y": 0, "z": 0}
    assert filtered["mission_time_seconds"] == 12.5
    assert filtered["active_maneuver"]["maneuver_id"] == "maneuver-7"
    assert "hyper-heartbeat" not in {
        item["event_kind"] for item in filtered["timeline"]
    }
    assert {item["event_kind"] for item in filtered["timeline"]} == {
        "planner-execution",
        "belief.updated",
    }

    raw = _view(client, "environment", "raw=true").json()["environment"]
    assert raw["raw"] is True
    assert "hyper-heartbeat" in {item["event_kind"] for item in raw["timeline"]}


def test_artifacts_merge_public_and_allowlisted_planner_files_with_bounded_content(
    tmp_path: Path,
) -> None:
    client, _, _, config = _client(tmp_path)
    _publish_public_artifact(config)
    planner_root = config.storage.root / "runtime-host" / "planner-artifacts" / "run-1"
    accepted = planner_root / "statechart-attempts" / "001" / "accepted-statechart.json"
    accepted.parent.mkdir(parents=True)
    accepted.write_text('{"accepted":true}\n' + "x" * 5000, encoding="utf-8")
    ignored = planner_root / "private-secrets.txt"
    ignored.write_text("must not appear", encoding="utf-8")
    outside = tmp_path / "outside-model.mzn"
    outside.write_text("outside", encoding="utf-8")
    (planner_root / "linked-model.mzn").symlink_to(outside)

    body = _view(client, "artifacts").json()
    assert {item["source"] for item in body["artifacts"]} == {
        "public_inbox",
        "planner",
    }
    planner = next(item for item in body["artifacts"] if item["source"] == "planner")
    assert planner["kind"] == "accepted-statechart.json"
    assert planner["ref"] == "statechart-attempts/001/accepted-statechart.json"
    assert "private-secrets.txt" not in json.dumps(body)
    assert "linked-model.mzn" not in json.dumps(body)

    first = client.get(
        f"/api/v1/mission-runs/run-1/artifacts/{planner['artifact_id']}/content"
    )
    assert first.status_code == 200
    page = first.json()
    assert page["offset"] == 0
    assert page["next_offset"] == 4096
    assert page["eof"] is False
    final = client.get(
        f"/api/v1/mission-runs/run-1/artifacts/{planner['artifact_id']}/content"
        f"?offset={page['next_offset']}"
    ).json()
    assert final["eof"] is True
    assert first.json()["content"] + final["content"] == accepted.read_text(
        encoding="utf-8"
    )

    too_large = client.get(
        f"/api/v1/mission-runs/run-1/artifacts/{planner['artifact_id']}/content?limit=4097"
    )
    assert too_large.status_code == 422
    assert (
        client.get(
            "/api/v1/mission-runs/run-1/artifacts/planner-Li4vbW9kZWwubXpu/content"
        ).status_code
        == 404
    )


def test_before_pages_are_bounded_and_section_scoped(tmp_path: Path) -> None:
    client, _, evidence, _ = _client(tmp_path)
    evidence.items.extend(
        _operational(index, f"event-{index}") for index in range(1, 6)
    )

    newest = _view(client, "environment", "limit=2").json()
    assert [item["event_kind"] for item in newest["environment"]["timeline"]] == [
        "event-4",
        "event-5",
    ]
    assert newest["before_cursor"] is not None
    older = _view(
        client,
        "environment",
        f"limit=2&before={newest['before_cursor']}",
    ).json()
    assert [item["event_kind"] for item in older["environment"]["timeline"]] == [
        "event-2",
        "event-3",
    ]
    foreign = _view(client, "agents", f"cursor={newest['next_cursor']}")
    assert foreign.status_code == 422


def test_barrier_controlled_live_invocation_updates_in_place_while_evidence_arrives(
    tmp_path: Path,
) -> None:
    client, _, evidence, config = _client(tmp_path)
    recorder = AgentDebugRecorder(
        config.storage.root.parent / "debug" / "agent",
        "mission-1",
        role="maneuver-control",
    )
    callback = recorder.callback_for("maneuver-control")
    started = Event()
    release = Event()
    invocation_id = UUID("10000000-0000-0000-0000-000000000056")

    def invoke() -> None:
        callback.on_tool_start(
            {"name": "inspect_environment"},
            "draft",
            run_id=invocation_id,
            inputs={"target": "sector-7"},
        )
        started.set()
        assert release.wait(timeout=5)
        callback.on_tool_end({"status": "complete"}, run_id=invocation_id)

    worker = Thread(target=invoke)
    worker.start()
    assert started.wait(timeout=5)
    first = _view(client, "agents").json()
    live = next(
        item for item in first["agents"] if item["invocation_id"] == str(invocation_id)
    )
    assert live["completion_state"] == "live"
    stable_id = live["stable_id"]

    evidence.items.append(_operational(1, "belief.updated", source="environment"))
    environment = _view(client, "environment").json()["environment"]
    assert [item["event_kind"] for item in environment["timeline"]] == [
        "belief.updated"
    ]

    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    update = _view(client, "agents", f"cursor={first['next_cursor']}").json()
    completed = [
        item for item in update["agents"] if item["invocation_id"] == str(invocation_id)
    ]
    assert len(completed) == 1
    assert completed[0]["stable_id"] == stable_id
    assert completed[0]["completion_state"] == "complete"
    assert completed[0]["tool_calls"][0]["result"] == {"status": "complete"}


@pytest.mark.parametrize("raw", ["TRUE", "1", "yes", ""])
def test_environment_raw_is_strict_boolean(tmp_path: Path, raw: str) -> None:
    client, _, _, _ = _client(tmp_path)
    assert _view(client, "environment", f"raw={raw}").status_code == 422
