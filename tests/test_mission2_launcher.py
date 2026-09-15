"""Verify launcher mode selection without creating herdr panes or services."""
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("mode", ["mission1", "mission2", "joint"])
def test_launcher_dry_run_preserves_default_and_selects_mission2(tmp_path, mode):
    repo = Path(__file__).parents[1]
    agent, physical = tmp_path / "agent", tmp_path / "physical"
    (agent / "conf").mkdir(parents=True)
    (agent / "examples").mkdir()
    for name in ("onr_agent_params.yaml", "environment_physical.yaml"):
        shutil.copyfile(repo / "conf" / name, agent / "conf" / name)
    for name in ("mission.json", "mission2.json", "mission1-and-2.json"):
        shutil.copyfile(repo / "examples" / name, agent / "examples" / name)
    instance = physical / "data/harbor_world/mission1_instances/demo-001"
    instance.mkdir(parents=True)
    (instance / "events_report.json").write_text("[]")
    config = tmp_path / "scenario's configuration.yaml"
    config.write_text("{}")
    scenario = tmp_path / "moving" / "0"
    (scenario / "ships").mkdir(parents=True)
    (scenario / "ships/events.json").write_text("[]")
    script = tmp_path / "launcher.sh"
    script.write_text((repo / "scripts/live_demo_with_wm/herdr_start_live_demo.sh").read_text()
        .replace('/data/ccu/sukaih/ONR/onr_agent_slim', str(agent))
        .replace('/data/ccu/sukaih/ONR/onr_physical_runtime', str(physical)))
    env = {k: v for k, v in os.environ.items() if not k.startswith("ONR_DEMO_")}
    env.update(ONR_DEMO_DRY_RUN="1", ONR_DEMO_SCENARIO_CONFIG=str(config),
               ONR_DEMO_MISSION2_SCENARIO=str(scenario))
    planning_input = tmp_path / "mission1-planning.json"
    planning_input.write_text("{}")
    if mode != "mission2":
        env["ONR_DEMO_MISSION1_PLANNING_INPUT"] = str(planning_input)
    if mode != "mission1":
        env["ONR_DEMO_MISSION_MODE"] = mode
    if mode == "joint":
        env["ONR_DEMO_MISSION1_INSTANCE"] = str(instance)
    result = subprocess.run(["bash", str(script), "not-a-real-herdr-session"], env=env,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    commands = {}
    for line in result.stdout.splitlines():
        if line.startswith(("Physical command:", "Agent command:")):
            key, command = line.split(": ", 1)
            inner = shlex.split(command)[2]
            commands[key] = shlex.split(inner.rsplit("exec ", 1)[1])
    argv = commands["Physical command"]
    assert argv[argv.index("--scenario-config") + 1] == str(config)
    if mode == "mission1":
        assert "--mission-mode" not in argv
    else:
        assert argv[argv.index("--mission-mode") + 1] == mode
        assert argv[argv.index("--mission2-scenario-dir") + 1] == str(scenario)
    assert ("--mission1-instance-dir" in argv) == (mode != "mission2")
    expected = {"mission1": "mission.json", "mission2": "mission2.json", "joint": "mission1-and-2.json"}[mode]
    agent_argv = commands["Agent command"]
    assert agent_argv[agent_argv.index("--mission-file") + 1].endswith(expected)
    run_root = Path(next(
        line.removeprefix("Run configuration: ")
        for line in result.stdout.splitlines()
        if line.startswith("Run configuration: ")
    ))
    generated_profile = yaml.safe_load(
        (run_root / "environment_physical.yaml").read_text()
    )
    expected_planning_input = str(planning_input) if mode != "mission2" else None
    assert generated_profile["external"]["mission1_planning_input_path"] == expected_planning_input
    assert "no services started" in result.stdout
