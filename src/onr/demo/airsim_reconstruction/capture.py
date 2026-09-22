"""Archival AirSim capture with sailing beats and deferred local writes."""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .beats import POLL_BAND_S, advance_to_phase, beat_landing_passed
from .certify import (
    DEFAULT_FIXTURE,
    ENGINE_CONFIG,
    ENGINE_EXECUTABLE,
    ENGINE_OBJECT_IDS,
    INSTANCE_OBJECT_IDS,
    SCENARIO_NAME,
    VEHICLE_NAME,
    EngineConfigSwap,
    _load_ships,
    _pose_record,
    _read_ship_phases,
    _temporary_environment,
    _validate_fixture,
    _wait_for_active_scenario,
    _wait_for_rpc,
    _yaw_degrees,
    launch_engine,
    mapped_dynamic_object_ids,
    parity_errors,
    parse_static_instance_ids,
    pause_patiently,
    pose_within_tolerance,
    preflight_ports_free,
    reconcile_cleanup_failure,
    resolve_lead_in_s,
    sha256_path,
    wait_for_ship_spawns,
)
from .overlays import decode_instance_ids

DEFAULT_FRAME_METADATA = Path(
    "var/demo-video/mission1-20260916-prior-guided/frame-metadata.json"
)
DEFAULT_OBSERVATIONS_DIR = Path(
    "var/live_demo_with_wm/run.LYXubI/physical-state/observations"
)
BYTES_PER_IMAGE_ESTIMATE = 4 * 1024 * 1024
STAGING_MIN_FREE_BYTES = 200 * 1024 * 1024
IMAGE_LABELS = ("front-rgb", "front-seg", "third-rgb")
FLUSH_SECONDS = 0.1
# Configured mount coordinates from the engine settings file — NOT measured
# AirSim response camera poses (per-frame response poses were not retained;
# certification measured front ≈ [0,−0.4,−0.54], third ≈ [0,8.0,−6.34]).
# The verified aircraft pose is the world-pose authority.
CAMERA_EXTRINSICS = {
    "front-rgb": {
        "frame": "vehicle-relative",
        "translation_m": [0.4, 0.0, -0.2],
        "rpy_degrees": [0.0, -34.0, 0.0],
    },
    "front-seg": {
        "frame": "vehicle-relative",
        "translation_m": [0.4, 0.0, -0.2],
        "rpy_degrees": [0.0, -34.0, 0.0],
    },
    "third-rgb": {
        "frame": "vehicle-relative",
        "translation_m": [-8.0, 0.0, -6.0],
        "rpy_degrees": [0.0, -35.0, 0.0],
    },
}


@dataclass(frozen=True, slots=True)
class DronePose:
    tick: int
    mission_time_s: float
    ned_m: tuple[float, float, float]
    yaw_degrees: float


@dataclass(slots=True)
class _ImageTask:
    tick: int
    raw: bytes
    width: int
    height: int
    record: dict[str, Any]


