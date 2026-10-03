"""Live AirSim visualization follower for perception-off Mission Runs (ADR 0016).

This is the issue #65 reconstruction approach run live. Harbor plays a lead-in
fixture of the world model's own ship trajectories (``fixture.py``) under the
freeze shim, so the scene starts behind Mission time 0 and can only be stepped
forward. For each new world-model Mission time the follower:

1. steps the frozen scene to ``lead_in + mission_time - FLUSH_SECONDS``;
2. teleports the drone to the world-model NED pose (full kinematics reset);
3. flushes one ``FLUSH_SECONDS`` render step, landing on ``lead_in + mission_time``;
4. captures front Scene + Segmentation and third-person Scene in one request;
5. publishes raw and ideal-segmentation-annotated frames plus ``airsim.json``.

Scene time is advanced through the shim's private wall clock exactly as the
physical runtime's ``SceneClock`` does: after each ``simContinueForTime`` the
wall clock is set to the measured physics interval, so RPC latency never
accumulates as playback drift. Ship playback is checked against the fixture
trajectories every beat and reported as ``ship_phase_error_seconds``.

AirSim is visualization only here: the follower never publishes evidence and
the world model never waits for it. When it falls behind it skips to the newest
world-model state and reports the lag.
"""

from __future__ import annotations

import argparse
import json
import math
import signal
import statistics
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from onr.runtime_host.airsim_overlay import (
    ideal_segmentation_annotation,
    labels_for_mapping,
    render_annotated,
    scene_rgb,
    segmentation_ids,
)
from onr.runtime_host.world import AIRSIM_NUMBER_FIELDS, CameraFrameStore

from .capture import FLUSH_SECONDS, DronePose, _reset_drone_kinematics

MIN_STEP_SECONDS = 0.005
YAW_RATE_DEGREES_S = 90.0
"""The live AirSim synchronizer's yaw rate; world-model headings snap by 90."""
SCENE_END_MARGIN_SECONDS = 0.5
PHASE_SHIPS = 3
MIN_BOX_PIXELS = 30
"""At the follower's 960x540 capture a distant hull covers only tens of pixels."""
_MAX_EVENT_BYTES = 256 * 1024


class FollowerError(RuntimeError):
    """A recoverable follower failure reported as the ``airsim.json`` reason."""


@dataclass(frozen=True, slots=True)
class WorldSample:
    """One committed world-model state from ``environment-data``."""

    mission_time_s: float
    ned_m: tuple[float, float, float]
    heading_degrees: float


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def world_sample(payload: Mapping[str, Any]) -> WorldSample | None:
    """Mission time and controlled-vehicle pose from one environment-data payload."""

    vehicle = payload.get("controlled_vehicle")
    if not isinstance(vehicle, Mapping):
        return None
    position = vehicle.get("position")
    if not isinstance(position, Mapping):
        return None
    values = [_number(position.get(axis)) for axis in ("x", "y", "z")]
    mission_time = _number(payload.get("mission_time_seconds"))
    heading = _number(vehicle.get("heading_degrees"))
    if mission_time is None or heading is None or any(value is None for value in values):
        return None
    north, east, down = (float(value) for value in values if value is not None)
    return WorldSample(mission_time, (north, east, down), heading % 360.0)


class EnvironmentTail:
    """Newest world-model sample from one Mission's ``environment-data`` topic.

    Topic file names start with a zero-padded sequence, so lexical order is
    publication order and only names after the last one read are inspected.
    """

    def __init__(self, transport_root: Path, mission_id: str) -> None:
        self.directory = (
            Path(transport_root)
            / "topics/environment-data/missions"
            / quote(mission_id, safe="")
        )
        self._last_name = ""
        self.latest: WorldSample | None = None

    def poll(self) -> WorldSample | None:
        try:
            names = sorted(
                entry.name
                for entry in self.directory.iterdir()
                if entry.name > self._last_name and entry.name.endswith(".json")
            )
        except OSError:
            return self.latest
        for name in reversed(names):
            try:
                with (self.directory / name).open("rb") as handle:
                    event = json.loads(handle.read(_MAX_EVENT_BYTES))
            except (OSError, ValueError):
                continue
            payload = event.get("payload") if isinstance(event, dict) else None
            sample = world_sample(payload) if isinstance(payload, Mapping) else None
            if sample is not None:
                if self.latest is None or sample.mission_time_s >= self.latest.mission_time_s:
                    self.latest = sample
                break
        if names:
            self._last_name = names[-1]
        return self.latest


