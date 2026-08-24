from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any

import pytest
from fastapi.testclient import TestClient

from onr.contracts.transport import TransportEvent
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
from onr.runtime_host.narrative import RunNarrativeSummarizer


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

    def __call__(self) -> str:
        return self.now.isoformat()

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class ScriptedEvidenceSource:
    def __init__(self) -> None:
        self.by_mission: dict[str, list[Mapping[str, object]]] = {}

    def records(self, mission_id: str) -> Iterable[Mapping[str, object]]:
        return list(self.by_mission.get(mission_id, ()))


class ScriptedSummarizer:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.calls: list[dict[str, object]] = []

    def summarize_narrative(
        self,
        *,
        mission_id: str,
        mission_run_id: str,
        terminal: bool,
        observations: list[dict[str, object]],
    ) -> str:
        self.calls.append(
            {
                "mission_id": mission_id,
                "mission_run_id": mission_run_id,
                "terminal": terminal,
                "observations": observations,
            }
        )
        result = self.results.pop(0) if self.results else "summary"
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]


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


def _ids() -> Callable[[str], str]:
    counts: dict[str, int] = {}

    def generate(kind: str) -> str:
        counts[kind] = counts.get(kind, 0) + 1
        return f"{kind}-{counts[kind]}"

    return generate


def _client(
    tmp_path: Path,
    *,
    clock: FakeClock | None = None,
    source: ScriptedEvidenceSource | None = None,
    summarizer: RunNarrativeSummarizer | None = None,
    worker: Callable[[Any], None] | None = None,
    config: RuntimeConfig | None = None,
) -> tuple[
    TestClient,
    RuntimeHost,
    list[Callable[[], None]],
    FakeClock,
    ScriptedEvidenceSource,
    RuntimeConfig,
]:
    selected_clock = clock or FakeClock()
    selected_source = source or ScriptedEvidenceSource()
    selected_config = config or _config(tmp_path)
    pending: list[Callable[[], None]] = []
    host = RuntimeHost(
        selected_config,
        clock=selected_clock,
        generate_id=_ids(),
        worker_entrypoint=worker,
        launch_worker=pending.append,
        evidence_source=selected_source,
        narrative_summarizer=summarizer,
        narrative_interval_seconds=30.0,
    )
    return (
        TestClient(create_app(host=host)),
        host,
        pending,
        selected_clock,
        selected_source,
        selected_config,
    )


def _activate(client: TestClient, *, mission_intent: str = "Survey sector seven") -> dict:
    response = client.post(
        "/api/v1/mission-activations",
        headers={"Authorization": "Bearer console-secret"},
        json={
            "activation_request_id": "request-1",
            "console_session_id": "session-1",
            "mission_intent": mission_intent,
            "source_authority": "operator_console",
        },
    )
    assert response.status_code == 202
    return response.json()


def _op(mission_id: str, sequence: int) -> dict[str, object]:
    return OperationalLogRecord.create(
        mission_id,
        "runtime-host",
        f"event-{sequence}",
        "recorded",
        details={"public": f"value-{sequence}"},
        sequence=sequence,
        event_time=f"2026-08-24T12:00:{sequence:02d}+00:00",
        record_id=f"{mission_id}:{sequence}",
    ).to_dict()


def _redaction_challenge(mission_id: str, secret: str) -> dict[str, object]:
    return TransportEvent(
        1,
        "redaction-challenge",
        mission_id,
        1,
        "heartbeat",
        {
            "action": "navigate",
            "api_key": secret,
            "messages": [{"role": "system", "content": "private prompt"}],
        },
    ).to_dict()


def _dangling_record(
    run_id: str, *, started_at: str, terminal: bool
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mission_run_id": run_id,
        "source_watermark": 0,
        "last_attempt_at": started_at,
        "terminal_generated": False,
        "attempt_in_progress": {
            "started_at": started_at,
            "terminal": terminal,
        },
        "narrative": {
            "status": "none",
            "text": None,
            "generated_at": None,
            "source_watermark": 0,
            "terminal": False,
            "evidence": None,
        },
    }


