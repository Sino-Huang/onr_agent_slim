from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from onr.adapters.inprocess_transport import InProcessTransport
import onr.runtime.composition as composition
from onr.runtime.config import (
    HeartbeatsConfig,
    LLMConfig,
    PlannerConfig,
    PlannersConfig,
    RuntimeConfig,
    ServicesConfig,
    StorageConfig,
    TransportConfig,
)
from onr.runtime.llm_debug import LLMResponseRecorder


def test_recorder_captures_raw_content_without_private_reasoning(tmp_path: Path) -> None:
    request_body = {
        "model": "reasoning-model",
        "messages": [
            {"role": "system", "content": "Follow the private system prompt."},
            {
                "role": "user",
                "content": "private prompt",
                "reasoning_content": "prior private reasoning",
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "interpret",
                    "description": "Interpret the mission",
                    "parameters": {
                        "type": "object",
                        "properties": {"mission": {"type": "string"}},
                        "required": ["mission"],
                    },
                },
            }
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    response_body = {
        "id": "chatcmpl-debug",
        "model": "reasoning-model",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": "final answer",
                    "function_call": {"name": "interpret", "arguments": "{}"},
                    "reasoning": "raw reasoning",
                    "reasoning_content": "reasoning content",
                    "reasoning_details": [{"type": "summary", "text": "detail"}],
                    "tool_calls": [{"id": "call-1", "type": "function"}],
                },
            }
        ],
    }
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=response_body, request=request)
    )
    recorder = LLMResponseRecorder(
        tmp_path / "debug" / "llm", "mission:demo", transport=transport
    )

    response = recorder.http_client.post(
        "http://vllm.test/v1/chat/completions",
        json=request_body,
        headers={
            "Authorization": "Bearer test-secret-key",
            "Cookie": "session=test-secret-cookie",
            "X-Debug-Secret": "test-secret-header",
        },
    )
    recorder.close()

    assert response.json() == response_body
    artifacts = list((tmp_path / "debug/llm/mission%3Ademo").glob("*.json"))
    assert len(artifacts) == 1
    assert json.loads(artifacts[0].read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "request": {
            **request_body,
            "messages": [
                {"role": "system", "content": "Follow the private system prompt."},
                {"role": "user", "content": "private prompt"},
            ],
        },
        "response_id": "chatcmpl-debug",
        "model": "reasoning-model",
        "status_code": 200,
        "finish_reason": "stop",
        "content": "final answer",
        "function_call": {"name": "interpret", "arguments": "{}"},
        "tool_calls": [{"id": "call-1", "type": "function"}],
    }
    serialized = artifacts[0].read_text(encoding="utf-8")
    assert "private prompt" in serialized
    assert "raw reasoning" not in serialized
    assert "prior private reasoning" not in serialized
    assert "reasoning_content" not in serialized
    assert "test-secret-key" not in serialized
    assert "test-secret-cookie" not in serialized
    assert "test-secret-header" not in serialized


def test_recorder_omits_reasoning_and_ignores_other_responses(
    tmp_path: Path,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-plain",
                    "model": "plain-model",
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": "plain answer"},
                        }
                    ],
                },
                request=request,
            )
        return httpx.Response(200, json={"data": []}, request=request)

    recorder = LLMResponseRecorder(
        tmp_path / "llm", "mission/demo", transport=httpx.MockTransport(respond)
    )
    recorder.http_client.get("http://vllm.test/v1/models")
    recorder.http_client.post("http://vllm.test/v1/chat/completions", json={})
    recorder.close()

    artifacts = list((tmp_path / "llm/mission%2Fdemo").glob("*.json"))
    assert len(artifacts) == 1
    artifact = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert artifact["request"] == {}
    assert artifact["content"] == "plain answer"
    assert artifact["function_call"] is None
    assert "reasoning" not in artifact
    assert "reasoning_content" not in artifact
    assert "reasoning_details" not in artifact
    assert artifact["tool_calls"] is None


@pytest.mark.parametrize("content", [None, b"{malformed"])
def test_recorder_writes_null_for_absent_or_malformed_request_body(
    tmp_path: Path, content: bytes | None
) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "answer"}}
                ]
            },
            request=request,
        )
    )
    recorder = LLMResponseRecorder(
        tmp_path / "llm", "mission", transport=transport
    )
    request = recorder.http_client.build_request(
        "POST", "http://vllm.test/v1/chat/completions", content=content
    )

    recorder.http_client.send(request)
    recorder.close()

    artifact_path = tmp_path / "llm/mission/00000000000000000001.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["request"] is None
    assert artifact["content"] == "answer"


