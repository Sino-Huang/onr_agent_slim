"""Live AirSim follower: scene stepping, world-model tail and one rendered beat."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import numpy as np
import pytest

from onr.demo.airsim_reconstruction import follower as follower_module
from onr.demo.airsim_reconstruction.follower import (
    EnvironmentTail,
    Follower,
    FollowerError,
    SceneStepper,
    WorldSample,
    approach_yaw,
)
from onr.runtime_host.world import CameraFrameStore, WorldView

START_S = 1_000.0


class FakeClock:
    """The freeze shim's state: frozen wall time drives ship playback."""

    def __init__(self, phase: float) -> None:
        self.frozen_ns = int((START_S + phase) * 1e9)
        self.paused = True

    def status(self) -> dict[str, Any]:
        return {"frozen_ns": self.frozen_ns, "paused": self.paused}

    def set_frozen_time(self, value_ns: int) -> None:
        self.frozen_ns = int(value_ns)


class FakeFreeze:
    """``FullFreeze.step`` whose wall clock overshoots physics by ``slip``."""

    def __init__(self, clock: FakeClock, log: list[tuple[Any, ...]], *, slip: float = 0.05) -> None:
        self.clock = clock
        self.sensor_ns = 7_000_000_000
        self._log = log
        self._slip = slip

    def step(self, seconds: float) -> None:
        self._log.append(("step", round(seconds, 6)))
        self.sensor_ns += int(seconds * 1e9)
        self.clock.frozen_ns += int((seconds + self._slip) * 1e9)


def _stepper(phase: float, log: list[tuple[Any, ...]], **kwargs: Any) -> tuple[SceneStepper, FakeFreeze]:
    freeze = FakeFreeze(FakeClock(phase), log)
    return SceneStepper(freeze, scenario_start_s=START_S, sensor_ns=lambda: freeze.sensor_ns, **kwargs), freeze


def test_scene_steps_forward_by_measured_physics_not_wall_overshoot() -> None:
    log: list[tuple[Any, ...]] = []
    stepper, _freeze = _stepper(10.0, log, playback_offset_s=0.5)

    stepper.step_to(31.0)
    # Wall slip (RPC/scheduling) is discarded: phase lands on the physics interval.
    assert stepper.phase() == pytest.approx(31.0)
    assert log == [("step", 20.5)]

    stepper.step_to(30.0)  # never backwards
    stepper.step_to(31.003)  # below the minimum step
    assert log == [("step", 20.5)]

    stepper.freeze.clock.paused = False
    with pytest.raises(FollowerError, match="pause_ownership_lost"):
        stepper.step_to(40.0)


def _event(directory: Path, sequence: int, payload: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{sequence:020d}-environment-data.json").write_text(
        json.dumps({"payload": payload}), encoding="utf-8"
    )


def _payload(t: float, north: float = 1.0, heading: float = 90.0) -> dict[str, Any]:
    return {
        "mission_time_seconds": t,
        "controlled_vehicle": {
            "position": {"x": north, "y": 2.0, "z": -25.0},
            "heading_degrees": heading,
        },
    }


def test_environment_tail_returns_the_newest_world_model_pose(tmp_path: Path) -> None:
    mission = "mission-0b1c2d3e"
    directory = tmp_path / "topics/environment-data/missions" / quote(mission, safe="")
    tail = EnvironmentTail(tmp_path, mission)
    assert tail.poll() is None

    _event(directory, 1, _payload(0.5))
    _event(directory, 2, _payload(1.0, north=4.0, heading=450.0))
    _event(directory, 3, {"mission_time_seconds": 1.5})  # no controlled vehicle
    assert tail.poll() == WorldSample(1.0, (4.0, 2.0, -25.0), 90.0)

    _event(directory, 4, _payload(1.5, north=6.0))
    assert tail.poll() == WorldSample(1.5, (6.0, 2.0, -25.0), 90.0)
    assert tail.poll() == WorldSample(1.5, (6.0, 2.0, -25.0), 90.0)


def test_yaw_turns_at_a_bounded_rate_across_north() -> None:
    assert approach_yaw(350.0, 80.0, 45.0) == pytest.approx(35.0)
    assert approach_yaw(10.0, 280.0, 45.0) == pytest.approx(325.0)
    assert approach_yaw(90.0, 100.0, 45.0) == pytest.approx(100.0)


class FakeAirSim:
    class ImageType:
        Scene = 0
        Segmentation = 5

    @staticmethod
    def ImageRequest(camera: str, image_type: int, *_args: Any) -> tuple[str, int]:
        return camera, image_type


def _image(pixels: np.ndarray, stamp: int) -> SimpleNamespace:
    height, width = pixels.shape[:2]
    return SimpleNamespace(
        image_data_uint8=pixels.tobytes(), width=width, height=height, time_stamp=stamp
    )


class FakeClient:
    """Scene, segmentation and ship poses for a frozen engine."""

    def __init__(self, freeze: FakeFreeze, log: list[tuple[Any, ...]], ship_lag_s: float) -> None:
        self.freeze = freeze
        self.log = log
        self.ship_lag = ship_lag_s

    def simGetObjectPose(self, name: str) -> SimpleNamespace:
        # Encode the ship's playback phase in its north coordinate (see the
        # monkeypatched trajectory phase below).
        phase = self.freeze.clock.frozen_ns / 1e9 - START_S - self.ship_lag
        return SimpleNamespace(position=SimpleNamespace(x_val=phase, y_val=0.0, z_val=0.0))

    def simGetImages(self, requests: list[tuple[str, int]], vehicle_name: str) -> list[SimpleNamespace]:
        self.log.append(("images", tuple(requests), vehicle_name))
        scene = np.zeros((54, 96, 3), dtype=np.uint8)
        segmentation = np.zeros((54, 96, 3), dtype=np.uint8)
        segmentation[10:30, 20:60] = (0, 0, 7)  # object 7 = ship 3, decoded in raw order
        stamp = self.freeze.sensor_ns
        return [_image(scene, stamp), _image(segmentation, stamp), _image(scene, stamp)]