def approach_yaw(current: float, target: float, max_delta: float) -> float:
    """Turn ``current`` toward ``target`` by at most ``max_delta`` degrees."""

    difference = (target - current + 180.0) % 360.0 - 180.0
    if abs(difference) <= max_delta:
        return target % 360.0
    return (current + math.copysign(max_delta, difference)) % 360.0


class SceneStepper:
    """Forward-only scene time for a frozen, shimmed Harbor engine.

    Ship playback follows the shim's frozen wall clock with a constant start
    offset (Harbor begins playback shortly after ``scenario_start_time``), so
    scene phase is ``frozen wall - scenario start + playback offset``; the
    offset is measured once against the fixture trajectories. Each step
    advances physics with ``FullFreeze.step`` and then sets the wall clock to
    the measured physics interval, as ``SceneClock.prepare`` does.
    """

    def __init__(
        self,
        freeze: Any,
        *,
        scenario_start_s: float,
        sensor_ns: Callable[[], int],
        playback_offset_s: float = 0.0,
    ) -> None:
        self.freeze = freeze
        self.start = scenario_start_s
        self.offset = playback_offset_s
        self._sensor_ns = sensor_ns

    def clock_phase(self) -> float:
        return self.freeze.clock.status()["frozen_ns"] / 1e9 - self.start

    def phase(self) -> float:
        return self.clock_phase() + self.offset

    def step_to(self, target_phase: float) -> None:
        clock = self.freeze.clock
        status = clock.status()
        if not status["paused"]:
            raise FollowerError("airsim_pause_ownership_lost")
        remaining = target_phase - (status["frozen_ns"] / 1e9 - self.start + self.offset)
        if remaining <= MIN_STEP_SECONDS:
            return
        before_wall = status["frozen_ns"]
        before_sensor = self._sensor_ns()
        self.freeze.step(remaining)
        advanced = self._sensor_ns() - before_sensor
        if advanced <= 0:
            raise FollowerError("airsim_step_did_not_advance")
        clock.set_frozen_time(before_wall + advanced)


def _trajectory_phase(ship: Mapping[str, Any], measured_ned: Sequence[float]) -> float:
    from onr_physical_runtime.sim.experimental_freeze.scene_clock import (
        trajectory_phase,
    )

    return trajectory_phase(ship, measured_ned)


def ship_phases(client: Any, ships: Sequence[Mapping[str, Any]]) -> list[float]:
    """Trajectory phase of each sampled ship's measured AirSim position."""

    phases: list[float] = []
    for ship in ships:
        pose = client.simGetObjectPose(ship["name"]).position
        try:
            phases.append(_trajectory_phase(ship, [pose.x_val, pose.y_val, pose.z_val]))
        except RuntimeError:
            continue  # stationary or off-trajectory sample: no phase to compare
    return phases


def moving_ships(ships_dir: Path, count: int = PHASE_SHIPS) -> list[dict[str, Any]]:
    """The first ``count`` fixture ships whose trajectory moves."""

    selected: list[dict[str, Any]] = []
    for path in sorted(ships_dir.glob("*.json"), key=lambda item: item.stem.zfill(6)):
        if not path.stem.isdigit():
            continue
        ship = json.loads(path.read_text(encoding="utf-8"))
        pose = ship.get("pose") or []
        if len(pose) > 1 and math.dist(pose[0][:2], pose[-1][:2]) > 100.0:
            selected.append(ship)
        if len(selected) == count:
            break
    return selected


