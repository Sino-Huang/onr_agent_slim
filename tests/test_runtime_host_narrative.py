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
        narrative_input: Mapping[str, object],
    ) -> str:
        self.calls.append(
            {
                "mission_id": mission_id,
                "mission_run_id": mission_run_id,
                "terminal": terminal,
                "narrative_input": narrative_input,
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
        narrative_poll_seconds=None,
    )
    return (
        TestClient(create_app(host=host), client=("127.0.0.1", 50000)),
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


def _tick_and_read(client: TestClient, host: RuntimeHost) -> dict[str, Any]:
    host.narrative_tick()
    return _narrative(client).json()["narrative"]


def test_narrative_stays_none_until_evidence_advances(tmp_path: Path) -> None:
    summarizer = ScriptedSummarizer()
    client, host, _, _, _, _ = _client(tmp_path, summarizer=summarizer)
    activated = _activate(client)

    host.narrative_tick()
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


def test_narrative_requests_only_read_the_stored_record(tmp_path: Path) -> None:
    summarizer = ScriptedSummarizer("generated in the background")
    client, host, _, _, source, _ = _client(tmp_path, summarizer=summarizer)
    mission_id = str(_activate(client)["mission_id"])
    source.by_mission[mission_id] = [_op(mission_id, 1)]

    before = _narrative(client).json()["narrative"]
    overview = client.get(
        "/api/v1/mission-runs/run-1/operator-view", params={"section": "overview"}
    ).json()["overview"]["narrative"]
    assert summarizer.calls == []
    assert before["status"] == overview["status"] == "none"

    host.narrative_tick()

    stored = _narrative(client).json()["narrative"]
    overview = client.get(
        "/api/v1/mission-runs/run-1/operator-view", params={"section": "overview"}
    ).json()["overview"]["narrative"]
    assert stored["text"] == overview["text"] == "generated in the background"
    assert len(summarizer.calls) == 1


def test_narrative_coalesces_evidence_advances_within_interval(tmp_path: Path) -> None:
    summarizer = ScriptedSummarizer("first", "second")
    client, host, _, clock, source, _ = _client(tmp_path, summarizer=summarizer)
    mission_id = str(_activate(client)["mission_id"])
    source.by_mission[mission_id] = [_op(mission_id, 1)]

    first = _tick_and_read(client, host)
    source.by_mission[mission_id].append(_op(mission_id, 2))
    coalesced = _tick_and_read(client, host)
    clock.advance(30)
    second = _tick_and_read(client, host)
    clock.advance(30)
    unchanged = _tick_and_read(client, host)

    assert first["text"] == "first"
    assert first["source_watermark"] == 1
    assert coalesced == first
    assert second["text"] == "second"
    assert second["source_watermark"] == 2
    assert unchanged == second
    assert [call["terminal"] for call in summarizer.calls] == [False, False]


def test_progress_narrative_reports_the_newest_operational_record_sequence(
    tmp_path: Path,
) -> None:
    summarizer = ScriptedSummarizer("first")
    client, host, _, _, source, _ = _client(tmp_path, summarizer=summarizer)
    mission_id = str(_activate(client)["mission_id"])
    url = "/api/v1/mission-runs/run-1/operator-view"

    def progress_narrative() -> dict[str, Any]:
        response = client.get(url, params={"section": "progress"})
        assert response.status_code == 200
        return response.json()["progress"]["narrative"]

    source.by_mission[mission_id] = [_op(mission_id, 1)]
    pending = progress_narrative()
    assert pending["status"] == "none"
    assert pending["latest_operational_sequence"] == 1

    host.narrative_tick()
    source.by_mission[mission_id].extend([_op(mission_id, 2), _op(mission_id, 3)])
    host.narrative_tick()  # coalesced within the interval: still covers #1
    first = progress_narrative()

    assert first["status"] == "available"
    assert first["terminal"] is False
    assert first["source_watermark"] == 1
    assert first["latest_operational_sequence"] == 3
    # A page through a cursor still reports record sequences, not cursors.
    cursor = client.get(url, params={"section": "progress"}).json()["next_cursor"]
    paged = client.get(url, params={"section": "progress", "cursor": cursor}).json()
    assert paged["progress"]["narrative"]["latest_operational_sequence"] == 3


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

    client, host, _, _, source, _ = _client(tmp_path, summarizer=BlockingSummarizer())
    mission_id = str(_activate(client)["mission_id"])
    source.by_mission[mission_id] = [_op(mission_id, 1)]
    first = Thread(target=host.narrative_tick, daemon=True)
    first.start()
    assert entered.wait(timeout=5)

    host.narrative_tick()
    during = _narrative(client).json()["narrative"]
    release.set()
    first.join(timeout=5)
    assert not first.is_alive()

    assert calls == 1
    assert during["status"] == "none"
    assert _narrative(client).json()["narrative"]["status"] == "available"


def test_background_thread_generates_without_requests(tmp_path: Path) -> None:
    generated = Event()

    class SignalingSummarizer:
        def summarize_narrative(self, **_kwargs: object) -> str:
            generated.set()
            return "background narrative"

    source = ScriptedEvidenceSource()
    pending: list[Callable[[], None]] = []
    host = RuntimeHost(
        _config(tmp_path),
        clock=FakeClock(),
        generate_id=_ids(),
        launch_worker=pending.append,
        evidence_source=source,
        narrative_summarizer=SignalingSummarizer(),
        narrative_poll_seconds=0.01,
    )
    try:
        client = TestClient(create_app(host=host), client=("127.0.0.1", 50000))
        mission_id = str(_activate(client)["mission_id"])
        source.by_mission[mission_id] = [_op(mission_id, 1)]

        assert generated.wait(timeout=10)
    finally:
        host.close()
    assert _narrative(client).json()["narrative"]["text"] == "background narrative"


@pytest.mark.parametrize("tick_while_running", [False, True])
def test_run_replaced_right_after_ending_still_gets_terminal_narrative(
    tmp_path: Path, tick_while_running: bool
) -> None:
    summarizer = ScriptedSummarizer(
        *(["running"] if tick_while_running else []), "terminal", "second run"
    )
    client, host, pending, _, source, _ = _client(
        tmp_path, summarizer=summarizer, worker=lambda _context: None
    )
    first_mission = str(_activate(client)["mission_id"])
    source.by_mission[first_mission] = [_op(first_mission, 1)]
    if tick_while_running:
        host.narrative_tick()
    pending.pop()()
    second = client.post(
        "/api/v1/mission-activations",
        headers={"Authorization": "Bearer console-secret"},
        json={
            "activation_request_id": "request-2",
            "console_session_id": "session-1",
            "mission_intent": "Survey sector eight",
            "source_authority": "operator_console",
        },
    ).json()
    source.by_mission[str(second["mission_id"])] = [_op(str(second["mission_id"]), 1)]
    assert "run-1" in host._narrative_watch

    host.narrative_tick()
    assert "run-1" not in host._narrative_watch
    assert "run-1" not in host._run_observations

    first = _narrative(client, "run-1").json()["narrative"]
    assert first["terminal"] is True
    assert first["text"] == "terminal"
    assert _narrative(client, "run-2").json()["narrative"]["text"] == "second run"
    expected_calls = (
        [("run-1", False)] if tick_while_running else []
    ) + [("run-1", True), ("run-2", False)]
    assert [(call["mission_run_id"], call["terminal"]) for call in summarizer.calls] == (
        expected_calls
    )


def test_terminal_narrative_is_attempted_once_across_restart_and_replay(
    tmp_path: Path,
) -> None:
    summarizer = ScriptedSummarizer("terminal summary")
    client, host, pending, clock, source, config = _client(
        tmp_path, summarizer=summarizer, worker=lambda _context: None
    )
    mission_id = str(_activate(client)["mission_id"])
    source.by_mission[mission_id] = [_op(mission_id, 1)]
    pending.pop()()

    first = _tick_and_read(client, host)
    repeated = _tick_and_read(client, host)
    restarted, restarted_host, _, _, _, _ = _client(
        tmp_path,
        clock=clock,
        source=source,
        summarizer=summarizer,
        config=config,
    )
    after_restart = _tick_and_read(restarted, restarted_host)

    assert first == repeated == after_restart
    assert first["terminal"] is True
    assert first["text"] == "terminal summary"
    assert len(summarizer.calls) == 1
    assert summarizer.calls[0]["terminal"] is True


def test_failed_narrative_is_interval_gated_and_can_recover(tmp_path: Path) -> None:
    summarizer = ScriptedSummarizer(RuntimeError("private model failure"), "recovered")
    client, host, _, clock, source, _ = _client(tmp_path, summarizer=summarizer)
    mission_id = str(_activate(client)["mission_id"])
    source.by_mission[mission_id] = [_op(mission_id, 1)]

    host.narrative_tick()
    failed_response = _narrative(client)
    immediate = _tick_and_read(client, host)
    clock.advance(30)
    recovered = _tick_and_read(client, host)

    assert failed_response.json()["narrative"] == _unavailable()
    assert "private model failure" not in failed_response.text
    assert immediate == failed_response.json()["narrative"]
    assert recovered["status"] == "available"
    assert recovered["text"] == "recovered"
    assert recovered["source_watermark"] == 1
    assert len(summarizer.calls) == 2


def test_failed_terminal_narrative_is_never_retried(tmp_path: Path) -> None:
    summarizer = ScriptedSummarizer(RuntimeError("terminal failure"), "must not run")
    client, host, pending, clock, source, _ = _client(
        tmp_path, summarizer=summarizer, worker=lambda _context: None
    )
    mission_id = str(_activate(client)["mission_id"])
    source.by_mission[mission_id] = [_op(mission_id, 1)]
    pending.pop()()

    failed = _tick_and_read(client, host)
    clock.advance(300)
    repeated = _tick_and_read(client, host)

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
        config.storage.root / "runtime-host" / "runs" / "run-1" / "narrative.json"
    )
    narrative_path.parent.mkdir(parents=True, exist_ok=True)
    narrative_path.write_text(
        json.dumps(
            _dangling_record("run-1", started_at=clock(), terminal=True),
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    restarted, restarted_host, _, _, _, _ = _client(
        tmp_path,
        clock=clock,
        source=source,
        summarizer=summarizer,
        config=config,
    )

    assert _tick_and_read(restarted, restarted_host) == _unavailable(terminal=True)
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
        config.storage.root / "runtime-host" / "runs" / "run-1" / "narrative.json"
    )
    narrative_path.parent.mkdir(parents=True, exist_ok=True)
    narrative_path.write_text(
        json.dumps(
            _dangling_record("run-1", started_at=clock(), terminal=False),
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    restarted, restarted_host, _, _, _, _ = _client(
        tmp_path,
        clock=clock,
        source=source,
        summarizer=summarizer,
        config=config,
    )
    state_path.write_bytes(original_state)
    run_before = restarted.get("/api/v1/mission-runs/current").content

    first = _tick_and_read(restarted, restarted_host)
    clock.advance(29)
    gated = _tick_and_read(restarted, restarted_host)
    clock.advance(1)
    recovered = _tick_and_read(restarted, restarted_host)

    assert first == gated == _unavailable()
    assert len(summarizer.calls) == 1
    assert summarizer.calls[0]["terminal"] is False
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
    client, host, _, _, source, _ = _client(tmp_path, summarizer=summarizer)
    mission_id = str(_activate(client)["mission_id"])
    source.by_mission[mission_id] = [_op(mission_id, 1)]

    narrative = _tick_and_read(client, host)

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


def test_summarizer_input_excludes_mission_intent_and_raw_evidence(
    tmp_path: Path,
) -> None:
    mission_intent = "SECRET MISSION INTENT"
    evidence_secret = "super-secret-observation-token"
    summarizer = ScriptedSummarizer("safe")
    client, host, _, _, source, _ = _client(tmp_path, summarizer=summarizer)
    mission_id = str(_activate(client, mission_intent=mission_intent)["mission_id"])
    source.by_mission[mission_id] = [
        _op(mission_id, 1),
        _redaction_challenge(mission_id, evidence_secret),
    ]

    host.narrative_tick()

    narrative_input = summarizer.calls[0]["narrative_input"]
    assert isinstance(narrative_input, Mapping)
    assert narrative_input["source_watermark"] == 1
    serialized = json.dumps(narrative_input)
    assert mission_intent not in serialized
    assert evidence_secret not in serialized


@pytest.mark.parametrize("result", ["published", RuntimeError("hidden")])
def test_narrative_attempts_do_not_change_run_or_activities(
    tmp_path: Path, result: object
) -> None:
    summarizer = ScriptedSummarizer(result)
    client, host, _, _, source, _ = _client(tmp_path, summarizer=summarizer)
    mission_id = str(_activate(client)["mission_id"])
    source.by_mission[mission_id] = [_op(mission_id, 1)]
    run_before = client.get("/api/v1/mission-runs/current").content
    activities_before = client.get(
        "/api/v1/mission-runs/run-1/activities"
    ).content

    host.narrative_tick()

    assert len(summarizer.calls) == 1
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
