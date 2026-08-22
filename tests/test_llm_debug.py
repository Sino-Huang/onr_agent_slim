from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from onr.adapters.inprocess_transport import InProcessTransport
from onr.runtime import composition
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


class _FragmentedStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks


def _sse(*payloads: object, done: bool = True) -> bytes:
    events = [
        b"data: " + json.dumps(payload, separators=(",", ":")).encode() + b"\n\n"
        for payload in payloads
    ]
    if done:
        events.append(b"data: [DONE]\n\n")
    return b"".join(events)


def test_recorder_captures_raw_content_and_provider_reasoning(tmp_path: Path) -> None:
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
        tmp_path / "debug" / "llm",
        "mission:demo",
        role="hyper-agent",
        transport=transport,
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
    artifacts = list((tmp_path / "debug/llm/hyper-agent/mission%3Ademo").glob("*.json"))
    assert len(artifacts) == 1
    artifact = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 2
    assert artifact["sequence"] == 1
    assert artifact["request"] == request_body
    assert artifact["response_id"] == "chatcmpl-debug"
    assert artifact["model"] == "reasoning-model"
    assert artifact["status_code"] == 200
    assert artifact["finish_reason"] == "stop"
    assert artifact["content"] == "final answer"
    assert artifact["function_call"] == {"name": "interpret", "arguments": "{}"}
    assert artifact["reasoning"] == "raw reasoning"
    assert artifact["reasoning_content"] == "reasoning content"
    assert artifact["reasoning_details"] == [{"type": "summary", "text": "detail"}]
    assert artifact["tool_calls"] == [{"id": "call-1", "type": "function"}]
    assert artifact["completion_state"] == "complete"
    assert artifact["revision"] == 2
    assert artifact["error"] is None
    assert isinstance(artifact["finished_at"], str)
    assert isinstance(artifact["updated_at"], str)
    serialized = artifacts[0].read_text(encoding="utf-8")
    assert "private prompt" in serialized
    assert "raw reasoning" in serialized
    assert "prior private reasoning" in serialized
    assert "reasoning_content" in serialized
    assert "test-secret-key" not in serialized
    assert "test-secret-cookie" not in serialized
    assert "test-secret-header" not in serialized


def test_recorder_records_null_reasoning_and_ignores_other_responses(
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
        tmp_path / "llm",
        "mission/demo",
        role="maneuver-control",
        transport=httpx.MockTransport(respond),
    )
    recorder.http_client.get("http://vllm.test/v1/models")
    recorder.http_client.post("http://vllm.test/v1/chat/completions", json={})
    recorder.close()

    artifacts = list((tmp_path / "llm/maneuver-control/mission%2Fdemo").glob("*.json"))
    assert len(artifacts) == 1
    artifact = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert artifact["request"] == {}
    assert artifact["content"] == "plain answer"
    assert artifact["function_call"] is None
    assert artifact["reasoning"] is None
    assert artifact["reasoning_content"] is None
    assert artifact["reasoning_details"] is None
    assert artifact["tool_calls"] is None


@pytest.mark.parametrize("content", [None, b"{malformed"])
def test_recorder_writes_null_for_absent_or_malformed_request_body(
    tmp_path: Path, content: bytes | None
) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "answer"}}]
            },
            request=request,
        )
    )
    recorder = LLMResponseRecorder(
        tmp_path / "llm", "mission", role="mission-summary", transport=transport
    )
    request = recorder.http_client.build_request(
        "POST", "http://vllm.test/v1/chat/completions", content=content
    )

    recorder.http_client.send(request)
    recorder.close()

    artifact_path = tmp_path / "llm/mission-summary/mission/00000000000000000001.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["request"] is None
    assert artifact["content"] == "answer"


