"""Verify the discoverable Mission 2-4 live-demo adapters."""

from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import sys
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
        ["bash", str(script), "05_onr"],
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
    assert run_root.parent == repository / "var/live_demo_with_wm" / mode
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
    audit_line = next(
        line.removeprefix("Terminal audit: ")
        for line in result.stdout.splitlines()
        if line.startswith("Terminal audit: ")
    )
    audit = shlex.split(audit_line)
    assert audit[audit.index("--run-root") + 1] == str(run_root)
    assert audit[audit.index("--mission-mode") + 1] == mode
    if mode == "mission2":
        assert physical[physical.index("--scenario-config") + 1].endswith(
            "config/mission2_live_demo.yaml"
        )
        assert audit[audit.index("--mission2-scenario-dir") + 1].endswith(
            "onr_scenario/offshore_dock_1/collision/0"
        )
    else:
        assert "--mission2-scenario-dir" not in audit
    if mode == "mission3":
        assert physical[physical.index("--mission3-fixture") + 1].endswith(
            "config/mission3_smoke/private_fixture.json"
        )
    if mode == "mission4":
        assert physical[physical.index("--scenario-config") + 1].endswith(
            "config/mission4_offshore_demo.yaml"
        )
        assert physical[physical.index("--mission4-package") + 1].endswith(
            "docs/mission_desc/mission4_offshore_non_collision_0/package.json"
        )
        assert physical[physical.index("--mission4-fixture") + 1].endswith(
            "docs/mission_desc/mission4_offshore_non_collision_0/fixture.json"
        )
        assert audit[audit.index("--mission4-answers") + 1].endswith(
            "docs/mission_desc/mission4_offshore_non_collision_0/answers.json"
        )
        worker = commands["Worker command"]
        assert worker[worker.index("--script") + 1].endswith(
            "examples/mission4_requests.json"
        )
        assert "--ready-file" in worker
        assert worker[worker.index("--timeout-seconds") + 1] == "3600"
    else:
        assert "Worker command:" not in result.stdout


def _dry_run(script: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("ONR_DEMO_")
    }
    environment.update(ONR_DEMO_DRY_RUN="1", ONR_DEMO_VIEWER_PORT="5099", **overrides)
    return subprocess.run(
        ["bash", str(script), "05_onr"], env=environment, text=True,
        capture_output=True, timeout=10, check=False,
    )


def test_mission1_airsim_launcher_wires_engine_producer_and_runtime() -> None:
    repository = Path(__file__).parents[1]
    script = repository / "scripts/live_demo_with_wm/herdr_start_mission1_airsim_live_demo.sh"
    result = _dry_run(script)
    assert result.returncode == 0, result.stderr
    lines = dict(line.split(": ", 1) for line in result.stdout.splitlines() if ": " in line)
    run_root = Path(lines["Run configuration"])
    try:
        commands = {
            key: shlex.split(shlex.split(lines[key])[2].rsplit("exec ", 1)[1])
            for key in ("Physical command", "Agent command", "Engine command", "Perception command")
        }
        physical, producer, engine = (
            commands["Physical command"], commands["Perception command"], commands["Engine command"]
        )
        engine_root = Path(engine[engine.index("--output") + 1])
        assert engine_root == run_root / "engine"
        scene = engine[engine.index("--scenario") + 1]
        # Runtime, producer and engine agree on the run, scene and clock.
        assert physical[physical.index("--perception-run-id") + 1] == producer[producer.index("--run-id") + 1]
        assert physical[physical.index("--perception-url") + 1].endswith(producer[producer.index("--port") + 1])
        assert physical[physical.index("--experimental-scene-clock-state") + 1] == str(engine_root / "shim/clock.bin")
        assert physical[physical.index("--experimental-scene-times") + 1] == str(
            engine_root / "status" / Path(scene).name / "scenario_times.json"
        )
        assert producer[producer.index("--runtime-entity-map") + 1] == str(engine_root / "actor-entities.json")
        assert producer[producer.index("--ship-config-dir") + 1] == f"{scene}/ships"
        assert producer[producer.index("--perception") + 1] == "yolo"
        assert physical[physical.index("--mission1-instance-dir") + 1].endswith(
            "data/offshore_dock_1/mission1_instances/airsim-live-001"
        )
        assert "--airsim-rpc-url" not in physical
        # The scene clock provisions its epoch only into fresh runtime state.
        assert not (run_root / "physical-state").exists()
        agent = commands["Agent command"]
        assert agent[agent.index("--simulation-limit-seconds") + 1] == "290"
        audit = shlex.split(lines["Terminal audit"])
        assert audit[audit.index("--perception") + 1] == "yolo"
    finally:
        shutil.rmtree(run_root)


