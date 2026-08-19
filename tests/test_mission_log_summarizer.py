from __future__ import annotations

from pathlib import Path

import pytest

from onr.adapters.mission_log_summarizer import (
    FileMissionLogSummarizer,
    SummarizationError,
)
from onr.adapters.operational_log import InProcessOperationalLog
from onr.adapters.inprocess_transport import InProcessTransport
from onr.runtime import (
    HeartbeatsConfig,
    LLMConfig,
    PlannerConfig,
    PlannersConfig,
    RuntimeComposition,
    RuntimeConfig,
    ServicesConfig,
    StorageConfig,
    TransportConfig,
)


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _RecordingModel:
    def __init__(self, responses: list[str] | None = None) -> None:
        self.prompts: list[str] = []
        self.invocation_kwargs: list[dict[str, object]] = []
        self.responses = responses or ["summary"]
        self.calls = 0
        self.error: Exception | None = None

    def invoke(self, prompt: str, **kwargs: object) -> _Response:
        self.prompts.append(prompt)
        self.invocation_kwargs.append(kwargs)
        if self.error is not None:
            raise self.error
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return _Response(response)


def _logger_with_records() -> InProcessOperationalLog:
    logger = InProcessOperationalLog()
    logger.emit("mission-1", "runtime", "agent", "started", details={"operation": "start"})
    logger.emit("mission-1", "runtime", "agent", "completed", details={"operation": "step"})
    return logger


def test_heartbeat_consumes_incrementally_and_persists_across_restart(tmp_path: Path) -> None:
    logger = _logger_with_records()
    model = _RecordingModel(["first summary", "second summary"])
    root = tmp_path / "storage"
    summarizer = FileMissionLogSummarizer(logger, root, model)

    first = summarizer.heartbeat("mission-1")
    assert first is not None
    assert first.sequence == 1
    assert first.input_start_sequence == 1
    assert first.input_end_sequence == 2
    assert first.prior_summary_ids == ()
    assert (root / "summaries" / "mission-1" / "00000000000000000001.json").is_file()
    assert (root / "summaries" / "mission-1" / "cursor.json").is_file()
    assert model.invocation_kwargs == [
        {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    ]

    assert summarizer.heartbeat("mission-1") is None
    assert len(model.prompts) == 1

    logger.emit("mission-1", "runtime", "control", "completed", details={"operation": "finish"})
    second = summarizer.heartbeat("mission-1")
    assert second is not None
    assert second.input_start_sequence == 3
    assert second.input_end_sequence == 3
    assert first.summary in model.prompts[1]
    assert '"sequence":1' not in model.prompts[1].split("NEW LOG RECORDS", 1)[1]
    assert model.invocation_kwargs == [
        {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
        {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
    ]

    restarted_model = _RecordingModel(["unused"])
    restarted = FileMissionLogSummarizer(logger, root, restarted_model)
    assert restarted.heartbeat("mission-1") is None
    assert restarted_model.prompts == []


def test_heartbeat_keeps_only_previous_three_summaries_in_context(tmp_path: Path) -> None:
    logger = InProcessOperationalLog()
    model = _RecordingModel([f"summary-{index}" for index in range(1, 6)])
    summarizer = FileMissionLogSummarizer(logger, tmp_path / "storage", model)

    for index in range(1, 6):
        logger.emit(
            "mission-rolling",
            "runtime",
            "heartbeat",
            "completed",
            details={"sequence": index},
        )
        artifact = summarizer.heartbeat("mission-rolling")
        assert artifact is not None

    latest_prompt = model.prompts[-1]
    assert "summary-1" not in latest_prompt
    assert all(f"summary-{index}" in latest_prompt for index in (2, 3, 4))
    assert "summary-5" not in latest_prompt


def test_model_failure_does_not_advance_cursor_or_create_artifact(tmp_path: Path) -> None:
    logger = _logger_with_records()
    model = _RecordingModel()
    model.error = RuntimeError("vLLM unavailable")
    root = tmp_path / "storage"
    summarizer = FileMissionLogSummarizer(logger, root, model)

    with pytest.raises(SummarizationError, match="vLLM unavailable"):
        summarizer.heartbeat("mission-1")

    summary_dir = root / "summaries" / "mission-1"
    assert not summary_dir.exists()

    model.error = None
    artifact = summarizer.heartbeat("mission-1")
    assert artifact is not None
    assert artifact.sequence == 1
    assert artifact.input_start_sequence == 1


def test_runtime_composes_and_drives_the_summarizer_heartbeat(tmp_path: Path) -> None:
    logger = InProcessOperationalLog()
    model = _RecordingModel(["runtime summary"])
    runtime = RuntimeComposition(
        RuntimeConfig(
            LLMConfig("test", "http://127.0.0.1:14398/v1", "model", "key", 0),
            PlannersConfig(
                PlannerConfig(Path(__file__), 1), PlannerConfig(Path(__file__), 1)
            ),
            HeartbeatsConfig(1, 1),
            TransportConfig("inprocess", tmp_path / "transport"),
            StorageConfig(tmp_path / "storage"),
            ServicesConfig("hyper", "maneuver", "context", "fsm", "planner"),
            debug=False,
            agent_name="test-agent",
        ),
        InProcessTransport(),
        logger,
    )
    logger.emit("mission-runtime", "runtime", "heartbeat", "completed")

    summarizer = runtime.create_mission_log_summarizer(model=model)
    artifact = runtime.heartbeat("mission-runtime", summarizer=summarizer)

    assert artifact is not None
    assert artifact.summary == "runtime summary"
    assert artifact.input_start_sequence == artifact.input_end_sequence == 1
