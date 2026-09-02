from pathlib import Path
from typing import Any

import pytest
import yaml

from onr.adapters.file_transport import FileTransport
from onr.adapters.inprocess_transport import InProcessTransport
from onr.runtime import (
    EnvironmentUpdateOwnership,
    HeartbeatsConfig,
    RuntimeConfig,
    create_runtime,
    load_environment_profile,
    load_runtime_config,
)


def _shipped_runtime_values() -> dict[str, Any]:
    root = Path(__file__).parents[1]
    values = yaml.safe_load(
        (root / "conf/onr_agent_params.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(values, dict)
    return values


def _write_runtime_values(path: Path, values: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")


def _write_environment_profile(tmp_path: Path) -> Path:
    scenario = tmp_path / "scenario.json"
    scenario.write_text("[]\n", encoding="utf-8")
    profile = tmp_path / "environment.yaml"
    values = yaml.safe_load(
        (Path(__file__).parents[1] / "conf/environment_params.yaml").read_text(
            encoding="utf-8"
        )
    )
    values["fake"]["scenario_path"] = str(scenario)
    values["fake"]["artifact_root"] = str(tmp_path / "environment")
    profile.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
    return profile


def test_default_runtime_config_is_complete_and_repo_relative() -> None:
    root = Path(__file__).parents[1]
    config = load_runtime_config(repo_root=root)
    assert config.agent_name == "drone-1"
    assert config.debug is True
    assert config.llm.provider == "vllm"
    assert config.llm.base_url == "http://127.0.0.1:11411/v1"
    assert config.llm.model == "Qwen/Qwen3.8-27B-FP8"
    assert config.llm.api_key == "EMPTY"
    assert (
        config.planners.temporal.entrypoint
        == root / "modules/MiniZincIDE-2.10.1-appimage/usr/bin/minizinc"
    )
    assert (
        config.planners.symbolic.entrypoint
        == root / "modules/downward/fast-downward.py"
    )
    assert config.planners.symbolic.validator_entrypoint == (
        root / "modules/VAL/build/linux64/Release/bin/Validate"
    )
    assert config.transport.root == (root / "var/transport").resolve()
    assert (
        config.storage.planner_artifacts == (root / "var/planner-artifacts").resolve()
    )
    assert config.heartbeats.hyper_seconds == 10
    assert config.heartbeats.maneuver_seconds == 5
    assert config.heartbeats.summary_seconds == 30
    assert config.agents.hyper_agent.output_structure_retry.max_retries == 2
    assert config.agents.maneuver_control.output_structure_retry.max_retries == 1
    profile = config.environment_profile
    assert profile.source_path == (root / "conf/environment_params.yaml").resolve()
    assert profile.adapter_kind == "fake"
    assert profile.protocols.maneuver_command == 1
    assert profile.protocols.maneuver_feedback == 1
    assert profile.protocols.environment_data == 1
    assert profile.protocols.perception == 1
    assert profile.update_ownership is EnvironmentUpdateOwnership.COORDINATOR_DRIVEN
    assert profile.update_cadence_seconds == 0.5
    assert {str(item) for item in profile.supported_actions} == {
        "navigate",
        "takeoff",
        "land",
        "search_area",
        "pursue",
        "investigate",
    }
    assert profile.fake is not None
    assert profile.fake.scenario_path == (
        root / "data/ships_report_and_trajectory_example/ships/events_report.json"
    ).resolve()
    assert profile.fake.initial_position == (0.0, 0.0, -250.0)
    assert profile.external is None
    assert profile.artifact_root == (root / "var/environment").resolve()
    assert HeartbeatsConfig(1, 2).summary_seconds == 30
    for invalid in (0, -1, True):
        with pytest.raises(
            ValueError, match="heartbeats.summary_seconds must be a positive number"
        ):
            HeartbeatsConfig(1, 2, invalid)
    for field in (0, -1, True):
        with pytest.raises(ValueError, match="heartbeats.hyper_seconds"):
            HeartbeatsConfig(field, 2)
        with pytest.raises(ValueError, match="heartbeats.maneuver_seconds"):
            HeartbeatsConfig(1, field)


def test_runtime_config_rejects_unknown_keys_and_boolean_durations(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "planner"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    environment_profile = _write_environment_profile(tmp_path)
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""agent_name: test-agent\ndebug: false\nenvironment_profile: {environment_profile}\nllm:\n  provider: test\n  base_url: http://127.0.0.1:14398/v1\n  model: model\n  api_key: test-key\n  temperature: 0\nplanners:\n  temporal:\n    entrypoint: planner\n    timeout_seconds: 1\n  symbolic:\n    entrypoint: planner\n    timeout_seconds: 1\nheartbeats:\n  hyper_seconds: true\n  maneuver_seconds: 1\n  summary_seconds: 30\ntransport:\n  backend: inprocess\n  root: transport\nstorage:\n  root: storage\n  planner_artifacts: planner-artifacts\nservices:\n  hyper_agent: hyper\n  maneuver_control: maneuver\n  context_coordination: context\n  fsm_runner: fsm\n  planner: planner\n""",  # noqa: E501
        encoding="utf-8",
    )
    config.write_text(
        config.read_text(encoding="utf-8")
        + "agents:\n  hyper_agent:\n    output_structure_retry:\n      max_retries: 2\n  maneuver_control:\n    output_structure_retry:\n      max_retries: 1\n",  # noqa: E501
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_runtime_config(config, repo_root=tmp_path)
    with pytest.raises(ValueError):
        create_runtime(repo_root=tmp_path, config_path=config)

    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "hyper_seconds: true", "hyper_seconds: 1"
        ),
        encoding="utf-8",
    )
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
    config.write_text(
        valid_config.replace(
            "base_url: http://127.0.0.1:14398/v1", "base_url: not-a-url"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_runtime_config(config, repo_root=tmp_path)
    config.write_text(
        valid_config.replace("provider: test", "provider: vllm").replace(
            "base_url: http://127.0.0.1:14398/v1",
            "base_url: http://127.0.0.1:14398/api",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_runtime_config(config, repo_root=tmp_path)
    config.write_text(valid_config, encoding="utf-8")

    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "backend: inprocess", "backend: file"
        ),
        encoding="utf-8",
    )
    file_runtime = create_runtime(repo_root=tmp_path, config_path=config)
    assert isinstance(file_runtime.transport, FileTransport)


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
        agent_name=loaded.agent_name,
    )
    assert config.agents.hyper_agent.output_structure_retry.max_retries == 2
    assert config.agents.maneuver_control.output_structure_retry.max_retries == 1


def test_runtime_config_requires_agent_name(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    config = tmp_path / "config.yaml"
    values = _shipped_runtime_values()
    values.pop("agent_name")
    _write_runtime_values(config, values)

    with pytest.raises(
        ValueError, match="runtime configuration has unknown or missing keys"
    ):
        load_runtime_config(config, repo_root=root)


def test_environment_profile_rejects_unknown_missing_and_invalid_values(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[1]
    shipped = yaml.safe_load(
        (root / "conf/environment_params.yaml").read_text(encoding="utf-8")
    )
    profile = tmp_path / "environment.yaml"

    cases = (
        ({**shipped, "unknown": True}, "environment profile"),
        (
            {**shipped, "updates": {"ownership": "external", "cadence_seconds": 1}},
            "ownership",
        ),
        (
            {
                **shipped,
                "protocols": {
                    **shipped["protocols"],
                    "maneuver_feedback": 0,
                },
            },
            "maneuver_feedback",
        ),
        (
            {**shipped, "supported_actions": ["navigate", "unsupported"]},
            "PhysicalAction",
        ),
    )
    for values, message in cases:
        profile.write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            load_environment_profile(profile, repo_root=root)


@pytest.mark.parametrize("value", ["", "   ", 1, True])
def test_runtime_config_rejects_invalid_agent_name(
    tmp_path: Path, value: object
) -> None:
    root = Path(__file__).parents[1]
    config = tmp_path / "config.yaml"
    values = _shipped_runtime_values()
    values["agent_name"] = value
    _write_runtime_values(config, values)

    with pytest.raises(ValueError, match="agent_name must be a non-empty string"):
        load_runtime_config(config, repo_root=root)


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
    values["agents"]["maneuver_control"]["output_structure_retry"].pop("max_retries")
    _write_runtime_values(config, values)
    with pytest.raises(
        ValueError,
        match=(
            "agents.maneuver_control.output_structure_retry has unknown or missing keys"
        ),
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
        match=(
            rf"agents.{agent_name}.output_structure_retry.max_retries must be a "
            "non-negative integer"
        ),
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