def test_perception_launcher_rejects_a_second_airsim_owner() -> None:
    repository = Path(__file__).parents[1]
    script = repository / "scripts/live_demo_with_wm/herdr_start_mission1_airsim_live_demo.sh"
    result = _dry_run(script, ONR_DEMO_AIRSIM_RPC_URL="http://127.0.0.1:8767")
    assert result.returncode == 2
    assert "ONR_DEMO_AIRSIM_RPC_URL" in result.stderr


def test_follower_launcher_prints_fixture_engine_and_visualizer_without_running_prep() -> None:
    repository = Path(__file__).parents[1]
    script = repository / "scripts/live_demo_with_wm/herdr_start_mission1_follower_demo.sh"
    result = _dry_run(script)
    assert result.returncode == 0, result.stderr
    lines = dict(line.split(": ", 1) for line in result.stdout.splitlines() if ": " in line)
    run_root = Path(lines["Run configuration"])
    try:
        commands = {
            key: shlex.split(shlex.split(lines[key])[2].rsplit("exec ", 1)[1])
            for key in ("Physical command", "Engine command", "Visualizer command")
        }
        physical, engine, follower = (
            commands["Physical command"], commands["Engine command"], commands["Visualizer command"]
        )
        assert "Perception command" not in lines
        prepare = shlex.split(shlex.split(lines["Prepare command"])[2])
        assert "onr.demo.airsim_reconstruction.fixture" in prepare
        fixture = Path(prepare[prepare.index("--out") + 1])
        assert fixture == run_root / "airsim-fixture"
        # Dry run prints the prep step without building the fixture.
        assert not fixture.exists()
        # The engine plays the prepared lead-in scene that the follower steps.
        assert engine[engine.index("--scenario") + 1] == str(fixture / "scenarios/follower")
        assert engine[engine.index("--settings") + 1] == str(fixture / "engine/settings_airsim.json")
        engine_root = Path(engine[engine.index("--output") + 1])
        assert follower[follower.index("--engine-ready") + 1] == str(engine_root / "ready.json")
        assert follower[follower.index("--fixture") + 1] == str(fixture)
        assert follower[follower.index("--run-root") + 1] == str(run_root)
        # Simulated information feeds the world model: no producer, no scene clock.
        assert not {"--perception-url", "--experimental-scene-clock-state"} & set(physical)
        assert physical[physical.index("--scenario-config") + 1].endswith("config/harbor_world.yaml")
        # As under the Host, the closed loop waits for the follower's first frame.
        agent_prefix = shlex.split(lines["Agent command"])[2].rsplit("exec ", 1)[0]
        assert str(engine_root / "ready.json") in agent_prefix
        assert str(run_root / "airsim-follower-ready.json") in agent_prefix
    finally:
        shutil.rmtree(run_root)


