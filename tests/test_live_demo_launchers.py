"""Verify the discoverable Mission 2-4 live-demo adapters."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("mode", ["mission2", "mission3", "mission4"])
def test_mission_live_demo_adapter_selects_shared_launcher(mode: str) -> None:
    repository = Path(__file__).parents[1]
    script = repository / f"scripts/live_demo_with_wm/herdr_start_{mode}_live_demo.sh"
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("ONR_DEMO_")
    }
    environment["ONR_DEMO_DRY_RUN"] = "1"
    environment["ONR_DEMO_VIEWER_PORT"] = "5099"
    result = subprocess.run(
        ["bash", str(script), "unused-session"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert f"DRY RUN: mode={mode}; no services started" in result.stdout
    run_root = Path(
        next(
            line.removeprefix("Run configuration: ")
            for line in result.stdout.splitlines()
            if line.startswith("Run configuration: ")
        )
    )
    assert "  maneuver_seconds: 300\n" in (
        run_root / "onr_agent_params.yaml"
    ).read_text(encoding="utf-8")
    commands = {}
    for line in result.stdout.splitlines():
        if line.startswith(("Physical command:", "Agent command:", "Worker command:")):
            key, command = line.split(": ", 1)
            commands[key] = shlex.split(shlex.split(command)[2].rsplit("exec ", 1)[1])
    physical = commands["Physical command"]
    assert physical[physical.index("--mission-mode") + 1] == mode
    assert physical[physical.index("--viewer-port") + 1] == "5099"
    agent = commands["Agent command"]
    assert agent[agent.index("--mission-file") + 1].endswith(f"{mode}.json")
    assert "--result-path" in agent
    if mode == "mission2":
        assert physical[physical.index("--scenario-config") + 1].endswith(
            "config/mission2_live_demo.yaml"
        )
    if mode == "mission3":
        assert physical[physical.index("--mission3-fixture") + 1].endswith(
            "config/mission3_smoke/private_fixture.json"
        )
    if mode == "mission4":
        assert physical[physical.index("--scenario-config") + 1].endswith(
            "config/mission4_live_demo.yaml"
        )
        assert physical[physical.index("--mission4-package") + 1].endswith(
            "docs/mission_desc/mission4_package.json"
        )
        assert physical[physical.index("--mission4-fixture") + 1].endswith(
            "docs/mission_desc/mission4_fixture.json"
        )
        worker = commands["Worker command"]
        assert worker[worker.index("--script") + 1].endswith(
            "examples/mission4_requests.json"
        )
        assert "--ready-file" in worker
        assert worker[worker.index("--timeout-seconds") + 1] == "3600"
    else:
        assert "Worker command:" not in result.stdout
