from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

import onr.runtime_host.app as runtime_host_app
from onr.adapters.run_narrative_summarizer import (
    ModelRunNarrativeSummarizer,
    RunNarrativeSummarizationError,
)
from onr.ports.mission_log_summarizer import SummaryArtifact
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
from onr.runtime_host import create_app
from onr.runtime_host.narrative import build_narrative_input

_FIXTURE = (
    Path(__file__).parent / "support" / "run_a6cqxx_progress" / "operational_log.jsonl"
)


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _RecordingModel:
    def __init__(self, response: object = "narrative text") -> None:
        self.response = response
        self.prompts: list[str] = []
        self.invocation_kwargs: list[dict[str, object]] = []

    def invoke(self, prompt: str, **kwargs: object) -> object:
        self.prompts.append(prompt)
        self.invocation_kwargs.append(kwargs)
        if isinstance(self.response, BaseException):
            raise self.response
        if isinstance(self.response, str):
            return _Response(self.response)
        return self.response


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


def _records() -> list[dict[str, object]]:
    return [
        json.loads(line) for line in _FIXTURE.read_text(encoding="utf-8").splitlines()
    ]


def test_adapter_prompts_with_window_summaries_and_run_state_only() -> None:
    model = _RecordingModel("  generated narrative  ")
    summarizer = ModelRunNarrativeSummarizer(model)
    records = _records()
    summaries = [
        SummaryArtifact.create(
            "mission:demo",
            1,
            1,
            30,
            (),
            "Planning finished; FSM entered leg 1.",
            created_at="2026-09-30T05:50:10+00:00",
        ),
    ]
    run = {
        "status": "running",
        "terminal_classification": None,
        "terminal_detail": None,
        "mission_intent": "SECRET MISSION INTENT",
    }
    narrative_input = build_narrative_input(
        run=run, records=records, summaries=summaries, previous_narrative="Earlier."
    )

    result = summarizer.summarize_narrative(
        mission_id="mission-1",
        mission_run_id="run-1",
        terminal=True,
        narrative_input=narrative_input,
    )

    assert result == "generated narrative"
    assert len(model.prompts) == 1
    prompt = model.prompts[0]
    assert "MISSION_ID: mission-1" in prompt
    assert "TERMINAL: true" in prompt
    assert "Planning finished; FSM entered leg 1." in prompt
    assert '"previous_narrative":"Earlier."' in prompt
    assert '"current":"executing"' in prompt
    assert "SECRET MISSION INTENT" not in prompt
    # Uncovered debug heartbeats stay out of the prompt; raw details never enter.
    assert "publish_snapshot" not in prompt
    assert "rationale" not in prompt
    assert len(prompt) < 16_000
    assert model.invocation_kwargs == [
        {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    ]


def test_narrative_input_keeps_latest_summaries_and_uncovered_notable_records() -> None:
    records = _records()
    summaries = [
        SummaryArtifact.create(
            "mission:demo", sequence, sequence, sequence, (), f"window {sequence}"
        )
        for sequence in range(1, 12)
    ]

    narrative_input = cast(
        dict[str, Any],
        build_narrative_input(
            run={"status": "running"}, records=records, summaries=summaries
        ),
    )

    assert [item["sequence"] for item in narrative_input["summaries"]] == list(
        range(4, 12)
    )
    assert narrative_input["summaries_omitted"] == 3
    # Each one-record window takes that record's importance (D5).
    assert (
        narrative_input["summaries"][0]["importance"] == "debug"
    )  # 4: prior-knowledge
    assert (
        narrative_input["summaries"][1]["importance"] == "notable"
    )  # 5: planning-intent
    live = narrative_input["live_records"]
    assert live and all(item["sequence"] > 11 for item in live)
    assert {item["importance"] for item in live} <= {"critical", "warning", "notable"}
    assert any(
        item["title"]
        == "FSM patrol-awaiting-first-assignment → assignment-1-in-progress"
        for item in live
    )
    assert narrative_input["fsm"] == {
        "state": "assignment-1-in-progress",
        "status": "updated",
        "plan_revision": 2,
    }
    assert narrative_input["source_watermark"] == 175


@pytest.mark.parametrize(
    "response",
    [RuntimeError("model offline"), _Response("  "), object()],
)
def test_adapter_raises_typed_error_on_model_failure_or_empty_response(
    response: object,
) -> None:
    summarizer = ModelRunNarrativeSummarizer(_RecordingModel(response))

    with pytest.raises(RunNarrativeSummarizationError):
        summarizer.summarize_narrative(
            mission_id="mission-1",
            mission_run_id="run-1",
            terminal=False,
            narrative_input={},
        )


def test_adapter_rejects_an_unbounded_prompt_before_model_invocation() -> None:
    model = _RecordingModel()
    summarizer = ModelRunNarrativeSummarizer(model, max_prompt_characters=128)

    with pytest.raises(RunNarrativeSummarizationError, match="prompt is too large"):
        summarizer.summarize_narrative(
            mission_id="mission-1",
            mission_run_id="run-1",
            terminal=False,
            narrative_input={"payload": "x" * 1000},
        )

    assert model.prompts == []


def test_create_app_wires_the_configured_model_into_the_production_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _RecordingModel()
    monkeypatch.setattr(runtime_host_app, "create_chat_model", lambda config: model)

    app = create_app(config=_config(tmp_path), repo_root=tmp_path)

    host = app.state.runtime_host
    assert isinstance(host._narrative_summarizer, ModelRunNarrativeSummarizer)
    assert host._narrative_summarizer.model is model
