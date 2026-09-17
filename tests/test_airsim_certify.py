from __future__ import annotations

import copy
import json
import socket
from pathlib import Path
from typing import Any

import pytest

from onr.demo.airsim_reconstruction.certify import (
    PatientPauseError,
    angular_error_degrees,
    certification_times,
    decode_segmentation_ids,
    effective_mission_epoch,
    epoch_headroom_available,
    expected_ship_pose,
    image_pose_skew_s,
    mapped_dynamic_object_ids,
    parity_errors,
    pause_alignment_acceptable,
    pause_alignment_error_s,
    pause_patiently,
    pause_placement_acceptable,
    pause_placement_error_s,
    pause_window_delay_s,
    pose_within_tolerance,
    reconcile_cleanup_failure,
    resolve_lead_in_s,
    sample_ship_trajectory_phases,
    scenario_phase_if_fresh,
    segmentation_ids_valid,
    skew_within_tolerance,
    trajectory_timestamp_index,
    wait_for_ship_spawns,
    warm_up_stepped_playback,
)
from onr.demo.airsim_reconstruction.engine import (
    EngineConfigSwap,
    build_engine_settings,
    preflight_ports_free,
)


def _swap(
    tmp_path: Path, *, cooldown_s: float = 2.0
) -> tuple[EngineConfigSwap, Path, Path, bytes, bytes]:
    engine_config = tmp_path / "environment.json"
    object_ids = tmp_path / "object_ids.txt"
    instance_ids = tmp_path / "instance_object_ids.txt"
    original_config = b'{"Map":"Harbor","Keep":{"nested":true}}\n'
    original_ids = b"Actor 60 1\n"
    engine_config.write_bytes(original_config)
    object_ids.write_bytes(original_ids)
    instance_ids.write_bytes(b"Actor 60 21016\n")
    swap = EngineConfigSwap(
        engine_config,
        object_ids,
        instance_ids,
        tmp_path / "scenarios",
        tmp_path / "status",
        warmup_s=30.0,
        cooldown_s=cooldown_s,
        evidence_dir=tmp_path / "evidence",
    )
    return swap, engine_config, object_ids, original_config, original_ids


def test_engine_config_swap_round_trip(tmp_path: Path) -> None:
    swap, engine_config, object_ids, original_config, original_ids = _swap(
        tmp_path, cooldown_s=5.0
    )

    with swap:
        changed = json.loads(engine_config.read_text())
        assert changed["Map"] == "Harbor"
        assert changed["Keep"] == {"nested": True}
        assert changed["ScenarioDir"] == str(tmp_path / "scenarios")
        assert changed["StatusDir"] == str(tmp_path / "status")
        assert changed["WarmUpTime"] == 30.0
        assert changed["CoolDownTime"] == 5.0
        assert object_ids.read_bytes() == b"Actor 60 21016\n"
        assert (tmp_path / "evidence/environment.before.json").read_bytes() == original_config
        assert (tmp_path / "evidence/object_ids.before.txt").read_bytes() == original_ids

    assert engine_config.read_bytes() == original_config
    assert object_ids.read_bytes() == original_ids
    assert swap.configuration_restored is True


def test_engine_config_swap_restores_after_exception(tmp_path: Path) -> None:
    swap, engine_config, object_ids, original_config, original_ids = _swap(tmp_path)

    with pytest.raises(LookupError, match="synthetic"), swap:
        raise LookupError("synthetic")

    assert engine_config.read_bytes() == original_config
    assert object_ids.read_bytes() == original_ids
    assert swap.configuration_restored is True


