from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from onr.adapters.file_transport import FileTransport
from onr.contracts.hyper_agent import MissionInput
import onr.runtime.cli as runtime_cli


def _mission_file(tmp_path: Path, **overrides: object) -> Path:
    value: dict[str, object] = {
        "mission_id": "mission:demo",
        "mission_text": "Survey the demo area without exposing this input.",
        "source_authority": "demo-operator",
    }
    value.update(overrides)
    path = tmp_path / "mission.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_load_mission_file_is_exact_and_strict(tmp_path: Path) -> None:
    mission = runtime_cli.load_mission_file(_mission_file(tmp_path))
    assert mission == MissionInput(
        "mission:demo",
        "Survey the demo area without exposing this input.",
        "demo-operator",
    )

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be read as JSON"):
        runtime_cli.load_mission_file(invalid)

    with pytest.raises(ValueError, match="exactly the required fields"):
        runtime_cli.load_mission_file(_mission_file(tmp_path, unexpected="value"))
    with pytest.raises(ValueError, match="must be a non-empty string"):
        runtime_cli.load_mission_file(_mission_file(tmp_path, mission_text="  "))
    with pytest.raises(ValueError, match="must be a non-empty string"):
        runtime_cli.load_mission_file(_mission_file(tmp_path, source_authority=3))


def test_demo_environment_flag_is_explicitly_required(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        runtime_cli.main(["--mission-file", str(_mission_file(tmp_path))])
    assert exc.value.code == 2


def test_installed_cli_help_works_outside_checkout(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-m", "onr.runtime.cli", "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0
    assert "--demo-environment" in result.stdout
    assert "ModuleNotFoundError" not in result.stderr


def test_cli_composes_and_runs_offline_through_injected_seams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[object] = []
    model = object()

    class FakeRuntime:
        def __init__(self) -> None:
            self.transport = FileTransport(tmp_path / "transport")

        def verify_llm_reachability(self) -> None:
            calls.append("verify")

        def create_planners(self, root: Path) -> dict[str, object]:
            calls.append(("planners", root))
            return {"temporal": "planner"}

        def create_chat_model(self) -> object:
            calls.append("model")
            return model

        def create_hyper_agent(self, **kwargs: object) -> object:
            calls.append(("hyper", kwargs))
            return "hyper-agent"

        def create_maneuver_control(self, adapter: object, **kwargs: object) -> object:
            calls.append(("maneuver", adapter, kwargs))
            return "maneuver-control"

        def create_context_coordination(self, **kwargs: object) -> object:
            calls.append(("context", kwargs))
            return "context-coordination"

        def create_fsm_runner(self, **kwargs: object) -> object:
            calls.append(("fsm", kwargs))
            return "fsm-runner"

        def run_mission(self, mission: MissionInput, **kwargs: object) -> object:
            calls.append(("run", mission, kwargs))
            assert kwargs["environment_step"]() == "demo-evidence"  # type: ignore[operator]
            return SimpleNamespace(
                authority=SimpleNamespace(mission_id=mission.mission_id),
                plan=SimpleNamespace(plan_revision=3),
                command=SimpleNamespace(
                    command_id="command-demo", maneuver_id="maneuver-demo"
                ),
                final_status=SimpleNamespace(active_state="state-1", status="active"),
            )

    class FakeEnvironment:
        def run_once(self) -> str:
            calls.append("environment")
            return "demo-evidence"

    runtime = FakeRuntime()
    monkeypatch.setattr(
        runtime_cli,
        "_create_runtime",
        lambda **kwargs: calls.append(("runtime", kwargs)) or runtime,
    )
    monkeypatch.setattr(
        runtime_cli,
        "_create_demo_environment",
        lambda selected, mission_id: calls.append(
            ("demo-environment", selected, mission_id)
        )
        or FakeEnvironment(),
    )
    planner_root = tmp_path / "planner-artifacts"
    result = runtime_cli.main(
        [
            "--mission-file",
            str(_mission_file(tmp_path)),
            "--repo-root",
            str(tmp_path),
            "--config-path",
            "runtime.yaml",
            "--planner-artifacts",
            str(planner_root),
            "--demo-environment",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0 and captured.err == ""
    assert json.loads(captured.out) == {
        "mission_id": "mission:demo",
        "plan_revision": 3,
        "command_id": "command-demo",
        "maneuver_id": "maneuver-demo",
        "final_state": "state-1",
        "final_status": "active",
    }
    assert calls[0] == (
        "runtime",
        {"repo_root": tmp_path, "config_path": Path("runtime.yaml")},
    )
    assert ("planners", planner_root) in calls
    run_call = next(item for item in calls if isinstance(item, tuple) and item[0] == "run")
    assert run_call[2]["model"] is model
    assert run_call[2]["hyper_agent"] == "hyper-agent"
    assert run_call[2]["maneuver_control"] == "maneuver-control"
    assert "environment" in calls


def test_cli_failure_is_nonzero_actionable_and_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mission_path = _mission_file(tmp_path)

    def fail_runtime(**kwargs: object) -> object:
        _ = kwargs
        raise RuntimeError("Survey the demo area without exposing this input. api_key=secret")

    monkeypatch.setattr(runtime_cli, "_create_runtime", fail_runtime)
    result = runtime_cli.main(
        ["--mission-file", str(mission_path), "--demo-environment"]
    )

    captured = capsys.readouterr()
    assert result == 1 and captured.out == ""
    assert "runtime configuration" in captured.err
    assert "RuntimeError" in captured.err
    assert "Survey the demo area" not in captured.err
    assert "api_key" not in captured.err and "secret" not in captured.err