@dataclass(slots=True)
class Beat:
    sample: WorldSample
    scene_phase: float
    yaw_degrees: float
    ship_phase_error: float | None


class Follower:
    """Own the frozen engine's clock and render one beat per new Mission time."""

    def __init__(
        self,
        *,
        client: Any,
        sdk: Any,
        freeze: Any,
        stepper: SceneStepper,
        tail: EnvironmentTail,
        store: CameraFrameStore,
        lead_in_s: float,
        scene_end_phase_s: float,
        labels_by_object_id: Mapping[int, str],
        phase_ships: Sequence[Mapping[str, Any]],
        vehicle: str,
        front_camera: str,
        third_person_camera: str,
        encode: Callable[..., bytes],
    ) -> None:
        self.client = client
        self.sdk = sdk
        self.freeze = freeze
        self.stepper = stepper
        self.tail = tail
        self.store = store
        self.lead_in = lead_in_s
        self.scene_end = scene_end_phase_s
        self.labels = dict(labels_by_object_id)
        self.phase_ships = list(phase_ships)
        self.vehicle = vehicle
        self.front = front_camera
        self.third = third_person_camera
        self.encode = encode
        self.last_beat: Beat | None = None

    def beat(self, sample: WorldSample) -> Beat:
        """Render ``sample``; raise :class:`FollowerError` when it cannot."""

        target = self.lead_in + sample.mission_time_s
        if target > self.scene_end - SCENE_END_MARGIN_SECONDS:
            raise FollowerError("airsim_scene_ended: the fixture scene has no later ship poses")
        phase = self.stepper.phase()
        if target < phase:
            raise FollowerError(
                f"airsim_scene_ahead: scene {phase - self.lead_in:.1f} s is ahead of "
                f"world-model Mission time {sample.mission_time_s:.1f} s"
            )
        previous = self.last_beat
        yaw = sample.heading_degrees
        if previous is not None:
            elapsed = max(0.0, sample.mission_time_s - previous.sample.mission_time_s)
            yaw = approach_yaw(previous.yaw_degrees, yaw, YAW_RATE_DEGREES_S * elapsed)
        self.stepper.step_to(target - FLUSH_SECONDS)
        # Harbor's playback offset is re-measured every beat: it is not constant
        # across the synthetic lead-in and can creep between steps. A measured
        # lag is closed before the flush; ships that are ahead stay ahead.
        self._calibrate()
        self.stepper.step_to(target - FLUSH_SECONDS)
        _reset_drone_kinematics(
            self.client,
            DronePose(0, sample.mission_time_s, sample.ned_m, yaw),
            vehicle_name=self.vehicle,
        )
        self.stepper.step_to(target)
        scene_phase = self.stepper.phase()
        responses = self.client.simGetImages(
            [
                self.sdk.ImageRequest(self.front, self.sdk.ImageType.Scene, False, False),
                self.sdk.ImageRequest(
                    self.front, self.sdk.ImageType.Segmentation, False, False
                ),
                self.sdk.ImageRequest(self.third, self.sdk.ImageType.Scene, False, False),
            ],
            vehicle_name=self.vehicle,
        )
        if len(responses) != 3 or any(int(item.width) <= 0 for item in responses):
            raise FollowerError("airsim_frame_unavailable")
        front, segmentation, third = responses
        annotation = ideal_segmentation_annotation(
            segmentation_ids(segmentation, (int(front.height), int(front.width))),
            self.labels,
            min_pixels=MIN_BOX_PIXELS,
        )
        stamp = int(front.time_stamp)
        measured = ship_phases(self.client, self.phase_ships)
        error = max((abs(value - target) for value in measured), default=None)
        if measured:
            scene_phase = statistics.median(measured)
            self.stepper.offset += scene_phase - self.stepper.phase()
        timing = {
            "sensor_timestamp_ns": stamp,
            "scene_phase_seconds": scene_phase,
            "scene_mission_time_seconds": scene_phase - self.lead_in,
        }
        self.store.persist(
            {
                "camera_front": self.encode(
                    bytes(front.image_data_uint8), int(front.width), int(front.height)
                ),
                "camera_front_annotated": render_annotated(scene_rgb(front), annotation),
                "camera_third_person": self.encode(
                    bytes(third.image_data_uint8), int(third.width), int(third.height)
                ),
            },
            sample.mission_time_s,
            {
                "camera_front": timing,
                "camera_third_person": timing,
                "camera_front_annotated": {
                    **timing,
                    "annotation": annotation.status(),
                    "boxes": [
                        {"label": box.label, "bbox": [box.x0, box.y0, box.x1, box.y1]}
                        for box in annotation.boxes
                    ],
                },
            },
        )
        beat = Beat(sample, scene_phase, yaw, error)
        self.last_beat = beat
        self.publish("capturing", None, annotation.status())
        return beat

    def _calibrate(self) -> None:
        """Set the playback offset from the sampled ships' measured phases."""

        measured = ship_phases(self.client, self.phase_ships)
        if measured:
            self.stepper.offset += statistics.median(measured) - self.stepper.phase()

    def publish(
        self, state: str, reason: str | None, annotation: Mapping[str, object] | None = None
    ) -> None:
        beat = self.last_beat
        newest = self.tail.poll()
        frame_time = None if beat is None else beat.sample.mission_time_s
        world_time = None if newest is None else newest.mission_time_s
        status: dict[str, object] = {
            "mode": "world_model_follower",
            "perception": "off",
            "state": state,
            "reason": reason,
            **dict.fromkeys(AIRSIM_NUMBER_FIELDS),
            "annotation": annotation,
        }
        if beat is not None:
            status["frame_mission_time_seconds"] = frame_time
            status["ship_phase_error_seconds"] = beat.ship_phase_error
            status["scene_phase_seconds"] = beat.scene_phase
        if world_time is not None:
            status["world_mission_time_seconds"] = world_time
            if frame_time is not None:
                status["lag_seconds"] = max(0.0, world_time - frame_time)
        self.store.report("unavailable" if state == "unavailable" else state, reason)
        self.store.write_airsim(status)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} is not a JSON object")
    return value


