"""Compatibility layer for ``scripts/live_demo_with_wm/herdr_start_live_demo.sh``.

The launcher keeps its ``ONR_DEMO_*`` environment and herdr panes; this module
translates that environment into a :class:`StackRequest` and renders the
builder's plan into the pane commands the launcher used to compose itself.
Panes start together, so each pane command waits in shell for the readiness
probe of the service it depends on.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from onr.runtime_host.stack.builder import (
    DEMO_MISSION_ID,
    ReadinessProbe,
    StackPlan,
    StackPlanError,
    StackRequest,
)
from onr.runtime_host.stack.presets import (
    MISSION_MODES,
    PERCEPTION_MODES,
    StackCatalog,
    StackRequestError,
    StackToggles,
)

DEFAULT_VIEWER_PORT = 5066
VLLM_WAIT_SECONDS = 120
_SEPARATE_RUN_PARENT_MODES = frozenset(
    {"mission2", "mission3", "mission4", "joint24", "joint34"}
)

_PATH_OVERRIDES = {
    "ONR_DEMO_MISSION_FILE": "mission_file",
    "ONR_DEMO_SCENARIO_CONFIG": "scenario_config",
    "ONR_DEMO_MISSION1_INSTANCE": "mission1_instance",
    "ONR_DEMO_MISSION2_SCENARIO": "mission2_scenario",
    "ONR_DEMO_MISSION3_DESCRIPTION": "mission3_selection",
    "ONR_DEMO_MISSION3_FIXTURE": "mission3_fixture",
    "ONR_DEMO_MISSION4_PACKAGE": "mission4_package",
    "ONR_DEMO_MISSION4_FIXTURE": "mission4_fixture",
    "ONR_DEMO_MISSION4_ANSWERS": "mission4_answers",
    "ONR_DEMO_MISSION4_REQUESTS": "mission4_requests",
    "ONR_DEMO_ENGINE_SCENARIO": "engine_scenario",
}


def demo_env_request(
    environ: Mapping[str, str],
    *,
    catalog: StackCatalog,
    repo_root: Path,
    physical_root: Path | None = None,
) -> StackRequest:
    """Translate the launcher's ``ONR_DEMO_*`` variables into a request.

    ``ONR_DEMO_PRESET`` selects a preset (the mode wrappers set it); without
    it ``ONR_DEMO_MISSION_MODE`` selects the bare mode defaults. Every other
    variable overrides one resolved value; empty means unset, as ``${VAR:-}``.
    """

    def value(name: str) -> str | None:
        return environ.get(name) or None

    roots = catalog.resolve_roots(repo_root, physical_runtime=physical_root)
    preset_id = value("ONR_DEMO_PRESET")
    mode = value("ONR_DEMO_MISSION_MODE")
    try:
        preset = catalog.preset(preset_id) if preset_id is not None else None
    except StackRequestError as error:
        raise StackPlanError(f"ONR_DEMO_PRESET: {error}", exit_code=2) from error
    if preset is not None:
        if mode is not None and mode != preset.mission_mode:
            raise StackPlanError(
                f"ONR_DEMO_MISSION_MODE={mode} conflicts with preset {preset.preset_id}.",
                exit_code=2,
            )
        mode = preset.mission_mode
        inputs = catalog.preset_inputs(preset, roots)
    else:
        mode = mode or "mission1"
        if mode not in MISSION_MODES:
            raise StackPlanError(
                "ONR_DEMO_MISSION_MODE must be mission1, mission2, mission3, mission4, "
                "joint, joint24 or joint34.",
                exit_code=2,
            )
        inputs = catalog.mode_inputs(mode, roots)
    inputs = replace(
        inputs,
        **{
            field: Path(override)
            for name, field in _PATH_OVERRIDES.items()
            if (override := value(name)) is not None
        },
    )
    maneuver = value("ONR_DEMO_MANEUVER_SECONDS")
    if maneuver is not None:
        inputs = replace(
            inputs,
            maneuver_seconds=_positive_int("ONR_DEMO_MANEUVER_SECONDS", maneuver),
        )

    airsim_flag = value("ONR_DEMO_AIRSIM")
    if airsim_flag not in {None, "1"}:
        raise StackPlanError("ONR_DEMO_AIRSIM must be 1 or unset.", exit_code=2)
    perception = value("ONR_DEMO_PERCEPTION")
    if perception is not None and perception not in PERCEPTION_MODES:
        raise StackPlanError(
            "ONR_DEMO_PERCEPTION must be off, yolo or ideal.", exit_code=2
        )
    if airsim_flag is not None and perception not in {None, "off"}:
        raise StackPlanError(
            f"ONR_DEMO_AIRSIM=1 runs the AirSim Follower, which needs perception off; "
            f"ONR_DEMO_PERCEPTION={perception} already starts AirSim with the scene "
            "clock. Unset one of them.",
            exit_code=2,
        )
    if perception is None:
        perception = (
            "off"
            if airsim_flag is not None or preset is None
            else preset.defaults.perception
        )
    airsim = (
        airsim_flag is not None
        or perception != "off"
        or (preset is not None and preset.defaults.airsim)
    )
    limit = value("ONR_DEMO_SIMULATION_LIMIT_SECONDS")
    explicit_limit = (
        _positive_float("ONR_DEMO_SIMULATION_LIMIT_SECONDS", limit)
        if limit is not None
        else preset.explicit_simulation_limit_seconds
        if preset is not None
        else None
    )
    try:
        toggles = StackToggles(
            airsim=airsim,
            perception=perception,
            update_ownership=(
                preset.defaults.update_ownership
                if preset is not None
                else "coordinator_driven"
            ),
            **(
                {}
                if explicit_limit is None
                else {"simulation_limit_seconds": explicit_limit}
            ),
        )
    except StackRequestError as error:
        raise StackPlanError(str(error), exit_code=2) from error
    if preset is not None and not preset.supports(toggles):
        raise StackPlanError(
            f"{preset.preset_id} does not support airsim={str(airsim).lower()} "
            f"perception={perception}: "
            f"{preset.unsupported_reason or 'the preset does not offer these toggles'}",
            exit_code=2,
        )

    engine = catalog.engine_settings(roots)
    engine = replace(
        engine,
        executable=Path(value("ONR_DEMO_ENGINE_EXECUTABLE") or engine.executable),
        airsim_settings=Path(
            value("ONR_DEMO_AIRSIM_SETTINGS") or engine.airsim_settings
        ),
    )
    perception_settings = catalog.perception_settings(roots)
    perception_settings = replace(
        perception_settings,
        calibration=Path(
            value("ONR_DEMO_PERCEPTION_CALIBRATION") or perception_settings.calibration
        ),
        yolo_device=value("ONR_DEMO_YOLO_DEVICE") or perception_settings.yolo_device,
    )
    viewer_port = _positive_int(
        "ONR_DEMO_VIEWER_PORT",
        value("ONR_DEMO_VIEWER_PORT") or str(DEFAULT_VIEWER_PORT),
    )
    prior = value("ONR_DEMO_DIAGNOSTIC_PRIOR")
    planning_input = value("ONR_DEMO_MISSION1_PLANNING_INPUT")
    return StackRequest(
        mission_mode=mode,
        toggles=toggles,
        inputs=inputs,
        roots=roots,
        engine=engine,
        perception=perception_settings,
        mission_id=DEMO_MISSION_ID,
        viewer_port=viewer_port,
        preset_id=preset.preset_id if preset is not None else None,
        emit_simulation_limit=explicit_limit is not None,
        airsim_rpc_url=value("ONR_DEMO_AIRSIM_RPC_URL"),
        camera_owner=value("ONR_DEMO_CAMERA_OWNER") or "external",
        mission4_worker_timeout_seconds=(
            value("ONR_DEMO_MISSION4_WORKER_TIMEOUT_SECONDS") or "3600"
        ),
        diagnostic_prior=Path(prior) if prior is not None else None,
        mission1_planning_input=Path(planning_input)
        if planning_input is not None
        else None,
    )


def run_parent(repo_root: Path, mission_mode: str) -> Path:
    """Missions 2-4 keep separate parents so runs are not mistaken for Mission 1."""

    parent = Path(repo_root) / "var" / "live_demo_with_wm"
    return (
        parent / mission_mode if mission_mode in _SEPARATE_RUN_PARENT_MODES else parent
    )


def workspace_label(mission_mode: str) -> str:
    return f"{mission_mode}-live-demo"


def probe_ports(request: StackRequest) -> list[int]:
    """Ports that must be free before the launcher creates its workspace."""

    ports = [request.viewer_port]
    if request.toggles.airsim:
        ports.append(request.engine.rpc_port)
    if request.toggles.perception != "off":
        ports.append(request.perception.port)
    return ports


def pane_commands(plan: StackPlan) -> dict[str, str]:
    """``bash -lc`` command per herdr pane, keyed by service name plus ``agent``."""

    conda_init = plan.request.roots.conda_init
    commands: dict[str, str] = {}
    previous: ReadinessProbe | None = None
    previous_timeout = 0.0
    for service in plan.services:
        wait = ""
        if service.name in {"perception", "physical-runtime"} and previous is not None:
            wait = _wait(previous, previous_timeout)
        commands[service.name] = _pane(conda_init, service.cwd, wait, service.argv)
        if service.name in {"airsim-engine", "perception"}:
            previous, previous_timeout = service.readiness[0], service.timeout_seconds
    loop = plan.closed_loop
    closed_loop_wait = _wait(loop.wait_for, loop.wait_timeout_seconds)
    engine = plan.service("airsim-engine")
    follower = plan.service("airsim-visualizer")
    if engine is not None and follower is not None:
        # As under the Host supervisor, the closed loop starts only once the
        # engine has booted and the AirSim Follower rendered its first frame.
        closed_loop_wait = (
            _wait(engine.readiness[0], engine.timeout_seconds)
            + closed_loop_wait
            + _wait(follower.readiness[0], follower.timeout_seconds)
        )
    agent_wait = (
        closed_loop_wait
        + _wait(
            ReadinessProbe(
                "http",
                loop.vllm_models_url,
                "the configured vLLM endpoint",
                "configured vLLM endpoint was not available",
            ),
            VLLM_WAIT_SECONDS,
        )
        + "".join(f"{shlex.join(step.argv)} && " for step in plan.post_ready)
    )
    commands["agent"] = _pane(conda_init, loop.cwd, agent_wait, loop.argv)
    return commands


def prepare_command(plan: StackPlan) -> str:
    """One ``bash -lc`` command running every pre-service prep step, or ``""``."""

    if not plan.prepare:
        return ""
    conda_init = plan.request.roots.conda_init
    steps = " && ".join(shlex.join(step.argv) for step in plan.prepare)
    inner = (
        f"set -e; source {shlex.quote(str(conda_init))}; conda activate onr; "
        f"cd {shlex.quote(str(plan.prepare[0].cwd))}; {steps}"
    )
    return f"bash -lc {shlex.quote(inner)}"


def summary_lines(plan: StackPlan) -> list[str]:
    request = plan.request
    inputs = plan.inputs
    mode = request.mission_mode
    root = plan.run_root.path
    fixture = (
        str(inputs.mission3_fixture) if inputs.mission3_fixture is not None else "live"
    )
    lines = [
        f"Run data: {root}",
        f"Scenario: {inputs.scenario_config}",
        f"Mission mode: {mode}; Mission Input: {inputs.mission_file}",
    ]
    if mode in {"mission1", "joint"}:
        lines.append(f"Mission 1 instance: {inputs.mission1_instance}")
    if mode in {"mission2", "joint", "joint24"}:
        lines.append(f"Mission 2 scenario: {inputs.mission2_scenario}")
    if mode == "mission3":
        lines.append(
            f"Mission 3 description: {inputs.mission3_selection}; fixture: {fixture}"
        )
    if mode in {"mission4", "joint24", "joint34"}:
        lines.append(
            f"Mission 4 package: {inputs.mission4_package}; fixture: {inputs.mission4_fixture}; "
            f"requests: {inputs.mission4_requests}"
        )
    if mode in {"mission3", "joint34"}:
        lines.append(
            f"Mission 3 selection: {inputs.mission3_selection}; fixture: {fixture}"
        )
    if request.toggles.perception != "off":
        lines.append(
            f"Perception: {request.toggles.perception}; engine scenario: "
            f"{inputs.engine_scenario}; producer run: perception-{root.name}"
        )
    elif (engine := plan.service("airsim-engine")) is not None:
        scene = engine.argv[engine.argv.index("--scenario") + 1]
        lines.append(f"AirSim Follower: perception off; engine scenario: {scene}")
    lines += [
        f"Terminal audit: {shlex.join(plan.audit_argv)}",
        f"World-model frame stream: http://127.0.0.1:{request.viewer_port}",
    ]
    return lines


def shell_assignments(values: Mapping[str, object]) -> str:
    """``name=value`` lines safe for the launcher's ``eval``."""

    return "".join(
        f"{name}={shlex.quote(str(value))}\n" for name, value in values.items()
    )


