"""Preflight Checks for one preset and toggle selection (issue #75 §3.1).

Every external probe is injectable through :class:`PreflightProbes` so tests
never touch the network, GPUs or the real port table. ``launchable`` is false
when any check fails; ``warn`` checks never block a launch.

A failing or warning check may carry ``remediation`` (API v1.5): a copyable
read-only diagnostic command the operator can run, such as the lookup of a
port's listener. Preflight never runs it and never remediates anything itself.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import socket
import subprocess
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from onr.runtime_host.stack.builder import (
    StackPlanError,
    allocate_port,
    stack_request,
    validate_stack_request,
)
from onr.runtime_host.stack.presets import (
    StackCatalog,
    StackRequestError,
)

PASS = "pass"
WARN = "warn"
FAIL = "fail"
DISK_WARN_BYTES = 10 * 1024**3
YOLO_GPU_BYTES = 2 * 1024**3
_GIB = 1024**3


def _http_get(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # A just-stopped engine leaves TIME_WAIT sockets; only a listener blocks.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _disk_free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _gpu_query() -> str | None:
    """``nvidia-smi`` CSV ``index, memory.free, memory.total`` (MiB) or ``None``."""

    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,memory.free,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None


@dataclass(frozen=True, slots=True)
class PreflightProbes:
    http_get: Callable[[str, float], bytes] = _http_get
    port_free: Callable[[int], bool] = _port_free
    module_available: Callable[[str], bool] = _module_available
    disk_free_bytes: Callable[[Path], int] = _disk_free_bytes
    gpu_query: Callable[[], str | None] = _gpu_query
    allocate_port: Callable[[], int] = field(default=allocate_port)


def _check(
    check_id: str,
    label: str,
    status: str,
    detail: str | None,
    hint: str | None = None,
    remediation: str | None = None,
) -> dict[str, object]:
    check: dict[str, object] = {
        "check_id": check_id,
        "label": label,
        "status": status,
        "detail": detail,
        "hint": hint,
    }
    if remediation is not None:
        check["remediation"] = remediation
    return check


def run_preflight(
    catalog: StackCatalog,
    preset_id: str | None,
    stack: Mapping[str, object] | None,
    *,
    repo_root: Path,
    viewer_port: int | None = None,
    active_run: Callable[[], str | None] | None = None,
    probes: PreflightProbes | None = None,
) -> dict[str, object]:
    """``GET /api/v1/stack/preflight`` body.

    ``active_run`` returns the active Mission Run ID (or ``None``); the Host
    supplies it, the CLI has none and omits that check. An unknown preset
    raises :class:`StackRequestError`; an unsupported toggle combination is a
    failing ``toggles`` check.
    """

    probes = probes or PreflightProbes()
    preset = catalog.preset(preset_id)
    checks: list[dict[str, object]] = []
    try:
        toggles = catalog.toggles(preset, stack)
    except StackRequestError as error:
        requested = {**preset.defaults.payload(), **dict(stack or {})}
        requested.pop("simulation_limit_seconds", None)
        return {
            "schema_version": 1,
            "preset_id": preset.preset_id,
            "toggles": requested,
            "launchable": False,
            "checks": [
                _check(
                    "toggles",
                    "Toggles supported",
                    FAIL,
                    str(error),
                    preset.unsupported_reason,
                )
            ],
        }

    request = stack_request(
        catalog,
        preset.preset_id,
        toggles,
        mission_id="mission-preflight",
        repo_root=repo_root,
        viewer_port=viewer_port,
        port_allocator=probes.allocate_port,
    )
    port = request.viewer_port
    agent_config = _yaml(repo_root / "conf/onr_agent_params.yaml")
    checks.append(_vllm_check(agent_config, probes))
    checks.append(_planner_check(agent_config, repo_root))

    physical = request.roots.physical_runtime
    scenario = request.inputs.scenario_config
    if not (physical / "src/onr_physical_runtime").is_dir():
        checks.append(
            _check(
                "physical-runtime",
                "Physical runtime checkout",
                FAIL,
                f"missing: {physical}",
                "clone onr_physical_runtime next to onr_agent_slim",
            )
        )
    elif not scenario.is_file():
        checks.append(
            _check(
                "physical-runtime",
                "Physical runtime checkout",
                FAIL,
                f"scenario config missing: {scenario}",
                None,
            )
        )
    else:
        checks.append(
            _check(
                "physical-runtime",
                "Physical runtime checkout",
                PASS,
                f"{physical.name} · {scenario.name}",
            )
        )

    try:
        validate_stack_request(request)
    except StackPlanError as error:
        checks.append(
            _check("mission-inputs", "Mission inputs", FAIL, str(error), None)
        )
    else:
        checks.append(
            _check(
                "mission-inputs",
                "Mission inputs",
                PASS,
                request.inputs.mission_file.name,
            )
        )

    perception = toggles.perception
    if toggles.airsim:
        engine = request.engine
        missing = [
            str(path)
            for path, ok in (
                (
                    engine.executable,
                    engine.executable.is_file()
                    and os.access(engine.executable, os.X_OK),
                ),
                (engine.airsim_settings, engine.airsim_settings.is_file()),
            )
            if not ok
        ]
        checks.append(
            _check(
                "engine",
                "Harbor engine and AirSim settings",
                FAIL if missing else PASS,
                f"missing: {', '.join(missing)}"
                if missing
                else f"{engine.executable.name} + {engine.airsim_settings.name}",
                "install the Harbor build under onr_env/Linux" if missing else None,
            )
        )
    if perception != "off":
        settings = request.perception
        solution = request.roots.solution
        solution_ok = (
            solution / "sukai_interface"
        ).is_dir() and settings.calibration.is_file()
        checks.append(
            _check(
                "perception",
                "Perception producer (onr_solution)",
                PASS if solution_ok else FAIL,
                f"{perception} · {settings.calibration.name}"
                if solution_ok
                else f"missing sukai_interface or calibration under {solution}",
                None
                if solution_ok
                else "check out onr_solution next to onr_agent_slim",
            )
        )
    if perception == "yolo":
        weights = request.perception.yolo_weights
        ultralytics = probes.module_available("ultralytics")
        missing_parts = ([] if weights.is_file() else [f"weights {weights}"]) + (
            [] if ultralytics else ["ultralytics not importable"]
        )
        checks.append(
            _check(
                "yolo",
                "YOLO weights and ultralytics",
                FAIL if missing_parts else PASS,
                "; ".join(missing_parts)
                if missing_parts
                else f"{weights.name} · ultralytics",
                "pip install ultralytics in the onr environment"
                if not ultralytics
                else None,
            )
        )

    # (check_id, port, label, hint); the allocated viewer port keeps a stable ID
    # because the console re-runs preflight on every toggle change.
    ports: list[tuple[str, int, str, str]] = []
    if toggles.airsim:
        rpc = request.engine.rpc_port
        ports.append(
            (f"port:{rpc}", rpc, "AirSim RPC port free", "stop the other Harbor engine")
        )
    if perception != "off":
        producer = request.perception.port
        ports.append(
            (
                f"port:{producer}",
                producer,
                "Perception port free",
                "stop the other perception producer",
            )
        )
    viewer_id = f"port:{port}" if viewer_port is not None else "port:viewer"
    ports.append(
        (viewer_id, port, "World-model viewer port free", "choose another viewer port")
    )
    for check_id, number, label, hint in ports:
        free = probes.port_free(number)
        state = "free" if free else "in use"
        checks.append(
            _check(
                check_id,
                label,
                PASS if free else FAIL,
                state if check_id == f"port:{number}" else f"{number} {state}",
                None if free else hint,
                None if free else f"ss -ltnp 'sport = :{number}'",
            )
        )

    if active_run is not None:
        active = active_run()
        checks.append(
            _check(
                "active-run",
                "No other active Mission Run",
                FAIL if active else PASS,
                f"{active} is active" if active else "none active",
                "wait for it to finish or cancel it" if active else None,
            )
        )
    checks.append(_disk_check(repo_root, probes))
    checks.append(_gpu_check(probes, perception, request.perception.yolo_device))

    return {
        "schema_version": 1,
        "preset_id": preset.preset_id,
        "toggles": toggles.payload(),
        "launchable": all(check["status"] != FAIL for check in checks),
        "checks": checks,
    }


def _vllm_check(
    agent_config: Mapping[str, Any], probes: PreflightProbes
) -> dict[str, object]:
    llm = agent_config.get("llm") or {}
    base_url = str(llm.get("base_url", "")).rstrip("/")
    model = str(llm.get("model", ""))
    location = (
        base_url.removeprefix("http://").removeprefix("https://").removesuffix("/v1")
    )
    hint = "start it with scripts/vllm/start_vllm.sh"
    remediation = f"curl -sS {shlex.quote(base_url + '/models')}"
    try:
        document = json.loads(probes.http_get(f"{base_url}/models", 3.0))
    except (OSError, ValueError) as error:
        return _check(
            "vllm",
            "vLLM reachable",
            FAIL,
            f"{location}: {error}",
            hint,
            remediation,
        )
    served = [
        str(item.get("id"))
        for item in document.get("data", ())
        if isinstance(item, Mapping)
    ]
    if model not in served:
        return _check(
            "vllm",
            "vLLM reachable",
            FAIL,
            f"{model} not served at {location} (serving: {', '.join(served) or 'nothing'})",
            hint,
            remediation,
        )
    return _check("vllm", "vLLM reachable", PASS, f"{model} at {location}")


def _planner_check(
    agent_config: Mapping[str, Any], repo_root: Path
) -> dict[str, object]:
    planners = agent_config.get("planners") or {}
    temporal = planners.get("temporal") or {}
    symbolic = planners.get("symbolic") or {}
    entries = (
        ("minizinc", temporal.get("entrypoint")),
        ("fast-downward", symbolic.get("entrypoint")),
        ("VAL", symbolic.get("validator_entrypoint")),
    )
    missing = []
    for name, entrypoint in entries:
        path = repo_root / str(entrypoint) if entrypoint else None
        if path is None or not path.is_file():
            missing.append(f"{name} ({entrypoint})")
    if missing:
        return _check(
            "planners",
            "Planner executables",
            FAIL,
            f"missing: {', '.join(missing)}",
            "build the planners under modules/ (see conf/onr_agent_params.yaml)",
        )
    return _check(
        "planners", "Planner executables", PASS, " · ".join(name for name, _ in entries)
    )


def _disk_check(repo_root: Path, probes: PreflightProbes) -> dict[str, object]:
    var = repo_root / "var"
    target = var if var.exists() else repo_root
    try:
        free = probes.disk_free_bytes(target)
    except OSError as error:
        return _check("disk", "Free disk space in var/", WARN, str(error))
    detail = f"{free / _GIB:.1f} GiB free"
    if free < DISK_WARN_BYTES:
        return _check(
            "disk",
            "Free disk space in var/",
            WARN,
            detail,
            "a Mission 1 run writes about 150 MB; prune old runs under var/",
            f"du -sh {shlex.quote(str(target))}/* | sort -h",
        )
    return _check("disk", "Free disk space in var/", PASS, detail)


def _gpu_check(
    probes: PreflightProbes, perception: str, yolo_device: str
) -> dict[str, object]:
    output = probes.gpu_query()
    if output is None:
        return _check("gpu", "GPU memory", WARN, "nvidia-smi unavailable", None)
    free_by_index: dict[str, float] = {}
    for line in output.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 2:
            try:
                free_by_index[parts[0]] = float(parts[1]) * 1024**2
            except ValueError:
                continue
    if not free_by_index:
        return _check("gpu", "GPU memory", WARN, "no GPUs reported", None)
    detail = " · ".join(
        f"GPU{index} free {free / _GIB:.1f} GiB"
        for index, free in free_by_index.items()
    )
    if perception == "yolo":
        index = (
            yolo_device.removeprefix("cuda:")
            if yolo_device.startswith("cuda:")
            else None
        )
        free = free_by_index.get(index) if index is not None else None
        if free is None or free < YOLO_GPU_BYTES:
            return _check(
                "gpu",
                "GPU memory",
                WARN,
                f"GPU{index} free {(free or 0) / _GIB:.1f} GiB"
                if index is not None
                else detail,
                "YOLO needs about 2 GiB",
                "nvidia-smi",
            )
    return _check("gpu", "GPU memory", PASS, detail)


def _yaml(path: Path) -> Mapping[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return document if isinstance(document, Mapping) else {}


__all__ = ["FAIL", "PASS", "WARN", "PreflightProbes", "run_preflight"]
