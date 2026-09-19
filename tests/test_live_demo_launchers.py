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