def _wait_for(path: Path, stop: Callable[[], bool], timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while not path.is_file():
        if stop() or time.monotonic() > deadline:
            raise FollowerError(f"airsim_engine_not_ready: {path} is missing")
        time.sleep(0.5)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Live AirSim visualization follower for perception-off Mission Runs."
    )
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--engine-ready", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--vehicle", default="SimpleFlight")
    parser.add_argument("--front-camera", default="front_center_custom")
    parser.add_argument("--third-person-camera", default="third_person_demo")
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=float, default=0.1)
    parser.add_argument("--engine-timeout-seconds", type=float, default=600.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stopping = False

    def stop(signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop)

    store = CameraFrameStore(
        args.run_root,
        cameras={
            "camera_front_annotated": args.front_camera,
            "camera_front": args.front_camera,
            "camera_third_person": args.third_person_camera,
        },
        vehicle_name=args.vehicle,
    )
    store.load_persisted()
    status = {
        "mode": "world_model_follower",
        "perception": "off",
        **dict.fromkeys(AIRSIM_NUMBER_FIELDS),
        "annotation": None,
    }
    store.write_airsim({**status, "state": "unavailable", "reason": "airsim_follower_starting"})
    try:
        _wait_for(args.engine_ready, lambda: stopping, args.engine_timeout_seconds)
        follower = _connect(args, store)
    except Exception as exc:  # noqa: BLE001 - reported, then the service fails
        reason = (
            str(exc)
            if isinstance(exc, FollowerError)
            else f"airsim_follower_failed: {type(exc).__name__}: {exc}"
        )
        store.write_airsim({**status, "state": "stopped", "reason": reason})
        print(reason, file=sys.stderr, flush=True)
        return 1
    print(
        "AirSim follower connected: lead-in "
        f"{follower.lead_in:g} s, scene at {follower.stepper.phase():.2f} s",
        flush=True,
    )
    failure: str | None = None
    rendered: float | None = None
    try:
        while not stopping:
            sample = follower.tail.poll()
            if sample is None or (rendered is not None and sample.mission_time_s <= rendered):
                time.sleep(args.poll_seconds)
                continue
            try:
                beat = follower.beat(sample)
            except FollowerError as exc:
                failure = str(exc)
                follower.publish("unavailable", failure)
                time.sleep(max(args.poll_seconds, 0.5))
                continue
            except Exception as exc:  # noqa: BLE001 - SDK errors vary by engine state
                failure = f"airsim_follower_failed: {type(exc).__name__}"
                follower.publish("unavailable", failure)
                print(f"{failure}: {exc}", file=sys.stderr, flush=True)
                time.sleep(1.0)
                continue
            failure = None
            rendered = beat.sample.mission_time_s
            if not args.ready_file.exists():
                args.ready_file.write_text(
                    json.dumps(
                        {
                            "lead_in_seconds": follower.lead_in,
                            "first_mission_time_seconds": rendered,
                            "scene_phase_seconds": beat.scene_phase,
                        }
                    ),
                    encoding="utf-8",
                )
                print(f"AirSim follower ready at Mission time {rendered:.1f} s", flush=True)
    finally:
        # Leave the engine frozen: the engine service owns teardown.
        follower.publish("stopped", failure)
    return 0


def _connect(args: argparse.Namespace, store: CameraFrameStore) -> Follower:
    import airsim
    from onr_physical_runtime.sim.experimental_freeze import EngineClock, FullFreeze
    from onr_physical_runtime.sim.viewer_frames import encode_bgr_jpeg

    ready = _load_json(args.engine_ready)
    times = _load_json(Path(ready["scenario_times"]))
    start = float(times["scenario_start_time"])
    fixture = Path(args.fixture)
    manifest = _load_json(fixture / "manifest.json")
    mapping = _load_json(fixture / "mapping.json")
    scenario = fixture / "scenarios" / str(mapping["scenario_name"])
    client = airsim.MultirotorClient(port=int(ready["airsim_port"]), timeout_value=30)
    clock = EngineClock(Path(ready["clock_state"]))
    if clock.status()["intercepted_reads"] == 0:
        clock.close()
        raise FollowerError("airsim_shim_not_loaded")
    freeze = FullFreeze(clock, client)
    freeze.pause()
    client.enableApiControl(True, vehicle_name=args.vehicle)

    def sensor_ns() -> int:
        return int(client.getMultirotorState(vehicle_name=args.vehicle).timestamp)

    tail = EnvironmentTail(args.run_root / "transport", args.mission_id)
    phase_ships = moving_ships(scenario / "ships")
    stepper = SceneStepper(freeze, scenario_start_s=start, sensor_ns=sensor_ns)
    # Harbor starts ship playback a fixed interval after scenario_start_time.
    measured = sorted(phase - stepper.clock_phase() for phase in ship_phases(client, phase_ships))
    if measured:
        stepper.offset = measured[len(measured) // 2]
    print(f"Ship playback offset {stepper.offset:+.3f} s from {len(measured)} ships", flush=True)
    return Follower(
        client=client,
        sdk=airsim,
        freeze=freeze,
        stepper=stepper,
        tail=tail,
        store=store,
        lead_in_s=float(manifest["lead_in_seconds"]),
        scene_end_phase_s=float(times["scenario_end_time"]) - start,
        labels_by_object_id=labels_for_mapping(mapping),
        phase_ships=phase_ships,
        vehicle=args.vehicle,
        front_camera=args.front_camera,
        third_person_camera=args.third_person_camera,
        encode=encode_bgr_jpeg,
    )


if __name__ == "__main__":
    raise SystemExit(main())
