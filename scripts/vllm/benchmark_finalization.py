"""Replay one recorded Hyper phase request without executing any returned tools.

Both variants use the same tool schemas and generation settings. A fresh label
before the system instructions prevents reusing the old conversation prefix;
the stable tool-schema prefix can still be cached. Outputs stay under Agent var/.
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

from onr.contracts.fsm import Statechart
from onr.contracts.hyper_agent import MissionInput
from onr.contracts.planning import PlannerPlan
from onr.contracts.planning_intent import PlanningIntent


def post(base_url: str, endpoint: str, body: dict):
    return urlopen(
        Request(
            f"{base_url.rstrip('/')}/{endpoint}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        ),
        timeout=240,
    )


def latest_todos(messages: list[dict]) -> list[dict]:
    for message in reversed(messages):
        for call in reversed(message.get("tool_calls", [])):
            function = call["function"]
            if function["name"] == "write_todos":
                return json.loads(function["arguments"])["todos"]
    raise ValueError("recorded request has no todo state")


def cache_counters(base_url: str) -> dict[str, float]:
    with urlopen(f"{base_url.rstrip('/')}/metrics", timeout=10) as response:
        lines = response.read().decode().splitlines()
    return {
        key: sum(
            float(line.rsplit(" ", 1)[1])
            for line in lines
            if line.startswith(f"vllm:{key}" + "{")
        )
        for key in (
            "prefix_cache_queries_total",
            "prefix_cache_hits_total",
            "e2e_request_latency_seconds_count",
            "num_requests_running",
            "num_requests_waiting",
        )
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--variant", choices=("baseline", "compact"), required=True)
    parser.add_argument(
        "--stage", choices=("finalization", "statechart"), default="finalization"
    )
    parser.add_argument("--planner-plan", type=Path, required=True)
    parser.add_argument("--statechart", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:11411")
    parser.add_argument(
        "--cache-label", help="Reuse a label to measure warm-prefix reuse"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if not args.output.resolve().is_relative_to(root / "var"):
        parser.error("output must be under this Agent repository's var directory")
    recorded = json.loads(args.request.read_text())
    payload = recorded["request"]
    plan = PlannerPlan.from_dict(json.loads(args.planner_plan.read_text()))
    chart = Statechart.from_dict(json.loads(args.statechart.read_text()))
    todos = latest_todos(payload["messages"])
    if args.variant == "compact" and args.stage == "finalization":
        from onr.agents.hyper_workflow import _finalization_model_context

        system, messages = _finalization_model_context(
            SimpleNamespace(
                planner_plan=plan,
                statechart=chart,
                statechart_reference=str(args.statechart.resolve()),
            ),
            todos,
        )
        payload["messages"] = [
            {"role": "system", "content": system.content},
            {"role": "user", "content": messages[0].content},
        ]
    elif args.variant == "compact":
        from langchain_core.messages import convert_to_messages
        from langchain_openai.chat_models.base import _convert_message_to_dict

        from onr.agents.hyper_workflow import _statechart_model_messages

        mission = MissionInput(
            **json.loads(
                next(
                    message["content"]
                    for message in payload["messages"]
                    if message["role"] == "user"
                )
            )
        )
        intent_args = next(
            json.loads(call["function"]["arguments"])
            for message in payload["messages"]
            for call in message.get("tool_calls", [])
            if call["function"]["name"] == "record_planning_intent"
        )
        intent = PlanningIntent.from_dict(
            {
                "schema_version": 1,
                "mission_id": mission.mission_id,
                "source_authority": mission.source_authority,
                "objective": intent_args["objective"],
                "planner_choice": plan.planner_choice.to_dict(),
                "rationale": intent_args["rationale"],
                "details": intent_args["details"],
            }
        )
        original = payload["messages"]
        planner_call_ids = {
            call["id"]
            for message in original
            for call in message.get("tool_calls", [])
            if call["function"]["name"] == "planner_executor"
        }
        planner_reply = next(
            message["content"]
            for message in reversed(original)
            if message.get("tool_call_id") in planner_call_ids
        )
        locations = {
            name: planner_reply.rsplit(f"\n{name}: ", 1)[1].splitlines()[0]
            for name in (
                "statechart_generator_file_location",
                "statechart_file_location",
                "statechart_shell_workspace",
            )
        }
        messages = _statechart_model_messages(
            SimpleNamespace(
                mission_input=mission,
                planning_intent=intent,
                planner_plan=plan,
                statechart_generator_location=locations[
                    "statechart_generator_file_location"
                ],
                statechart_file_location=locations["statechart_file_location"],
                planner_shell_workspace_location=locations[
                    "statechart_shell_workspace"
                ].rsplit("/", 1)[0],
            ),
            convert_to_messages(original[1:]),
        )
        payload["messages"] = [original[0], *map(_convert_message_to_dict, messages)]
    label = f"Local benchmark label: {args.cache_label or uuid.uuid4().hex}.\n"
    content = payload["messages"][0]["content"]
    payload["messages"][0]["content"] = (
        label + content
        if isinstance(content, str)
        else [{"type": "text", "text": label}, *content]
    )
    payload["max_tokens"] = 1536
    payload["stream_options"] = {"include_usage": True}
    payload["seed"] = 0
    with post(
        args.base_url,
        "tokenize",
        {key: payload[key] for key in ("model", "messages", "tools")}
        | {"add_generation_prompt": True},
    ) as response:
        input_tokens = json.load(response)["count"]

    cache_before = cache_counters(args.base_url)
    if cache_before["num_requests_running"] or cache_before["num_requests_waiting"]:
        raise SystemExit(
            "another LLM request is active; run this timing test on an idle server"
        )
    started = time.monotonic()
    first_token = None
    calls: dict[int, dict] = {}
    usage = None
    finish_reason = None
    output_chars = 0
    with post(args.base_url, "v1/chat/completions", payload) as response:
        for line in response:
            if not line.startswith(b"data: ") or line.strip() == b"data: [DONE]":
                continue
            event = json.loads(line[6:])
            if event.get("usage"):
                usage = event["usage"]
            for choice in event.get("choices", []):
                delta = choice.get("delta", {})
                text = "".join(
                    delta.get(key) or ""
                    for key in ("content", "reasoning", "reasoning_content")
                )
                output_chars += len(text)
                if first_token is None and (text or delta.get("tool_calls")):
                    first_token = time.monotonic() - started
                for call in delta.get("tool_calls", []):
                    target = calls.setdefault(
                        call["index"], {"name": "", "arguments": ""}
                    )
                    for key in ("name", "arguments"):
                        target[key] += call.get("function", {}).get(key) or ""
                finish_reason = choice.get("finish_reason") or finish_reason
    elapsed = time.monotonic() - started
    cache_after = cache_counters(args.base_url)
    parsed = [
        {"name": call["name"], "arguments": json.loads(call["arguments"])}
        for _, call in sorted(calls.items())
    ]
    allowed = {tool["function"]["name"] for tool in payload["tools"]}
    if args.stage == "statechart":
        expected_read = next(
            json.loads(call["function"]["arguments"])["file_path"]
            for call in recorded["tool_calls"]
            if call["function"]["name"] == "read_file"
        )
        correct = any(
            call["name"] == "read_file"
            and call["arguments"].get("file_path") == expected_read
            for call in parsed
        ) and all(call["name"] in {"read_file", "write_todos"} for call in parsed)
        for call in parsed:
            if call["name"] == "write_todos":
                correct &= call["arguments"]["todos"] == [
                    {
                        "content": item["content"],
                        "status": "completed"
                        if index < 5
                        else "in_progress"
                        if index == 5
                        else "pending",
                    }
                    for index, item in enumerate(todos)
                ]
    elif allowed == {"write_todos"}:
        expected = [
            {"content": item["content"], "status": "completed"} for item in todos
        ]
        correct = len(parsed) == 1 and parsed[0] == {
            "name": "write_todos",
            "arguments": {"todos": expected},
        }
    else:
        correct = len(parsed) == 1 and parsed[0] == {
            "name": "HyperWorkflowResultCandidate",
            "arguments": {"mission_id": plan.mission_id, "outcome": "execution_ready"},
        }
    result = {
        "variant": args.variant,
        "stage": args.stage,
        "request": str(args.request.resolve()),
        "tokenized_input_tokens": input_tokens,
        "input_tokens": usage["prompt_tokens"] if usage is not None else input_tokens,
        "seconds": elapsed,
        "first_token_seconds": first_token,
        "usage": usage,
        "output_chars": output_chars,
        "finish_reason": finish_reason,
        "calls": parsed,
        "correct": correct,
        "tools_executed": False,
        "cache_token_deltas": {
            key: cache_after[key] - cache_before[key]
            for key in ("prefix_cache_queries_total", "prefix_cache_hits_total")
        },
        "isolated": (
            cache_after["e2e_request_latency_seconds_count"]
            - cache_before["e2e_request_latency_seconds_count"]
            == 1
            and cache_after["num_requests_running"] == 0
            and cache_after["num_requests_waiting"] == 0
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "calls"}))
    if not correct:
        raise SystemExit(
            "finalization output did not preserve the verified result/todos"
        )
    if not result["isolated"]:
        raise SystemExit(
            "other requests overlapped this measurement; timing is not isolated"
        )


if __name__ == "__main__":
    main()