def test_recorder_continues_after_existing_artifacts(tmp_path: Path) -> None:
    directory = tmp_path / "llm" / "runtime" / "mission"
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

    artifacts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((tmp_path / "llm/runtime/mission").glob("*.json"))
    ]
    assert [artifact["completion_state"] for artifact in artifacts] == [
        "error",
        "error",
    ]
    assert [artifact["status_code"] for artifact in artifacts] == [500, 429]


def test_fragmented_sse_persists_first_delta_and_folds_tool_arguments(
    tmp_path: Path,
) -> None:
    first = {
        "id": "chatcmpl-stream",
        "model": "reasoning-model",
        "choices": [{"index": 0, "delta": {"reasoning_content": "Think "}}],
    }
    second = {
        "id": "chatcmpl-stream",
        "model": "reasoning-model",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "content": "answer",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "interpret",
                                "arguments": '{"mission":',
                            },
                        }
                    ],
                },
            }
        ],
    }
    third = {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "reasoning_content": "carefully",
                    "tool_calls": [
                        {
                            "index": 0,
                            "function": {"arguments": '"demo"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    encoded = _sse(first, second, third)
    split = encoded.index(b"\n\n") + 2
    chunks = [
        encoded[:7],
        encoded[7:split],
        encoded[split : split + 19],
        encoded[split + 19 :],
    ]
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_FragmentedStream(chunks),
            request=request,
        )
    )
    recorder = LLMResponseRecorder(tmp_path / "llm", "mission", transport=transport)
    artifact_path = tmp_path / "llm/runtime/mission/00000000000000000001.json"
    request = recorder.http_client.build_request(
        "POST",
        "http://vllm.test/v1/chat/completions",
        json={"model": "reasoning-model", "stream": True},
    )

    response = recorder.http_client.send(request, stream=True)
    try:
        iterator = response.iter_raw()
        assert next(iterator) == chunks[0]
        started = json.loads(artifact_path.read_text(encoding="utf-8"))
        assert started["completion_state"] == "live"
        assert started["revision"] == 1
        assert next(iterator) == chunks[1]
        first_delta = json.loads(artifact_path.read_text(encoding="utf-8"))
        assert first_delta["reasoning_content"] == "Think "
        assert first_delta["revision"] == 2
        assert b"".join(iterator) == b"".join(chunks[2:])
    finally:
        response.close()
    recorder.close()

    completed = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert completed["completion_state"] == "complete"
    assert completed["reasoning_content"] == "Think carefully"
    assert completed["content"] == "answer"
    assert completed["finish_reason"] == "tool_calls"
    assert completed["tool_calls"] == [
        {
            "index": 0,
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "interpret",
                "arguments": '{"mission":"demo"}',
            },
        }
    ]
    assert not list(artifact_path.parent.glob("*.tmp"))


@pytest.mark.parametrize(
    ("stream_body", "expected_state"),
    [
        (_sse({"choices": [{"delta": {"content": "partial"}}]}, done=False), "partial"),
        (b"data: {malformed}\n\n", "error"),
    ],
)
def test_stream_finalizes_premature_eof_and_malformed_events(
    tmp_path: Path, stream_body: bytes, expected_state: str
) -> None:
    recorder = LLMResponseRecorder(
        tmp_path / "llm",
        "mission",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                stream=_FragmentedStream([stream_body]),
                request=request,
            )
        ),
    )

    response = recorder.http_client.post(
        "http://vllm.test/v1/chat/completions",
        json={"stream": True},
    )
    recorder.close()

    assert response.content == stream_body
    artifact = json.loads(
        (tmp_path / "llm/runtime/mission/00000000000000000001.json").read_text()
    )
    assert artifact["completion_state"] == expected_state
    assert isinstance(artifact["finished_at"], str)


def test_transport_failure_finalizes_the_started_record(tmp_path: Path) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("endpoint unavailable", request=request)

    recorder = LLMResponseRecorder(
        tmp_path / "llm", "mission", transport=httpx.MockTransport(fail)
    )

    with pytest.raises(httpx.ConnectError):
        recorder.http_client.post(
            "http://vllm.test/v1/chat/completions", json={"stream": True}
        )
    recorder.close()

    artifact = json.loads(
        (tmp_path / "llm/runtime/mission/00000000000000000001.json").read_text()
    )
    assert artifact["completion_state"] == "error"
    assert artifact["status_code"] is None
    assert artifact["error"] == {
        "type": "ConnectError",
        "message": "endpoint unavailable",
    }


