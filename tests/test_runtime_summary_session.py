from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Lock, current_thread, enumerate as enumerate_threads

import pytest

from onr.adapters.mission_log_summarizer import FileMissionLogSummarizer
from onr.adapters.inprocess_transport import InProcessTransport
from onr.adapters.operational_log import FileOperationalLog, InProcessOperationalLog
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
from onr.viewer.server import _load_public_artifacts


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _RecordingModel:
    def __init__(self, content: str = "mission summary") -> None:
        self.content = content
        self.prompts: list[str] = []
        self.invocation_kwargs: list[dict[str, object]] = []
        self.called = Event()
        self.error: Exception | None = None

    def invoke(self, prompt: str, **kwargs: object) -> _Response:
        self.prompts.append(prompt)
        self.invocation_kwargs.append(kwargs)
        self.called.set()
        if self.error is not None:
            raise self.error
        return _Response(self.content)


class _BlockingSummarizer:
    def __init__(self) -> None:
        self.calls = 0
        self.active = 0
        self.overlapped = False
        self.non_daemon = False
        self.first = Event()
        self.second = Event()
        self.release = Event()
        self._lock = Lock()

    def heartbeat(self, mission_id: str) -> None:
        assert mission_id == "mission-periodic"
        with self._lock:
            self.calls += 1
            self.active += 1
            self.overlapped = self.overlapped or self.active > 1
            call = self.calls
        self.non_daemon = self.non_daemon or not current_thread().daemon
        if call == 1:
            self.first.set()
            assert self.release.wait(1)
        elif call == 2:
            self.second.set()
        with self._lock:
            self.active -= 1


class _LeaseObservingSummarizer:
    def __init__(self, runtime: RuntimeComposition) -> None:
        self.runtime = runtime
        self.flushed = Event()
        self.lease_was_active = False

    def heartbeat(self, mission_id: str) -> None:
        assert mission_id == "mission-error"
        lease = self.runtime.lease
        assert lease is not None
        current = lease.inspect()
        self.lease_was_active = current is not None and current.status == "active"
        self.flushed.set()


class _ExplodingSummarizer:
    def heartbeat(self, mission_id: str) -> None:
        assert mission_id == "mission-ordinary-failure"
        raise RuntimeError("private model failure")


class _SignalingSummarizer:
    def __init__(self, delegate: FileMissionLogSummarizer) -> None:
        self.delegate = delegate
        self.persisted = Event()

    def heartbeat(self, mission_id: str):
        artifact = self.delegate.heartbeat(mission_id)
        if artifact is not None:
            self.persisted.set()
        return artifact


def _runtime(
    tmp_path: Path,
    *,
    summary_seconds: float,
    file_log: bool = False,
) -> tuple[RuntimeComposition, InProcessOperationalLog | FileOperationalLog]:
    storage = tmp_path / "storage"
    logger: InProcessOperationalLog | FileOperationalLog
    logger = (
        FileOperationalLog(storage / "operational-log")
        if file_log
        else InProcessOperationalLog()
    )
    planner = PlannerConfig(Path(__file__), 1)
    config = RuntimeConfig(
        LLMConfig("test", "http://127.0.0.1:1/v1", "model", "key", 0),
        PlannersConfig(planner, planner),
        HeartbeatsConfig(1, 1, summary_seconds),
        TransportConfig("inprocess", tmp_path / "transport"),
        StorageConfig(storage),
        ServicesConfig("hyper", "maneuver", "context", "fsm", "planner"),
        debug=False,
        agent_name="test-agent",
    )
    return RuntimeComposition(config, InProcessTransport(), logger), logger


def test_periodic_worker_waits_full_cadence_and_never_overlaps(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path, summary_seconds=0.2)
    summarizer = _BlockingSummarizer()

    with runtime.mission_session("mission-periodic", summarizer=summarizer):
        assert summarizer.calls == 0
        assert summarizer.first.wait(1)
        assert summarizer.calls == 1
        summarizer.release.set()
        assert summarizer.second.wait(1)

    assert summarizer.calls >= 3  # two periodic calls and one final flush
    assert summarizer.non_daemon
    assert not summarizer.overlapped


