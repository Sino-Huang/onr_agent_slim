from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from onr.runtime.agent_debug import AgentDebugRecorder


def _artifacts(directory: Path) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("[0-9]*.json"))
    ]


def test_recorder_writes_profile_and_paired_successful_invocations(
    tmp_path: Path,
) -> None:
    recorder = AgentDebugRecorder(
        tmp_path / "debug/agent", "mission:demo", role="hyper-agent"
    )
    recorder.record_profile(
        "hyper-agent",
        [
            {
                "name": "mission-parsing",
                "version": "1.0.0",
                "path": "/original/skills/mission-parsing",
            }
        ],
        [],
    )
    callback = recorder.callback_for("hyper-agent")
    parent_id = UUID("00000000-0000-0000-0000-000000000001")
    llm_id = UUID("00000000-0000-0000-0000-000000000002")
    tool_id = UUID("00000000-0000-0000-0000-000000000003")

    callback.on_chat_model_start(
        {"id": ["langchain", "ChatOpenAI"]},
        [[HumanMessage(content="inspect the skill")]],
        run_id=llm_id,
        parent_run_id=parent_id,
        invocation_params={
            "model_name": "solver-model",
            "provider_input": {
                "reasoning": "input reasoning",
                "reasoning_content": "input reasoning content",
                "reasoning_details": [{"type": "input", "text": "detail"}],
            },
            "tools": [{"type": "function", "function": {"name": "read_file"}}],
        },
    )
    callback.on_tool_start(
        {"name": "read_file"},
        '{"file_path":"/original/skills/mission-parsing/SKILL.md"}',
        run_id=tool_id,
        parent_run_id=llm_id,
        inputs={"file_path": "/original/skills/mission-parsing/SKILL.md"},
    )
    callback.on_tool_end({"content": "skill contents"}, run_id=tool_id)
    callback.on_llm_end(
        LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(
                            content="done",
                            tool_calls=[
                                {
                                    "name": "read_file",
                                    "args": {"file_path": "SKILL.md"},
                                    "id": "call-1",
                                    "type": "tool_call",
                                }
                            ],
                        )
                    )
                ]
            ],
            llm_output={
                "structured": {
                    "status": "complete",
                    "provider_output": {
                        "reasoning": "output reasoning",
                        "reasoning_content": "output reasoning content",
                        "reasoning_details": [{"type": "output", "text": "detail"}],
                    },
                }
            },
        ),
        run_id=llm_id,
    )

    directory = tmp_path / "debug/agent/hyper-agent/mission%3Ademo"
    profile = json.loads(
        (directory / "profiles/hyper-agent.json").read_text(encoding="utf-8")
    )
    assert profile == {
        "schema_version": 1,
        "agent_role": "hyper-agent",
        "skills": [
            {
                "name": "mission-parsing",
                "version": "1.0.0",
                "path": "/original/skills/mission-parsing",
            }
        ],
        "tools": ["read_file"],
    }
    llm, tool = _artifacts(directory)
    assert llm["sequence"] == 1
    assert tool["sequence"] == 2
    assert tool["invocation_id"] == str(tool_id)
    assert tool["parent_id"] == str(llm_id)
    assert tool["kind"] == "tool"
    assert tool["name"] == "read_file"
    assert tool["input"] == {"file_path": "/original/skills/mission-parsing/SKILL.md"}
    assert tool["output"] == {"content": "skill contents"}
    assert tool["error"] is None
    assert llm["invocation_id"] == str(llm_id)
    assert llm["parent_id"] == str(parent_id)
    assert llm["kind"] == "llm"
    assert llm["name"] == "solver-model"
    assert llm["input"]["messages"][0][0]["content"] == "inspect the skill"
    assert llm["input"]["invocation_params"]["provider_input"] == {
        "reasoning": "input reasoning",
        "reasoning_content": "input reasoning content",
        "reasoning_details": [{"type": "input", "text": "detail"}],
    }
    assert llm["output"]["generations"][0][0]["message"]["content"] == "done"
    assert llm["output"]["llm_output"]["structured"]["provider_output"] == {
        "reasoning": "output reasoning",
        "reasoning_content": "output reasoning content",
        "reasoning_details": [{"type": "output", "text": "detail"}],
    }
    assert llm["error"] is None
    assert llm["schema_version"] == tool["schema_version"] == 2
    assert llm["completion_state"] == tool["completion_state"] == "complete"
    assert llm["revision"] == tool["revision"] == 2
    assert isinstance(llm["started_at"], str)
    assert isinstance(llm["finished_at"], str)
    assert not list(directory.rglob("*.tmp"))


