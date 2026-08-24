from __future__ import annotations

from pathlib import Path

import pytest

from onr.adapters.run_narrative_summarizer import (
    ModelRunNarrativeSummarizer,
    RunNarrativeSummarizationError,
)
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
import onr.runtime_host.app as runtime_host_app


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


def test_adapter_prompts_with_only_issued_envelopes_and_returns_text() -> None:
    model = _RecordingModel("  generated narrative  ")
    summarizer = ModelRunNarrativeSummarizer(model)
    observations = [
        {
            "observation_sequence": 1,
            "observed_at": "2026-08-24T12:00:00+00:00",
            "event_id": "safe-event",
            "item": {"event_kind": "heartbeat", "payload": {"status": "safe"}},
        }
    ]

    result = summarizer.summarize_narrative(
        mission_id="mission-1",
        mission_run_id="run-1",
        terminal=True,
        observations=observations,
    )

    assert result == "generated narrative"
    assert len(model.prompts) == 1
    assert "MISSION_ID: mission-1" in model.prompts[0]
    assert "TERMINAL: true" in model.prompts[0]
    assert '"status":"safe"' in model.prompts[0]
    assert "mission intent" not in model.prompts[0].lower()
    assert "operational log" not in model.prompts[0].lower()
    assert model.invocation_kwargs == [
        {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    ]


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
            observations=[],
        )


def test_adapter_rejects_an_unbounded_prompt_before_model_invocation() -> None:
    model = _RecordingModel()
    summarizer = ModelRunNarrativeSummarizer(model, max_prompt_characters=128)

    with pytest.raises(RunNarrativeSummarizationError, match="prompt is too large"):
        summarizer.summarize_narrative(
            mission_id="mission-1",
            mission_run_id="run-1",
            terminal=False,
            observations=[{"payload": "x" * 1000}],
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