def _narrative(client: TestClient, run_id: str = "run-1") -> Any:
    return client.get(f"/api/v1/mission-runs/{run_id}/narrative")


def _unavailable(*, terminal: bool = False) -> dict[str, object]:
    return {
        "status": "unavailable",
        "text": None,
        "generated_at": "2026-08-24T12:00:00+00:00",
        "source_watermark": 0,
        "terminal": terminal,
        "evidence": {
            "kind": "summary-unavailable",
            "message": (
                "Run Narrative generation failed; Mission Run state is unaffected."
            ),
        },
    }


def test_narrative_stays_none_until_evidence_advances(tmp_path: Path) -> None:
    summarizer = ScriptedSummarizer()
    client, _, _, _, _, _ = _client(tmp_path, summarizer=summarizer)
    activated = _activate(client)

    response = _narrative(client)

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "mission_id": activated["mission_id"],
        "mission_run_id": "run-1",
        "narrative": {
            "status": "none",
            "text": None,
            "generated_at": None,
            "source_watermark": 0,
            "terminal": False,
            "evidence": None,
        },
    }
    assert summarizer.calls == []


def test_narrative_coalesces_evidence_advances_within_interval(tmp_path: Path) -> None:
    summarizer = ScriptedSummarizer("first", "second")
    client, _, _, clock, source, _ = _client(tmp_path, summarizer=summarizer)
    mission_id = str(_activate(client)["mission_id"])
    source.by_mission[mission_id] = [_op(mission_id, 1)]

    first = _narrative(client).json()["narrative"]
    source.by_mission[mission_id].append(_op(mission_id, 2))
    coalesced = _narrative(client).json()["narrative"]
    clock.advance(30)
    second = _narrative(client).json()["narrative"]

    assert first["text"] == "first"
    assert first["source_watermark"] == 1
    assert coalesced == first
    assert second["text"] == "second"
    assert second["source_watermark"] == 2
    assert [call["terminal"] for call in summarizer.calls] == [False, False]


def test_narrative_generation_does_not_overlap(tmp_path: Path) -> None:
    entered = Event()
    release = Event()
    calls = 0
    calls_lock = Lock()

    class BlockingSummarizer:
        def summarize_narrative(self, **_kwargs: object) -> str:
            nonlocal calls
            with calls_lock:
                calls += 1
            entered.set()
            assert release.wait(timeout=5)
            return "complete"

    client, _, _, _, source, _ = _client(tmp_path, summarizer=BlockingSummarizer())
    mission_id = str(_activate(client)["mission_id"])
    source.by_mission[mission_id] = [_op(mission_id, 1)]
    responses: list[Any] = []
    first = Thread(target=lambda: responses.append(_narrative(client)), daemon=True)
    first.start()
    assert entered.wait(timeout=5)

    second = Thread(target=lambda: responses.append(_narrative(client)), daemon=True)
    second.start()
    second.join(timeout=5)
    assert not second.is_alive()
    release.set()
    first.join(timeout=5)

    assert calls == 1
    assert sorted(response.json()["narrative"]["status"] for response in responses) == [
        "available",
        "none",
    ]


def test_terminal_narrative_is_attempted_once_across_restart_and_replay(
    tmp_path: Path,
) -> None:
    summarizer = ScriptedSummarizer("terminal summary")
    client, _, pending, clock, source, config = _client(
        tmp_path, summarizer=summarizer, worker=lambda _context: None
    )
    mission_id = str(_activate(client)["mission_id"])
    source.by_mission[mission_id] = [_op(mission_id, 1)]
    pending.pop()()

    first = _narrative(client).json()["narrative"]
    repeated = _narrative(client).json()["narrative"]
    restarted, _, _, _, _, _ = _client(
        tmp_path,
        clock=clock,
        source=source,
        summarizer=summarizer,
        config=config,
    )
    source.by_mission[mission_id] = [_op(mission_id, 1)]
    after_restart = _narrative(restarted).json()["narrative"]

    assert first == repeated == after_restart
    assert first["terminal"] is True
    assert first["text"] == "terminal summary"
    assert len(summarizer.calls) == 1
    assert summarizer.calls[0]["terminal"] is True