def test_logging_write_failure_does_not_interrupt_the_http_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response_body = {
        "choices": [{"finish_reason": "stop", "message": {"content": "answer"}}]
    }
    recorder = LLMResponseRecorder(
        tmp_path / "llm",
        "mission",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=response_body, request=request)
        ),
    )

    def fail_write(path: Path, artifact: object) -> None:
        del path, artifact
        raise OSError("debug storage unavailable")

    monkeypatch.setattr(recorder, "_write_atomic", fail_write)

    response = recorder.http_client.post(
        "http://vllm.test/v1/chat/completions", json={}
    )
    recorder.close()

    assert response.json() == response_body


def test_streamed_chat_openai_invoke_preserves_shape_and_pairs_invocation(
    tmp_path: Path,
) -> None:
    chunks = _sse(
        {
            "id": "chatcmpl-integration",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "reasoning-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "reasoning_content": "Reasoning live.",
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-integration",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "reasoning-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "assembled answer"},
                    "finish_reason": "stop",
                }
            ],
        },
    )
    recorder = LLMResponseRecorder(
        tmp_path / "debug/llm",
        "mission",
        role="hyper-agent",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_FragmentedStream([chunks[:31], chunks[31:]]),
                request=request,
            )
        ),
    )
    agent_recorder = composition.AgentDebugRecorder(
        tmp_path / "debug/agent", "mission", role="hyper-agent"
    )
    callback = agent_recorder.callback_for("hyper-agent")
    model = ChatOpenAI(
        base_url="http://vllm.test/v1",
        model="reasoning-model",
        api_key="EMPTY",
        streaming=True,
        max_retries=0,
        http_client=recorder.http_client,
    )

    result = model.invoke(
        [HumanMessage(content="hello")], config={"callbacks": [callback]}
    )
    recorder.close()

    assert result.content == "assembled answer"
    raw = json.loads(
        (
            tmp_path / "debug/llm/hyper-agent/mission/00000000000000000001.json"
        ).read_text()
    )
    agent = json.loads(
        (
            tmp_path / "debug/agent/hyper-agent/mission/00000000000000000001.json"
        ).read_text()
    )
    assert raw["reasoning_content"] == "Reasoning live."
    assert raw["completion_state"] == "complete"
    assert agent["completion_state"] == "complete"
    assert raw["invocation_id"] == agent["invocation_id"]


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
        agent_name="test-agent",
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
        def __init__(self, root: Path, mission_id: str, *, role: str) -> None:
            constructed.append((root, role, mission_id))
            self.http_client = object()

    class FakeAgentRecorder:
        def __init__(self, root: Path, mission_id: str, *, role: str) -> None:
            constructed.append((root, role, mission_id))

    class FakeChatModel:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr(composition, "ChatOpenAI", FakeChatModel)
    monkeypatch.setattr(composition, "LLMResponseRecorder", FakeRecorder)
    monkeypatch.setattr(composition, "AgentDebugRecorder", FakeAgentRecorder)
    runtime = composition.RuntimeComposition(config, InProcessTransport())

    model = cast(
        Any,
        runtime.create_chat_model(mission_id="mission:demo", debug_scope="hyper-agent"),
    )

    assert constructed == [
        (tmp_path / "var/debug/llm", "hyper-agent", "mission:demo"),
        (tmp_path / "var/debug/agent", "hyper-agent", "mission:demo"),
    ]
    assert model.kwargs["http_client"] is model._llm_response_recorder.http_client
    assert model.kwargs["streaming"] is True
    assert isinstance(model._agent_debug_recorder, FakeAgentRecorder)
