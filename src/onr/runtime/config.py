"""Validated, non-authoritative ONR runtime configuration."""

from __future__ import annotations

import os
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _duration(value: object, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be a non-negative number")
    return value


def _exact(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    if set(value) != expected:
        raise ValueError(f"{label} has unknown or missing keys")
    return value


@dataclass(frozen=True, slots=True)
class LLMConfig:
    provider: str
    model: str
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
class RuntimeConfig:
    llm: LLMConfig
    planners: PlannersConfig
    heartbeats: HeartbeatsConfig
    transport: TransportConfig
    storage: StorageConfig
    services: ServicesConfig


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
    top = _exact(raw, {"llm", "planners", "heartbeats", "transport", "storage", "services"}, "runtime configuration")
    llm = _exact(top["llm"], {"provider", "model", "temperature"}, "llm")
    provider = _text(llm["provider"], "llm.provider")
    model = _text(llm["model"], "llm.model")
    temperature = _duration(llm["temperature"], "llm.temperature")

    planner_values = _exact(top["planners"], {"temporal", "symbolic"}, "planners")
    planner_records: dict[str, PlannerConfig] = {}
    for name in ("temporal", "symbolic"):
        values = _exact(planner_values[name], {"entrypoint", "timeout_seconds"}, f"planners.{name}")
        planner_records[name] = PlannerConfig(
            entrypoint=_path(values["entrypoint"], f"planners.{name}.entrypoint", root, executable=True),
            timeout_seconds=_duration(values["timeout_seconds"], f"planners.{name}.timeout_seconds"),
        )

    heartbeat_values = _exact(top["heartbeats"], {"hyper_seconds", "maneuver_seconds"}, "heartbeats")
    heartbeats = HeartbeatsConfig(
        _duration(heartbeat_values["hyper_seconds"], "heartbeats.hyper_seconds"),
        _duration(heartbeat_values["maneuver_seconds"], "heartbeats.maneuver_seconds"),
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
    return RuntimeConfig(
        LLMConfig(provider, model, temperature),
        PlannersConfig(planner_records["temporal"], planner_records["symbolic"]),
        heartbeats,
        transport,
        storage,
        services,
    )


load_runtime_configuration = load_runtime_config