def test_failed_narrative_is_interval_gated_and_can_recover(tmp_path: Path) -> None:
    summarizer = ScriptedSummarizer(RuntimeError("private model failure"), "recovered")
    client, _, _, clock, source, _ = _client(tmp_path, summarizer=summarizer)
    mission_id = str(_activate(client)["mission_id"])
    source.by_mission[mission_id] = [_op(mission_id, 1)]

    failed_response = _narrative(client)
    immediate = _narrative(client)
    clock.advance(30)
    recovered = _narrative(client).json()["narrative"]

    assert failed_response.json()["narrative"] == _unavailable()
    assert "private model failure" not in failed_response.text
    assert immediate.json() == failed_response.json()
    assert recovered["status"] == "available"
    assert recovered["text"] == "recovered"
    assert recovered["source_watermark"] == 1
    assert len(summarizer.calls) == 2


def test_failed_terminal_narrative_is_never_retried(tmp_path: Path) -> None:
    summarizer = ScriptedSummarizer(RuntimeError("terminal failure"), "must not run")
    client, _, pending, clock, source, _ = _client(
        tmp_path, summarizer=summarizer, worker=lambda _context: None
    )
    mission_id = str(_activate(client)["mission_id"])
    source.by_mission[mission_id] = [_op(mission_id, 1)]
    pending.pop()()

    failed = _narrative(client).json()["narrative"]
    clock.advance(300)
    repeated = _narrative(client).json()["narrative"]

    assert failed == _unavailable(terminal=True)
    assert repeated == failed
    assert len(summarizer.calls) == 1