def test_recorder_continues_after_existing_artifacts(tmp_path: Path) -> None:
    directory = tmp_path / "llm" / "mission"
    directory.mkdir(parents=True)
    (directory / "00000000000000000001.json").write_text("{}\n", encoding="utf-8")
    recorder = LLMResponseRecorder(
        tmp_path / "llm",
        "mission",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": "answer"}}
                    ]
                },
                request=request,
            )
        ),
    )

    recorder.http_client.post("http://vllm.test/v1/chat/completions", json={})
    recorder.close()

    assert (directory / "00000000000000000002.json").is_file()


def test_recorder_ignores_malformed_and_choice_free_completion_errors(
    tmp_path: Path,
) -> None:
    responses = iter(
        [
            httpx.Response(500, text="not json"),
            httpx.Response(429, json={"error": {"message": "rate limited"}}),
        ]
    )

    def respond(request: httpx.Request) -> httpx.Response:
        response = next(responses)
        response.request = request
        return response

    recorder = LLMResponseRecorder(
        tmp_path / "llm", "mission", transport=httpx.MockTransport(respond)
    )
    recorder.http_client.post("http://vllm.test/v1/chat/completions")
    recorder.http_client.post("http://vllm.test/v1/chat/completions")
    recorder.close()

    assert not (tmp_path / "llm/mission").exists()


def _runtime_config(tmp_path: Path, *, debug: bool) -> RuntimeConfig:
    planner = PlannerConfig(tmp_path / "planner", 1)
    return RuntimeConfig(
        llm=LLMConfig("vllm", "http://127.0.0.1:1/v1", "model", "EMPTY", 0),
        planners=PlannersConfig(planner, planner),
        heartbeats=HeartbeatsConfig(1, 1),
        transport=TransportConfig("inprocess", tmp_path / "var/transport"),
        storage=StorageConfig(tmp_path / "var/storage"),
        services=ServicesConfig("hyper", "maneuver", "context", "fsm", "planner"),
        debug=debug,
    )


@pytest.mark.parametrize(
    ("debug", "mission_id"),
    [(False, "mission:demo"), (True, None)],
)
def test_model_without_active_debug_capture_does_not_build_recorder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    debug: bool,
    mission_id: str | None,
) -> None:
    config = _runtime_config(tmp_path, debug=debug)
    created: dict[str, object] = {}

    def fake_chat_openai(**kwargs: object) -> object:
        created.update(kwargs)
        return SimpleNamespace()

    def fail_recorder(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"recorder unexpectedly built: {args!r} {kwargs!r}")

    monkeypatch.setattr(composition, "ChatOpenAI", fake_chat_openai)
    monkeypatch.setattr(composition, "LLMResponseRecorder", fail_recorder)
    runtime = composition.RuntimeComposition(config, InProcessTransport())

    runtime.create_chat_model(mission_id=mission_id)

    assert "http_client" not in created


def test_debug_model_owns_mission_scoped_recorder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _runtime_config(tmp_path, debug=True)
    constructed: list[object] = []

    class FakeRecorder:
        def __init__(self, root: Path, mission_id: str) -> None:
            constructed.extend((root, mission_id))
            self.http_client = object()

    class FakeAgentRecorder:
        def __init__(self, root: Path, mission_id: str) -> None:
            constructed.extend((root, mission_id))

    class FakeChatModel:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr(composition, "ChatOpenAI", FakeChatModel)
    monkeypatch.setattr(composition, "LLMResponseRecorder", FakeRecorder)
    monkeypatch.setattr(composition, "AgentDebugRecorder", FakeAgentRecorder)
    runtime = composition.RuntimeComposition(config, InProcessTransport())

    model = cast(Any, runtime.create_chat_model(mission_id="mission:demo"))

    assert constructed == [
        tmp_path / "var/debug/llm",
        "mission:demo",
        tmp_path / "var/debug/agent",
        "mission:demo",
    ]
    assert model.kwargs["http_client"] is model._llm_response_recorder.http_client
    assert isinstance(model._agent_debug_recorder, FakeAgentRecorder)
