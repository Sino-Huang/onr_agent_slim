"""Validated, non-authoritative ONR runtime configuration."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from onr.contracts.maneuver_control import PhysicalAction


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _duration(value: object, label: str) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{label} must be a non-negative number")
    return value


def _positive_duration(value: object, label: str) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{label} must be a positive number")
    return value


def _exact(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    if set(value) != expected:
        raise ValueError(f"{label} has unknown or missing keys")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _non_negative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class LLMConfig:
    provider: str
    base_url: str
    model: str
    api_key: str
    temperature: int | float


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    entrypoint: Path
    timeout_seconds: int | float
    validator_entrypoint: Path | None = None


@dataclass(frozen=True, slots=True)
class PlannersConfig:
    temporal: PlannerConfig
    symbolic: PlannerConfig


@dataclass(frozen=True, slots=True)
class HeartbeatsConfig:
    hyper_seconds: int | float
    maneuver_seconds: int | float
    summary_seconds: int | float = 30

    def __post_init__(self) -> None:
        _positive_duration(self.hyper_seconds, "heartbeats.hyper_seconds")
        _positive_duration(self.maneuver_seconds, "heartbeats.maneuver_seconds")
        _positive_duration(self.summary_seconds, "heartbeats.summary_seconds")

    @property
    def hyper_agent_seconds(self) -> int | float:
        return self.hyper_seconds

    @property
    def maneuver_control_seconds(self) -> int | float:
        return self.maneuver_seconds


@dataclass(frozen=True, slots=True)
class TransportConfig:
    backend: str
    root: Path


@dataclass(frozen=True, slots=True)
class StorageConfig:
    root: Path
    planner_artifacts: Path = Path("var/planner-artifacts")


@dataclass(frozen=True, slots=True)
class ServicesConfig:
    hyper_agent: str
    maneuver_control: str
    context_coordination: str
    fsm_runner: str
    planner: str


@dataclass(frozen=True, slots=True)
class OutputStructureRetryConfig:
    max_retries: int


@dataclass(frozen=True, slots=True)
class AgentConfig:
    output_structure_retry: OutputStructureRetryConfig


@dataclass(frozen=True, slots=True)
class AgentsConfig:
    hyper_agent: AgentConfig
    maneuver_control: AgentConfig


DEFAULT_AGENTS_CONFIG = AgentsConfig(
    hyper_agent=AgentConfig(OutputStructureRetryConfig(max_retries=2)),
    maneuver_control=AgentConfig(OutputStructureRetryConfig(max_retries=1)),
)


class EnvironmentUpdateOwnership(StrEnum):
    """The authority that advances environment updates."""

    COORDINATOR_DRIVEN = "coordinator_driven"
    ENVIRONMENT_DRIVEN = "environment_driven"


@dataclass(frozen=True, slots=True)
class EnvironmentProtocolVersions:
    maneuver_command: int
    maneuver_feedback: int
    environment_data: int
    perception: int


@dataclass(frozen=True, slots=True)
class EnvironmentUpdatesConfig:
    ownership: EnvironmentUpdateOwnership
    cadence_seconds: int | float


@dataclass(frozen=True, slots=True)
class EnvironmentTopicsConfig:
    command_target: str
    command: str
    feedback: str
    perception: str
    environment_data: str
    context: str


@dataclass(frozen=True, slots=True)
class FakeEnvironmentConfig:
    scenario_path: Path
    initial_position: tuple[float, float, float]
    max_velocity: int | float
    sensing_radius: int | float
    max_retries: int
    artifact_root: Path


@dataclass(frozen=True, slots=True)
class EnvironmentProfile:
    """Code-facing environment capabilities and transport protocol configuration."""

    adapter_kind: str
    protocols: EnvironmentProtocolVersions
    updates: EnvironmentUpdatesConfig
    topics: EnvironmentTopicsConfig
    supported_actions: tuple[PhysicalAction, ...]
    fake: FakeEnvironmentConfig
    source_path: Path = Path("conf/environment_params.yaml")

    @property
    def update_ownership(self) -> EnvironmentUpdateOwnership:
        return self.updates.ownership

    @property
    def update_cadence_seconds(self) -> int | float:
        return self.updates.cadence_seconds


DEFAULT_ENVIRONMENT_PROFILE = EnvironmentProfile(
    adapter_kind="fake",
    protocols=EnvironmentProtocolVersions(1, 1, 1, 1),
    updates=EnvironmentUpdatesConfig(
        EnvironmentUpdateOwnership.COORDINATOR_DRIVEN, 0.5
    ),
    topics=EnvironmentTopicsConfig(
        command_target="maneuver-adapter",
        command="maneuver",
        feedback="maneuver-feedback",
        perception="environment-perceptions",
        environment_data="environment-data",
        context="planning-evidence",
    ),
    supported_actions=tuple(PhysicalAction),
    fake=FakeEnvironmentConfig(
        scenario_path=Path(
            "data/ships_report_and_trajectory_example/ships/events_report.json"
        ),
        initial_position=(0.0, 0.0, -250.0),
        max_velocity=20,
        sensing_radius=30,
        max_retries=3,
        artifact_root=Path("var/environment"),
    ),
)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    llm: LLMConfig
    planners: PlannersConfig
    heartbeats: HeartbeatsConfig
    transport: TransportConfig
    storage: StorageConfig
    services: ServicesConfig
    debug: bool
    agent_name: str
    agents: AgentsConfig = DEFAULT_AGENTS_CONFIG
    environment_profile: EnvironmentProfile = DEFAULT_ENVIRONMENT_PROFILE


def _path(
    value: object, label: str, repo_root: Path, *, executable: bool = False
) -> Path:
    raw = Path(_text(value, label))
    result = (raw if raw.is_absolute() else repo_root / raw).resolve()
    if not result.exists() or not result.is_file():
        raise ValueError(f"{label} must name an existing file")
    if executable and not os.access(result, os.X_OK):
        raise ValueError(f"{label} must name an executable file")
    return result


def _config_path(value: object, label: str, repo_root: Path) -> Path:
    raw = Path(_text(value, label))
    return (raw if raw.is_absolute() else repo_root / raw).resolve()


def _url(value: object, label: str, *, require_v1: bool = False) -> str:
    result = _text(value, label)
    parsed = urlparse(result)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label} must be a valid HTTP(S) URL")
    if require_v1 and "v1" not in parsed.path.strip("/").split("/"):
        raise ValueError(f"{label} must include /v1 for vLLM")
    return result.rstrip("/")


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _position(value: object, label: str) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{label} must contain exactly three numbers")
    result: list[float] = []
    for item in value:
        if (
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
        ):
            raise ValueError(f"{label} must contain exactly three finite numbers")
        result.append(float(item))
    return result[0], result[1], result[2]


def load_environment_profile(
    path: Path | str, *, repo_root: Path
) -> EnvironmentProfile:
    """Load the strict environment-owned profile referenced by runtime config."""

    root = Path(repo_root).resolve()
    selected = Path(path)
    if not selected.is_absolute():
        selected = root / selected
    selected = selected.resolve()
    try:
        raw = yaml.safe_load(selected.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"environment profile cannot be read: {selected}") from exc
    top = _exact(
        raw,
        {
            "adapter_kind",
            "protocols",
            "updates",
            "topics",
            "supported_actions",
            "fake",
        },
        "environment profile",
    )
    adapter_kind = _text(top["adapter_kind"], "environment.adapter_kind")
    if adapter_kind != "fake":
        raise ValueError("environment.adapter_kind must be fake")

    protocol_values = _exact(
        top["protocols"],
        {"maneuver_command", "maneuver_feedback", "environment_data", "perception"},
        "environment.protocols",
    )
    protocols = EnvironmentProtocolVersions(
        **{
            name: _positive_integer(
                protocol_values[name], f"environment.protocols.{name}"
            )
            for name in protocol_values
        }
    )

    update_values = _exact(
        top["updates"], {"ownership", "cadence_seconds"}, "environment.updates"
    )
    try:
        ownership = EnvironmentUpdateOwnership(update_values["ownership"])
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "environment.updates.ownership must be coordinator_driven or "
            "environment_driven"
        ) from exc
    updates = EnvironmentUpdatesConfig(
        ownership,
        _positive_duration(
            update_values["cadence_seconds"],
            "environment.updates.cadence_seconds",
        ),
    )

    topic_values = _exact(
        top["topics"],
        {
            "command_target",
            "command",
            "feedback",
            "perception",
            "environment_data",
            "context",
        },
        "environment.topics",
    )
    topics = EnvironmentTopicsConfig(
        **{
            name: _text(topic_values[name], f"environment.topics.{name}")
            for name in topic_values
        }
    )

    raw_actions = top["supported_actions"]
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ValueError("environment.supported_actions must be a non-empty list")
    try:
        supported_actions = tuple(PhysicalAction(item) for item in raw_actions)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "environment.supported_actions contains an unsupported PhysicalAction"
        ) from exc
    if len(set(supported_actions)) != len(supported_actions):
        raise ValueError("environment.supported_actions must not contain duplicates")

    fake_values = _exact(
        top["fake"],
        {
            "scenario_path",
            "initial_position",
            "max_velocity",
            "sensing_radius",
            "max_retries",
            "artifact_root",
        },
        "environment.fake",
    )
    fake = FakeEnvironmentConfig(
        scenario_path=_path(
            fake_values["scenario_path"], "environment.fake.scenario_path", root
        ),
        initial_position=_position(
            fake_values["initial_position"], "environment.fake.initial_position"
        ),
        max_velocity=_positive_duration(
            fake_values["max_velocity"], "environment.fake.max_velocity"
        ),
        sensing_radius=_positive_duration(
            fake_values["sensing_radius"], "environment.fake.sensing_radius"
        ),
        max_retries=_positive_integer(
            fake_values["max_retries"], "environment.fake.max_retries"
        ),
        artifact_root=_config_path(
            fake_values["artifact_root"], "environment.fake.artifact_root", root
        ),
    )
    return EnvironmentProfile(
        adapter_kind=adapter_kind,
        protocols=protocols,
        updates=updates,
        topics=topics,
        supported_actions=supported_actions,
        fake=fake,
        source_path=selected,
    )


def load_runtime_config(path: Path | None = None, *, repo_root: Path) -> RuntimeConfig:
    """Load a complete stable config; environment variables never overlay fields."""

    root = Path(repo_root).resolve()
    selected = (
        Path(path)
        if path is not None
        else Path(os.environ.get("ONR_CONFIG_PATH", "conf/onr_agent_params.yaml"))
    )
    if not selected.is_absolute():
        selected = root / selected
    try:
        raw = yaml.safe_load(selected.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"runtime configuration cannot be read: {selected}") from exc
    top = _exact(
        raw,
        {
            "agent_name",
            "debug",
            "environment_profile",
            "llm",
            "planners",
            "heartbeats",
            "transport",
            "storage",
            "services",
            "agents",
        },
        "runtime configuration",
    )
    agent_name = _text(top["agent_name"], "agent_name")
    debug = _boolean(top["debug"], "debug")
    environment_profile_path = _config_path(
        top["environment_profile"], "environment_profile", root
    )
    environment_profile = load_environment_profile(
        environment_profile_path, repo_root=root
    )
    llm = _exact(
        top["llm"],
        {"provider", "base_url", "model", "api_key", "temperature"},
        "llm",
    )
    provider = _text(llm["provider"], "llm.provider")
    base_url = _url(llm["base_url"], "llm.base_url", require_v1=provider == "vllm")
    model = _text(llm["model"], "llm.model")
    api_key = _text(llm["api_key"], "llm.api_key")
    temperature = _duration(llm["temperature"], "llm.temperature")

    planner_values = _exact(top["planners"], {"temporal", "symbolic"}, "planners")
    planner_records: dict[str, PlannerConfig] = {}
    for name in ("temporal", "symbolic"):
        required = {"entrypoint", "timeout_seconds"}
        configured = planner_values[name]
        if (
            name == "symbolic"
            and isinstance(configured, dict)
            and "validator_entrypoint" in configured
        ):
            required.add("validator_entrypoint")
        values = _exact(
            configured,
            required,
            f"planners.{name}",
        )
        validator_entrypoint = (
            _path(
                values["validator_entrypoint"],
                "planners.symbolic.validator_entrypoint",
                root,
                executable=True,
            )
            if name == "symbolic" and "validator_entrypoint" in values
            else None
        )
        planner_records[name] = PlannerConfig(
            entrypoint=_path(
                values["entrypoint"],
                f"planners.{name}.entrypoint",
                root,
                executable=True,
            ),
            timeout_seconds=_positive_duration(
                values["timeout_seconds"],
                f"planners.{name}.timeout_seconds",
            ),
            validator_entrypoint=validator_entrypoint,
        )

    heartbeat_values = _exact(
        top["heartbeats"],
        {"hyper_seconds", "maneuver_seconds", "summary_seconds"},
        "heartbeats",
    )
    heartbeats = HeartbeatsConfig(
        _duration(heartbeat_values["hyper_seconds"], "heartbeats.hyper_seconds"),
        _duration(heartbeat_values["maneuver_seconds"], "heartbeats.maneuver_seconds"),
        _positive_duration(
            heartbeat_values["summary_seconds"], "heartbeats.summary_seconds"
        ),
    )
    transport_values = _exact(top["transport"], {"backend", "root"}, "transport")
    backend = _text(transport_values["backend"], "transport.backend")
    if backend not in {"file", "inprocess"}:
        raise ValueError("transport.backend must be file or inprocess")
    transport = TransportConfig(
        backend, _config_path(transport_values["root"], "transport.root", root)
    )
    storage_values = _exact(top["storage"], {"root", "planner_artifacts"}, "storage")
    storage = StorageConfig(
        _config_path(storage_values["root"], "storage.root", root),
        _config_path(
            storage_values["planner_artifacts"],
            "storage.planner_artifacts",
            root,
        ),
    )
    service_values = _exact(
        top["services"],
        {
            "hyper_agent",
            "maneuver_control",
            "context_coordination",
            "fsm_runner",
            "planner",
        },
        "services",
    )
    services = ServicesConfig(
        **{key: _text(service_values[key], f"services.{key}") for key in service_values}
    )
    agent_values = _exact(top["agents"], {"hyper_agent", "maneuver_control"}, "agents")
    agent_records: dict[str, AgentConfig] = {}
    for name in ("hyper_agent", "maneuver_control"):
        values = _exact(
            agent_values[name], {"output_structure_retry"}, f"agents.{name}"
        )
        retry_values = _exact(
            values["output_structure_retry"],
            {"max_retries"},
            f"agents.{name}.output_structure_retry",
        )
        agent_records[name] = AgentConfig(
            OutputStructureRetryConfig(
                _non_negative_integer(
                    retry_values["max_retries"],
                    f"agents.{name}.output_structure_retry.max_retries",
                )
            )
        )
    agents = AgentsConfig(
        hyper_agent=agent_records["hyper_agent"],
        maneuver_control=agent_records["maneuver_control"],
    )
    return RuntimeConfig(
        llm=LLMConfig(provider, base_url, model, api_key, temperature),
        planners=PlannersConfig(
            planner_records["temporal"], planner_records["symbolic"]
        ),
        heartbeats=heartbeats,
        transport=transport,
        storage=storage,
        services=services,
        debug=debug,
        agent_name=agent_name,
        agents=agents,
        environment_profile=environment_profile,
    )


load_runtime_configuration = load_runtime_config