@pytest.mark.parametrize("perception", ["yolo", "ideal"])
def test_follower_launcher_rejects_perception(perception: str) -> None:
    repository = Path(__file__).parents[1]
    script = repository / "scripts/live_demo_with_wm/herdr_start_mission1_follower_demo.sh"
    result = _dry_run(script, ONR_DEMO_PERCEPTION=perception)
    assert result.returncode == 2
    assert "ONR_DEMO_AIRSIM=1" in result.stderr
    assert f"ONR_DEMO_PERCEPTION={perception}" in result.stderr
    assert "Run configuration" not in result.stdout


def test_follower_launcher_accepts_explicit_perception_off_and_probes_the_engine_port() -> None:
    from onr.runtime_host.stack import herdr, load_stack_catalog

    repository = Path(__file__).parents[1]
    request = herdr.demo_env_request(
        {"ONR_DEMO_PRESET": "mission1-harbor", "ONR_DEMO_AIRSIM": "1", "ONR_DEMO_PERCEPTION": "off"},
        catalog=load_stack_catalog(),
        repo_root=repository,
    )
    assert (request.toggles.airsim, request.toggles.perception) == (True, "off")
    assert herdr.probe_ports(request) == [request.viewer_port, request.engine.rpc_port]


def test_airsim_toggle_is_rejected_where_the_preset_offers_no_airsim() -> None:
    repository = Path(__file__).parents[1]
    script = repository / "scripts/live_demo_with_wm/herdr_start_mission2_live_demo.sh"
    result = _dry_run(script, ONR_DEMO_AIRSIM="1")
    assert result.returncode == 2
    assert "mission2 does not support airsim=true" in result.stderr


