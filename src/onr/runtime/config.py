"""Validated, non-authoritative ONR runtime configuration."""

from __future__ import annotations

import os
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


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


def _path(value: object, label: str, repo_root: Path, *, executable: bool = False) -> Path:
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


def load_runtime_config(path: Path | None = None, *, repo_root: Path) -> RuntimeConfig:
    """Load a complete stable config; environment variables never overlay fields."""

    root = Path(repo_root).resolve()
    selected = Path(path) if path is not None else Path(os.environ.get("ONR_CONFIG_PATH", "conf/onr_agent_params.yaml"))
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
        values = _exact(
            planner_values[name],
            {"entrypoint", "timeout_seconds"},
            f"planners.{name}",
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
    transport = TransportConfig(backend, _config_path(transport_values["root"], "transport.root", root))
    storage_values = _exact(top["storage"], {"root"}, "storage")
    storage = StorageConfig(_config_path(storage_values["root"], "storage.root", root))
    service_values = _exact(top["services"], {"hyper_agent", "maneuver_control", "context_coordination", "fsm_runner", "planner"}, "services")
    services = ServicesConfig(**{key: _text(service_values[key], f"services.{key}") for key in service_values})
    agent_values = _exact(
        top["agents"], {"hyper_agent", "maneuver_control"}, "agents"
    )
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
    )


load_runtime_configuration = load_runtime_config