def test_invocation_is_visible_at_start_and_replaced_in_place(tmp_path: Path) -> None:
    recorder = AgentDebugRecorder(tmp_path / "agent", "mission")
    callback = recorder.callback_for("hyper-agent")
    run_id = UUID("20000000-0000-0000-0000-000000000001")

    callback.on_tool_start({"name": "inspect"}, "draft", run_id=run_id)

    directory = tmp_path / "agent/runtime/mission"
    [path] = sorted(directory.glob("*.json"))
    started = json.loads(path.read_text(encoding="utf-8"))
    assert started["sequence"] == 1
    assert started["invocation_id"] == str(run_id)
    assert started["completion_state"] == "live"
    assert started["finished_at"] is None
    assert started["revision"] == 1

    callback.on_tool_error(RuntimeError("stopped"), run_id=run_id)

    assert sorted(directory.glob("*.json")) == [path]
    finished = json.loads(path.read_text(encoding="utf-8"))
    assert finished["completion_state"] == "error"
    assert finished["revision"] == 2
    assert finished["error"] == {"type": "RuntimeError", "message": "stopped"}


def test_callback_logging_failure_is_fail_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = AgentDebugRecorder(tmp_path / "agent", "mission")
    callback = recorder.callback_for("hyper-agent")
    run_id = UUID("30000000-0000-0000-0000-000000000001")

    def fail_write(path: Path, artifact: object) -> None:
        del path, artifact
        raise OSError("debug storage unavailable")

    monkeypatch.setattr(recorder, "_write_atomic", fail_write)

    callback.on_tool_start({"name": "inspect"}, "input", run_id=run_id)
    callback.on_tool_end("output", run_id=run_id)
    recorder.record_profile("hyper-agent", [], [])


def test_recorder_pairs_errors_retains_reasoning_and_continues_sequence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "agent"
    recorder = AgentDebugRecorder(root, "mission/demo", role="maneuver-control")
    recorder.record_profile("maneuver-control", [], ["execute"])
    callback = recorder.callback_for("maneuver-control")
    first_id = UUID("10000000-0000-0000-0000-000000000001")
    second_id = UUID("10000000-0000-0000-0000-000000000002")
    callback.on_chat_model_start(
        {"name": "private-model"},
        [[HumanMessage(content="decide", additional_kwargs={"reasoning": "hidden"})]],
        run_id=first_id,
        invocation_params={
            "reasoning_content": "hidden content",
            "reasoning_details": ["hidden detail"],
        },
    )
    callback.on_tool_start(
        {"name": "execute"},
        "raw request",
        run_id=second_id,
        parent_run_id=first_id,
    )
    callback.on_llm_error(RuntimeError("model unavailable"), run_id=first_id)
    callback.on_tool_error(ValueError("tool failed"), run_id=second_id)

    restarted = AgentDebugRecorder(root, "mission/demo", role="maneuver-control")
    restarted.record_profile("maneuver-control", [], ["execute"])
    callback = restarted.callback_for("maneuver-control")
    third_id = UUID("10000000-0000-0000-0000-000000000003")
    callback.on_tool_start({"name": "execute"}, "raw input", run_id=third_id)
    callback.on_tool_end("ok", run_id=third_id)

    directory = root / "maneuver-control/mission%2Fdemo"
    artifacts = _artifacts(directory)
    assert [artifact["sequence"] for artifact in artifacts] == [1, 2, 3]
    assert artifacts[0]["invocation_id"] == str(first_id)
    assert artifacts[0]["output"] is None
    assert artifacts[0]["error"] == {
        "type": "RuntimeError",
        "message": "model unavailable",
    }
    assert artifacts[1]["invocation_id"] == str(second_id)
    assert artifacts[1]["input"] == "raw request"
    assert artifacts[1]["error"] == {"type": "ValueError", "message": "tool failed"}
    assert artifacts[2]["invocation_id"] == str(third_id)
    assert artifacts[2]["output"] == "ok"
    assert artifacts[0]["input"]["messages"][0][0]["additional_kwargs"] == {
        "reasoning": "hidden"
    }
    assert artifacts[0]["input"]["invocation_params"] == {
        "reasoning_content": "hidden content",
        "reasoning_details": ["hidden detail"],
    }
    assert not list(directory.rglob("*.tmp"))
