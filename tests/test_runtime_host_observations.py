from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from onr.contracts.transport import (
    Command,
    CommandOutcome,
    CommandReceipt,
    TransportEvent,
)
from onr.ports.operational_log import OperationalLogRecord
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
from onr.runtime_host import RuntimeHost, create_app
from onr.runtime_host.observations import decode_cursor, encode_cursor


class ScriptedEvidenceSource:
    def __init__(self) -> None:
        self.by_mission: dict[str, list[Any]] = {}

    def records(self, mission_id: str) -> Iterable[Mapping[str, object]]:
        return list(self.by_mission.get(mission_id, ()))


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
    source: ScriptedEvidenceSource,
    *,
    config: RuntimeConfig | None = None,
) -> tuple[TestClient, RuntimeHost, list[Callable[[], None]], RuntimeConfig]:
    selected_config = config or _config(tmp_path)
    pending: list[Callable[[], None]] = []
    host = RuntimeHost(
        selected_config,
        clock=_clock,
        generate_id=_ids(),
        launch_worker=pending.append,
        evidence_source=source,
    )
    return TestClient(create_app(host=host)), host, pending, selected_config


def _activate(client: TestClient) -> dict[str, object]:
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
    return response.json()


def _op(
    mission_id: str,
    sequence: int,
    event_kind: str,
    *,
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return OperationalLogRecord.create(
        mission_id,
        "runtime-host",
        event_kind,
        "recorded",
        details=details,
        sequence=sequence,
        event_time=f"2026-08-24T12:00:0{sequence}+00:00",
        record_id=f"{mission_id}:{sequence}:{event_kind}",
    ).to_dict()


def _event(
    mission_id: str,
    sequence: int,
    *,
    event_id: str | None = None,
    event_kind: str = "heartbeat",
    payload: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return TransportEvent(
        1,
        event_id or f"event-{sequence}",
        mission_id,
        sequence,
        event_kind,
        payload or {"status": "active"},
    ).to_dict()


def _get(client: TestClient, run_id: str, resource: str, query: str = "") -> Any:
    return client.get(f"/api/v1/mission-runs/{run_id}/{resource}{query}")


def test_unknown_run_is_not_found_for_both_public_read_paths(tmp_path: Path) -> None:
    client, _, _, _ = _client(tmp_path, ScriptedEvidenceSource())
    expected = {
        "error": {
            "code": "mission_run_not_found",
            "message": "Mission Run is unknown to this Runtime Host",
        }
    }

    for resource in ("observations", "activities"):
        response = _get(client, "missing", resource)
        assert response.status_code == 404
        assert response.json() == expected


def test_empty_observation_page_echoes_run_identity(tmp_path: Path) -> None:
    source = ScriptedEvidenceSource()
    client, _, _, _ = _client(tmp_path, source)
    activated = _activate(client)

    response = _get(client, "run-1", "observations")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "mission_id": activated["mission_id"],
        "mission_run_id": "run-1",
        "observations": [],
        "next_cursor": None,
    }


def test_observations_ingest_in_deterministic_order_and_page(tmp_path: Path) -> None:
    source = ScriptedEvidenceSource()
    client, _, _, _ = _client(tmp_path, source)
    activated = _activate(client)
    mission_id = str(activated["mission_id"])
    source.by_mission[mission_id] = [
        _op(mission_id, 1, "mission-started"),
        _op(mission_id, 2, "checkpoint"),
        _event(mission_id, 1, event_id="transport-event-1"),
    ]

    response = _get(client, "run-1", "observations")
    body = response.json()

    assert response.status_code == 200
    assert [item["observation_sequence"] for item in body["observations"]] == [
        1,
        2,
        3,
    ]
    assert all(item["observed_at"] == _clock() for item in body["observations"])
    assert all(item["item"]["schema_version"] == 1 for item in body["observations"])
    assert decode_cursor(
        body["next_cursor"], mission_run_id="run-1", max_sequence=3
    ) == 3

    first = _get(client, "run-1", "observations", "?limit=2").json()
    second = _get(
        client,
        "run-1",
        "observations",
        f"?limit=2&cursor={first['next_cursor']}",
    ).json()
    third = _get(
        client,
        "run-1",
        "observations",
        f"?limit=2&cursor={second['next_cursor']}",
    ).json()
    assert [item["observation_sequence"] for item in first["observations"]] == [1, 2]
    assert [item["observation_sequence"] for item in second["observations"]] == [3]
    assert third["observations"] == []
    assert third["next_cursor"] is None

    for limit in ("0", "501", "abc"):
        invalid = _get(client, "run-1", "observations", f"?limit={limit}")
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "invalid_request"


def test_cursor_validation_and_retry_stability(tmp_path: Path) -> None:
    source = ScriptedEvidenceSource()
    client, _, _, _ = _client(tmp_path, source)
    mission_id = str(_activate(client)["mission_id"])
    source.by_mission[mission_id] = [_op(mission_id, 1, "started")]
    expected = {
        "error": {
            "code": "invalid_cursor",
            "message": (
                "cursor is malformed, expired, or does not belong to this Mission Run"
            ),
        }
    }

    for cursor in (
        "garbage",
        encode_cursor("run-other", 1),
        encode_cursor("run-1", 2),
    ):
        response = _get(client, "run-1", "observations", f"?cursor={cursor}")
        assert response.status_code == 422
        assert response.json() == expected

    cursor = encode_cursor("run-1", 0)
    first = _get(client, "run-1", "observations", f"?cursor={cursor}")
    retry = _get(client, "run-1", "observations", f"?cursor={cursor}")
    assert first.content == retry.content


def test_observation_sequences_survive_host_restart(tmp_path: Path) -> None:
    source = ScriptedEvidenceSource()
    client, _, _, config = _client(tmp_path, source)
    mission_id = str(_activate(client)["mission_id"])
    source.by_mission[mission_id] = [
        _op(mission_id, 1, "started"),
        _op(mission_id, 2, "finished"),
    ]
    before = _get(client, "run-1", "observations").json()

    restarted, _, _, _ = _client(tmp_path, source, config=config)
    after = _get(restarted, "run-1", "observations").json()

    assert after == before


def test_projection_redacts_secrets_and_emits_safe_errors(tmp_path: Path) -> None:
    source = ScriptedEvidenceSource()
    client, _, _, _ = _client(tmp_path, source)
    mission_id = str(_activate(client)["mission_id"])
    # Reuses the adversarial categories from tests/test_trace_view.py.
    source.by_mission[mission_id] = [
        '{"schema_version": 1, "prompt_sk-secret-json": ',
        ["analysis sk-secret-nonmapping"],
        {
            **_event(mission_id, 1, event_id="bad-fields"),
            "prompt_sk-secret-field": "secret-value",
        },
        {
            **_event(mission_id, 2, event_id="bad-schema"),
            "schema_version": "prompt sk-secret-schema",
        },
        _event(
            mission_id,
            3,
            event_id="safe-event",
            payload={
                "action": "navigate",
                "text": "raw prompt",
                "messages": [{"role": "system", "content": "credential"}],
                "analysis": "private reasoning",
                "api_key": "super-secret-token",
            },
        ),
    ]

    observations = _get(client, "run-1", "observations")
    activities = _get(client, "run-1", "activities")
    combined = observations.text + activities.text

    assert observations.status_code == activities.status_code == 200
    assert sum(
        item["item"]["event_kind"] == "error"
        for item in observations.json()["observations"]
    ) >= 4
    for prohibited in (
        "prompt",
        "sk-secret",
        "secret-value",
        "analysis",
        "bad-fields",
        "bad-schema",
        "super-secret-token",
        "Survey sector seven",
        "raw prompt",
        "credential",
        "private reasoning",
    ):
        assert prohibited not in combined


def test_activities_correlate_commands_and_preserve_prior_mapping(tmp_path: Path) -> None:
    source = ScriptedEvidenceSource()
    client, _, _, _ = _client(tmp_path, source)
    mission_id = str(_activate(client)["mission_id"])
    correlation_id = "correlation-1"
    command = Command(
        1,
        "command-1",
        correlation_id,
        mission_id,
        "maneuver",
        "execute",
        {"token": "command-secret", "mission_intent": "Survey sector seven"},
    )
    receipt = CommandReceipt(
        1, "command-1", correlation_id, mission_id, "maneuver"
    )
    outcome = CommandOutcome(
        1,
        "command-1",
        correlation_id,
        mission_id,
        "completed",
        {"api_key": "outcome-secret"},
    )
    source.by_mission[mission_id] = [
        command.to_dict(),
        receipt.to_dict(),
        outcome.to_dict(),
        _op(mission_id, 1, "unusual-operational-event"),
    ]

    first_response = _get(client, "run-1", "activities")
    retry_response = _get(client, "run-1", "activities")
    first = first_response.json()

    assert first_response.content == retry_response.content
    correlated = [
        item for item in first["activities"] if item["activity_id"] == "correlation:correlation-1"
    ]
    assert len(correlated) == 1
    assert correlated[0]["kind"] == "maneuver_command"
    assert correlated[0]["status"] == "completed"
    assert len(correlated[0]["observation_sequences"]) == 3
    assert any(
        item["kind"] in {"operational", "observation"}
        for item in first["activities"]
    )
    assert "Survey sector seven" not in first_response.text
    assert "command-secret" not in first_response.text
    assert "outcome-secret" not in first_response.text

    prior = {
        item["activity_id"]: item["activity_sequence"] for item in first["activities"]
    }
    source.by_mission[mission_id].append(_op(mission_id, 2, "later-event"))
    updated = _get(client, "run-1", "activities").json()
    assert {
        item["activity_id"]: item["activity_sequence"]
        for item in updated["activities"]
        if item["activity_id"] in prior
    } == prior


def test_activity_markers_and_paging_use_independent_sequence_space(
    tmp_path: Path,
) -> None:
    source = ScriptedEvidenceSource()
    client, _, _, _ = _client(tmp_path, source)
    mission_id = str(_activate(client)["mission_id"])
    duplicate = _event(mission_id, 1, event_id="duplicate-event")
    source.by_mission[mission_id] = [
        duplicate,
        duplicate,
        _op(mission_id, 1, "first"),
        _op(mission_id, 3, "third"),
    ]

    all_activities = _get(client, "run-1", "activities").json()["activities"]
    dispositions = {item["replay_disposition"] for item in all_activities}
    assert dispositions & {"duplicate", "replayed"}
    assert "gap" in dispositions
    assert all(
        item["kind"] == "evidence_marker"
        for item in all_activities
        if item["replay_disposition"] != "normal"
    )

    first = _get(client, "run-1", "activities", "?limit=1").json()
    second = _get(
        client,
        "run-1",
        "activities",
        f"?limit=500&cursor={first['next_cursor']}",
    ).json()
    assert len(first["activities"]) == 1
    assert len(second["activities"]) == len(all_activities) - 1
    assert decode_cursor(
        first["next_cursor"],
        mission_run_id="run-1",
        max_sequence=len(all_activities),
    ) == 1

    invalid = _get(
        client,
        "run-1",
        "activities",
        f"?cursor={encode_cursor('run-1', len(all_activities) + 1)}",
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_cursor"


def test_interleaved_evidence_preserves_cross_record_projection_semantics(
    tmp_path: Path,
) -> None:
    source = ScriptedEvidenceSource()
    client, _, _, _ = _client(tmp_path, source)
    mission_id = str(_activate(client)["mission_id"])
    duplicate = _event(mission_id, 1, event_id="duplicate-event")
    source.by_mission[mission_id] = [
        duplicate,
        _op(mission_id, 1, "separator-one"),
        duplicate,
        _event(mission_id, 2, event_id="sequence-two-a"),
        _op(mission_id, 2, "separator-two"),
        _event(mission_id, 2, event_id="sequence-two-b"),
        _event(mission_id, 3, event_id="conflict-event", payload={"status": "a"}),
        _op(mission_id, 3, "separator-three"),
        _event(mission_id, 3, event_id="conflict-event", payload={"status": "b"}),
        _event(mission_id, 4, event_id="generic-event", event_kind="unusual-valid"),
    ]

    observations = _get(client, "run-1", "observations")
    activities = _get(client, "run-1", "activities")
    observation_items = [entry["item"] for entry in observations.json()["observations"]]
    dispositions = {item["replay_disposition"] for item in observation_items}

    assert observations.status_code == activities.status_code == 200
    assert {"duplicate", "replayed", "conflict"} <= dispositions
    assert not any(
        item["payload"].get("error_code") == "envelope_required"
        for item in observation_items
    )
    marker_dispositions = {
        activity["replay_disposition"]
        for activity in activities.json()["activities"]
        if activity["kind"] == "evidence_marker"
    }
    assert {"duplicate", "replayed", "conflict"} <= marker_dispositions
    assert any(
        activity["kind"] == "observation"
        and activity["event_kind"] == "unusual-valid"
        for activity in activities.json()["activities"]
    )


def test_activities_are_byte_identical_across_host_restart(tmp_path: Path) -> None:
    source = ScriptedEvidenceSource()
    client, _, _, config = _client(tmp_path, source)
    mission_id = str(_activate(client)["mission_id"])
    source.by_mission[mission_id] = [
        _event(mission_id, 1, event_id="generic-event", event_kind="unusual-valid"),
        _op(mission_id, 1, "recorded"),
    ]
    before = _get(client, "run-1", "activities")

    restarted, _, _, _ = _client(tmp_path, source, config=config)
    after = _get(restarted, "run-1", "activities")

    assert after.content == before.content


def test_cursor_codec_rejects_noncanonical_or_typed_variants() -> None:
    valid = encode_cursor("run-1", 0)
    assert decode_cursor(valid, mission_run_id="run-1", max_sequence=0) == 0
    assert "=" not in valid
    compact = json.dumps(
        {"run": "run-1", "seq": True, "v": 1}, separators=(",", ":"), sort_keys=True
    )
    import base64

    bad_bool = base64.urlsafe_b64encode(compact.encode()).decode().rstrip("=")
    try:
        decode_cursor(bad_bool, mission_run_id="run-1", max_sequence=1)
    except ValueError:
        pass
    else:
        raise AssertionError("boolean cursor sequence was accepted")
