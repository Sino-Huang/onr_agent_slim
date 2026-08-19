from pathlib import Path
from typing import Any

import pytest
import yaml

from onr.runtime import HeartbeatsConfig, RuntimeConfig, load_runtime_config
from onr.runtime import create_runtime
from onr.adapters.file_transport import FileTransport
from onr.adapters.inprocess_transport import InProcessTransport
from onr.contracts.planning import ManeuverIntent, MissionSpec, NormalizedPlan, PlannerChoice, PlanningOutcome, TemporalManeuver
from onr.contracts.transport import Command, TransportEvent
from onr.ports.transport import Subscription


def _shipped_runtime_values() -> dict[str, Any]:
    root = Path(__file__).parents[1]
    values = yaml.safe_load(
        (root / "conf/onr_agent_params.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(values, dict)
    return values


def _write_runtime_values(path: Path, values: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")


def test_default_runtime_config_is_complete_and_repo_relative() -> None:
    root = Path(__file__).parents[1]
    config = load_runtime_config(repo_root=root)
    assert config.debug is True
    assert config.llm.provider == "vllm"
    assert config.llm.base_url == "http://127.0.0.1:11411/v1"
    assert config.llm.model == "google/gemma-4-31B-it"
    assert config.llm.api_key == "EMPTY"
    assert config.planners.temporal.entrypoint == root / "modules/MiniZincIDE-2.9.7-bundle-linux-x86_64/bin/minizinc"
    assert config.planners.symbolic.entrypoint == root / "modules/downward/fast-downward.py"
    assert config.transport.root == (root / "var/transport").resolve()
    assert config.heartbeats.summary_seconds == 30
    assert config.agents.hyper_agent.output_structure_retry.max_retries == 2
    assert config.agents.maneuver_control.output_structure_retry.max_retries == 1
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
        """debug: false\nllm:\n  provider: test\n  base_url: http://127.0.0.1:14398/v1\n  model: model\n  api_key: test-key\n  temperature: 0\nplanners:\n  temporal:\n    entrypoint: planner\n    timeout_seconds: 1\n  symbolic:\n    entrypoint: planner\n    timeout_seconds: 1\nheartbeats:\n  hyper_seconds: true\n  maneuver_seconds: 1\n  summary_seconds: 30\ntransport:\n  backend: inprocess\n  root: transport\nstorage:\n  root: storage\nservices:\n  hyper_agent: hyper\n  maneuver_control: maneuver\n  context_coordination: context\n  fsm_runner: fsm\n  planner: planner\n""",
        encoding="utf-8",
    )
    config.write_text(
        config.read_text(encoding="utf-8")
        + "agents:\n  hyper_agent:\n    output_structure_retry:\n      max_retries: 2\n  maneuver_control:\n    output_structure_retry:\n      max_retries: 1\n",
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
    config.write_text(
        valid_config.replace("debug: false", "debug: disabled"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="debug must be a boolean"):
        load_runtime_config(config, repo_root=tmp_path)

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
    assert delivery is not None
    assert isinstance(delivery.message, TransportEvent)
    assert delivery.message.event_kind == "normalized-plan"
    delivery.ack()
    reader.close()


def test_runtime_config_direct_construction_uses_shipped_agent_defaults() -> None:
    root = Path(__file__).parents[1]
    loaded = load_runtime_config(repo_root=root)
    config = RuntimeConfig(
        llm=loaded.llm,
        planners=loaded.planners,
        heartbeats=loaded.heartbeats,
        transport=loaded.transport,
        storage=loaded.storage,
        services=loaded.services,
        debug=loaded.debug,
    )
    assert config.agents.hyper_agent.output_structure_retry.max_retries == 2
    assert config.agents.maneuver_control.output_structure_retry.max_retries == 1


def test_runtime_config_requires_agents_and_every_nested_key(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    config = tmp_path / "config.yaml"

    values = _shipped_runtime_values()
    values.pop("agents")
    _write_runtime_values(config, values)
    with pytest.raises(
        ValueError, match="runtime configuration has unknown or missing keys"
    ):
        load_runtime_config(config, repo_root=root)

    values = _shipped_runtime_values()
    values["agents"].pop("maneuver_control")
    _write_runtime_values(config, values)
    with pytest.raises(ValueError, match="agents has unknown or missing keys"):
        load_runtime_config(config, repo_root=root)

    values = _shipped_runtime_values()
    values["agents"]["hyper_agent"].pop("output_structure_retry")
    _write_runtime_values(config, values)
    with pytest.raises(
        ValueError, match="agents.hyper_agent has unknown or missing keys"
    ):
        load_runtime_config(config, repo_root=root)

    values = _shipped_runtime_values()
    values["agents"]["maneuver_control"]["output_structure_retry"].pop(
        "max_retries"
    )
    _write_runtime_values(config, values)
    with pytest.raises(
        ValueError,
        match="agents.maneuver_control.output_structure_retry has unknown or missing keys",
    ):
        load_runtime_config(config, repo_root=root)


def test_runtime_config_rejects_unknown_agent_retry_keys(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    config = tmp_path / "config.yaml"
    values = _shipped_runtime_values()
    values["agents"]["hyper_agent"]["output_structure_retry"]["retryable"] = [
        "parse_error"
    ]
    _write_runtime_values(config, values)
    with pytest.raises(
        ValueError,
        match="agents.hyper_agent.output_structure_retry has unknown or missing keys",
    ):
        load_runtime_config(config, repo_root=root)


@pytest.mark.parametrize("agent_name", ["hyper_agent", "maneuver_control"])
@pytest.mark.parametrize("value", [True, 1.5, "2", -1])
def test_runtime_config_rejects_invalid_output_structure_retry_counts(
    tmp_path: Path, agent_name: str, value: object
) -> None:
    root = Path(__file__).parents[1]
    config = tmp_path / "config.yaml"
    values = _shipped_runtime_values()
    values["agents"][agent_name]["output_structure_retry"]["max_retries"] = value
    _write_runtime_values(config, values)
    with pytest.raises(
        ValueError,
        match=rf"agents.{agent_name}.output_structure_retry.max_retries must be a non-negative integer",
    ):
        load_runtime_config(config, repo_root=root)


@pytest.mark.parametrize("agent_name", ["hyper_agent", "maneuver_control"])
@pytest.mark.parametrize("value", [0, 1, 7])
def test_runtime_config_accepts_zero_and_positive_output_structure_retry_counts(
    tmp_path: Path, agent_name: str, value: int
) -> None:
    root = Path(__file__).parents[1]
    config = tmp_path / "config.yaml"
    values = _shipped_runtime_values()
    values["agents"][agent_name]["output_structure_retry"]["max_retries"] = value
    _write_runtime_values(config, values)
    loaded = load_runtime_config(config, repo_root=root)
    selected = getattr(loaded.agents, agent_name)
    assert selected.output_structure_retry.max_retries == value
