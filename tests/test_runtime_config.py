from pathlib import Path

import pytest

from onr.runtime import HeartbeatsConfig, load_runtime_config
from onr.runtime import create_runtime
from onr.adapters.file_transport import FileTransport
from onr.adapters.inprocess_transport import InProcessTransport
from onr.contracts.planning import ManeuverIntent, MissionSpec, NormalizedPlan, PlannerChoice, PlanningOutcome, TemporalManeuver
from onr.contracts.transport import Command
from onr.ports.transport import Subscription


def test_default_runtime_config_is_complete_and_repo_relative() -> None:
    root = Path(__file__).parents[1]
    config = load_runtime_config(repo_root=root)
    assert config.llm.provider == "vllm"
    assert config.llm.base_url == "http://127.0.0.1:11411/v1"
    assert config.llm.model == "google/gemma-4-31B-it"
    assert config.llm.api_key == "EMPTY"
    assert config.planners.temporal.entrypoint == root / "modules/MiniZincIDE-2.9.7-bundle-linux-x86_64/bin/minizinc"
    assert config.planners.symbolic.entrypoint == root / "modules/downward/fast-downward.py"
    assert config.transport.root == (root / "var/transport").resolve()
    assert config.heartbeats.summary_seconds == 30
    assert HeartbeatsConfig(1, 2).summary_seconds == 30
    for invalid in (0, -1, True):
        with pytest.raises(
            ValueError, match="heartbeats.summary_seconds must be a positive number"
        ):
            HeartbeatsConfig(1, 2, invalid)


def test_runtime_config_rejects_unknown_keys_and_boolean_durations(tmp_path: Path) -> None:
    executable = tmp_path / "planner"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    config = tmp_path / "config.yaml"
    config.write_text(
        """llm:\n  provider: test\n  base_url: http://127.0.0.1:14398/v1\n  model: model\n  api_key: test-key\n  temperature: 0\nplanners:\n  temporal:\n    entrypoint: planner\n    timeout_seconds: 1\n  symbolic:\n    entrypoint: planner\n    timeout_seconds: 1\nheartbeats:\n  hyper_seconds: true\n  maneuver_seconds: 1\n  summary_seconds: 30\ntransport:\n  backend: inprocess\n  root: transport\nstorage:\n  root: storage\nservices:\n  hyper_agent: hyper\n  maneuver_control: maneuver\n  context_coordination: context\n  fsm_runner: fsm\n  planner: planner\n""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_runtime_config(config, repo_root=tmp_path)
    with pytest.raises(ValueError):
        create_runtime(repo_root=tmp_path, config_path=config)

    config.write_text(config.read_text(encoding="utf-8").replace("hyper_seconds: true", "hyper_seconds: 1"), encoding="utf-8")
    runtime = create_runtime(repo_root=tmp_path, config_path=config)
    assert isinstance(runtime.transport, InProcessTransport)

    valid_config = config.read_text(encoding="utf-8")
    for invalid in ("0", "-1", "true"):
        config.write_text(
            valid_config.replace("summary_seconds: 30", f"summary_seconds: {invalid}"),
            encoding="utf-8",
        )
        with pytest.raises(
            ValueError, match="heartbeats.summary_seconds must be a positive number"
        ):
            load_runtime_config(config, repo_root=tmp_path)
    config.write_text(
        valid_config.replace(
            "summary_seconds: 30", "summary_seconds: 30\n  unknown_seconds: 1"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="heartbeats has unknown or missing keys"):
        load_runtime_config(config, repo_root=tmp_path)

    config.write_text(
        valid_config.replace("timeout_seconds: 1", "timeout_seconds: 0", 1),
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError,
        match="planners.temporal.timeout_seconds must be a positive number",
    ):
        load_runtime_config(config, repo_root=tmp_path)

    config.write_text(valid_config, encoding="utf-8")
    config.write_text(valid_config.replace("base_url: http://127.0.0.1:14398/v1", "base_url: not-a-url"), encoding="utf-8")
    with pytest.raises(ValueError):
        load_runtime_config(config, repo_root=tmp_path)
    config.write_text(
        valid_config.replace("provider: test", "provider: vllm").replace(
            "base_url: http://127.0.0.1:14398/v1", "base_url: http://127.0.0.1:14398/api"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_runtime_config(config, repo_root=tmp_path)
    config.write_text(valid_config, encoding="utf-8")

    config.write_text(config.read_text(encoding="utf-8").replace("backend: inprocess", "backend: file"), encoding="utf-8")
    file_runtime = create_runtime(repo_root=tmp_path, config_path=config)
    assert isinstance(file_runtime.transport, FileTransport)

    subscriptions = (
        Subscription("planner", "mission", "plan"),
        Subscription("reader", "mission", "plans"),
    )
    runtime = create_runtime(repo_root=tmp_path, config_path=config, subscriptions=subscriptions)
    mission = MissionSpec(
        mission_id="mission",
        objective="test",
        planner_choice=PlannerChoice("temporal", "minizinc"),
        maneuvers=(TemporalManeuver("survey", ManeuverIntent("survey"), (), 1),),
        horizon=2,
        source_authority="authority",
    )
    normalized = NormalizedPlan(mission, 1, "snapshot", mission.planner_choice, PlanningOutcome.UNSOLVABLE)
    command = Command(1, "runtime-command", "correlation", "mission", "planner", "plan", {})
    outcome = runtime.run_planning_command(command, lambda _: normalized, topic="plans")
    assert outcome.status == "completed"
    reader = runtime.transport.open_consumer(subscriptions[1])
    delivery = reader.receive()
    assert delivery is not None and delivery.message.event_kind == "normalized-plan"
    delivery.ack()
    reader.close()
