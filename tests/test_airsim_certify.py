from __future__ import annotations

import copy
import json
import math
import socket
from pathlib import Path
from typing import Any

import pytest

import onr.demo.airsim_reconstruction.certify as certify_module
from onr.demo.airsim_reconstruction.certify import (
    PatientPauseError,
    _capture_sample,
    _read_ship_phases,
    _record_segmentation_probe_pose,
    _require_beat_landing,
    _sample_ship_parity,
    _verify_aircraft_pose,
    angular_error_degrees,
    build_parser,
    certification_times,
    decode_segmentation_ids,
    epoch_headroom_available,
    expected_probe_visible_ship_ids,
    expected_ship_pose,
    interpolate_heading_degrees,
    mapped_dynamic_object_ids,
    measure_ship_phase,
    parity_errors,
    parity_sample_passes,
    parse_static_instance_ids,
    pause_patiently,
    pose_within_tolerance,
    reconcile_cleanup_failure,
    resolve_lead_in_s,
    scenario_phase_if_fresh,
    segmentation_ids_valid,
    trajectory_heading_at_phase,
    trajectory_timestamp_index,
    wait_for_ship_spawns,
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


def test_parity_and_tolerance_helpers() -> None:
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

    times = certification_times(1.0, 1, 20, final_time_s=21.5)
    assert times == [0.0, 0.5, 1.0, 11.0, 21.0, 21.5]


def test_lead_in_headroom_and_timestamp_lookup() -> None:
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


def test_probe_expected_visibility_uses_fixture_time_bearing_range_and_pitch() -> None:
    probe_ned = (1090.0, -800.0, -25.0)
    scene_time_s = 277.0

    def ship_at(
        ship_id: int, bearing_degrees: float, distance_m: float
    ) -> dict[str, object]:
        radians = math.radians(bearing_degrees)
        north = probe_ned[0] + math.cos(radians) * distance_m
        east = probe_ned[1] + math.sin(radians) * distance_m
        return {
            "id": ship_id,
            "objectId": ship_id + 100,
            "pose": [
                [north * 100.0, east * 100.0, 0.0, 0.0, scene_time_s]
            ],
        }

    ships = [
        ship_at(1, 90.0, 200.0),  # dead ahead, mid-frustum
        ship_at(2, 47.0, 150.0),  # exactly on the 43-degree bearing gate
        ship_at(3, 46.9, 150.0),  # just outside the bearing gate
        ship_at(4, 90.0, 320.0),  # past the pitched top edge (~308 m)
        ship_at(5, 90.0, 12.0),  # inside the near edge below the drone
        ship_at(6, 270.0, 100.0),  # behind the camera
        ship_at(7, 90.0, 1000.1),  # past the range cap
        ship_at(8, 90.0, 300.0),  # near the top edge, still visible
    ]

    assert expected_probe_visible_ship_ids(
        ships, 217.0, 60.0, probe_ned_m=probe_ned
    ) == {101, 102, 108}


def test_certification_parser_enables_segmentation_probe_by_default() -> None:
    args = build_parser().parse_args([])

    assert args.capture_times_s == [0.0, 30.0, 60.0]
    assert args.segmentation_probe_time_s == 217.0


def test_measure_ship_phase_projection_stationary_and_z_sign() -> None:
    moving = [
        [0.0, 0.0, -100.0, 350.0, 0.0],
        [100.0, 0.0, -300.0, 10.0, 0.5],
    ]
    phase, error, speed = measure_ship_phase(moving, (1.0, 0.0, 3.0))
    assert phase == pytest.approx(0.5)
    assert error == pytest.approx(0.0)
    assert speed == pytest.approx((5.0**0.5) / 0.5)

    phase, error, _ = measure_ship_phase(moving, (0.5, 0.0, 2.0))
    assert phase == pytest.approx(0.25)
    assert error == pytest.approx(0.0)

    _, error, _ = measure_ship_phase(moving, (0.5, 1.0, 2.0))
    assert error == pytest.approx(1.0)

    stationary = [
        [0.0, 0.0, -100.0, 90.0, 0.0],
        [0.0, 0.0, -100.0, 90.0, 0.5],
    ]
    _, error, speed = measure_ship_phase(stationary, (0.0, 0.0, 1.0))
    assert error == pytest.approx(0.0)
    assert speed == pytest.approx(0.0)

    assert interpolate_heading_degrees(350.0, 10.0, 0.5) == pytest.approx(0.0)
    assert trajectory_heading_at_phase(moving, 0.25) == pytest.approx(0.0)


def test_measure_ship_phase_clamps_final_window_to_terminal_segment() -> None:
    poses = [
        [0.0, 0.0, -2500.0, 0.0, 0.0],
        [100.0, 0.0, -2500.0, 0.0, 0.5],
        [200.0, 0.0, -2500.0, 0.0, 1.0],
    ]

    phase, error, speed = measure_ship_phase(
        poses,
        (2.0, 0.0, 25.0),
        center_s=20.0,
        window_s=12.0,
    )

    assert phase == pytest.approx(1.0)
    assert error == pytest.approx(0.0)
    assert speed == pytest.approx(2.0)


def test_measure_ship_phase_terminal_nonfinite_position_returns_finite_end() -> None:
    poses = [
        [0.0, 0.0, -2500.0, 0.0, 0.0],
        [100.0, 0.0, -2500.0, 0.0, 0.5],
        [200.0, 0.0, -2500.0, 0.0, 1.0],
    ]

    phase, error, speed = measure_ship_phase(
        poses,
        (float("nan"), float("nan"), float("nan")),
        center_s=0.9,
        window_s=12.0,
    )

    assert math.isfinite(phase)
    assert phase == 1.0
    assert error == 0.0
    assert speed == 0.0


def test_measure_ship_phase_midtrajectory_window_keeps_existing_selection() -> None:
    poses = [
        [0.0, 0.0, -2500.0, 0.0, 0.0],
        [100.0, 0.0, -2500.0, 0.0, 0.5],
        [200.0, 0.0, -2500.0, 0.0, 1.0],
    ]

    phase, error, speed = measure_ship_phase(
        poses,
        (1.0, 0.0, 25.0),
        center_s=0.5,
        window_s=0.1,
    )

    assert phase == pytest.approx(0.5)
    assert error == pytest.approx(0.0)
    assert speed == pytest.approx(2.0)


def test_windowed_phase_lookup_disambiguates_self_crossing() -> None:
    poses = [
        [0.0, 0.0, 0.0, 0.0, 0.0],
        [100.0, 0.0, 0.0, 0.0, 10.0],
        [0.0, 0.0, 0.0, 0.0, 20.0],
        [0.0, 0.0, 0.0, 0.0, 90.0],
        [100.0, 0.0, 0.0, 0.0, 100.0],
        [0.0, 0.0, 0.0, 0.0, 110.0],
    ]

    phase_10, _, _ = measure_ship_phase(
        poses, (1.0, 0.0, 0.0), center_s=10.0, window_s=5.0
    )
    phase_100, _, _ = measure_ship_phase(
        poses, (1.0, 0.0, 0.0), center_s=100.0, window_s=5.0
    )

    assert phase_10 == pytest.approx(10.0)
    assert phase_100 == pytest.approx(100.0)
    with pytest.raises(ValueError, match="no trajectory segments"):
        measure_ship_phase(
            poses, (1.0, 0.0, 0.0), center_s=200.0, window_s=5.0
        )


class _PhasePosition:
    def __init__(self, x: float) -> None:
        self.x_val = x
        self.y_val = 0.0
        self.z_val = 0.0


class _PhaseOrientation:
    x_val = 0.0
    y_val = 0.0
    z_val = 0.0
    w_val = 1.0


class _PhasePose:
    def __init__(self, x: float) -> None:
        self.position = _PhasePosition(x)
        self.orientation = _PhaseOrientation()


class _PhaseClient:
    def __init__(self, positions: dict[str, float]) -> None:
        self.positions = positions

    def simGetObjectPose(self, name: str) -> _PhasePose:
        return _PhasePose(self.positions[name])


def test_read_ship_phases_uses_per_ship_centers() -> None:
    poses = [
        [0.0, 0.0, 0.0, 0.0, 0.0],
        [100.0, 0.0, 0.0, 0.0, 10.0],
        [0.0, 0.0, 0.0, 0.0, 20.0],
        [0.0, 0.0, 0.0, 0.0, 90.0],
        [100.0, 0.0, 0.0, 0.0, 100.0],
        [0.0, 0.0, 0.0, 0.0, 110.0],
    ]
    ships = [
        {"id": 1, "name": "one", "pose": poses},
        {"id": 10, "name": "ten", "pose": poses},
    ]

    phases = _read_ship_phases(
        _PhaseClient({"one": 1.0, "ten": 1.0}),
        ships,
        centers_s={"1": 10.0, "10": 100.0},
        window_s=5.0,
    )

    assert phases == pytest.approx({"1": 10.0, "10": 100.0})


def test_parity_phase_lookup_centers_on_expected_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ship = {
        "id": 1,
        "name": "one",
        "objectId": 1,
        "pose": [
            [0.0, 0.0, 0.0, 0.0, 99.5],
            [0.0, 0.0, 0.0, 0.0, 100.0],
        ],
    }
    observed: list[tuple[float | None, float | None]] = []

    def fake_measure(
        _rows: object,
        _ned: object,
        *,
        center_s: float | None = None,
        window_s: float | None = None,
    ) -> tuple[float, float, float]:
        observed.append((center_s, window_s))
        assert center_s is not None
        return center_s, 0.0, 1.0

    monkeypatch.setattr(certify_module, "measure_ship_phase", fake_measure)
    record = {
        "1": {
            "max_abs_position_error_m": 0.0,
            "max_projection_error_m": 0.0,
            "max_abs_phase_error_s": 0.0,
            "max_heading_error_deg": 0.0,
            "worst_position_time_s": None,
            "worst_position_pose_index": None,
            "worst_projection_time_s": None,
            "worst_phase_time_s": None,
            "worst_heading_time_s": None,
            "passed": True,
            "samples": [],
        }
    }

    _sample_ship_parity(
        _PhaseClient({"one": 0.0}),
        [ship],
        0.0,
        record,
        {1: trajectory_timestamp_index(ship)},
        100.0,
    )

    assert observed == [(100.0, 5.0)]


def test_phase_aware_parity_gates_moving_and_stationary_samples() -> None:
    common = {"position_error_m": 0.4, "heading_error_deg": 4.0}
    assert parity_sample_passes(
        projection_error_m=0.05,
        abs_phase_error_s=0.25,
        segment_speed_mps=1.0,
        **common,
    )
    assert not parity_sample_passes(
        projection_error_m=0.05,
        abs_phase_error_s=0.251,
        segment_speed_mps=1.0,
        **common,
    )
    assert not parity_sample_passes(
        projection_error_m=0.051,
        abs_phase_error_s=0.25,
        segment_speed_mps=1.0,
        **common,
    )
    assert parity_sample_passes(
        projection_error_m=9.0,
        abs_phase_error_s=9.0,
        segment_speed_mps=0.0,
        **common,
    )
    assert not parity_sample_passes(
        projection_error_m=0.0,
        abs_phase_error_s=0.0,
        segment_speed_mps=0.0,
        position_error_m=0.501,
        heading_error_deg=4.0,
    )


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
    assert segmentation_ids_valid(counts, mapped, {21016})
    assert not segmentation_ids_valid([1, 21, 999], mapped, {21016})


def test_parse_static_instance_ids_uses_exact_third_column(tmp_path: Path) -> None:
    table = tmp_path / "instance_object_ids.txt"
    table.write_text("Actor_A 60 21016\nActor_B 61 21019\n")

    assert parse_static_instance_ids(table) == {21016, 21019}


class _ImageVector:
    x_val = 0.0
    y_val = 0.0
    z_val = 0.0
    w_val = 1.0


class _ImageResponse:
    width = 1
    height = 1
    time_stamp = 123
    image_data_uint8 = bytes([0, 0, 0])
    camera_position = _ImageVector()
    camera_orientation = _ImageVector()


class _FakeAirSim:
    class ImageType:
        Scene = 0
        Segmentation = 5

    class ImageRequest:
        def __init__(self, *args: object) -> None:
            self.args = args


class _CaptureClient:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def simGetImages(
        self, requests: list[object], *, vehicle_name: str
    ) -> list[_ImageResponse]:
        del requests, vehicle_name
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("capture timed out")
        return [_ImageResponse(), _ImageResponse(), _ImageResponse()]


def test_capture_retries_once_then_records_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(certify_module, "_save_png", lambda *_: None)
    client = _CaptureClient(failures=1)
    sleeps: list[float] = []

    record = _capture_sample(
        client,
        _FakeAirSim,
        0.0,
        tmp_path,
        set(),
        [],
        60.0,
        60.0,
        {21016},
        {"passed": True},
        sleep=sleeps.append,
    )

    assert record["capture_attempts"] == 2
    assert client.calls == 2
    assert sleeps == [2.0]


def test_capture_raises_third_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(certify_module, "_save_png", lambda *_: None)
    client = _CaptureClient(failures=3)
    sleeps: list[float] = []

    with pytest.raises(TimeoutError, match="capture timed out"):
        _capture_sample(
            client,
            _FakeAirSim,
            0.0,
            tmp_path,
            set(),
            [],
            60.0,
            60.0,
            {21016},
            {"passed": True},
            sleep=sleeps.append,
        )

    assert client.calls == 3
    assert sleeps == [2.0, 2.0]


class _AircraftPosition:
    def __init__(self, north_m: float) -> None:
        self.x_val = north_m
        self.y_val = 0.0
        self.z_val = -25.0


class _AircraftKinematics:
    def __init__(self, north_m: float, yaw_degrees: float) -> None:
        self.position = _AircraftPosition(north_m)
        yaw_radians = math.radians(yaw_degrees)
        self.orientation = _PhaseOrientation()
        self.orientation.z_val = math.sin(yaw_radians / 2.0)
        self.orientation.w_val = math.cos(yaw_radians / 2.0)


class _AircraftState:
    def __init__(self, north_m: float, yaw_degrees: float) -> None:
        self.timestamp = 123
        self.kinematics_estimated = _AircraftKinematics(
            north_m, yaw_degrees
        )


class _AircraftClient:
    def __init__(self, north_m: float, yaw_degrees: float) -> None:
        self.state = _AircraftState(north_m, yaw_degrees)

    def getMultirotorState(self, *, vehicle_name: str) -> _AircraftState:
        del vehicle_name
        return self.state


def test_aircraft_pose_verification_passes_and_fails() -> None:
    passed = _verify_aircraft_pose(_AircraftClient(0.0, 270.0))
    failed = _verify_aircraft_pose(_AircraftClient(0.6, 270.0))

    assert passed["passed"] is True
    assert failed["passed"] is False


def test_probe_pose_failure_is_recorded_before_diagnostic_error() -> None:
    report: dict[str, Any] = {"measurements": {}}
    aircraft_pose = {
        "commanded": {"ned_m": [1090.0, -800.0, -25.0], "yaw_degrees": 90.0},
        "measured": {"position_ned_m": [1091.2, -800.0, -25.0]},
        "max_abs_position_error_m": 1.2,
        "heading_error_deg": 0.5,
        "passed": False,
    }

    with pytest.raises(RuntimeError, match=r"measured=.*1\.2.*heading_error_deg=0\.5"):
        _record_segmentation_probe_pose(report, 217.0, 277.1, aircraft_pose)

    assert report["measurements"]["segmentation_probe"] == {
        "mission_time_s": 217.0,
        "flush_step_s": 0.1,
        "measured_min_ship_phase_s": 277.1,
        "aircraft_pose": aircraft_pose,
    }


def test_bad_beat_fails_fast() -> None:
    good = {"target_phase_s": 10.0, "landing_errors_s": {"1": 0.25}}
    bad = {"target_phase_s": 10.0, "landing_errors_s": {"1": 0.251}}

    _require_beat_landing(good)
    with pytest.raises(RuntimeError, match="outside tolerance"):
        _require_beat_landing(bad)


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