def test_dangling_terminal_attempt_is_published_unavailable_without_retry(
    tmp_path: Path,
) -> None:
    summarizer = ScriptedSummarizer("must not run")
    client, _, pending, clock, source, config = _client(
        tmp_path, summarizer=summarizer, worker=lambda _context: None
    )
    mission_id = str(_activate(client)["mission_id"])
    source.by_mission[mission_id] = [_op(mission_id, 1)]
    pending.pop()()
    run_before = client.get("/api/v1/mission-runs/current").content
    narrative_path = (
        config.storage.root / "runtime-host" / "narratives" / "run-1.json"
    )
    narrative_path.parent.mkdir(parents=True, exist_ok=True)
    narrative_path.write_text(
        json.dumps(
            _dangling_record("run-1", started_at=clock(), terminal=True),
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    restarted, _, _, _, _, _ = _client(
        tmp_path,
        clock=clock,
        source=source,
        summarizer=summarizer,
        config=config,
    )
    response = _narrative(restarted)

    assert response.json()["narrative"] == _unavailable(terminal=True)
    assert summarizer.calls == []
    assert restarted.get("/api/v1/mission-runs/current").content == run_before


def test_dangling_nonterminal_attempt_keeps_interval_gated_retry(
    tmp_path: Path,
) -> None:
    summarizer = ScriptedSummarizer("recovered")
    client, _, _, clock, source, config = _client(tmp_path, summarizer=summarizer)
    mission_id = str(_activate(client)["mission_id"])
    source.by_mission[mission_id] = [_op(mission_id, 1)]
    state_path = config.storage.root / "runtime-host" / "state.json"
    original_state = state_path.read_bytes()
    narrative_path = (
        config.storage.root / "runtime-host" / "narratives" / "run-1.json"
    )
    narrative_path.parent.mkdir(parents=True, exist_ok=True)
    narrative_path.write_text(
        json.dumps(
            _dangling_record("run-1", started_at=clock(), terminal=False),
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    restarted, _, _, _, _, _ = _client(
        tmp_path,
        clock=clock,
        source=source,
        summarizer=summarizer,
        config=config,
    )
    state_path.write_bytes(original_state)
    run_before = restarted.get("/api/v1/mission-runs/current").content

    first = _narrative(restarted).json()["narrative"]
    clock.advance(29)
    gated = _narrative(restarted).json()["narrative"]
    clock.advance(1)
    recovered = _narrative(restarted).json()["narrative"]

    assert first == gated == _unavailable()
    assert summarizer.calls[0]["terminal"] is False
    assert len(summarizer.calls) == 1
    assert recovered["status"] == "available"
    assert recovered["text"] == "recovered"
    assert restarted.get("/api/v1/mission-runs/current").content == run_before


@pytest.mark.parametrize(
    ("result", "status", "expected_text"),
    [
        ("  clean\x00text\t\n" + "x" * 5000 + "  ", "available", None),
        ("\x00\t\r", "unavailable", None),
        (123, "unavailable", None),
    ],
)
def test_narrative_output_is_sanitized(
    tmp_path: Path, result: object, status: str, expected_text: str | None
) -> None:
    summarizer = ScriptedSummarizer(result)
    client, _, _, _, source, _ = _client(tmp_path, summarizer=summarizer)
    mission_id = str(_activate(client)["mission_id"])
    source.by_mission[mission_id] = [_op(mission_id, 1)]

    narrative = _narrative(client).json()["narrative"]

    assert narrative["status"] == status
    if status == "available":
        text = narrative["text"]
        assert isinstance(text, str)
        assert len(text) == 4000
        assert "\x00" not in text
        assert "\t" not in text
        assert "\n" in text
    else:
        assert narrative["text"] == expected_text


def test_summarizer_receives_only_issued_observation_envelopes(tmp_path: Path) -> None:
    mission_intent = "SECRET MISSION INTENT"
    evidence_secret = "super-secret-observation-token"
    summarizer = ScriptedSummarizer("safe")
    client, _, _, _, source, _ = _client(tmp_path, summarizer=summarizer)
    mission_id = str(_activate(client, mission_intent=mission_intent)["mission_id"])
    source.by_mission[mission_id] = [
        _op(mission_id, 1),
        _redaction_challenge(mission_id, evidence_secret),
    ]
    issued = client.get("/api/v1/mission-runs/run-1/observations").json()[
        "observations"
    ]

    _narrative(client)

    assert summarizer.calls[0]["observations"] == issued
    serialized = json.dumps(summarizer.calls[0]["observations"])
    assert mission_intent not in serialized
    assert evidence_secret not in serialized


@pytest.mark.parametrize("result", ["published", RuntimeError("hidden")])
def test_narrative_attempts_do_not_change_run_or_activities(
    tmp_path: Path, result: object
) -> None:
    summarizer = ScriptedSummarizer(result)
    client, _, _, _, source, _ = _client(tmp_path, summarizer=summarizer)
    mission_id = str(_activate(client)["mission_id"])
    source.by_mission[mission_id] = [_op(mission_id, 1)]
    run_before = client.get("/api/v1/mission-runs/current").content
    activities_before = client.get(
        "/api/v1/mission-runs/run-1/activities"
    ).content

    _narrative(client)

    assert client.get("/api/v1/mission-runs/current").content == run_before
    assert (
        client.get("/api/v1/mission-runs/run-1/activities").content
        == activities_before
    )


def test_unknown_run_narrative_matches_observations_error(tmp_path: Path) -> None:
    client, _, _, _, _, _ = _client(tmp_path, summarizer=ScriptedSummarizer())

    narrative = _narrative(client, "missing")
    observations = client.get(
        "/api/v1/mission-runs/missing/observations"
    )

    assert narrative.status_code == observations.status_code == 404
    assert narrative.json() == observations.json()