def test_build_engine_settings_preserves_non_target_fields(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    output = tmp_path / "out/settings.json"
    settings = {
        "SettingsVersion": 1.2,
        "Keep": {"unchanged": [1, 2, 3]},
        "Vehicles": {
            "SimpleFlight": {
                "VehicleType": "SimpleFlight",
                "Cameras": {
                    "front_center_custom": {
                        "X": 0.4,
                        "CaptureSettings": [
                            {"ImageType": 0, "Width": 640, "Height": 480},
                            {"ImageType": 5, "Width": 640, "Height": 480},
                        ],
                    },
                    "third_person_demo": {
                        "CaptureSettings": [
                            {"ImageType": 0, "Width": 960, "Height": 540}
                        ]
                    },
                },
            }
        },
    }
    original = copy.deepcopy(settings)
    source.write_text(json.dumps(settings))

    result = build_engine_settings(
        source, output, api_port=41461, width=1920, height=1080
    )
    built = json.loads(result.read_text())

    assert result == output
    assert built["ApiServerPort"] == 41461
    assert built["Keep"] == original["Keep"]
    assert built["Vehicles"]["SimpleFlight"]["VehicleType"] == "SimpleFlight"
    assert built["Vehicles"]["SimpleFlight"]["Cameras"]["front_center_custom"]["X"] == 0.4
    captures = built["Vehicles"]["SimpleFlight"]["Cameras"]
    assert [row["ImageType"] for row in captures["front_center_custom"]["CaptureSettings"]] == [0, 5]
    assert [row["ImageType"] for row in captures["third_person_demo"]["CaptureSettings"]] == [0]
    assert all(
        row["Width"] == 1920 and row["Height"] == 1080
        for camera in captures.values()
        for row in camera["CaptureSettings"]
    )


def test_preflight_ports_free_detects_listener_and_accepts_free_port() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    try:
        with pytest.raises(RuntimeError, match=rf"Port {port} occupied"):
            preflight_ports_free([port])
    finally:
        listener.close()

    free_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    free_probe.bind(("127.0.0.1", 0))
    free_port = free_probe.getsockname()[1]
    free_probe.close()
    preflight_ports_free([free_port])


def test_parity_skew_and_tolerance_helpers() -> None:
    ship = {
        "pose": [
            [100.0, 200.0, -300.0, 359.0, 0.0],
            [150.0, 250.0, -350.0, 1.0, 0.5],
        ]
    }
    expected_ned, heading, index = expected_ship_pose(ship, 0.5)
    assert expected_ned == (1.5, 2.5, 3.5)
    assert heading == 1.0
    assert index == 1
    position_error, heading_error = parity_errors(
        (1.9, 2.4, 3.2), 359.0, expected_ned, heading
    )
    assert position_error == pytest.approx(0.4)
    assert heading_error == pytest.approx(2.0)
    assert angular_error_degrees(1.0, 359.0) == pytest.approx(2.0)
    assert pose_within_tolerance(position_error, heading_error)
    assert not pose_within_tolerance(0.5001, heading_error)

    skew = image_pose_skew_s(
        10_200_000_000,
        5.0,
        sensor_epoch_ns=5_000_000_000,
        mission_epoch_s=0.0,
    )
    assert skew == pytest.approx(0.2)
    assert skew_within_tolerance(skew)
    assert not skew_within_tolerance(-0.251)

    times = certification_times(1.0, 1, 20, final_time_s=21.5)
    assert times == [0.0, 0.5, 1.0, 11.0, 21.0, 21.5]


def test_lead_in_epoch_shift_headroom_and_timestamp_lookup() -> None:
    clock_context = {"mission_epoch_s": 1.25}
    effective_epoch = effective_mission_epoch(
        clock_context["mission_epoch_s"], 10.0
    )
    assert effective_epoch == pytest.approx(-8.75)
    assert epoch_headroom_available(8.0, 10.0)
    assert not epoch_headroom_available(8.000001, 10.0)

    ship = {
        "pose": [
            [50.0, 100.0, -150.0, 90.0, 9.5],
            [100.0, 200.0, -300.0, 180.0, 10.0],
            [150.0, 250.0, -350.0, 270.0, 10.5],
        ]
    }
    timestamp_index = trajectory_timestamp_index(ship)
    expected_ned, heading, index = expected_ship_pose(
        ship,
        0.5,
        lead_in_s=10.0,
        timestamp_index=timestamp_index,
    )
    assert expected_ned == (1.5, 2.5, 3.5)
    assert heading == 270.0
    assert index == 2

    bad_spacing = {"pose": [ship["pose"][0], [100.0, 200.0, -300.0, 180.0, 10.1]]}
    with pytest.raises(ValueError, match="spaced by 0.5"):
        trajectory_timestamp_index(bad_spacing)


def test_pause_boundary_window_and_alignment_decisions() -> None:
    assert pause_window_delay_s(1.01) == pytest.approx(0.02)
    assert pause_window_delay_s(1.03) == pytest.approx(0.0)
    assert pause_window_delay_s(1.08) == pytest.approx(0.0)
    assert pause_window_delay_s(1.09) == pytest.approx(0.44)

    assert pause_alignment_error_s(2.19) == pytest.approx(0.19)
    assert pause_alignment_error_s(2.49) == pytest.approx(0.01)
    assert pause_alignment_acceptable(2.20)
    assert not pause_alignment_acceptable(2.201)


class _TrajectoryPosition:
    def __init__(self, phase_s: float) -> None:
        self.x_val = phase_s
        self.y_val = 0.0
        self.z_val = 0.0


class _TrajectoryPose:
    def __init__(self, phase_s: float) -> None:
        self.position = _TrajectoryPosition(phase_s)


class _TrajectoryPoseClient:
    def __init__(self, phases: dict[str, float]) -> None:
        self.phases = phases

    def simGetObjectPose(self, name: str) -> _TrajectoryPose:
        return _TrajectoryPose(self.phases[name])


def test_pause_placement_verdict_uses_sampled_trajectory_phases() -> None:
    ships = [
        {"id": 1, "name": "ship-1"},
        {"id": 10, "name": "ship-10"},
        {"id": 11, "name": "ship-11"},
    ]
    client = _TrajectoryPoseClient(
        {"ship-1": 1.0, "ship-10": 1.1, "ship-11": 0.9}
    )

    samples = sample_ship_trajectory_phases(
        client,
        ships,
        lambda _ship, measured_ned: measured_ned[0],
    )

    assert [sample["id"] for sample in samples] == [1, 10, 11]
    assert pause_placement_error_s(1.03, samples) == pytest.approx(0.13)
    assert pause_placement_acceptable(1.03, samples)

    samples[-1]["trajectory_phase_s"] = 0.8
    assert pause_placement_error_s(1.03, samples) == pytest.approx(0.23)
    assert not pause_placement_acceptable(1.03, samples)


def test_segmentation_accepts_ship_passenger_and_static_ids() -> None:
    mapping = {
        "ships": {"1": {"object_id": 1}},
        "passengers": {"21": {"object_id": 21}},
    }
    mapped = mapped_dynamic_object_ids(mapping)
    pixels = bytes(
        [
            0, 0, 1,
            0, 0, 21,
            0, 82, 24,
            0, 0, 0,
        ]
    )
    counts = decode_segmentation_ids(pixels, width=2, height=2)
    assert counts[1] == 1
    assert counts[21] == 1
    assert counts[21016] == 1
    assert segmentation_ids_valid(counts, mapped)
    assert not segmentation_ids_valid([1, 21, 999], mapped)


class _PauseStub:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.attempts = 0

    def pause(self) -> None:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise TimeoutError("msgpackrpc timeout")


def test_pause_patiently_retries_transient_timeout() -> None:
    freeze = _PauseStub(failures=2)
    sleeps: list[float] = []

    attempts = pause_patiently(freeze, sleep=sleeps.append)

    assert attempts == 3
    assert freeze.attempts == 3
    assert sleeps == [5.0, 5.0]


def test_pause_patiently_raises_after_three_failures() -> None:
    freeze = _PauseStub(failures=10)

    with pytest.raises(PatientPauseError, match="after 3 attempts") as failure:
        pause_patiently(freeze, sleep=lambda _: None)

    assert failure.value.attempts == 3
    assert freeze.attempts == 3


class _StepWarmupStub:
    def __init__(self, now: list[float], wall_durations: list[float]) -> None:
        self.now = now
        self.wall_durations = iter(wall_durations)
        self.steps: list[float] = []

    def step(self, seconds: float) -> None:
        self.steps.append(seconds)
        self.now[0] += next(self.wall_durations)


def test_warm_up_stepped_playback_runs_three_measured_steps() -> None:
    now = [10.0]
    freeze = _StepWarmupStub(now, [1.8, 1.3, 1.05])

    durations = warm_up_stepped_playback(
        freeze, monotonic=lambda: now[0]
    )

    assert freeze.steps == [1.0, 1.0, 1.0]
    assert durations == pytest.approx([1.8, 1.3, 1.05])


class _SceneObjectsStub:
    def __init__(self, responses: list[set[str] | BaseException]) -> None:
        self.responses = responses
        self.calls = 0

    def simListSceneObjects(self) -> set[str]:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(response, BaseException):
            raise response
        return response


def test_wait_for_ship_spawns_partial_then_full() -> None:
    client = _SceneObjectsStub([{"ship-a"}, {"ship-a", "ship-b"}])
    now = [0.0]

    elapsed, rpc_timeouts = wait_for_ship_spawns(
        client,
        ["ship-a", "ship-b"],
        monotonic=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    assert elapsed == pytest.approx(1.0)
    assert rpc_timeouts == 0
    assert client.calls == 2


def test_wait_for_ship_spawns_tolerates_rpc_timeout_then_succeeds() -> None:
    client = _SceneObjectsStub(
        [TimeoutError("Request timed out"), {"ship-a", "ship-b"}]
    )
    now = [0.0]

    elapsed, rpc_timeouts = wait_for_ship_spawns(
        client,
        ["ship-a", "ship-b"],
        monotonic=lambda: now[0],
        sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    assert elapsed == pytest.approx(1.0)
    assert rpc_timeouts == 1
    assert client.calls == 2


def test_wait_for_ship_spawns_timeout_lists_missing_names() -> None:
    client = _SceneObjectsStub([{"ship-a"}])
    now = [0.0]

    with pytest.raises(TimeoutError, match="ship-b"):
        wait_for_ship_spawns(
            client,
            ["ship-a", "ship-b"],
            timeout_s=2.0,
            monotonic=lambda: now[0],
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )


def test_manifest_derived_lead_in_and_explicit_override(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"lead_in_seconds": 60.0}))

    assert resolve_lead_in_s(None, manifest) == 60.0
    assert resolve_lead_in_s(12.0, manifest) == 12.0

    manifest.write_text(json.dumps({"other": True}))
    with pytest.raises(ValueError, match="missing lead_in_seconds"):
        resolve_lead_in_s(None, manifest)


def test_scenario_phase_rejects_stale_metadata_and_accepts_current_launch() -> None:
    stale = {
        "scenario_initialization_time": 900.0,
        "scenario_start_time": 930.0,
    }
    fresh = {
        "scenario_initialization_time": 1000.0,
        "scenario_start_time": 1030.0,
    }

    assert (
        scenario_phase_if_fresh(
            stale, launch_wall_s=1000.0, clock_wall_s=1040.0
        )
        is None
    )
    assert scenario_phase_if_fresh(
        fresh, launch_wall_s=1000.0, clock_wall_s=1029.5
    ) == pytest.approx(-0.5)
    assert scenario_phase_if_fresh(
        fresh, launch_wall_s=1000.0, clock_wall_s=1030.25
    ) == pytest.approx(0.25)


def test_cleanup_error_does_not_mask_primary_failure() -> None:
    report: dict[str, Any] = {"measurements": {}}
    primary = ValueError("primary failure")
    cleanup = OSError("cleanup failure")

    result = reconcile_cleanup_failure(report, primary, [cleanup])

    assert result is primary
    assert report["measurements"]["cleanup_errors"] == [
        {"type": "OSError", "message": "cleanup failure"}
    ]


def test_cleanup_error_becomes_failure_without_primary() -> None:
    report: dict[str, Any] = {"measurements": {}}
    cleanup = OSError("cleanup failure")

    result = reconcile_cleanup_failure(report, None, [cleanup])

    assert isinstance(result, RuntimeError)
    assert str(result) == "live cleanup failed: OSError: cleanup failure"
    assert report["measurements"]["cleanup_errors"]