def _pane(conda_init: Path, cwd: Path, wait: str, argv: tuple[str, ...]) -> str:
    inner = (
        f"set -e; source {shlex.quote(str(conda_init))}; conda activate onr; "
        f"cd {shlex.quote(str(cwd))}; {wait}exec {shlex.join(argv)}"
    )
    return f"bash -lc {shlex.quote(inner)}"


def _wait(probe: ReadinessProbe, timeout_seconds: float) -> str:
    seconds = int(timeout_seconds)
    if probe.kind == "file":
        check = f"[ -f {shlex.quote(probe.target)} ]"
        negated = f"[ ! -f {shlex.quote(probe.target)} ]"
    else:
        check = f"curl -fsS --max-time 2 {shlex.quote(probe.target)} >/dev/null 2>&1"
        negated = f"! {check}"
    waiting = shlex.quote(f"Waiting for {probe.waiting}...")
    failure = shlex.quote(f"{probe.failure} within {seconds} seconds.")
    return (
        f"echo {waiting}; for attempt in {{1..{seconds}}}; do {check} && break; sleep 1; "
        f"done; if {negated}; then echo {failure} >&2; exit 1; fi; "
    )


def _positive_int(name: str, text: str) -> int:
    try:
        number = int(text)
    except ValueError:
        number = 0
    if number <= 0:
        raise StackPlanError(f"{name} must be a positive integer.", exit_code=2)
    return number


def _positive_float(name: str, text: str) -> float:
    try:
        number = float(text)
    except ValueError:
        number = 0.0
    if not number > 0:
        raise StackPlanError(f"{name} must be a positive number.", exit_code=2)
    return number


__all__ = [
    "demo_env_request",
    "pane_commands",
    "prepare_command",
    "probe_ports",
    "run_parent",
    "shell_assignments",
    "summary_lines",
    "workspace_label",
]