def test_joint34_launcher_exposes_runtime_camera_ownership() -> None:
    repository = Path(__file__).parents[1]
    script = repository / "scripts/live_demo_with_wm/herdr_start_live_demo.sh"
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("ONR_DEMO_")
    }
    environment.update(
        ONR_DEMO_MISSION_MODE="joint34",
        ONR_DEMO_DRY_RUN="1",
        ONR_DEMO_VIEWER_PORT="5098",
        ONR_DEMO_AIRSIM_RPC_URL="http://127.0.0.1:8767",
        ONR_DEMO_CAMERA_OWNER="runtime",
        ONR_DEMO_MISSION3_FIXTURE=(
            "/data/ccu/sukaih/ONR/onr_physical_runtime/"
            "config/joint34_demo/mission3_private_fixture.json"
        ),
    )
    result = subprocess.run(
        ["bash", str(script), "05_onr"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    physical_line = next(
        line for line in result.stdout.splitlines() if line.startswith("Physical command:")
    )
    command = shlex.split(shlex.split(physical_line.split(": ", 1)[1])[2].rsplit("exec ", 1)[1])
    assert command[command.index("--airsim-rpc-url") + 1] == "http://127.0.0.1:8767"
    assert command[command.index("--camera-owner") + 1] == "runtime"
    assert command[command.index("--mission3-fixture") + 1].endswith(
        "config/joint34_demo/mission3_private_fixture.json"
    )

def test_mission1_live_demo_keeps_legacy_run_directory() -> None:
    repository = Path(__file__).parents[1]
    script = repository / "scripts/live_demo_with_wm/herdr_start_live_demo.sh"
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("ONR_DEMO_")
    }
    environment["ONR_DEMO_DRY_RUN"] = "1"
    result = subprocess.run(
        ["bash", str(script), "05_onr"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    run_root = Path(
        next(
            line.removeprefix("Run configuration: ")
            for line in result.stdout.splitlines()
            if line.startswith("Run configuration: ")
        )
    )
    assert run_root.parent == repository / "var/live_demo_with_wm"


def test_launcher_rejects_occupied_viewer_port_before_creating_run_or_workspace(
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
    launcher = tmp_path / "launcher.sh"
    launcher.write_text(
        (repository / "scripts/live_demo_with_wm/herdr_start_live_demo.sh")
        .read_text(encoding="utf-8")
        .replace("/data/ccu/sukaih/ONR/onr_agent_slim", str(agent_root))
        .replace("/data/ccu/sukaih/ONR/onr_physical_runtime", str(physical_root)),
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "herdr-calls.log"
    fake_herdr = fake_bin / "herdr"
    fake_herdr.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['HERDR_CALL_LOG'], 'a', encoding='utf-8') as log:\n"
        "    log.write(' '.join(args) + '\\n')\n"
        "if args == ['session', 'list']:\n"
        "    print('demo running')\n"
        "elif args == ['workspace', 'list']:\n"
        "    print(json.dumps({'result': {'workspaces': ["
        "{'label': 'mission3-live-demo', 'workspace_id': 'owned-workspace'}, "
        "{'label': 'mission2-live-demo', 'workspace_id': 'foreign-workspace'}]}}))\n"
        "elif args[:2] == ['workspace', 'create']:\n"
        "    print(json.dumps({'result': {'root_pane': {'pane_id': 'physical-pane'}, "
        "'workspace': {'workspace_id': 'new-workspace'}}}))\n"
        "elif args[:2] == ['pane', 'split']:\n"
        "    print(json.dumps({'result': {'pane': {'pane_id': 'agent-pane'}}}))\n"
        "else:\n"
        "    print('{}')\n",
        encoding="utf-8",
    )
    fake_herdr.chmod(0o755)

    run_parent = agent_root / "var/live_demo_with_wm/mission3"
    run_parent.mkdir(parents=True)
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("ONR_DEMO_")
    }
    environment.update(
        HERDR_CALL_LOG=str(call_log),
        ONR_DEMO_MISSION_MODE="mission3",
        ONR_DEMO_SCENARIO_CONFIG=str(scenario),
        PATH=f"{fake_bin}{os.pathsep}{environment['PATH']}",
    )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        viewer_port = listener.getsockname()[1]
        environment["ONR_DEMO_VIEWER_PORT"] = str(viewer_port)

        result = subprocess.run(
            ["bash", str(launcher), "demo"],
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

        with socket.create_connection(("127.0.0.1", viewer_port), timeout=1):
            listener.settimeout(1)
            connection, _ = listener.accept()
            connection.close()

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert f"127.0.0.1:{viewer_port}" in output
    assert "ONR_DEMO_VIEWER_PORT" in output
    assert any(phrase in output.lower() for phrase in ("in use", "occupied"))
    assert list(run_parent.iterdir()) == []
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert calls == [
        "session list",
        "workspace list",
        "workspace close owned-workspace",
    ]
    assert "workspace close foreign-workspace" not in calls
    assert not any(call.startswith(("workspace create", "pane ")) for call in calls)


def _prepare_mission3_launcher(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Copy the shared launcher into a fake agent root wired for mission3."""
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
    launcher = tmp_path / "launcher.sh"
    launcher.write_text(
        (repository / "scripts/live_demo_with_wm/herdr_start_live_demo.sh")
        .read_text(encoding="utf-8")
        .replace("/data/ccu/sukaih/ONR/onr_agent_slim", str(agent_root))
        .replace("/data/ccu/sukaih/ONR/onr_physical_runtime", str(physical_root)),
        encoding="utf-8",
    )
    run_parent = agent_root / "var/live_demo_with_wm/mission3"
    run_parent.mkdir(parents=True)
    return launcher, agent_root, run_parent, scenario


def _write_fake_herdr(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "herdr-calls.log"
    fake_herdr = fake_bin / "herdr"
    fake_herdr.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['HERDR_CALL_LOG'], 'a', encoding='utf-8') as log:\n"
        "    log.write(' '.join(args) + '\\n')\n"
        "if args == ['session', 'list']:\n"
        "    print('demo running')\n"
        "elif args == ['workspace', 'list']:\n"
        "    print(json.dumps({'result': {'workspaces': ["
        "{'label': 'mission3-live-demo', 'workspace_id': 'owned-workspace'}, "
        "{'label': 'mission2-live-demo', 'workspace_id': 'foreign-workspace'}]}}))\n"
        "elif args[:2] == ['workspace', 'create']:\n"
        "    print(json.dumps({'result': {'root_pane': {'pane_id': 'physical-pane'}, "
        "'workspace': {'workspace_id': 'new-workspace'}}}))\n"
        "elif args[:2] == ['pane', 'split']:\n"
        "    print(json.dumps({'result': {'pane': {'pane_id': 'agent-pane'}}}))\n"
        "else:\n"
        "    print('{}')\n",
        encoding="utf-8",
    )
    fake_herdr.chmod(0o755)
    return fake_bin, call_log


def _python_bin(tmp_path: Path) -> Path:
    python_bin = tmp_path / "python-bin"
    python_bin.mkdir()
    for name in ("python", "python3"):
        (python_bin / name).symlink_to(sys.executable)
    return python_bin


def test_launcher_uses_vendored_jq_when_ambient_path_lacks_jq(tmp_path: Path) -> None:
    repository = Path(__file__).parents[1]
    launcher, agent_root, run_parent, scenario = _prepare_mission3_launcher(tmp_path)
    fake_bin, call_log = _write_fake_herdr(tmp_path)
    python_bin = _python_bin(tmp_path)

    jq_log = tmp_path / "jq-calls.log"
    vendored_jq_dir = agent_root / "modules/jq"
    vendored_jq_dir.mkdir(parents=True)
    jq_wrapper = vendored_jq_dir / "jq"
    jq_wrapper.write_text(
        "#!/usr/bin/env bash\n"
        'echo "jq $*" >> "$JQ_CALL_LOG"\n'
        f'exec "{repository / "modules/jq/jq"}" "$@"\n',
        encoding="utf-8",
    )
    jq_wrapper.chmod(0o755)

    environment = {
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "HERDR_CALL_LOG": str(call_log),
        "JQ_CALL_LOG": str(jq_log),
        "ONR_DEMO_MISSION_MODE": "mission3",
        "ONR_DEMO_SCENARIO_CONFIG": str(scenario),
        "PATH": os.pathsep.join([str(fake_bin), str(python_bin), "/usr/bin", "/bin"]),
    }
    assert shutil.which("jq", path=environment["PATH"]) is None

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        viewer_port = listener.getsockname()[1]
        environment["ONR_DEMO_VIEWER_PORT"] = str(viewer_port)

        result = subprocess.run(
            ["bash", str(launcher), "demo"],
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

        with socket.create_connection(("127.0.0.1", viewer_port), timeout=1):
            listener.settimeout(1)
            connection, _ = listener.accept()
            connection.close()

    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert f"127.0.0.1:{viewer_port}" in output
    assert "ONR_DEMO_VIEWER_PORT" in output
    assert list(run_parent.iterdir()) == []
    jq_calls = jq_log.read_text(encoding="utf-8").splitlines()
    assert any("workspaces" in call for call in jq_calls)
    calls = call_log.read_text(encoding="utf-8").splitlines()
    assert calls == [
        "session list",
        "workspace list",
        "workspace close owned-workspace",
    ]


def test_launcher_preflight_reports_missing_tool_before_side_effects(
    tmp_path: Path,
) -> None:
    launcher, _agent_root, run_parent, scenario = _prepare_mission3_launcher(tmp_path)
    python_bin = _python_bin(tmp_path)
    environment = {
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "ONR_DEMO_MISSION_MODE": "mission3",
        "ONR_DEMO_SCENARIO_CONFIG": str(scenario),
        "PATH": os.pathsep.join([str(python_bin), "/usr/bin", "/bin"]),
    }

    result = subprocess.run(
        ["bash", str(launcher), "demo"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "requires 'herdr'" in result.stderr
    assert "conda" in result.stderr
    assert list(run_parent.iterdir()) == []
