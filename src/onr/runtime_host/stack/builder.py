"""Compose one Mission Run's Environment Stack (issue #75, decisions D2-D4).

The builder is the single source of truth for service command lines, readiness
probes, the materialized run-local configuration and the run-local fixture
copies. ``scripts/live_demo_with_wm/herdr_start_live_demo.sh`` renders the same
plan into herdr panes; the Runtime Host runs it through the Stack Supervisor.

``build_stack_plan`` is pure (it only reads inputs); ``materialize_stack_plan``
writes the run root.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from urllib.parse import quote

import yaml

from onr.runtime_host.run_root import RunRoot
from onr.runtime_host.stack.presets import (
    MISSION4_MODES,
    EngineSettings,
    MissionInputs,
    PerceptionSettings,
    StackCatalog,
    StackRoots,
    StackToggles,
    load_stack_catalog,
)

DEMO_MISSION_ID = "mission:demo"
"""The mission ID the committed ``examples/*`` fixtures embed."""

VEHICLE_ID = "drone-1"
LOCALHOST = "127.0.0.1"
CAMERA_OWNERS = ("external", "runtime")
DEFAULT_STOP_GRACE_SECONDS = 10.0
ENGINE_STOP_GRACE_SECONDS = 30.0
"""``live_engine`` stops Harbor and restores ``environment.json`` on SIGTERM."""


class StackPlanError(ValueError):
    """An unusable composition request; ``exit_code`` follows the herdr launcher."""

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True, slots=True)
class ReadinessProbe:
    """One condition a service must satisfy before the next one starts."""

    kind: str
    """``file`` (path exists) or ``http`` (GET returns 2xx)."""
    target: str
    waiting: str
    """What an operator is waiting for, e.g. ``the frozen engine``."""
    failure: str
    """Failure phrase completed by ``within <timeout> seconds``."""

    def payload(self) -> dict[str, str]:
        return {"kind": self.kind, "target": self.target}


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    name: str
    argv: tuple[str, ...]
    env: Mapping[str, str]
    """Additions to the caller's environment."""
    cwd: Path
    readiness: tuple[ReadinessProbe, ...]
    timeout_seconds: float
    required: bool = True
    completes: bool = False
    """Exit status 0 means the service finished its job (not a stack failure);
    any other exit still fails a required service."""
    port: int | None = None
    stop_grace_seconds: float = DEFAULT_STOP_GRACE_SECONDS

    def payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "env": dict(self.env),
            "cwd": str(self.cwd),
            "readiness": [probe.payload() for probe in self.readiness],
            "timeout_seconds": self.timeout_seconds,
            "required": self.required,
            "completes": self.completes,
            "port": self.port,
            "stop_grace_seconds": self.stop_grace_seconds,
        }


@dataclass(frozen=True, slots=True)
class PrepStep:
    """A one-shot command run before the services or after they are ready."""

    name: str
    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: float = 600.0

    def payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "cwd": str(self.cwd),
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class ClosedLoopSpec:
    """The Agent closed loop that runs once every service is ready."""

    agent_config: Path
    """Run-local ``onr_agent_params.yaml``; load the ``RuntimeConfig`` from it."""
    mission_file: Path
    result_path: Path
    planner_artifacts: Path
    simulation_limit_seconds: float
    argv: tuple[str, ...]
    """Equivalent ``onr.runtime.cli`` command (herdr pane, reproduction)."""
    cwd: Path
    wait_for: ReadinessProbe
    wait_timeout_seconds: float
    vllm_models_url: str

    def payload(self) -> dict[str, object]:
        return {
            "agent_config": str(self.agent_config),
            "mission_file": str(self.mission_file),
            "result_path": str(self.result_path),
            "planner_artifacts": str(self.planner_artifacts),
            "simulation_limit_seconds": self.simulation_limit_seconds,
            "argv": list(self.argv),
            "cwd": str(self.cwd),
            "wait_for": self.wait_for.payload(),
            "wait_timeout_seconds": self.wait_timeout_seconds,
            "vllm_models_url": self.vllm_models_url,
        }


@dataclass(frozen=True, slots=True)
class StackRequest:
    """Everything one composition needs, already resolved to paths."""

    mission_mode: str
    toggles: StackToggles
    inputs: MissionInputs
    roots: StackRoots
    engine: EngineSettings
    perception: PerceptionSettings
    mission_id: str
    viewer_port: int
    preset_id: str | None = None
    emit_simulation_limit: bool = True
    """Pass ``--simulation-limit-seconds`` to the CLI form of the closed loop."""
    airsim_rpc_url: str | None = None
    camera_owner: str = "external"
    mission4_worker_timeout_seconds: str = "3600"
    diagnostic_prior: Path | None = None
    mission1_planning_input: Path | None = None
    python: str = "python"
    vehicle_id: str = VEHICLE_ID


@dataclass(frozen=True, slots=True)
class FixtureCopy:
    """A run-local copy of a fixture with the run's mission ID (D2)."""

    source: Path
    path: Path

    def payload(self) -> dict[str, str]:
        return {"source": str(self.source), "path": str(self.path)}


@dataclass(frozen=True, slots=True)
class StackPlan:
    request: StackRequest
    run_root: RunRoot
    inputs: MissionInputs
    """``request.inputs`` with run-local fixture copies substituted."""
    fixtures: Mapping[str, FixtureCopy]
    services: tuple[ServiceSpec, ...]
    prepare: tuple[PrepStep, ...]
    post_ready: tuple[PrepStep, ...]
    closed_loop: ClosedLoopSpec
    audit_argv: tuple[str, ...]
    agent_config_text: str = field(repr=False)
    environment_config_text: str = field(repr=False)

    @property
    def mission_id(self) -> str:
        return self.request.mission_id

    @property
    def viewer_port(self) -> int:
        return self.request.viewer_port

    @property
    def agent_config(self) -> Path:
        return self.closed_loop.agent_config

    @property
    def ports(self) -> dict[str, int]:
        return {service.name: service.port for service in self.services if service.port}

    def service(self, name: str) -> ServiceSpec | None:
        return next(
            (service for service in self.services if service.name == name), None
        )

    def payload(self) -> dict[str, object]:
        """The ``stack.json`` document."""

        request = self.request
        return {
            "schema_version": 1,
            "preset_id": request.preset_id,
            "mission_mode": request.mission_mode,
            "mission_id": request.mission_id,
            "toggles": {
                **request.toggles.payload(),
                "simulation_limit_seconds": request.toggles.simulation_limit_seconds,
            },
            "run_root": str(self.run_root.path),
            "viewer_port": request.viewer_port,
            "ports": self.ports,
            "agent_config": str(self.run_root.agent_params),
            "environment_config": str(self.run_root.environment_profile),
            "inputs": {
                item.name: _jsonable(getattr(self.inputs, item.name))
                for item in fields(MissionInputs)
            },
            "fixtures": {name: copy.payload() for name, copy in self.fixtures.items()},
            "services": [service.payload() for service in self.services],
            "prepare": [step.payload() for step in self.prepare],
            "post_ready": [step.payload() for step in self.post_ready],
            "closed_loop": self.closed_loop.payload(),
            "audit": list(self.audit_argv),
        }


def stack_request(
    catalog: StackCatalog,
    preset_id: str | None,
    toggles: StackToggles,
    *,
    mission_id: str,
    repo_root: Path,
    viewer_port: int | None,
    python: str = "python",
    port_allocator: Callable[[], int] | None = None,
) -> StackRequest:
    """Resolve a preset into a :class:`StackRequest` (no supports check)."""

    preset = catalog.preset(preset_id)
    roots = catalog.resolve_roots(repo_root)
    engine = catalog.engine_settings(roots)
    perception = catalog.perception_settings(roots)
    if viewer_port is None:
        allocator = port_allocator if port_allocator is not None else allocate_port
        viewer_port = allocator()
        while viewer_port in {engine.rpc_port, perception.port}:
            viewer_port = allocator()
    return StackRequest(
        mission_mode=preset.mission_mode,
        toggles=toggles,
        inputs=catalog.preset_inputs(preset, roots),
        roots=roots,
        engine=engine,
        perception=perception,
        mission_id=mission_id,
        viewer_port=viewer_port,
        preset_id=preset.preset_id,
        python=python,
    )


def validate_stack_request(request: StackRequest) -> None:
    """Reject missing inputs before any side effect, as the herdr launcher did."""

    mode = request.mission_mode
    inputs = request.inputs
    if not request.mission_id:
        raise StackPlanError("A mission ID is required.", exit_code=2)
    if (
        request.toggles.airsim and request.viewer_port == request.engine.rpc_port
    ) or (
        request.toggles.perception != "off"
        and request.viewer_port == request.perception.port
    ):
        raise StackPlanError(
            "World-model viewer port collides with a configured service port.",
            exit_code=2,
        )
    if not _readable(inputs.scenario_config) or not _readable(inputs.mission_file):
        raise StackPlanError("Scenario configuration or Mission Input file is missing.")
    mission1 = mode in {"mission1", "joint"}
    if request.diagnostic_prior is not None and (
        not mission1 or not _readable(request.diagnostic_prior / "manifest.json")
    ):
        raise StackPlanError(
            "A readable diagnostic prior bundle requires Mission 1 mode (alone or joint)."
        )
    if request.mission1_planning_input is not None and (
        not mission1 or not _readable(request.mission1_planning_input)
    ):
        raise StackPlanError(
            "A readable Mission 1 planning input requires Mission 1 mode (alone or joint)."
        )
    if request.camera_owner not in CAMERA_OWNERS:
        raise StackPlanError(
            "ONR_DEMO_CAMERA_OWNER must be external or runtime.", exit_code=2
        )
    if request.camera_owner == "runtime" and not request.airsim_rpc_url:
        raise StackPlanError(
            "Runtime camera ownership requires ONR_DEMO_AIRSIM_RPC_URL.", exit_code=2
        )
    perception = request.toggles.perception
    if perception != "off":
        if request.airsim_rpc_url:
            raise StackPlanError(
                "ONR_DEMO_PERCEPTION owns the engine clock; do not also set "
                "ONR_DEMO_AIRSIM_RPC_URL.",
                exit_code=2,
            )
        engine_scenario = inputs.engine_scenario
        if (
            engine_scenario is None
            or not (engine_scenario / "ships").is_dir()
            or not os.access(request.engine.executable, os.X_OK)
            or not request.engine.executable.is_file()
            or not _readable(request.engine.airsim_settings)
            or not _readable(request.perception.calibration)
        ):
            raise StackPlanError(
                "ONR_DEMO_PERCEPTION needs ONR_DEMO_ENGINE_SCENARIO (with ships/), the engine "
                "executable, AirSim settings and calibration."
            )
    elif request.toggles.airsim:
        if request.airsim_rpc_url:
            raise StackPlanError(
                "The AirSim follower owns the engine clock; do not also set "
                "ONR_DEMO_AIRSIM_RPC_URL.",
                exit_code=2,
            )
        try:
            source = follower_source(inputs.scenario_config)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise StackPlanError(
                f"The AirSim follower cannot read the scenario's ship trajectories: {error}"
            ) from error
        if (
            not (source.vessels / "1.json").is_file()
            or not os.access(request.engine.executable, os.X_OK)
            or not request.engine.executable.is_file()
            or not _readable(request.engine.airsim_settings)
            or not _readable(static_meshes_path(request.engine))
        ):
            raise StackPlanError(
                "AirSim without perception needs the scenario's ship trajectories, the "
                "engine executable, its static mesh catalog and AirSim settings."
            )
    if mission1:
        if mode == "joint" and inputs.mission1_instance is None:
            raise StackPlanError(
                "Joint mode requires ONR_DEMO_MISSION1_INSTANCE with reports for the "
                "selected moving scenario."
            )
        if inputs.mission1_instance is None or not _readable(
            inputs.mission1_instance / "events_report.json"
        ):
            raise StackPlanError("Mission 1 report stream is missing.")
    if mode in {"mission2", "joint", "joint24"} and not _mission2_ready(inputs):
        raise StackPlanError("Mission 2 scenario is missing.")
    if mode in {"mission3", "joint34"} and not _readable(inputs.mission3_selection):
        label = "description" if mode == "mission3" else "selection"
        raise StackPlanError(f"Mission 3 {label} is missing.")
    if mode in MISSION4_MODES and not all(
        _readable(path)
        for path in (
            inputs.mission4_package,
            inputs.mission4_fixture,
            inputs.mission4_requests,
        )
    ):
        raise StackPlanError("Mission 4 package, fixture or request script is missing.")
    if (
        mode in {"mission3", "joint34"}
        and inputs.mission3_fixture is not None
        and not _readable(inputs.mission3_fixture)
    ):
        raise StackPlanError("Mission 3 fixture is missing.")


FOLLOWER_LEAD_IN_SECONDS = 30.0
"""Lead-in of the follower fixture. ``live_engine`` freezes Harbor roughly
10-20 s into its scene, so Mission time 0 (scene ``lead_in``) is still ahead of
the frozen phase and the forward-only follower never needs to rewind."""
FOLLOWER_SCENARIO = "follower"
FOLLOWER_CAPTURE_SIZE = (960, 540)
"""Follower camera size: same 16:9 frustum as the 1080p perception settings at a
quarter of the pixels, so one three-image capture stays well under a second."""


@dataclass(frozen=True, slots=True)
class FollowerSource:
    """The world model's ship trajectories the AirSim follower replays."""

    vessels: Path
    ned_offset_m: tuple[float, float, float]


def follower_source(scenario_config: Path) -> FollowerSource:
    """Ship trajectories and NED offset from a physical-runtime scenario YAML."""

    document = yaml.safe_load(Path(scenario_config).read_text(encoding="utf-8"))
    world = document["world_model"]
    runtime = document.get("runtime") or {}
    offset = tuple(float(value) for value in runtime.get("trajectory_ned_offset", (0, 0, 0)))
    if len(offset) != 3:
        raise ValueError("trajectory_ned_offset must have three values")
    vessels = (Path(scenario_config).parent / str(world["trajectory_directory"])).resolve()
    return FollowerSource(vessels, (offset[0], offset[1], offset[2]))


def static_meshes_path(engine: EngineSettings) -> Path:
    """Harbor's mesh catalog next to the engine launcher script."""

    return (
        engine.executable.parent
        / engine.executable.stem
        / "EnvironmentResourceFiles/StaticMeshes.json"
    )


def _engine_service(
    request: StackRequest,
    scenario: Path,
    engine_root: Path,
    settings: Path | None = None,
) -> ServiceSpec:
    """Harbor under the freeze shim, frozen once every scenario ship spawned."""

    return ServiceSpec(
        name="airsim-engine",
        argv=(
            request.python,
            "-u",
            "-m",
            "onr_physical_runtime.sim.experimental_freeze.live_engine",
            "--engine-executable",
            str(request.engine.executable),
            "--settings",
            str(settings or request.engine.airsim_settings),
            "--scenario",
            str(scenario),
            "--output",
            str(engine_root),
        ),
        env={},
        cwd=request.roots.physical_runtime,
        readiness=(
            ReadinessProbe(
                "file",
                str(engine_root / "ready.json"),
                "the frozen engine",
                "engine was not ready",
            ),
        ),
        timeout_seconds=600.0,
        port=request.engine.rpc_port,
        stop_grace_seconds=ENGINE_STOP_GRACE_SECONDS,
    )


def build_stack_plan(request: StackRequest, run_root: RunRoot | Path) -> StackPlan:
    """Compose the ordered services, prep steps and closed loop for one run."""

    validate_stack_request(request)
    layout = run_root if isinstance(run_root, RunRoot) else RunRoot(Path(run_root))
    root = layout.path
    mode = request.mission_mode
    roots = request.roots
    agent_root = roots.agent
    physical_root = roots.physical_runtime
    python = request.python
    perception = request.toggles.perception
    with_perception = perception != "off"
    airsim_follower = request.toggles.airsim and not with_perception
    follower_fixture = root / "airsim-fixture"
    follower_ready = root / "airsim-follower-ready.json"

    fixtures = _fixture_copies(request, root)
    inputs = replace(
        request.inputs, **{name: copy.path for name, copy in fixtures.items()}
    )

    mission4_worker_state = root / "mission4-worker-session.json"
    mission4_worker_ready = root / "mission4-worker-ready.json"
    planning_input = request.mission1_planning_input
    prepare_planning_input = mode == "mission1" and planning_input is None
    if prepare_planning_input:
        planning_input = root / "mission1-planning-input" / "environment.json"

    initial_event = (
        layout.transport
        / "identity"
        / (
            "event-"
            + quote(f"environment-update:{request.mission_id}:initial", safe="._-")
            + ".json"
        )
    )
    ready_wait_seconds = 1200.0 if with_perception else 120.0

    prepare: list[PrepStep] = []
    if request.diagnostic_prior is not None:
        prepare.append(
            PrepStep(
                name="diagnostic-prior",
                argv=(
                    python,
                    str(agent_root / "scripts/prepare_reporting_prior.py"),
                    "--agent-var",
                    str(agent_root / "var"),
                    "install",
                    "--bundle",
                    str(request.diagnostic_prior),
                    "--storage-root",
                    str(layout.agent_storage),
                    "--mission-id",
                    request.mission_id,
                ),
                cwd=agent_root,
            )
        )

    services: list[ServiceSpec] = []
    physical = [
        python,
        "-u",
        "-m",
        "onr_physical_runtime.agent.service",
        "--scenario-config",
        str(inputs.scenario_config),
        "--transport-root",
        str(layout.transport),
        "--state-root",
        str(layout.physical_state),
        "--mission-id",
        request.mission_id,
        "--vehicle-id",
        request.vehicle_id,
        *_mission_args(mode, inputs),
        "--viewer-host",
        LOCALHOST,
        "--viewer-port",
        str(request.viewer_port),
    ]
    if request.airsim_rpc_url:
        physical += [
            "--airsim-rpc-url",
            request.airsim_rpc_url,
            "--camera-owner",
            request.camera_owner,
        ]
    if with_perception:
        engine_scenario = inputs.engine_scenario
        assert engine_scenario is not None
        engine_root = layout.engine
        perception_url = f"http://{LOCALHOST}:{request.perception.port}"
        perception_run_id = f"perception-{root.name}"
        physical += [
            "--perception-url",
            perception_url,
            "--perception-run-id",
            perception_run_id,
            "--experimental-scene-clock-state",
            str(engine_root / "shim/clock.bin"),
            "--experimental-scene-times",
            str(engine_root / "status" / engine_scenario.name / "scenario_times.json"),
            "--airsim-vehicle-name",
            request.engine.airsim_vehicle,
        ]
        services.append(_engine_service(request, engine_scenario, engine_root))
        perception_argv = [
            python,
            "-u",
            "-m",
            "sukai_interface.run",
            "--host",
            LOCALHOST,
            "--port",
            str(request.perception.port),
            "--run-id",
            perception_run_id,
            "--output-dir",
            str(layout.perception),
            "--runtime-entity-map",
            str(engine_root / "actor-entities.json"),
            "--ship-config-dir",
            f"{engine_scenario}/ships",
            "--calibration",
            str(request.perception.calibration),
            "--vehicle-name",
            request.engine.airsim_vehicle,
            "--camera-name",
            request.engine.airsim_camera,
            "--perception",
            perception,
        ]
        if perception == "yolo":
            perception_argv += ["--yolo-device", request.perception.yolo_device]
        services.append(
            ServiceSpec(
                name="perception",
                argv=tuple(perception_argv),
                env={},
                cwd=roots.solution,
                readiness=(
                    ReadinessProbe(
                        "http",
                        f"{perception_url}/api/v1/health",
                        "the perception producer",
                        "perception producer was not healthy",
                    ),
                ),
                timeout_seconds=900.0,
                port=request.perception.port,
            )
        )
    elif airsim_follower:
        source = follower_source(request.inputs.scenario_config)
        prepare.append(
            PrepStep(
                name="airsim-fixture",
                argv=(
                    python,
                    "-m",
                    "onr.demo.airsim_reconstruction.fixture",
                    "--vessels",
                    str(source.vessels),
                    "--static-meshes",
                    str(static_meshes_path(request.engine)),
                    "--out",
                    str(follower_fixture),
                    "--lead-in-s",
                    _number_text(FOLLOWER_LEAD_IN_SECONDS),
                    "--trajectory-ned-offset",
                    *(_number_text(value) for value in source.ned_offset_m),
                    "--scenario-name",
                    FOLLOWER_SCENARIO,
                    "--engine-settings",
                    str(request.engine.airsim_settings),
                    "--engine-port",
                    str(request.engine.rpc_port),
                    "--capture-size",
                    *(str(value) for value in FOLLOWER_CAPTURE_SIZE),
                ),
                cwd=agent_root,
                timeout_seconds=120.0,
            )
        )
        services.append(
            _engine_service(
                request,
                follower_fixture / "scenarios" / FOLLOWER_SCENARIO,
                layout.engine,
                follower_fixture / "engine" / "settings_airsim.json",
            )
        )
    initial_probe = ReadinessProbe(
        "file",
        str(initial_event),
        "the physical runtime initial update",
        "physical runtime initial update was not available",
    )
    services.append(
        ServiceSpec(
            name="physical-runtime",
            argv=tuple(physical),
            env={},
            cwd=physical_root,
            readiness=(
                initial_probe,
                ReadinessProbe(
                    "http",
                    f"http://{LOCALHOST}:{request.viewer_port}/health",
                    "the world-model viewer",
                    "world-model viewer was not healthy",
                ),
            ),
            timeout_seconds=ready_wait_seconds,
            port=request.viewer_port,
        )
    )
    if airsim_follower:
        engine = request.engine
        services.append(
            ServiceSpec(
                name="airsim-visualizer",
                argv=(
                    python,
                    "-u",
                    "-m",
                    "onr.demo.airsim_reconstruction.follower",
                    "--run-root",
                    str(root),
                    "--engine-ready",
                    str(layout.engine / "ready.json"),
                    "--fixture",
                    str(follower_fixture),
                    "--mission-id",
                    request.mission_id,
                    "--vehicle",
                    engine.airsim_vehicle,
                    "--front-camera",
                    engine.airsim_camera,
                    "--third-person-camera",
                    engine.airsim_third_person_camera,
                    "--ready-file",
                    str(follower_ready),
                ),
                env={},
                cwd=agent_root,
                readiness=(
                    ReadinessProbe(
                        "file",
                        str(follower_ready),
                        "the first AirSim follower frame",
                        "AirSim follower rendered no frame",
                    ),
                ),
                timeout_seconds=300.0,
                # Visualization only: a mid-run follower failure is a warning,
                # never a Mission Run failure. Startup still has to succeed.
                required=False,
            )
        )
    closed_loop_wait = initial_probe
    if mode in MISSION4_MODES:
        assert inputs.mission4_requests is not None
        closed_loop_wait = ReadinessProbe(
            "file",
            str(mission4_worker_ready),
            "the initial Mission 4 worker request receipt",
            "initial Mission 4 worker request receipt was not available",
        )
        services.append(
            ServiceSpec(
                name="mission4-worker",
                argv=(
                    python,
                    "-u",
                    "-m",
                    "onr.adapters.mission4_worker",
                    "--mission-id",
                    request.mission_id,
                    "--session",
                    str(mission4_worker_state),
                    "--request-directory",
                    str(layout.physical_state / "search_requests"),
                    "--transport-root",
                    str(layout.transport),
                    "--script",
                    str(inputs.mission4_requests),
                    "--ready-file",
                    str(mission4_worker_ready),
                    "--timeout-seconds",
                    request.mission4_worker_timeout_seconds,
                ),
                env={},
                cwd=agent_root,
                readiness=(closed_loop_wait,),
                timeout_seconds=ready_wait_seconds,
                # The worker exits 0 once its script is played and the search
                # has closed; Joint 3+4 keeps running Mission 3 after that.
                completes=True,
            )
        )

    post_ready: list[PrepStep] = []
    if prepare_planning_input:
        public_input = root / "mission1-public-input"
        post_ready += [
            PrepStep(
                name="mission1-public-input",
                argv=(
                    python,
                    str(agent_root / "scripts/prepare_live_mission1_public_inputs.py"),
                    "--transport-root",
                    str(layout.transport),
                    "--mission-id",
                    request.mission_id,
                    "--output",
                    str(public_input),
                ),
                cwd=agent_root,
            ),
            PrepStep(
                name="mission1-surveillance-views",
                argv=(
                    "env",
                    f"PYTHONPATH={physical_root / 'src'}:{agent_root / 'src'}",
                    python,
                    str(physical_root / "scripts/prepare_surveillance_views.py"),
                    "--scenario",
                    str(inputs.scenario_config),
                    "--environment",
                    str(public_input / "environment.json"),
                    "--belief",
                    str(public_input / "belief.json"),
                    "--agent-var",
                    str(agent_root / "var"),
                    "--output",
                    str(root / "mission1-planning-input"),
                ),
                cwd=agent_root,
            ),
        ]

    agent_argv = [
        python,
        "-u",
        "-m",
        "onr.runtime.cli",
        "--mission-file",
        str(inputs.mission_file),
        "--repo-root",
        str(agent_root),
        "--config-path",
        str(layout.agent_params),
        "--skip-runtime-artifact-rollover",
        "--result-path",
        str(layout.closed_loop_result),
    ]
    if request.emit_simulation_limit:
        agent_argv += [
            "--simulation-limit-seconds",
            _number_text(request.toggles.simulation_limit_seconds),
        ]
    source_agent_config = (agent_root / "conf/onr_agent_params.yaml").read_text(
        encoding="utf-8"
    )
    closed_loop = ClosedLoopSpec(
        agent_config=layout.agent_params,
        mission_file=inputs.mission_file,
        result_path=layout.closed_loop_result,
        planner_artifacts=layout.planner_artifacts,
        simulation_limit_seconds=request.toggles.simulation_limit_seconds,
        argv=tuple(agent_argv),
        cwd=agent_root,
        wait_for=closed_loop_wait,
        wait_timeout_seconds=ready_wait_seconds,
        vllm_models_url=_vllm_models_url(source_agent_config),
    )

    audit = [
        python,
        "scripts/audit_live_demo.py",
        "--run-root",
        str(root),
        "--mission-mode",
        mode,
    ]
    if mode in {"mission2", "joint24"}:
        audit += ["--mission2-scenario-dir", str(inputs.mission2_scenario)]
    if mode in MISSION4_MODES:
        audit += ["--mission4-answers", str(inputs.mission4_answers)]
    if mode == "joint34":
        audit += ["--mission4-package", str(inputs.mission4_package)]
    if with_perception:
        audit += ["--perception", perception]

    agent_config_text = _rewrite(
        source_agent_config,
        "conf/onr_agent_params.yaml",
        (
            (
                r"environment_profile: .*",
                f"environment_profile: {layout.environment_profile}",
            ),
            (
                r"  maneuver_seconds: .*",
                f"  maneuver_seconds: {inputs.maneuver_seconds}",
            ),
            (r"  root: var/transport", f"  root: {layout.transport}"),
            (r"  root: var/storage", f"  root: {layout.agent_storage}"),
            (
                r"  planner_artifacts: var/planner-artifacts",
                f"  planner_artifacts: {layout.planner_artifacts}",
            ),
        ),
    )
    environment_config_text = _rewrite(
        (agent_root / "conf/environment_physical.yaml").read_text(encoding="utf-8"),
        "conf/environment_physical.yaml",
        (
            (
                r"  planning_artifact_root: var/environment",
                f"  planning_artifact_root: {layout.environment_artifacts}",
            ),
            (
                r"  mission1_planning_input_path: null",
                f"  mission1_planning_input_path: {planning_input or 'null'}",
            ),
            (r"  ownership: .*", f"  ownership: {request.toggles.update_ownership}"),
        ),
    )
    return StackPlan(
        request=request,
        run_root=layout,
        inputs=inputs,
        fixtures=fixtures,
        services=tuple(services),
        prepare=tuple(prepare),
        post_ready=tuple(post_ready),
        closed_loop=closed_loop,
        audit_argv=tuple(audit),
        agent_config_text=agent_config_text,
        environment_config_text=environment_config_text,
    )


def materialize_stack_plan(plan: StackPlan) -> StackPlan:
    """Write the run-local configs, fixture copies and ``stack.json``."""

    layout = plan.run_root
    layout.path.mkdir(parents=True, exist_ok=True)
    for directory in (
        layout.transport,
        layout.agent_storage,
        layout.planner_artifacts,
        layout.environment_artifacts,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    # The engine scene clock provisions its epoch only into a fresh runtime
    # state, and live_engine refuses an existing output directory.
    if plan.request.toggles.perception == "off":
        layout.physical_state.mkdir(parents=True, exist_ok=True)
    layout.agent_params.write_text(plan.agent_config_text, encoding="utf-8")
    layout.environment_profile.write_text(
        plan.environment_config_text, encoding="utf-8"
    )
    replacement = json.dumps(plan.mission_id)
    for copy in plan.fixtures.values():
        copy.path.parent.mkdir(parents=True, exist_ok=True)
        text = copy.source.read_text(encoding="utf-8")
        copy.path.write_text(
            text.replace(json.dumps(DEMO_MISSION_ID), replacement), encoding="utf-8"
        )
    _atomic_json(layout.stack_plan, plan.payload())
    return plan


def plan_mission_run(
    *,
    preset_id: str | None,
    stack: Mapping[str, object] | None,
    mission_id: str,
    run_root: RunRoot | Path,
    repo_root: Path,
    catalog: StackCatalog | None = None,
    viewer_port: int | None = None,
    python: str = sys.executable,
) -> StackPlan:
    """Host entry point: resolve, check supports, build and materialize a plan.

    Raises :class:`~onr.runtime_host.stack.presets.StackRequestError` for an
    unknown preset or unsupported toggles and :class:`StackPlanError` for
    missing inputs.
    """

    catalog = catalog if catalog is not None else load_stack_catalog()
    preset = catalog.preset(preset_id)
    toggles = catalog.toggles(preset, stack)
    request = stack_request(
        catalog,
        preset.preset_id,
        toggles,
        mission_id=mission_id,
        repo_root=repo_root,
        viewer_port=viewer_port,
        python=python,
    )
    return materialize_stack_plan(build_stack_plan(request, run_root))


def allocate_port(host: str = LOCALHOST) -> int:
    """Return a currently free TCP port chosen by the kernel."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


def _mission_args(mode: str, inputs: MissionInputs) -> list[str]:
    args: list[str] = []
    if mode in {"mission1", "joint"}:
        args += ["--mission1-instance-dir", str(inputs.mission1_instance)]
    if mode in {"mission2", "joint"}:
        args += [
            "--mission-mode",
            mode,
            "--mission2-scenario-dir",
            str(inputs.mission2_scenario),
        ]
    if mode == "mission3":
        args += [
            "--mission-mode",
            "mission3",
            "--mission3-selection",
            str(inputs.mission3_selection),
        ]
        if inputs.mission3_fixture is not None:
            args += ["--mission3-fixture", str(inputs.mission3_fixture)]
    if mode == "mission4":
        args += [
            "--mission-mode",
            "mission4",
            "--mission4-package",
            str(inputs.mission4_package),
            "--mission4-fixture",
            str(inputs.mission4_fixture),
        ]
    if mode == "joint24":
        args += [
            "--mission-mode",
            "joint24",
            "--mission2-scenario-dir",
            str(inputs.mission2_scenario),
            "--mission4-package",
            str(inputs.mission4_package),
            "--mission4-fixture",
            str(inputs.mission4_fixture),
        ]
    if mode == "joint34":
        args += [
            "--mission-mode",
            "joint34",
            "--mission3-selection",
            str(inputs.mission3_selection),
            "--mission4-package",
            str(inputs.mission4_package),
            "--mission4-fixture",
            str(inputs.mission4_fixture),
        ]
        if inputs.mission3_fixture is not None:
            args += ["--mission3-fixture", str(inputs.mission3_fixture)]
    return args


def _fixture_copies(request: StackRequest, root: Path) -> dict[str, FixtureCopy]:
    """Run-local copies of every input file that embeds the demo mission ID."""

    if request.mission_id == DEMO_MISSION_ID:
        return {}
    marker = json.dumps(DEMO_MISSION_ID)
    copies: dict[str, FixtureCopy] = {}
    for item in fields(MissionInputs):
        source = getattr(request.inputs, item.name)
        if not isinstance(source, Path) or not source.is_file():
            continue
        if marker in source.read_text(encoding="utf-8", errors="replace"):
            copies[item.name] = FixtureCopy(
                source=source,
                path=root / "fixtures" / f"{item.name.replace('_', '-')}-{source.name}",
            )
    return copies


def _rewrite(text: str, label: str, rules: tuple[tuple[str, str], ...]) -> str:
    """Apply whole-line rewrites; every rule must match exactly one line."""

    lines = text.splitlines(keepends=True)
    for pattern, value in rules:
        expression = re.compile(pattern)
        matches = [
            index
            for index, line in enumerate(lines)
            if expression.fullmatch(line.rstrip("\n"))
        ]
        if len(matches) != 1:
            raise ValueError(f"{label}: expected exactly one line matching {pattern!r}")
        index = matches[0]
        ending = "\n" if lines[index].endswith("\n") else ""
        lines[index] = value + ending
    return "".join(lines)


def _vllm_models_url(agent_config_text: str) -> str:
    match = re.search(r"^  base_url: (\S+)\s*$", agent_config_text, re.MULTILINE)
    if match is None:
        raise ValueError("conf/onr_agent_params.yaml: llm.base_url is missing")
    return match.group(1).rstrip("/") + "/models"


def _mission2_ready(inputs: MissionInputs) -> bool:
    return inputs.mission2_scenario is not None and _readable(
        inputs.mission2_scenario / "ships/events.json"
    )


def _readable(path: Path | None) -> bool:
    return path is not None and path.is_file() and os.access(path, os.R_OK)


def _number_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _jsonable(value: object) -> object:
    return str(value) if isinstance(value, Path) else value


def _atomic_json(path: Path, document: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


__all__ = [
    "DEMO_MISSION_ID",
    "ClosedLoopSpec",
    "FixtureCopy",
    "PrepStep",
    "ReadinessProbe",
    "ServiceSpec",
    "StackPlan",
    "StackPlanError",
    "StackRequest",
    "allocate_port",
    "build_stack_plan",
    "materialize_stack_plan",
    "plan_mission_run",
    "stack_request",
    "validate_stack_request",
]