def test_short_session_final_flush_combines_all_component_records(
    tmp_path: Path,
) -> None:
    runtime, logger = _runtime(tmp_path, summary_seconds=60)
    model = _RecordingModel("one mission-level digest")

    with runtime.mission_session("mission-all", model=model):
        logger.emit("mission-all", "hyper-agent", "planning", "completed")
        logger.emit("mission-all", "fsm-runner", "fsm", "active")
        logger.emit("mission-all", "maneuver-control", "control", "completed")
        logger.emit("mission-all", "environment", "environment", "completed")

    artifact_path = (
        runtime.config.storage.root
        / "summaries"
        / "mission-all"
        / "00000000000000000001.json"
    )
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["summary"] == "one mission-level digest"
    assert artifact["input_start_sequence"] == 1
    assert artifact["input_end_sequence"] == 4
    assert len(model.prompts) == 1
    assert model.invocation_kwargs == [
        {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    ]
    assert len(logger.replay("mission-all")) == 4
    prompt = model.prompts[0]
    for source in ("hyper-agent", "fsm-runner", "maneuver-control", "environment"):
        assert f'"source":"{source}"' in prompt


def test_summary_failure_is_sanitized_does_not_advance_cursor_or_escape(
    tmp_path: Path,
) -> None:
    runtime, logger = _runtime(tmp_path, summary_seconds=60)
    model = _RecordingModel()
    model.error = RuntimeError("Bearer private-runtime-token")

    with runtime.mission_session("mission-failure", model=model):
        logger.emit("mission-failure", "fsm-runner", "fsm", "active")

    records = logger.replay("mission-failure")
    assert [record.event_kind for record in records] == ["fsm", "summary-unavailable"]
    failure = records[-1]
    assert failure.source == "runtime" and failure.outcome == "failed"
    assert dict(failure.details) == {
        "error_type": "SummarizationError",
        "operation": "mission_summary",
    }
    assert "private-runtime-token" not in json.dumps(failure.to_dict())
    summary_dir = runtime.config.storage.root / "summaries" / "mission-failure"
    assert not (summary_dir / "cursor.json").exists()
    assert not list(summary_dir.glob("[0-9]*.json")) if summary_dir.exists() else True

    model.error = None
    artifact = runtime.heartbeat(
        "mission-failure",
        summarizer=runtime.create_mission_log_summarizer(model=model),
    )
    assert artifact is not None
    assert artifact.input_start_sequence == 1
    assert artifact.input_end_sequence == 2


def test_ordinary_injected_summary_failure_is_non_fatal(tmp_path: Path) -> None:
    runtime, logger = _runtime(tmp_path, summary_seconds=60)

    with runtime.mission_session(
        "mission-ordinary-failure", summarizer=_ExplodingSummarizer()
    ):
        logger.emit(
            "mission-ordinary-failure", "runtime", "agent", "completed"
        )

    records = logger.replay("mission-ordinary-failure")
    assert [record.event_kind for record in records] == [
        "agent",
        "summary-unavailable",
    ]
    assert records[-1].details["error_type"] == "RuntimeError"


def test_exception_cleanup_final_flushes_before_stopping_lease(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path, summary_seconds=60)
    summarizer = _LeaseObservingSummarizer(runtime)

    with pytest.raises(RuntimeError, match="mission producer failed"):
        with runtime.mission_session("mission-error", summarizer=summarizer):
            raise RuntimeError("mission producer failed")

    assert summarizer.flushed.is_set() and summarizer.lease_was_active
    lease_store = runtime.lease
    assert lease_store is not None
    lease = lease_store.inspect()
    assert lease is not None and lease.status == "stopped"
    assert not any(
        thread.name == "mission-summary-mission-error"
        for thread in enumerate_threads()
    )


def test_periodic_summary_is_discovered_by_read_only_viewer_loader(
    tmp_path: Path,
) -> None:
    runtime, logger = _runtime(tmp_path, summary_seconds=0.02, file_log=True)
    model = _RecordingModel("viewer-visible digest")
    delegate = FileMissionLogSummarizer(
        logger, runtime.config.storage.root, model
    )
    summarizer = _SignalingSummarizer(delegate)

    with runtime.mission_session("mission-viewer", summarizer=summarizer):
        logger.emit("mission-viewer", "runtime", "heartbeat", "completed")
        assert summarizer.persisted.wait(1)
        before = {
            path: path.stat().st_mtime_ns
            for path in runtime.config.storage.root.rglob("*")
            if path.is_file()
        }
        artifacts = _load_public_artifacts(runtime.config, "mission-viewer")
        after = {
            path: path.stat().st_mtime_ns
            for path in runtime.config.storage.root.rglob("*")
            if path.is_file()
        }

    assert before == after
    summaries = [item for item in artifacts if item.source_kind == "summary"]
    assert len(summaries) == 1
    assert summaries[0].record["summary"] == "viewer-visible digest"
