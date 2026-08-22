from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from onr.adapters.system_prompts import load_system_prompt

pytestmark = pytest.mark.live
_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_live_hyper_agent_repairs_existing_file_with_edit_file(
    tmp_path: Path,
) -> None:
    values = yaml.safe_load(
        (_REPO_ROOT / "conf/onr_agent_params.yaml").read_text(encoding="utf-8")
    )
    llm = values["llm"]
    model = ChatOpenAI(
        base_url=llm["base_url"],
        model=llm["model"],
        api_key=llm["api_key"],
        temperature=0.0,
        reasoning_effort="low",
        max_tokens=4096,
        max_retries=0,
        timeout=120.0,
    )
    planner_file = tmp_path / "workspace/model.mzn"
    planner_file.parent.mkdir(parents=True)
    planner_file.write_text("int: horizon = 250;\nsolve satisfy;\n", encoding="utf-8")
    agent = create_deep_agent(
        model=model,
        system_prompt=(
            load_system_prompt(
                _REPO_ROOT / "conf/system_prompt",
                "hyper-agent",
            )
            + "\n\nThis is a focused filesystem capability check. Perform only the "
            "requested repair and finish after it succeeds."
        ),
        backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True),
    )

    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Repair the existing planner file /workspace/model.mzn by "
                        "replacing `int: horizon = 250;` with "
                        "`int: horizon = 279;`."
                    ),
                }
            ]
        },
        config={"recursion_limit": 12},
    )

    tool_names = [
        call["name"]
        for message in result["messages"]
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    ]
    assert "write_file" not in tool_names
    assert tool_names.index("read_file") < tool_names.index("edit_file")
    assert planner_file.read_text(encoding="utf-8") == (
        "int: horizon = 279;\nsolve satisfy;\n"
    )