def _follower(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, phase: float, ship_lag_s: float = 0.0
) -> tuple[Follower, list[tuple[Any, ...]], EnvironmentTail]:
    log: list[tuple[Any, ...]] = []
    stepper, freeze = _stepper(phase, log)
    client = FakeClient(freeze, log, ship_lag_s)
    monkeypatch.setattr(
        follower_module, "_trajectory_phase", lambda _ship, measured: measured[0]
    )
    monkeypatch.setattr(
        follower_module,
        "_reset_drone_kinematics",
        lambda _client, pose, *, vehicle_name: log.append(
            ("teleport", pose.ned_m, round(pose.yaw_degrees, 3), vehicle_name, round(stepper.phase(), 3))
        ),
    )
    tail = EnvironmentTail(tmp_path / "transport", "mission-1")
    store = CameraFrameStore(
        tmp_path,
        cameras={
            "camera_front_annotated": "front_center_custom",
            "camera_front": "front_center_custom",
            "camera_third_person": "third_person_demo",
        },
        vehicle_name="SimpleFlight",
    )
    follower = Follower(
        client=client,
        sdk=FakeAirSim,
        freeze=freeze,
        stepper=stepper,
        tail=tail,
        store=store,
        lead_in_s=30.0,
        scene_end_phase_s=329.5,
        labels_by_object_id={7: "ship 3"},
        phase_ships=[{"name": "ship-3"}],
        vehicle="SimpleFlight",
        front_camera="front_center_custom",
        third_person_camera="third_person_demo",
        encode=lambda data, width, height: b"\xff\xd8" + bytes([width % 256, height % 256]) + data[:8],
    )
    return follower, log, tail


def test_beat_lands_ships_on_lead_in_plus_mission_time_and_publishes_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ship playback trails the shim clock by 0.4 s; the beat measures and closes it.
    follower, log, tail = _follower(tmp_path, monkeypatch, phase=12.0, ship_lag_s=0.4)
    directory = tmp_path / "transport/topics/environment-data/missions/mission-1"
    _event(directory, 1, _payload(5.0))
    sample = tail.poll()
    assert sample is not None
    _event(directory, 2, _payload(5.5))  # the world model moved on during the beat

    beat = follower.beat(sample)

    steps = [entry for entry in log if entry[0] == "step"]
    teleport = next(entry for entry in log if entry[0] == "teleport")
    assert teleport[1:4] == ((1.0, 2.0, -25.0), 90.0, "SimpleFlight")
    # The drone is placed one flush step before the target, after calibration.
    assert teleport[4] == pytest.approx(34.9, abs=1e-3)
    assert log.index(teleport) < log.index(steps[-1]) < next(
        index for index, entry in enumerate(log) if entry[0] == "images"
    )
    assert steps[-1][1] == pytest.approx(0.1)
    assert beat.scene_phase == pytest.approx(35.0, abs=1e-3)
    assert beat.ship_phase_error == pytest.approx(0.0, abs=1e-3)

    view = WorldView(tmp_path)
    section = view.section(live=False)
    assert [frame["source"] for frame in section["frames"]] == [
        "camera_front_annotated",
        "camera_front",
        "camera_third_person",
    ]
    assert section["airsim"] == {
        "mode": "world_model_follower",
        "perception": "off",
        "state": "stopped",  # a terminal reader never reports capturing
        "reason": None,
        "frame_mission_time_seconds": 5.0,
        "world_mission_time_seconds": 5.5,
        "lag_seconds": 0.5,
        "ship_phase_error_seconds": pytest.approx(0.0, abs=1e-3),
        "annotation": {
            "kind": "ideal_segmentation",
            "disclosure": (
                "AirSim follows the world model. Boxes are ideal instance "
                "segmentation, not agent perception."
            ),
            "match": "exact",
            "objects": 1,
            "perception_mission_time_seconds": None,
        },
    }
    metadata = json.loads((tmp_path / "world-frames/camera_front_annotated.json").read_text())
    assert metadata["boxes"] == [{"label": "ship 3", "bbox": [20, 10, 59, 29]}]
    assert metadata["scene_mission_time_seconds"] == pytest.approx(5.0, abs=1e-3)


def test_scene_ahead_or_past_its_end_is_reported_not_rendered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    follower, log, _tail = _follower(tmp_path, monkeypatch, phase=40.0)

    with pytest.raises(FollowerError, match="airsim_scene_ahead"):
        follower.beat(WorldSample(5.0, (0.0, 0.0, -25.0), 0.0))
    with pytest.raises(FollowerError, match="airsim_scene_ended"):
        follower.beat(WorldSample(299.5, (0.0, 0.0, -25.0), 0.0))
    assert log == []

    follower.publish("unavailable", "airsim_scene_ahead: waiting")
    status = WorldView(tmp_path).section()["airsim"]
    assert (status["state"], status["reason"], status["annotation"]) == (
        "unavailable",
        "airsim_scene_ahead: waiting",
        None,
    )