class UnknownInstanceIds(RuntimeError):
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record
        super().__init__(f"unknown segmentation IDs: {record['unknown_ids']}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()  # noqa: UP017


def load_drone_trajectory(
    frame_metadata_path: str | Path, observations_dir: str | Path
) -> list[DronePose]:
    rows = json.loads(Path(frame_metadata_path).read_text())
    if not isinstance(rows, list) or not rows:
        raise ValueError("frame metadata must be a non-empty list")
    headings: dict[int, float] = {}
    for path in sorted(Path(observations_dir).glob("*-observation.json")):
        observation = json.loads(path.read_text())
        if observation.get("observation_kind") != "state":
            continue
        tick = round(float(observation["mission_time_s"]) / 0.5)
        if tick in headings:
            raise ValueError(f"duplicate state observation for tick {tick}")
        headings[tick] = float(
            observation["controlled_vehicle"]["heading_degrees"]
        )
    poses = []
    for expected_tick, row in enumerate(rows):
        tick = int(row["tick"])
        mission_time_s = float(row["mission_time"])
        if tick != expected_tick or not math.isclose(
            mission_time_s, tick * 0.5, abs_tol=1e-9
        ):
            raise ValueError("frame metadata ticks must be contiguous at 0.5 s")
        ned = tuple(float(value) for value in row["position_ned"])
        if len(ned) != 3:
            raise ValueError(f"tick {tick} position_ned must have three values")
        if tick == 0:
            # Tick zero has no state observation; the reconstructed metadata
            # row carries the canonical post-snap initial pose.
            ned, yaw = ned, 0.0
        else:
            try:
                yaw = headings[tick]
            except KeyError as exc:
                raise ValueError(f"missing state observation for tick {tick}") from exc
        poses.append(DronePose(tick, mission_time_s, ned, yaw % 360.0))
    return poses


def disk_preflight(
    output_dir: str | Path,
    tick_count: int,
    *,
    disk_usage: Callable[[str | os.PathLike[str]], Any] = shutil.disk_usage,
) -> dict[str, int]:
    estimated = int(tick_count) * 3 * BYTES_PER_IMAGE_ESTIMATE
    required = 2 * estimated
    free = int(disk_usage(output_dir).free)
    if free < required:
        raise RuntimeError(
            f"insufficient capture disk space: free={free}, required={required}"
        )
    return {
        "estimated_bytes": estimated,
        "required_free_bytes": required,
        "free_bytes": free,
    }


def choose_staging_dir(
    *,
    pid: int | None = None,
    disk_usage: Callable[[str | os.PathLike[str]], Any] = shutil.disk_usage,
) -> Path:
    """Prefer shared memory with >=200 MB free, otherwise use local temp."""
    process_id = os.getpid() if pid is None else int(pid)
    shared = Path("/dev/shm")
    if shared.is_dir() and int(disk_usage(shared).free) >= STAGING_MIN_FREE_BYTES:
        path = shared / f"airsim-capture-{process_id}"
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(tempfile.mkdtemp(prefix=f"airsim-capture-{process_id}-"))


def write_manifest_atomic(
    path: str | Path,
    manifest: dict[str, Any],
    *,
    replace: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace,
) -> None:
    destination = Path(path)
    temporary = destination.with_name(f".{destination.name}.tmp")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with temporary.open("w") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    replace(temporary, destination)


def sync_to_nfs(staging_dir: str | Path, output_dir: str | Path) -> None:
    """Copy completed local artifacts to the final output directory."""
    source_root, destination_root = Path(staging_dir), Path(output_dir)
    destination_root.mkdir(parents=True, exist_ok=True)
    for source in source_root.iterdir():
        if source.name.startswith("."):
            continue
        destination = destination_root / source.name
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        elif source.name == "capture-manifest.json":
            temporary = destination.with_name(f".{destination.name}.sync.tmp")
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
        else:
            shutil.copy2(source, destination)


class DeferredCaptureWriter:
    """Encode/hash/write images off the engine timing thread."""

    def __init__(
        self,
        staging_dir: str | Path,
        manifest: dict[str, Any],
        *,
        maxsize: int = 30,
    ) -> None:
        self.staging_dir = Path(staging_dir)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = manifest
        self.manifest_path = self.staging_dir / "capture-manifest.json"
        self.queue: queue.Queue[_ImageTask | None] = queue.Queue(maxsize=maxsize)
        self._remaining: dict[int, int] = {}
        self._error: BaseException | None = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def register_tick(self, tick: int, record: dict[str, Any]) -> None:
        with self._lock:
            self.manifest["ticks"][str(int(tick))] = record

    def write_manifest(self) -> None:
        with self._lock:
            write_manifest_atomic(self.manifest_path, self.manifest)

    def enqueue_tick(
        self,
        tick: int,
        tasks: Sequence[tuple[bytes, int, int, dict[str, Any]]],
    ) -> None:
        self.raise_if_failed()
        self._remaining[int(tick)] = len(tasks)
        for raw, width, height, record in tasks:
            self.queue.put(_ImageTask(tick, raw, width, height, record))

    def _write_png(self, task: _ImageTask) -> None:
        from PIL import Image

        raw = np.frombuffer(task.raw, dtype=np.uint8)
        pixels = task.width * task.height
        channels = raw.size // pixels
        rgb = raw.reshape(task.height, task.width, channels)[:, :, :3]
        if task.record["kind"].endswith("-rgb"):
            # AirSim Scene buffers are BGR(A); swap to true RGB. Segmentation
            # PNGs intentionally keep raw buffer order: the instance-ID decode
            # convention (byte0 << 16 | byte1 << 8 | byte2) is defined on it.
            rgb = rgb[..., ::-1]
        path = self.staging_dir / task.record["path"]
        Image.fromarray(rgb, mode="RGB").save(path)
        task.record["sha256"] = sha256_path(path)

    def _run(self) -> None:
        while True:
            task = self.queue.get()
            try:
                if task is None:
                    return
                self._write_png(task)
                with self._lock:
                    self._remaining[task.tick] -= 1
                    if self._remaining[task.tick] == 0:
                        write_manifest_atomic(self.manifest_path, self.manifest)
            except Exception as exc:  # noqa: BLE001 - report worker failure
                self._error = exc
            finally:
                self.queue.task_done()

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise RuntimeError("deferred capture writer failed") from self._error

    def flush(self) -> None:
        self.queue.join()
        self.raise_if_failed()

    def close(self) -> None:
        self.flush()
        self.queue.put(None)
        self._thread.join()
        self.raise_if_failed()


def resume_tick_complete(
    tick: int, manifest: dict[str, Any], output_dir: str | Path
) -> bool:
    record = manifest.get("ticks", {}).get(str(int(tick)))
    if not isinstance(record, dict):
        return False
    images = record.get("images")
    if not isinstance(images, list) or len(images) != 3:
        return False
    root = Path(output_dir)
    return all(
        (root / image["path"]).is_file()
        and sha256_path(root / image["path"]) == image.get("sha256")
        for image in images
    )


def pending_capture_ticks(
    ticks: Sequence[int],
    manifest: dict[str, Any],
    output_dir: str | Path,
    *,
    resume: bool,
    recapture_ticks: Iterable[int] = (),
) -> list[int]:
    forced = {int(tick) for tick in recapture_ticks}
    return [
        int(tick)
        for tick in ticks
        if int(tick) in forced
        or not (resume and resume_tick_complete(tick, manifest, output_dir))
    ]


def capture_schedule(
    ticks: Sequence[int], pending_ticks: Sequence[int]
) -> list[tuple[int, bool]]:
    pending = {int(tick) for tick in pending_ticks}
    return [(int(tick), int(tick) in pending) for tick in ticks]


def _finite_minimum(values: Sequence[float]) -> float | None:
    """Return the minimum as a strict-JSON number, or ``None`` if non-finite."""

    numbers = tuple(float(value) for value in values)
    if any(not math.isfinite(value) for value in numbers):
        return None
    return min(numbers)


def _capture_landing_target(target_phase_s: float, should_capture: bool) -> float:
    """Aim capture beats early so the render flush lands on canonical time."""

    return float(target_phase_s) - FLUSH_SECONDS if should_capture else float(target_phase_s)


def _phase_errors(
    phases: Mapping[str, float], target_phase_s: float
) -> dict[str, float]:
    """Return all-ship phase errors against one absolute target."""

    return {
        str(ship_id): float(phase) - float(target_phase_s)
        for ship_id, phase in phases.items()
    }


def _enforce_landing_gate(
    tick: int,
    landing: Mapping[str, Any],
    *,
    resume: bool,
    capture_required: bool,
    manifest: dict[str, Any],
    output_dir: str | Path,
    gate_name: str = "pre-flush",
) -> bool:
    """Keep hard landing gates for new work, relaxing only complete replay ticks."""

    if beat_landing_passed(landing["landing_errors_s"]):
        return True
    if (
        resume
        and not capture_required
        and resume_tick_complete(tick, manifest, output_dir)
    ):
        print(
            f"Skipping {gate_name} landing failure for already-complete replay "
            f"tick {tick}: "
            f"{landing['landing_errors_s']}",
            flush=True,
        )
        return False
    raise RuntimeError(f"{gate_name} beat landing failed at capture tick {tick}")


def load_recapture_ticks(path: str | Path) -> set[int]:
    """Load a strict JSON list of non-negative tick integers."""

    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise TypeError("recapture ticks file must contain a JSON list")
    ticks: set[int] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError("recapture ticks must be non-negative integers")
        ticks.add(item)
    return ticks


def check_process_health(process: Any, client: Any, tick: int) -> None:
    if process.poll() is not None:
        raise RuntimeError(f"engine exited before capture tick {tick}")
    try:
        client.getServerVersion()
    except Exception as exc:
        raise RuntimeError(f"engine RPC unhealthy at capture tick {tick}") from exc


class BeatPhaseReader:
    """Use 20 ships for steps, three synchronized ships inside poll band."""

    def __init__(
        self,
        client: Any,
        ships: Sequence[Mapping[str, Any]],
        subset: Sequence[Mapping[str, Any]],
        centers: Mapping[str, float],
        *,
        read_fn: Callable[..., dict[str, float]] = _read_ship_phases,
    ) -> None:
        self.client = client
        self.ships = ships
        self.subset = subset
        self.centers = dict(centers)
        self.read_fn = read_fn
        self.target_phase_s = 0.0
        self.last_min_phase_s = min(self.centers.values())

    def set_target(self, target_phase_s: float) -> None:
        self.target_phase_s = float(target_phase_s)

    def __call__(self) -> dict[str, float]:
        use_subset = (
            self.target_phase_s - self.last_min_phase_s <= POLL_BAND_S
        )
        selected = self.subset if use_subset else self.ships
        phases = self.read_fn(
            self.client,
            selected,
            centers_s=self.centers,
            window_s=12.0,
        )
        self.centers.update(phases)
        self.last_min_phase_s = min(phases.values())
        return phases

    def read_full(self) -> dict[str, float]:
        phases = self.read_fn(
            self.client,
            self.ships,
            centers_s=self.centers,
            window_s=12.0,
        )
        self.centers.update(phases)
        self.last_min_phase_s = min(phases.values())
        return phases


def _visible_ship_ids(path: Path) -> dict[int, list[int]]:
    return {
        int(row["tick"]): [int(value) for value in row.get("visible_ship_ids", [])]
        for row in json.loads(path.read_text())
    }


def _reset_drone_kinematics(client: Any, pose: DronePose) -> None:
    """Command the pose while clearing accumulated SimpleFlight motion."""

    import airsim

    client.enableApiControl(True, vehicle_name=VEHICLE_NAME)
    # airsim.KinematicsState is a msgpack type: no-arg constructor only.
    state = airsim.KinematicsState()
    state.position = airsim.Vector3r(*pose.ned_m)
    state.orientation = airsim.to_quaternion(
        0.0, 0.0, math.radians(pose.yaw_degrees)
    )
    state.linear_velocity = airsim.Vector3r(0.0, 0.0, 0.0)
    state.angular_velocity = airsim.Vector3r(0.0, 0.0, 0.0)
    state.linear_acceleration = airsim.Vector3r(0.0, 0.0, 0.0)
    state.angular_acceleration = airsim.Vector3r(0.0, 0.0, 0.0)
    client.simSetKinematics(state, ignore_collision=True)


def _verify_drone_pose(client: Any, commanded: DronePose) -> dict[str, Any]:
    state = client.getMultirotorState(vehicle_name=VEHICLE_NAME)
    kinematics = state.kinematics_estimated
    measured_ned = (
        float(kinematics.position.x_val),
        float(kinematics.position.y_val),
        float(kinematics.position.z_val),
    )
    measured_yaw = _yaw_degrees(kinematics.orientation)
    position_error, heading_error = parity_errors(
        measured_ned, measured_yaw, commanded.ned_m, commanded.yaw_degrees
    )
    return {
        "commanded": {
            "ned_m": list(commanded.ned_m),
            "yaw_degrees": commanded.yaw_degrees,
        },
        "measured": _pose_record(kinematics.position, kinematics.orientation),
        "sensor_timestamp_ns": int(state.timestamp),
        "max_abs_position_error_m": position_error,
        "heading_error_deg": heading_error,
        "passed": pose_within_tolerance(position_error, heading_error),
    }


def _prepare_capture_frame(
    client: Any,
    freeze: Any,
    phase_reader: BeatPhaseReader,
    pose: DronePose,
) -> dict[str, float]:
    """Reset the aircraft and flush one bounded post-command render step."""

    # Position-only teleport retains ~4.6 m/s accumulated fall velocity after each
    # 0.5 s advance. Resetting full kinematics keeps the 0.1 s flush drift at the
    # probe-bounded ~0.04 m while still rendering a fresh post-command frame.
    _reset_drone_kinematics(client, pose)
    freeze.step(FLUSH_SECONDS)
    return phase_reader.read_full()


def _response_rgb(response: Any) -> np.ndarray:
    width, height = int(response.width), int(response.height)
    raw = np.frombuffer(bytes(response.image_data_uint8), dtype=np.uint8)
    pixels = width * height
    channels = raw.size // pixels
    return raw.reshape(height, width, channels)[:, :, :3]


def capture_image_set(
    client: Any,
    airsim: Any,
    writer: DeferredCaptureWriter,
    tick_record: dict[str, Any],
    valid_ids: set[int],
    ship_ids: set[int],
    passenger_ids: set[int],
    visible_ship_ids: list[int],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Copy image bytes, validate segmentation, and enqueue deferred writes."""
    requests = [
        airsim.ImageRequest("front_center_custom", airsim.ImageType.Scene, False, False),
        airsim.ImageRequest(
            "front_center_custom", airsim.ImageType.Segmentation, False, False
        ),
        airsim.ImageRequest("third_person_demo", airsim.ImageType.Scene, False, False),
    ]
    responses = None
    attempts = 0
    for attempts in range(1, 4):
        try:
            responses = client.simGetImages(requests, vehicle_name=VEHICLE_NAME)
        except Exception:
            if attempts >= 3:
                raise
            sleep(2.0)
        else:
            break
    assert responses is not None and len(responses) == 3
    segmentation = decode_instance_ids(_response_rgb(responses[1]))
    decoded, counts = np.unique(segmentation, return_counts=True)
    count_by_id = {int(key): int(value) for key, value in zip(decoded, counts)}
    unknown_ids = sorted(set(count_by_id) - valid_ids)
    images = []
    tasks = []
    for label, response in zip(IMAGE_LABELS, responses):
        image_record = {
            "kind": label,
            "path": f"tick-{tick_record['tick']:04d}-{label}.png",
            "sha256": None,
            "width": int(response.width),
            "height": int(response.height),
            "time_stamp_ns": int(response.time_stamp),
            "camera_extrinsic": CAMERA_EXTRINSICS[label],
        }
        images.append(image_record)
        tasks.append(
            (
                bytes(response.image_data_uint8),
                int(response.width),
                int(response.height),
                image_record,
            )
        )
    record = {
        "capture_attempts": attempts,
        "sensor_timestamp_ns": int(responses[0].time_stamp),
        "images": images,
        "ship_pixel_counts": {
            str(value): count_by_id.get(value, 0) for value in sorted(ship_ids)
        },
        "passenger_pixel_counts": {
            str(value): count_by_id.get(value, 0)
            for value in sorted(passenger_ids)
        },
        "unknown_ids": unknown_ids,
        "visible_ship_ids": visible_ship_ids,
        "timestamps_identical": len({int(row.time_stamp) for row in responses}) == 1,
    }
    tick_record.update(record)
    if unknown_ids:
        raise UnknownInstanceIds(tick_record)
    writer.enqueue_tick(int(tick_record["tick"]), tasks)
    return record


def run_capture(
    fixture: str | Path,
    output_dir: str | Path,
    ticks: range | None = None,
    resume: bool = False,
    recapture_ticks: Iterable[int] = (),
    *,
    frame_metadata: str | Path = DEFAULT_FRAME_METADATA,
    observations_dir: str | Path = DEFAULT_OBSERVATIONS_DIR,
    api_port: int = 41461,
) -> dict[str, Any]:
    import airsim
    from onr_physical_runtime.sim.experimental_freeze import (
        EngineClock,
        FullFreeze,
        build_shim,
    )
    from onr_physical_runtime.sim.processes import stop_process_group

    fixture = Path(fixture).resolve()
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    requested = sorted(set(ticks if ticks is not None else range(600)))
    recapture = {int(tick) for tick in recapture_ticks}
    outside_requested = sorted(recapture - set(requested))
    if outside_requested:
        raise ValueError(
            f"recapture ticks are outside the requested range: {outside_requested}"
        )
    disk = disk_preflight(output, len(requested))
    staging = choose_staging_dir()
    paths = _validate_fixture(fixture)
    lead_in_s = resolve_lead_in_s(None, paths["manifest"])
    trajectory = load_drone_trajectory(frame_metadata, observations_dir)
    visible_by_tick = _visible_ship_ids(frame_metadata)
    mapping = json.loads(paths["mapping"].read_text())
    dynamic_ids = mapped_dynamic_object_ids(mapping)
    static_ids = parse_static_instance_ids(INSTANCE_OBJECT_IDS)
    valid_ids = {0} | dynamic_ids | static_ids
    ship_ids = {
        int(row["object_id"]) for row in mapping.get("ships", {}).values()
    }
    passenger_ids = {
        int(row["object_id"]) for row in mapping.get("passengers", {}).values()
    }
    ships = _load_ships(paths["ships"])
    subset = ships[:3]  # scenario ships share one clock; three suffice in poll band
    output_manifest = output / "capture-manifest.json"
    if resume and output_manifest.is_file():
        manifest = json.loads(output_manifest.read_text())
    else:
        manifest = {
            "schema_version": 1,
            "model": "stepped-beat-v4",
            "created_at": _utc_now(),
            "fixture": str(fixture),
            "ticks": {},
            "disk_preflight": disk,
            "staging_dir": str(staging),
        }
    pending = pending_capture_ticks(
        requested,
        manifest,
        output,
        resume=resume,
        recapture_ticks=recapture,
    )
    if not pending:
        return manifest
    writer = DeferredCaptureWriter(staging, manifest)

    preflight_ports_free({int(api_port), 41451})
    root = output / "engine-state"
    status_dir = root / "status"
    scenario_times_path = (
        status_dir / paths["scenario"].name / "scenario_times.json"
    )
    swap = EngineConfigSwap(
        ENGINE_CONFIG,
        ENGINE_OBJECT_IDS,
        INSTANCE_OBJECT_IDS,
        fixture / "scenarios",
        status_dir,
        warmup_s=30.0,
        cooldown_s=5.0,
        evidence_dir=root / "engine-config",
    )
    process = clock = freeze = None
    captured = 0
    worst_aircraft_error = 0.0
    total_bytes = 0
    report: dict[str, Any] = {"measurements": {}}

    with swap:
        cleanup_errors: list[BaseException] = []
        try:
            library = build_shim(root / "shim")
            state_path = root / "shim" / f"clock-{time.time_ns()}.bin"
            clock = EngineClock(state_path, create=True)
            environment = clock.environment(library)
            with _temporary_environment(
                {
                    "ONR_FREEZE_STATE": environment["ONR_FREEZE_STATE"],
                    "LD_PRELOAD": environment["LD_PRELOAD"],
                }
            ):
                launch_wall_s = time.time()
                process = launch_engine(
                    ENGINE_EXECUTABLE,
                    paths["settings"],
                    root / "engine.log",
                    cuda_device=2,
                    extra_args=("-graphicsadapter=2",),
                )
            _wait_for_rpc(process, int(api_port))
            _scenario_times, _scenario_bytes = _wait_for_active_scenario(
                scenario_times_path, process, clock, launch_wall_s
            )
            client = airsim.MultirotorClient(port=int(api_port), timeout_value=60)
            wait_for_ship_spawns(
                client, (ship["name"] for ship in ships), timeout_s=180.0
            )
            freeze = FullFreeze(clock, client)
            pause_patiently(freeze)
            initial_centers = _read_ship_phases(client, ships)
            phase_reader = BeatPhaseReader(
                client, ships, subset, initial_centers
            )

            for tick, should_capture in capture_schedule(requested, pending):
                check_process_health(process, client, tick)
                pose = trajectory[tick]
                target_phase_s = lead_in_s + pose.mission_time_s
                landing_target_phase_s = _capture_landing_target(
                    target_phase_s, should_capture
                )
                phase_reader.set_target(landing_target_phase_s)
                landing = advance_to_phase(
                    freeze, phase_reader, landing_target_phase_s
                )
                full_phases = phase_reader.read_full()
                landing["landing_errors_s"] = _phase_errors(
                    full_phases, landing_target_phase_s
                )
                if not _enforce_landing_gate(
                    tick,
                    landing,
                    resume=resume,
                    capture_required=should_capture,
                    manifest=manifest,
                    output_dir=output,
                ):
                    continue
                if not should_capture:
                    continue

                tick_record = {
                    "tick": tick,
                    "mission_time_s": pose.mission_time_s,
                    "target_phase_s": target_phase_s,
                    "landing_target_phase_s": landing_target_phase_s,
                    "measured_min_ship_phase_pre_flush_s": _finite_minimum(
                        tuple(full_phases.values())
                    ),
                    "landing_errors_s": landing["landing_errors_s"],
                    "landing": landing,
                }
                writer.register_tick(tick, tick_record)
                post_flush_phases = _prepare_capture_frame(
                    client, freeze, phase_reader, pose
                )
                tick_record["measured_min_ship_phase_s"] = _finite_minimum(
                    tuple(post_flush_phases.values())
                )
                post_flush_errors = _phase_errors(
                    post_flush_phases, target_phase_s
                )
                tick_record["landing_errors_post_flush_s"] = post_flush_errors
                try:
                    _enforce_landing_gate(
                        tick,
                        {"landing_errors_s": post_flush_errors},
                        resume=resume,
                        capture_required=True,
                        manifest=manifest,
                        output_dir=output,
                        gate_name="post-flush acquisition",
                    )
                except RuntimeError:
                    writer.write_manifest()
                    raise
                aircraft_pose = _verify_drone_pose(client, pose)
                tick_record["aircraft_pose"] = aircraft_pose
                if not aircraft_pose["passed"]:
                    writer.write_manifest()
                    raise RuntimeError(f"aircraft pose failed at capture tick {tick}")
                worst_aircraft_error = max(
                    worst_aircraft_error,
                    float(aircraft_pose["max_abs_position_error_m"]),
                )
                try:
                    capture_image_set(
                        client,
                        airsim,
                        writer,
                        tick_record,
                        valid_ids,
                        ship_ids,
                        passenger_ids,
                        visible_by_tick[tick],
                    )
                except UnknownInstanceIds:
                    writer.write_manifest()
                    raise
                captured += 1
                if captured % 50 == 0 and writer.queue.empty():
                    writer.flush()
                    sync_to_nfs(staging, output)
        finally:
            primary_error = sys.exc_info()[1]
            try:
                writer.close()
                sync_to_nfs(staging, output)
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(exc)
            if freeze is not None:
                try:
                    freeze.pause()
                except Exception as exc:  # noqa: BLE001
                    cleanup_errors.append(exc)
            if process is not None:
                try:
                    stop_process_group(process)
                except Exception as exc:  # noqa: BLE001
                    cleanup_errors.append(exc)
            if clock is not None:
                try:
                    clock.close()
                except Exception as exc:  # noqa: BLE001
                    cleanup_errors.append(exc)
            cleanup_failure = reconcile_cleanup_failure(
                report, primary_error, cleanup_errors
            )
            if primary_error is None and cleanup_failure is not None:
                raise cleanup_failure

    manifest["configuration_restored"] = swap.configuration_restored
    manifest["completed_at"] = _utc_now()
    total_bytes = sum(
        path.stat().st_size for path in staging.glob("tick-*.png")
    )
    manifest["summary"] = {
        "ticks_captured": captured,
        "worst_aircraft_position_error_m": worst_aircraft_error,
        "total_bytes": total_bytes,
    }
    write_manifest_atomic(writer.manifest_path, manifest)
    sync_to_nfs(staging, output)
    print(json.dumps(manifest["summary"], indent=2), flush=True)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start-tick", type=int, default=0)
    parser.add_argument("--stop-tick", type=int, default=600)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--recapture-ticks", type=Path)
    parser.add_argument("--frame-metadata", type=Path, default=DEFAULT_FRAME_METADATA)
    parser.add_argument(
        "--observations", type=Path, default=DEFAULT_OBSERVATIONS_DIR
    )
    parser.add_argument("--api-port", type=int, default=41461)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_capture(
        args.fixture,
        args.output,
        range(args.start_tick, args.stop_tick),
        args.resume,
        recapture_ticks=(
            load_recapture_ticks(args.recapture_ticks) if args.recapture_ticks else ()
        ),
        frame_metadata=args.frame_metadata,
        observations_dir=args.observations,
        api_port=args.api_port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
