from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path


def test_launcher_dry_run_carries_mission3_description_and_optional_fixture(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[1]
    agent_root = tmp_path / "agent"
    physical_root = tmp_path / "physical"
    (agent_root / "conf").mkdir(parents=True)
    (agent_root / "examples").mkdir()
    for name in ("onr_agent_params.yaml", "environment_physical.yaml"):
        shutil.copyfile(repository / "conf" / name, agent_root / "conf" / name)
    for name in ("mission3.json", "mission3_description.json"):
        shutil.copyfile(repository / "examples" / name, agent_root / "examples" / name)
    scenario = tmp_path / "mission3-scenario.yaml"
    scenario.write_text("{}\n", encoding="utf-8")
    fixture = tmp_path / "private-fixture.json"
    fixture.write_text("{}\n", encoding="utf-8")
    description = agent_root / "examples/mission3_description.json"
    script = tmp_path / "launcher.sh"
    script.write_text(
        (repository / "scripts/live_demo_with_wm/herdr_start_live_demo.sh")
        .read_text(encoding="utf-8")
        .replace("/data/ccu/sukaih/ONR/onr_agent_slim", str(agent_root))
        .replace("/data/ccu/sukaih/ONR/onr_physical_runtime", str(physical_root)),
        encoding="utf-8",
    )
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("ONR_DEMO_")
    }
    environment.update(
        ONR_DEMO_DRY_RUN="1",
        ONR_DEMO_MISSION_MODE="mission3",
        ONR_DEMO_SCENARIO_CONFIG=str(scenario),
        ONR_DEMO_MISSION3_DESCRIPTION=str(description),
        ONR_DEMO_MISSION3_FIXTURE=str(fixture),
    )
    result = subprocess.run(
        ["bash", str(script), "not-a-real-herdr-session"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    commands = {}
    for line in result.stdout.splitlines():
        if line.startswith(("Physical command:", "Agent command:")):
            key, command = line.split(": ", 1)
            inner = shlex.split(command)[2]
            commands[key] = shlex.split(inner.rsplit("exec ", 1)[1])
    physical = commands["Physical command"]
    assert physical[physical.index("--mission-mode") + 1] == "mission3"
    assert physical[physical.index("--mission3-selection") + 1] == str(description)
    assert physical[physical.index("--mission3-fixture") + 1] == str(fixture)
    assert "--mission1-instance-dir" not in physical
    assert "--mission2-scenario-dir" not in physical
    agent = commands["Agent command"]
    assert agent[agent.index("--mission-file") + 1].endswith("mission3.json")
    assert "DRY RUN: mode=mission3; no services started" in result.stdout


def test_launcher_rejects_missing_mission3_description(tmp_path: Path) -> None:
    repository = Path(__file__).parents[1]
    script = repository / "scripts/live_demo_with_wm/herdr_start_live_demo.sh"
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("ONR_DEMO_")
    }
    environment.update(
        ONR_DEMO_DRY_RUN="1",
        ONR_DEMO_MISSION_MODE="mission3",
        ONR_DEMO_MISSION3_DESCRIPTION=str(tmp_path / "missing.json"),
    )
    result = subprocess.run(
        ["bash", str(script), "not-a-real-herdr-session"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert "Mission 3 description is missing" in result.stderr
