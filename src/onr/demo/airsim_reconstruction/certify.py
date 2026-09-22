"""Certify the issue-65 AirSim fixture with stepped beats and frozen holds."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import traceback
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np

from .beats import advance_to_phase, beat_landing_passed
from .engine import EngineConfigSwap, launch_engine, preflight_ports_free

ENGINE_CONFIG = Path(
    "/data/ccu/sukaih/ONR/onr_env/Linux/Harbor5_6/"
    "EnvironmentConfigFiles/environment.json"
)
ENGINE_OBJECT_IDS = ENGINE_CONFIG.with_name("object_ids.txt")
INSTANCE_OBJECT_IDS = Path(
    "/data/ccu/sukaih/ONR/onr_scenario/map/offshore_dock_1/"
    "instance_object_ids.txt"
)
ENGINE_EXECUTABLE = Path("/data/ccu/sukaih/ONR/onr_env/Linux/Harbor5_6.sh")
DEFAULT_FIXTURE = Path("var/demo-video/mission1-20260916-airsim/fixture")
SCENARIO_NAME = "mission1-20260916"
VEHICLE_NAME = "SimpleFlight"
TICK_S = 0.5
POSITION_TOLERANCE_M = 0.5
HEADING_TOLERANCE_DEG = 5.0
PROJECTION_TOLERANCE_M = 0.05
PHASE_TOLERANCE_S = 0.25
STATIONARY_SPEED_MPS = 0.01
INITIAL_AIRCRAFT_NED_M = (0.0, 0.0, -25.0)
INITIAL_AIRCRAFT_YAW_DEGREES = 270.0
# Mission tick 434 in the v1/v2 600-tick captures: open water with ship 5
# visible at roughly 300 m, unlike the port-yard initial-pose view.
SEGMENTATION_PROBE_NED_M = (1090.0, -800.0, -25.0)
SEGMENTATION_PROBE_YAW_DEGREES = 90.0
# front_center_custom geometry (settings_airsim_issue65.json): FOV 90 horizontal
# on 1920x1080, pitched down 34 degrees to align with the world model's
# 0-300 m ground-visibility swath. Horizontal gate keeps the historical 2
# degree margin; the vertical gate is exact pinhole geometry (waterline
# depression must fall between the top and bottom frame edges).
SEGMENTATION_PROBE_HALF_FOV_DEGREES = 43.0
SEGMENTATION_PROBE_PITCH_DOWN_DEGREES = 34.0
SEGMENTATION_PROBE_VERTICAL_HALF_FOV_DEGREES = math.degrees(
    math.atan(math.tan(math.radians(90.0 / 2)) * (1080.0 / 1920.0))
)
SEGMENTATION_PROBE_MAX_DISTANCE_M = 1000.0
SEGMENTATION_PROBE_FLUSH_STEP_S = 0.1


def utc_now() -> str:
    """Return a receipt-friendly UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()  # noqa: UP017


def sha256_path(path: str | Path) -> str:
    """Return the SHA-256 digest of a file."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def angular_error_degrees(actual: float, expected: float) -> float:
    """Return the smallest absolute angular separation in degrees."""
    return abs((float(actual) - float(expected) + 180.0) % 360.0 - 180.0)


def max_abs_position_error(
    actual_ned: Sequence[float], expected_ned: Sequence[float]
) -> float:
    """Return the largest per-axis NED error in metres."""
    if len(actual_ned) != 3 or len(expected_ned) != 3:
        raise ValueError("NED positions must contain exactly three coordinates")
    return max(abs(float(actual) - float(expected)) for actual, expected in zip(actual_ned, expected_ned))


def pose_within_tolerance(
    position_error_m: float,
    heading_error_deg: float,
    *,
    position_tolerance_m: float = POSITION_TOLERANCE_M,
    heading_tolerance_deg: float = HEADING_TOLERANCE_DEG,
) -> bool:
    """Evaluate the certification position and heading tolerance gate."""
    return (
        float(position_error_m) <= float(position_tolerance_m)
        and float(heading_error_deg) <= float(heading_tolerance_deg)
    )


def trajectory_row_to_ned(row: Sequence[float]) -> tuple[float, float, float]:
    """Apply ``trajectory_phase``'s centimetre/UE-up to NED conversion."""
    if len(row) < 3:
        raise ValueError("trajectory row must contain x, y, and z")
    return float(row[0]) / 100.0, float(row[1]) / 100.0, -float(row[2]) / 100.0


def epoch_headroom_available(
    phase_at_pause_s: float, lead_in_s: float, *, margin_s: float = 2.0
) -> bool:
    """Return whether establish has enough lead-in remaining to certify its step."""
    return float(phase_at_pause_s) <= float(lead_in_s) - float(margin_s)


def resolve_lead_in_s(
    explicit_lead_in_s: float | None, manifest_path: str | Path
) -> float:
    """Resolve lead-in from an explicit override or the fixture manifest."""
    if explicit_lead_in_s is None:
        manifest = json.loads(Path(manifest_path).read_text())
        if "lead_in_seconds" not in manifest:
            raise ValueError("fixture manifest is missing lead_in_seconds")
        value = manifest["lead_in_seconds"]
    else:
        value = explicit_lead_in_s
    try:
        lead_in_s = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("lead_in_s must be a number") from exc
    if not math.isfinite(lead_in_s) or lead_in_s < 2.0:
        raise ValueError("lead_in_s must be finite and at least 2.0 seconds")
    return lead_in_s


def scenario_phase_if_fresh(
    scenario_times: Mapping[str, Any],
    *,
    launch_wall_s: float,
    clock_wall_s: float,
    stale_tolerance_s: float = 1.0,
) -> float | None:
    """Return scene phase only for metadata initialized by the current launch."""
    initialization_s = float(scenario_times["scenario_initialization_time"])
    if initialization_s < float(launch_wall_s) - float(stale_tolerance_s):
        return None
    return float(clock_wall_s) - float(scenario_times["scenario_start_time"])


def trajectory_timestamp_index(
    ship: Mapping[str, Any], *, tick_s: float = TICK_S
) -> dict[float, int]:
    """Index trajectory rows by timestamp after asserting exact tick spacing."""
    poses = ship["pose"]
    if not poses:
        raise ValueError("ship trajectory must contain at least one pose")
    timestamps = [float(row[4]) for row in poses]
    if any(not math.isfinite(timestamp) for timestamp in timestamps):
        raise ValueError("ship trajectory timestamps must be finite")
    if any(
        not math.isclose(current - previous, tick_s, rel_tol=0.0, abs_tol=1e-9)
        for previous, current in pairwise(timestamps)
    ):
        raise ValueError(f"ship trajectory timestamps must be spaced by {tick_s} s")
    index = {_time_key(timestamp): row_index for row_index, timestamp in enumerate(timestamps)}
    if len(index) != len(poses):
        raise ValueError("ship trajectory timestamps must be unique")
    return index


def expected_ship_pose(
    ship: Mapping[str, Any],
    mission_time_s: float,
    *,
    lead_in_s: float = 0.0,
    timestamp_index: Mapping[float, int] | None = None,
    tick_s: float = TICK_S,
) -> tuple[tuple[float, float, float], float, int]:
    """Return the fixture pose at the lead-in-shifted canonical timestamp."""
    timestamp_index = timestamp_index or trajectory_timestamp_index(ship, tick_s=tick_s)
    scene_time_s = _time_key(float(lead_in_s) + float(mission_time_s))
    try:
        index = timestamp_index[scene_time_s]
    except KeyError as exc:
        raise IndexError(
            f"scene time {scene_time_s} is outside the trajectory timestamps"
        ) from exc
    poses = ship["pose"]
    row = poses[index]
    return trajectory_row_to_ned(row), float(row[3]) % 360.0, index


def expected_probe_visible_ship_ids(
    ships: Sequence[Mapping[str, Any]],
    mission_time_s: float,
    lead_in_s: float,
    *,
    probe_ned_m: Sequence[float] = SEGMENTATION_PROBE_NED_M,
    probe_yaw_degrees: float = SEGMENTATION_PROBE_YAW_DEGREES,
    half_fov_degrees: float = SEGMENTATION_PROBE_HALF_FOV_DEGREES,
    max_distance_m: float = SEGMENTATION_PROBE_MAX_DISTANCE_M,
    pitch_down_degrees: float = SEGMENTATION_PROBE_PITCH_DOWN_DEGREES,
    vertical_half_fov_degrees: float = SEGMENTATION_PROBE_VERTICAL_HALF_FOV_DEGREES,
    timestamp_indexes: Mapping[int, Mapping[float, int]] | None = None,
) -> set[int]:
    """Return fixture ship object IDs expected in the pointed probe frustum."""

    if len(probe_ned_m) != 3:
        raise ValueError("probe NED pose must contain exactly three coordinates")
    top_edge_depression = pitch_down_degrees - vertical_half_fov_degrees
    bottom_edge_depression = pitch_down_degrees + vertical_half_fov_degrees
    expected: set[int] = set()
    for ship in ships:
        ship_id = int(ship["id"])
        timestamp_index = (
            timestamp_indexes[ship_id] if timestamp_indexes is not None else None
        )
        ship_ned, _heading, _row = expected_ship_pose(
            ship,
            mission_time_s,
            lead_in_s=lead_in_s,
            timestamp_index=timestamp_index,
        )
        delta = tuple(
            float(ship_ned[axis]) - float(probe_ned_m[axis]) for axis in range(3)
        )
        distance_m = math.sqrt(sum(value * value for value in delta))
        ground_range_m = math.hypot(delta[0], delta[1])
        # Waterline depression below the horizon; the pitched frustum only
        # covers depressions between its top and bottom frame edges.
        depression_degrees = math.degrees(math.atan2(delta[2], ground_range_m))
        bearing_degrees = math.degrees(math.atan2(delta[1], delta[0])) % 360.0
        bearing_error = angular_error_degrees(bearing_degrees, probe_yaw_degrees)
        in_range = distance_m <= max_distance_m or math.isclose(
            distance_m, max_distance_m, abs_tol=1e-9
        )
        in_fov = bearing_error <= half_fov_degrees or math.isclose(
            bearing_error, half_fov_degrees, abs_tol=1e-9
        )
        in_vertical = top_edge_depression <= depression_degrees <= (
            bottom_edge_depression
        )
        if in_range and in_fov and in_vertical:
            expected.add(int(ship["objectId"]))
    return expected


def measure_ship_phase(
    pose_rows: Sequence[Sequence[float]],
    measured_ned: Sequence[float],
    *,
    center_s: float | None = None,
    window_s: float | None = None,
) -> tuple[float, float, float]:
    """Project a measured NED pose onto a trajectory and return phase/error/speed."""
    poses = np.asarray(pose_rows, dtype=float)
    if poses.ndim != 2 or poses.shape[0] < 2 or poses.shape[1] < 5:
        raise ValueError("trajectory requires at least two five-value pose rows")
    xyz = poses[:, :3] / 100.0
    xyz[:, 2] *= -1.0
    delta = np.diff(xyz, axis=0)
    measured = np.asarray(measured_ned, dtype=float)
    if (
        center_s is not None
        and float(center_s) >= float(poses[-1, 4]) - TICK_S
        and not np.all(np.isfinite(measured))
    ):
        # The engine reports non-finite object coordinates once a ship is parked
        # at the finite trajectory endpoint. Its phase is still the final time.
        return float(poses[-1, 4]), 0.0, 0.0
    denominator = np.maximum(np.sum(delta * delta, axis=1), 1e-12)
    fraction = np.clip(
        np.sum((measured - xyz[:-1]) * delta, axis=1) / denominator,
        0.0,
        1.0,
    )
    projected = xyz[:-1] + fraction[:, None] * delta
    errors = np.linalg.norm(projected - measured, axis=1)
    times = poses[:, 4]
    candidate_indices = np.arange(delta.shape[0])
    if center_s is not None and window_s is not None:
        center = float(center_s)
        window = float(window_s)
        low, high = center - window, center + window
        segment_low = np.minimum(times[:-1], times[1:])
        segment_high = np.maximum(times[:-1], times[1:])
        candidate_indices = np.flatnonzero(
            (segment_high >= low) & (segment_low <= high)
        )
        if candidate_indices.size == 0:
            # A finite fixture can be parked at its final row while the phase
            # reader's rolling center has advanced beyond the trajectory end.
            # Preserve normal window selection; only this empty-window fallback
            # intersects the request with the available trajectory interval.
            available_low = float(np.min(times))
            available_high = float(np.max(times))
            clamped_low = max(low, available_low)
            clamped_high = min(high, available_high)
            if clamped_low <= clamped_high:
                candidate_indices = np.flatnonzero(
                    (segment_high >= clamped_low) & (segment_low <= clamped_high)
                )
            else:
                boundary = (
                    available_high if low > available_high else available_low
                )
                endpoint = xyz[-1] if low > available_high else xyz[0]
                endpoint_error_m = float(np.linalg.norm(measured - endpoint))
                if endpoint_error_m > POSITION_TOLERANCE_M:
                    raise ValueError(
                        "no trajectory segments within phase window or parked "
                        f"endpoint tolerance: endpoint_error_m={endpoint_error_m:.6f}"
                    )
                candidate_indices = np.flatnonzero(
                    (segment_low <= boundary) & (segment_high >= boundary)
                )
            if candidate_indices.size == 0:
                raise ValueError(
                    "no trajectory segments within phase window or its "
                    f"available interval [{available_low}, {available_high}]"
                )
    index = int(candidate_indices[np.argmin(errors[candidate_indices])])
    duration_s = abs(float(times[index + 1] - times[index]))
    if duration_s <= 1e-12:
        raise ValueError("trajectory segment timestamps must increase")
    phase_s = float(
        times[index] + fraction[index] * (times[index + 1] - times[index])
    )
    projection_error_m = float(errors[index])
    segment_speed_mps = float(np.linalg.norm(delta[index]) / duration_s)
    return phase_s, projection_error_m, segment_speed_mps


def interpolate_heading_degrees(start: float, end: float, fraction: float) -> float:
    """Interpolate headings over the shortest wrapped angular path."""
    delta = (float(end) - float(start) + 180.0) % 360.0 - 180.0
    return (float(start) + float(fraction) * delta) % 360.0


def trajectory_heading_at_phase(
    pose_rows: Sequence[Sequence[float]], phase_s: float
) -> float:
    """Interpolate canonical heading between rows bracketing a scene phase."""
    if len(pose_rows) < 2:
        raise ValueError("trajectory requires at least two pose rows")
    times = [float(row[4]) for row in pose_rows]
    phase = float(phase_s)
    if phase <= times[0]:
        index, fraction = 0, 0.0
    elif phase >= times[-1]:
        index, fraction = len(times) - 2, 1.0
    else:
        index = int(np.searchsorted(times, phase, side="right")) - 1
        duration = times[index + 1] - times[index]
        if duration <= 0.0:
            raise ValueError("trajectory timestamps must increase")
        fraction = (phase - times[index]) / duration
    return interpolate_heading_degrees(
        float(pose_rows[index][3]),
        float(pose_rows[index + 1][3]),
        fraction,
    )


def parity_sample_passes(
    *,
    projection_error_m: float,
    abs_phase_error_s: float,
    segment_speed_mps: float,
    position_error_m: float,
    heading_error_deg: float,
) -> bool:
    """Apply moving-segment or stationary-segment ship parity gates."""
    heading_passed = heading_error_deg <= HEADING_TOLERANCE_DEG
    if segment_speed_mps < STATIONARY_SPEED_MPS:
        return heading_passed and position_error_m <= POSITION_TOLERANCE_M
    return (
        heading_passed
        and projection_error_m <= PROJECTION_TOLERANCE_M
        and abs_phase_error_s <= PHASE_TOLERANCE_S
    )


def parity_errors(
    actual_ned: Sequence[float],
    actual_heading_deg: float,
    expected_ned: Sequence[float],
    expected_heading_deg: float,
) -> tuple[float, float]:
    """Return max-axis position error and wrapped heading error."""
    return (
        max_abs_position_error(actual_ned, expected_ned),
        angular_error_degrees(actual_heading_deg, expected_heading_deg),
    )


def decode_segmentation_ids(
    image_data: bytes | bytearray | Sequence[int], width: int, height: int
) -> Counter[int]:
    """Decode Harbor's 24-bit ``ch0<<16|ch1<<8|ch2`` instance IDs."""
    raw = np.frombuffer(bytes(image_data), dtype=np.uint8)
    pixels = int(width) * int(height)
    if pixels <= 0 or raw.size % pixels != 0:
        raise ValueError("segmentation buffer does not match its dimensions")
    channels = raw.size // pixels
    if channels < 3:
        raise ValueError("segmentation buffer must have at least three channels")
    rgb = raw.reshape(int(height), int(width), channels)[:, :, :3].astype(np.uint32)
    decoded = (rgb[:, :, 0] << 16) | (rgb[:, :, 1] << 8) | rgb[:, :, 2]
    values, counts = np.unique(decoded, return_counts=True)
    return Counter(
        {int(value): int(count) for value, count in zip(values, counts)}
    )


def segmentation_ids_valid(
    decoded_ids: Iterable[int],
    mapped_object_ids: Iterable[int],
    static_object_ids: Iterable[int],
) -> bool:
    """Accept only background plus explicitly configured dynamic/static IDs."""
    valid = (
        {0}
        | {int(value) for value in mapped_object_ids}
        | {int(value) for value in static_object_ids}
    )
    return all(int(value) in valid for value in decoded_ids)


def parse_static_instance_ids(path: str | Path) -> set[int]:
    """Parse ``actor thermal_id instance_id`` rows from an instance table."""
    static_ids: set[int] = set()
    for line_number, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 3:
            raise ValueError(f"invalid instance table row {line_number}")
        try:
            static_ids.add(int(fields[2]))
        except ValueError as exc:
            raise ValueError(f"invalid instance ID on row {line_number}") from exc
    if not static_ids:
        raise ValueError("instance table contains no IDs")
    return static_ids


def certification_times(
    dense_end_s: float,
    dense_step_ticks: int,
    sparse_step_ticks: int,
    *,
    final_time_s: float = 299.5,
    tick_s: float = TICK_S,
) -> list[float]:
    """Build monotonic dense, sparse, and final parity boundaries."""
    if dense_end_s < 0 or dense_end_s > final_time_s:
        raise ValueError("dense_end_s must be within the certification interval")
    if dense_step_ticks <= 0 or sparse_step_ticks <= 0:
        raise ValueError("step ticks must be positive")
    final_tick = round(final_time_s / tick_s)
    dense_end_tick = min(round(dense_end_s / tick_s), final_tick)
    ticks = set(range(0, dense_end_tick + 1, dense_step_ticks))
    ticks.add(dense_end_tick)
    ticks.update(
        range(dense_end_tick + sparse_step_ticks, final_tick + 1, sparse_step_ticks)
    )
    ticks.add(final_tick)
    return [tick * tick_s for tick in sorted(ticks)]


def mapped_dynamic_object_ids(mapping: Mapping[str, Any]) -> set[int]:
    """Collect mapped ship, passenger, and static-fixture object IDs."""
    result: set[int] = set()
    for section_name in ("ships", "passengers", "static_objects"):
        section = mapping.get(section_name, {})
        rows = section.values() if isinstance(section, Mapping) else section
        for row in rows:
            result.add(int(row["object_id"]))
    return result


def _yaw_degrees(quaternion: Any) -> float:
    siny = 2.0 * (
        float(quaternion.w_val) * float(quaternion.z_val)
        + float(quaternion.x_val) * float(quaternion.y_val)
    )
    cosy = 1.0 - 2.0 * (
        float(quaternion.y_val) ** 2 + float(quaternion.z_val) ** 2
    )
    return math.degrees(math.atan2(siny, cosy)) % 360.0


def _vector(vector: Any) -> list[float]:
    return [float(vector.x_val), float(vector.y_val), float(vector.z_val)]


def _pose_record(position: Any, orientation: Any) -> dict[str, Any]:
    return {
        "ned_m": _vector(position),
        "quaternion_xyzw": [
            float(orientation.x_val),
            float(orientation.y_val),
            float(orientation.z_val),
            float(orientation.w_val),
        ],
        "yaw_degrees": _yaw_degrees(orientation),
    }


def _time_key(value: float) -> float:
    return round(float(value), 9)


def _display_time(value: float) -> str:
    return f"{float(value):g}"


def _progress(report: dict[str, Any], stage: str, detail: str = "") -> None:
    event = {"timestamp": utc_now(), "stage": stage, "detail": detail}
    report["progress"].append(event)
    print(f"[{stage}] {detail}".rstrip(), flush=True)


def _validate_fixture(fixture: Path) -> dict[str, Path]:
    mapping_path = fixture / "mapping.json"
    if mapping_path.is_file():
        mapping_scenario = json.loads(mapping_path.read_text()).get(
            "scenario_name", SCENARIO_NAME
        )
    else:
        mapping_scenario = SCENARIO_NAME
    scenario = fixture / "scenarios" / str(mapping_scenario)
    ships = scenario / "ships"
    required = {
        "mapping": mapping_path,
        "manifest": fixture / "manifest.json",
        "settings": fixture / "engine" / "settings_airsim_issue65.json",
    }
    missing = [str(path) for path in required.values() if not path.is_file()]
    missing.extend(
        str(ships / f"{ship_id}.json")
        for ship_id in range(1, 21)
        if not (ships / f"{ship_id}.json").is_file()
    )
    if missing:
        raise FileNotFoundError("Missing fixture files: " + ", ".join(missing))
    return {**required, "scenario": scenario, "ships": ships}


@contextmanager
def _temporary_environment(overrides: Mapping[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _wait_for_rpc(process: Any, port: int, timeout_s: float = 120.0) -> Any:
    import airsim

    deadline = time.monotonic() + timeout_s
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("engine exited during startup; see engine.log")
        try:
            client = airsim.MultirotorClient(port=port, timeout_value=2)
            if client.ping():
                return client
        except Exception as exc:  # noqa: BLE001 - RPC libraries raise broad errors
            last_error = exc
        time.sleep(1.0)
    raise TimeoutError(f"AirSim RPC did not become ready: {last_error}")


def _wait_for_active_scenario(
    path: Path,
    process: Any,
    clock: Any,
    launch_wall_s: float,
    timeout_s: float = 120.0,
) -> tuple[dict[str, Any], bytes]:
    """Wait for scenario metadata and for the unfrozen clock to reach its start."""
    deadline = time.monotonic() + timeout_s
    last_error: BaseException | None = None
    stale_reported = False
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("engine exited before scenario_times.json was available")
        try:
            content = path.read_bytes()
            scenario_times = json.loads(content)
            phase_s = scenario_phase_if_fresh(
                scenario_times,
                launch_wall_s=launch_wall_s,
                clock_wall_s=clock.time(),
            )
            if phase_s is None:
                if not stale_reported:
                    print(
                        "Ignoring stale scenario_times.json from a previous launch",
                        flush=True,
                    )
                    stale_reported = True
                time.sleep(0.2)
                continue
            if phase_s >= 0.0:
                return scenario_times, content
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.2)
    raise TimeoutError(f"active scenario did not become ready: {last_error}")


def wait_for_ship_spawns(
    client: Any,
    expected_names: Iterable[str],
    *,
    timeout_s: float = 180.0,
    poll_interval_s: float = 1.0,
    monotonic: Any = time.monotonic,
    sleep: Any = time.sleep,
) -> tuple[float, int]:
    """Wait for all ships, tolerating RPC blockage within the overall deadline."""
    expected = {str(name) for name in expected_names}
    started = monotonic()
    deadline = started + float(timeout_s)
    missing = set(expected)
    rpc_timeouts = 0
    while True:
        if monotonic() >= deadline:
            missing_names = ", ".join(sorted(missing))
            raise TimeoutError(f"ships did not spawn; missing: {missing_names}")
        try:
            present = set(client.simListSceneObjects())
        except Exception as exc:
            rpc_timeouts += 1
            now = monotonic()
            if now >= deadline:
                missing_names = ", ".join(sorted(missing))
                raise TimeoutError(
                    "ships did not spawn before the deadline; "
                    f"missing: {missing_names}; RPC timeouts: {rpc_timeouts}"
                ) from exc
            sleep(min(float(poll_interval_s), max(0.0, deadline - now)))
            continue
        missing = expected - present
        if not missing:
            return float(monotonic() - started), rpc_timeouts
        now = monotonic()
        if now >= deadline:
            missing_names = ", ".join(sorted(missing))
            raise TimeoutError(f"ships did not spawn; missing: {missing_names}")
        sleep(min(float(poll_interval_s), max(0.0, deadline - now)))


class PatientPauseError(RuntimeError):
    """Initial AirSim pause failed after all patient-client retries."""

    def __init__(self, attempts: int, last_error: BaseException) -> None:
        self.attempts = attempts
        super().__init__(
            f"initial pause failed after {attempts} attempts: "
            f"{type(last_error).__name__}: {last_error}"
        )


def pause_patiently(
    freeze: Any,
    *,
    max_attempts: int = 3,
    retry_delay_s: float = 5.0,
    sleep: Any = time.sleep,
) -> int:
    """Pause with bounded retries for transient RPC timeouts during spawning."""
    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")
    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            freeze.pause()
        except Exception as exc:  # noqa: BLE001 - msgpackrpc exceptions vary
            last_error = exc
            if attempt < max_attempts:
                sleep(float(retry_delay_s))
        else:
            return attempt
    assert last_error is not None
    raise PatientPauseError(max_attempts, last_error) from last_error


def reconcile_cleanup_failure(
    report: dict[str, Any],
    primary_error: BaseException | None,
    cleanup_errors: Sequence[BaseException],
) -> BaseException | None:
    """Record cleanup failures while preserving an already-propagating failure."""
    if cleanup_errors:
        summaries = [
            {"type": type(error).__name__, "message": str(error)}
            for error in cleanup_errors
        ]
        report.setdefault("measurements", {}).setdefault("cleanup_errors", []).extend(
            summaries
        )
    if primary_error is not None:
        return primary_error
    if cleanup_errors:
        detail = "; ".join(
            f"{type(error).__name__}: {error}" for error in cleanup_errors
        )
        return RuntimeError(f"live cleanup failed: {detail}")
    return None


def _load_ships(ships_directory: Path) -> list[dict[str, Any]]:
    ships = [
        json.loads((ships_directory / f"{ship_id}.json").read_text())
        for ship_id in range(1, 21)
    ]
    if {int(ship["id"]) for ship in ships} != set(range(1, 21)):
        raise ValueError("fixture must contain ship IDs 1 through 20 exactly once")
    return ships


def _load_verification_ships(ships_directory: Path) -> list[dict[str, Any]]:
    ships = [
        json.loads(path.read_text())
        for path in sorted(ships_directory.glob("*.json"))
        if path.stem.isdigit()
    ][:3]
    if [int(ship["id"]) for ship in ships] != [1, 10, 11]:
        raise ValueError("verification ships must be 1, 10, and 11")
    return ships


def _read_ship_phases(
    client: Any,
    ships: Sequence[Mapping[str, Any]],
    *,
    centers_s: Mapping[str, float] | None = None,
    window_s: float | None = None,
) -> dict[str, float]:
    phases: dict[str, float] = {}
    for ship in ships:
        ship_id = str(int(ship["id"]))
        position = client.simGetObjectPose(ship["name"]).position
        center_s = centers_s.get(ship_id) if centers_s is not None else None
        phase_s, _, _ = measure_ship_phase(
            ship["pose"],
            [position.x_val, position.y_val, position.z_val],
            center_s=center_s,
            window_s=window_s,
        )
        phases[ship_id] = phase_s
    return phases


def _sample_ship_parity(
    client: Any,
    ships: Sequence[Mapping[str, Any]],
    mission_time_s: float,
    per_ship: dict[str, dict[str, Any]],
    timestamp_indexes: Mapping[int, Mapping[float, int]],
    lead_in_s: float,
) -> None:
    expected_phase_s = float(lead_in_s) + float(mission_time_s)
    for ship in ships:
        expected_ned, _, index = expected_ship_pose(
            ship,
            mission_time_s,
            lead_in_s=lead_in_s,
            timestamp_index=timestamp_indexes[int(ship["id"])],
        )
        pose = client.simGetObjectPose(ship["name"])
        actual_ned = _vector(pose.position)
        actual_heading = _yaw_degrees(pose.orientation)
        measured_phase_s, projection_error_m, segment_speed_mps = (
            measure_ship_phase(
                ship["pose"],
                actual_ned,
                center_s=expected_phase_s,
                window_s=5.0,
            )
        )
        # Heading is phase-compensated: compare against the trajectory heading
        # at the ship's MEASURED phase, not the nominal phase. At ~50 deg/s
        # turn rates a 0.25 s phase jitter would otherwise produce spurious
        # ~12 deg heading errors (the position check is already phase-free
        # via the projection metric).
        expected_heading = trajectory_heading_at_phase(ship["pose"], measured_phase_s)
        position_error, heading_error = parity_errors(
            actual_ned, actual_heading, expected_ned, expected_heading
        )
        abs_phase_error_s = abs(measured_phase_s - expected_phase_s)
        sample_passed = parity_sample_passes(
            projection_error_m=projection_error_m,
            abs_phase_error_s=abs_phase_error_s,
            segment_speed_mps=segment_speed_mps,
            position_error_m=position_error,
            heading_error_deg=heading_error,
        )
        record = per_ship[str(int(ship["id"]))]
        if position_error > record["max_abs_position_error_m"]:
            record["max_abs_position_error_m"] = position_error
            record["worst_position_time_s"] = mission_time_s
            record["worst_position_pose_index"] = index
        if projection_error_m > record["max_projection_error_m"]:
            record["max_projection_error_m"] = projection_error_m
            record["worst_projection_time_s"] = mission_time_s
        if abs_phase_error_s > record["max_abs_phase_error_s"]:
            record["max_abs_phase_error_s"] = abs_phase_error_s
            record["worst_phase_time_s"] = mission_time_s
        if heading_error > record["max_heading_error_deg"]:
            record["max_heading_error_deg"] = heading_error
            record["worst_heading_time_s"] = mission_time_s
        record["passed"] = bool(record["passed"] and sample_passed)
        record["samples"].append(
            {
                "mission_time_s": mission_time_s,
                "expected_phase_s": expected_phase_s,
                "measured_phase_s": measured_phase_s,
                "projection_error_m": projection_error_m,
                "abs_phase_error_s": abs_phase_error_s,
                "segment_speed_mps": segment_speed_mps,
                "position_error_m": position_error,
                "heading_error_deg": heading_error,
                "stationary": segment_speed_mps < STATIONARY_SPEED_MPS,
                "passed": sample_passed,
            }
        )


def _sample_aircraft(
    client: Any,
    mission_time_s: float,
    *,
    expected_ned: Sequence[float] = INITIAL_AIRCRAFT_NED_M,
    expected_yaw_degrees: float = INITIAL_AIRCRAFT_YAW_DEGREES,
) -> dict[str, Any]:
    state = client.getMultirotorState(vehicle_name=VEHICLE_NAME)
    kinematics = state.kinematics_estimated
    actual_ned = _vector(kinematics.position)
    actual_heading = _yaw_degrees(kinematics.orientation)
    expected = [float(value) for value in expected_ned]
    position_error, heading_error = parity_errors(
        actual_ned, actual_heading, expected, expected_yaw_degrees
    )
    return {
        "mission_time_s": mission_time_s,
        "sensor_timestamp_ns": int(state.timestamp),
        "expected_ned_m": expected,
        "expected_yaw_degrees": expected_yaw_degrees,
        "actual": _pose_record(kinematics.position, kinematics.orientation),
        "max_abs_position_error_m": position_error,
        "heading_error_deg": heading_error,
        "passed": pose_within_tolerance(position_error, heading_error),
    }


def _verify_aircraft_pose(
    client: Any,
    ned_m: Sequence[float] = INITIAL_AIRCRAFT_NED_M,
    yaw_degrees: float = INITIAL_AIRCRAFT_YAW_DEGREES,
) -> dict[str, Any]:
    """Measure the frozen aircraft against a commanded pose."""

    if len(ned_m) != 3:
        raise ValueError("aircraft NED pose must contain exactly three coordinates")
    state = client.getMultirotorState(vehicle_name=VEHICLE_NAME)
    kinematics = state.kinematics_estimated
    measured_ned = _vector(kinematics.position)
    measured_heading = _yaw_degrees(kinematics.orientation)
    position_error, heading_error = parity_errors(
        measured_ned, measured_heading, ned_m, yaw_degrees
    )
    return {
        "commanded": {
            "ned_m": [float(value) for value in ned_m],
            "yaw_degrees": float(yaw_degrees),
        },
        "measured": _pose_record(
            kinematics.position, kinematics.orientation
        ),
        "sensor_timestamp_ns": int(state.timestamp),
        "max_abs_position_error_m": position_error,
        "heading_error_deg": heading_error,
        "passed": pose_within_tolerance(position_error, heading_error),
    }


def _place_aircraft_pose(
    client: Any, ned_m: Sequence[float], yaw_degrees: float
) -> None:
    """Teleport the uncontrolled aircraft to a frozen NED pose."""

    import airsim

    if len(ned_m) != 3:
        raise ValueError("aircraft NED pose must contain exactly three coordinates")
    client.enableApiControl(True, vehicle_name=VEHICLE_NAME)
    client.simSetVehiclePose(
        airsim.Pose(
            airsim.Vector3r(*(float(value) for value in ned_m)),
            airsim.to_quaternion(0.0, 0.0, math.radians(yaw_degrees)),
        ),
        True,
        vehicle_name=VEHICLE_NAME,
    )


def _place_aircraft_initial_pose(
    client: Any,
    ned_m: tuple[float, float, float] = INITIAL_AIRCRAFT_NED_M,
    yaw_degrees: float = INITIAL_AIRCRAFT_YAW_DEGREES,
) -> None:
    """Teleport the uncontrolled aircraft to the canonical initial pose."""

    _place_aircraft_pose(client, ned_m, yaw_degrees)


def _record_segmentation_probe_pose(
    report: dict[str, Any],
    mission_time_s: float,
    measured_min_ship_phase_s: float,
    aircraft_pose: Mapping[str, Any],
) -> None:
    """Persist probe pose diagnostics before enforcing pose tolerance."""

    record = {
        "mission_time_s": float(mission_time_s),
        "flush_step_s": SEGMENTATION_PROBE_FLUSH_STEP_S,
        "measured_min_ship_phase_s": float(measured_min_ship_phase_s),
        "aircraft_pose": dict(aircraft_pose),
    }
    report.setdefault("measurements", {})["segmentation_probe"] = record
    if not aircraft_pose["passed"]:
        raise RuntimeError(
            "segmentation probe aircraft pose verification failed: "
            f"mission_time_s={mission_time_s}, "
            f"commanded={aircraft_pose.get('commanded')}, "
            f"measured={aircraft_pose.get('measured')}, "
            f"max_abs_position_error_m="
            f"{aircraft_pose.get('max_abs_position_error_m')}, "
            f"heading_error_deg={aircraft_pose.get('heading_error_deg')}"
        )


def _save_png(response: Any, destination: Path) -> None:
    from PIL import Image

    width, height = int(response.width), int(response.height)
    raw = np.frombuffer(bytes(response.image_data_uint8), dtype=np.uint8)
    pixels = width * height
    if pixels <= 0 or raw.size % pixels != 0:
        raise ValueError("image buffer does not match its dimensions")
    channels = raw.size // pixels
    if channels < 3:
        raise ValueError("PNG image must have at least three channels")
    rgb = raw.reshape(height, width, channels)[:, :, :3]
    destination.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb, mode="RGB").save(destination)


def _capture_sample(
    client: Any,
    airsim: Any,
    mission_time_s: float,
    samples_directory: Path,
    mapped_ids: set[int],
    ships: Sequence[Mapping[str, Any]],
    lead_in_s: float,
    measured_min_ship_phase_s: float,
    static_ids: set[int],
    aircraft_pose: Mapping[str, Any],
    *,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    requests = [
        airsim.ImageRequest("front_center_custom", airsim.ImageType.Scene, False, False),
        airsim.ImageRequest(
            "front_center_custom", airsim.ImageType.Segmentation, False, False
        ),
        airsim.ImageRequest("third_person_demo", airsim.ImageType.Scene, False, False),
    ]
    host_started_ns = time.time_ns()
    capture_attempts = 0
    responses: Sequence[Any] | None = None
    for capture_attempts in range(1, 4):
        try:
            responses = client.simGetImages(
                requests, vehicle_name=VEHICLE_NAME
            )
        except Exception:
            if capture_attempts >= 3:
                raise
            sleep(2.0)
        else:
            break
    assert responses is not None
    host_received_ns = time.time_ns()
    if len(responses) != 3:
        raise RuntimeError(f"expected three image responses, received {len(responses)}")

    labels = ("front-rgb", "front-seg", "third-rgb")
    response_records: list[dict[str, Any]] = []
    dimensions_passed = True
    segmentation_counts: Counter[int] | None = None
    timestamps = [int(response.time_stamp) for response in responses]
    timestamps_identical = len(set(timestamps)) == 1
    for label, response in zip(labels, responses):
        width, height = int(response.width), int(response.height)
        dimensions_passed = dimensions_passed and (width, height) == (1920, 1080)
        output_path = samples_directory / (
            f"{_display_time(mission_time_s)}s_{label}.png"
        )
        _save_png(response, output_path)
        response_records.append(
            {
                "kind": label,
                "time_stamp_ns": int(response.time_stamp),
                "width": width,
                "height": height,
                "camera_pose": _pose_record(
                    response.camera_position, response.camera_orientation
                ),
                "path": str(output_path),
            }
        )
        if label == "front-seg":
            segmentation_counts = decode_segmentation_ids(
                response.image_data_uint8, width, height
            )

    if segmentation_counts is None:
        raise RuntimeError("segmentation response was not decoded")
    nonzero_ids = sorted(value for value in segmentation_counts if value != 0)
    valid_ids = {0} | set(mapped_ids) | set(static_ids)
    unknown_ids = sorted(
        value for value in segmentation_counts if value not in valid_ids
    )
    ship_counts = {
        str(object_id): int(segmentation_counts.get(object_id, 0))
        for object_id in range(1, 21)
    }
    expected_phase_s = float(lead_in_s) + float(mission_time_s)
    visible_ship_phase_offsets = []
    for ship in ships:
        object_id = int(ship["objectId"])
        pixel_count = int(segmentation_counts.get(object_id, 0))
        if pixel_count <= 0:
            continue
        position = client.simGetObjectPose(ship["name"]).position
        measured_ned = _vector(position)
        measured_phase_s, projection_error_m, segment_speed_mps = (
            measure_ship_phase(
                ship["pose"],
                measured_ned,
                center_s=expected_phase_s,
                window_s=5.0,
            )
        )
        visible_ship_phase_offsets.append(
            {
                "id": int(ship["id"]),
                "object_id": object_id,
                "pixel_count": pixel_count,
                "expected_phase_s": expected_phase_s,
                "measured_phase_s": measured_phase_s,
                "phase_offset_s": measured_phase_s - expected_phase_s,
                "projection_error_m": projection_error_m,
                "segment_speed_mps": segment_speed_mps,
            }
        )
    return {
        "mission_time_s": mission_time_s,
        "host_request_started_ns": host_started_ns,
        "host_received_ns": host_received_ns,
        "host_received_utc": utc_now(),
        "capture_attempts": capture_attempts,
        "sensor_timestamp_ns": timestamps[0],
        "measured_min_ship_phase_s": measured_min_ship_phase_s,
        "aircraft_pose": dict(aircraft_pose),
        "responses": response_records,
        "dimensions_passed": dimensions_passed,
        "timestamps_identical": timestamps_identical,
        "ship_pixel_counts": ship_counts,
        "visible_ship_phase_offsets": visible_ship_phase_offsets,
        "decoded_nonzero_ids": nonzero_ids,
        "unknown_segmentation_ids": unknown_ids,
        "decoded_ids_valid": segmentation_ids_valid(
            segmentation_counts, mapped_ids, static_ids
        ),
    }


def _require_beat_landing(landing: Mapping[str, Any]) -> None:
    """Fail before sampling any beat outside the landing tolerance."""
    if not beat_landing_passed(landing["landing_errors_s"]):
        raise RuntimeError(
            "beat landed outside tolerance: "
            f"target={landing['target_phase_s']}, "
            f"errors={landing['landing_errors_s']}"
        )


def _coverage(
    scenario_times: Mapping[str, Any], lead_in_s: float
) -> tuple[float, bool]:
    start = float(scenario_times["scenario_start_time"])
    end = float(scenario_times["scenario_end_time"])
    coverage_s = end - start
    return coverage_s, coverage_s >= float(lead_in_s) + 299.4


def _write_receipt(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def run_certification(args: argparse.Namespace) -> dict[str, Any]:
    """Run one live certification and always emit a receipt."""
    fixture = Path(args.fixture).resolve()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    receipt_path = output / "certification-receipt.json"
    report: dict[str, Any] = {
        "passed": False,
        "started_at": utc_now(),
        "finished_at": None,
        "failure": None,
        "progress": [],
        "checks": {
            "preflight_passed": False,
            "rpc_ready": False,
            "initial_pause_acknowledged": False,
            "ships_spawned": False,
            "epoch_headroom": False,
            "coverage_passed": False,
            "hold_passed": False,
            "beats_landed_passed": True,
            "final_beat_passed": False,
            "parity_passed": False,
            "aircraft_initial_pose_passed": False,
            "captures_passed": False,
            "segmentation_passed": False,
            "configuration_restored": False,
        },
        "measurements": {},
        "provenance": {
            "argv": list(sys.argv),
            "fixture": str(fixture),
            "output": str(output),
            "ports": {"airsim": int(args.airsim_port), "standard_airsim": 41451},
            "cuda_device": 2,
            "settings_sha256": None,
            "fixture_manifest_sha256": None,
            "engine_executable": str(ENGINE_EXECUTABLE),
        },
    }
    process = None
    launch_clock = None
    initial_freeze = None
    swap: EngineConfigSwap | None = None
    try:
        lead_in_s = resolve_lead_in_s(
            args.lead_in_s, fixture / "manifest.json"
        )
        report["measurements"]["lead_in_s"] = lead_in_s
        report["measurements"]["phase_lookup_window_s"] = {
            "landing": 12.0,
            "parity": 5.0,
        }
        _progress(report, "preflight", "checking ports and fixture files")
        preflight_ports_free({int(args.airsim_port), 41451})
        paths = _validate_fixture(fixture)
        report["provenance"]["settings_sha256"] = sha256_path(paths["settings"])
        report["provenance"]["fixture_manifest_sha256"] = sha256_path(
            paths["manifest"]
        )
        report["checks"]["preflight_passed"] = True

        mapping = json.loads(paths["mapping"].read_text())
        mapped_ids = mapped_dynamic_object_ids(mapping)
        static_ids = parse_static_instance_ids(INSTANCE_OBJECT_IDS)
        ships = _load_ships(paths["ships"])
        verification_ships = _load_verification_ships(paths["ships"])
        timestamp_indexes = {
            int(ship["id"]): trajectory_timestamp_index(ship) for ship in ships
        }
        status_dir = output / "status"
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
            evidence_dir=output / "engine-config",
        )
        with swap:
            cleanup_errors: list[BaseException] = []
            try:
                _progress(report, "engine", "building freeze shim and launching")
                from onr_physical_runtime.sim.experimental_freeze import (
                    EngineClock,
                    FullFreeze,
                    build_shim,
                )

                shim_directory = output / "shim"
                library = build_shim(shim_directory)
                state_path = shim_directory / f"clock-{time.time_ns()}.bin"
                launch_clock = EngineClock(state_path, create=True)
                clock_environment = launch_clock.environment(library)
                overrides = {
                    "ONR_FREEZE_STATE": clock_environment["ONR_FREEZE_STATE"],
                    "LD_PRELOAD": clock_environment["LD_PRELOAD"],
                }
                launch_started = time.monotonic()
                with _temporary_environment(overrides):
                    launch_wall_s = time.time()
                    process = launch_engine(
                        ENGINE_EXECUTABLE,
                        paths["settings"],
                        output / "engine.log",
                        cuda_device=2,
                        # Vulkan ignores SDL_HINT_CUDA_DEVICE; without the
                        # explicit adapter the engine renders on GPU 0, where
                        # the resident vLLM allocation leaves ~4 GB and the
                        # first 1080p capture OOMs the Vulkan RHI (attempts
                        # 9-10). Probe4 captures passed on GPU 2 (24 GB free).
                        extra_args=("-graphicsadapter=2",),
                    )
                report["provenance"]["engine_command"] = [
                    str(ENGINE_EXECUTABLE),
                    "-vulkan",
                    "-RenderOffscreen",
                    "-ResX=640",
                    "-ResY=480",
                    "-windowed",
                    "-graphicsadapter=2",
                    f"-settings={paths['settings']}",
                ]

                _progress(report, "rpc", "waiting up to 120 seconds")
                client = _wait_for_rpc(process, int(args.airsim_port))
                report["checks"]["rpc_ready"] = True
                _progress(
                    report,
                    "scenario",
                    "waiting unfrozen for scenario start before pausing",
                )
                scenario_times, scenario_times_bytes = _wait_for_active_scenario(
                    scenario_times_path, process, launch_clock, launch_wall_s
                )
                import airsim

                patient_client = airsim.MultirotorClient(
                    port=int(args.airsim_port), timeout_value=60
                )
                _progress(
                    report,
                    "ships",
                    "waiting for all fixture ships to spawn before pausing",
                )
                spawn_wait_s, spawn_wait_rpc_timeouts = wait_for_ship_spawns(
                    patient_client,
                    (ship["name"] for ship in ships),
                    timeout_s=180.0,
                    poll_interval_s=1.0,
                )
                report["measurements"]["spawn_wait_s"] = spawn_wait_s
                report["measurements"]["spawn_wait_rpc_timeouts"] = (
                    spawn_wait_rpc_timeouts
                )
                report["checks"]["ships_spawned"] = True
                initial_freeze = FullFreeze(launch_clock, patient_client)
                scenario_start_time_s = float(
                    scenario_times["scenario_start_time"]
                )
                pause_attempts = pause_patiently(
                    initial_freeze, max_attempts=3, retry_delay_s=5.0
                )
                pause_ack = time.monotonic()
                paused_clock_status = launch_clock.status()
                phase_at_pause_s = (
                    float(paused_clock_status["frozen_ns"]) / 1e9
                    - scenario_start_time_s
                )
                report["measurements"]["pause_attempts"] = pause_attempts
                report["measurements"]["clock_at_initial_pause"] = (
                    paused_clock_status
                )
                report["measurements"]["phase_at_pause_s"] = phase_at_pause_s
                report["checks"]["initial_pause_acknowledged"] = True
                report["measurements"]["launch_to_pause_ack_s"] = (
                    pause_ack - launch_started
                )
                report["checks"]["epoch_headroom"] = epoch_headroom_available(
                    phase_at_pause_s, lead_in_s
                )
                _progress(
                    report,
                    "freeze",
                    f"initial pause acknowledged at scene phase {phase_at_pause_s:.3f} s",
                )
                if not report["checks"]["epoch_headroom"]:
                    raise RuntimeError(
                        "insufficient lead-in headroom at initial pause: "
                        f"phase_at_pause_s={phase_at_pause_s:.6f}, "
                        f"required<={lead_in_s - 2.0:.6f}"
                    )
                client = patient_client

                def read_verification_state(
                    centers_s: Mapping[str, float] | None = None,
                    window_s: float | None = None,
                ) -> dict[str, Any]:
                    state = client.getMultirotorState(vehicle_name=VEHICLE_NAME)
                    return {
                        "sensor_ns": int(state.timestamp),
                        "phases": _read_ship_phases(
                            client,
                            verification_ships,
                            centers_s=centers_s,
                            window_s=window_s,
                        ),
                    }

                _progress(report, "hold", "verifying five-second frozen hold")
                hold_before = read_verification_state()
                time.sleep(5.0)
                hold_after = read_verification_state(
                    hold_before["phases"], 5.0
                )
                hold_ship_deltas = {
                    ship_id: hold_after["phases"][ship_id] - phase
                    for ship_id, phase in hold_before["phases"].items()
                }
                hold_sensor_delta_ns = (
                    hold_after["sensor_ns"] - hold_before["sensor_ns"]
                )
                report["measurements"]["hold_check"] = {
                    "before": hold_before,
                    "after": hold_after,
                    "sensor_delta_ns": hold_sensor_delta_ns,
                    "ship_phase_deltas_s": hold_ship_deltas,
                }
                report["checks"]["hold_passed"] = (
                    hold_sensor_delta_ns == 0
                    and all(delta == 0.0 for delta in hold_ship_deltas.values())
                )
                if not report["checks"]["hold_passed"]:
                    raise RuntimeError("frozen hold changed sensor or ship phase")

                _progress(report, "aircraft", "placing canonical initial pose")
                _place_aircraft_initial_pose(
                    client, args.initial_aircraft_ned, args.initial_aircraft_yaw
                )
                (output / "scenario_times.engine.json").write_bytes(
                    scenario_times_bytes
                )
                report["measurements"]["scenario_times"] = scenario_times
                coverage_s, coverage_passed = _coverage(scenario_times, lead_in_s)
                report["measurements"]["coverage_s"] = coverage_s
                report["checks"]["coverage_passed"] = coverage_passed
                _progress(
                    report,
                    "coverage",
                    f"scenario coverage is {coverage_s:.3f} seconds",
                )
                capture_epoch = {
                    "lead_in_s": lead_in_s,
                    "phase_at_pause_s": phase_at_pause_s,
                    "scenario_times": scenario_times,
                    "model": "stepped-beat-v4",
                }
                (output / "capture-epoch.json").write_text(
                    json.dumps(capture_epoch, indent=2) + "\n"
                )

                parity_times = certification_times(
                    float(args.dense_end_s),
                    int(args.dense_step_ticks),
                    int(args.sparse_step_ticks),
                    final_time_s=float(args.final_beat_time_s),
                )
                capture_times = sorted({_time_key(value) for value in args.capture_times_s})
                probe_time = float(args.segmentation_probe_time_s)
                if not math.isfinite(probe_time):
                    raise ValueError("segmentation probe time must be finite")
                probe_time_key = _time_key(probe_time) if probe_time >= 0.0 else None
                probe_times = set() if probe_time_key is None else {probe_time_key}
                all_times = sorted(
                    {_time_key(value) for value in parity_times}
                    | set(capture_times)
                    | probe_times
                )
                parity_time_keys = {_time_key(value) for value in parity_times}
                capture_time_keys = set(capture_times)
                per_ship = {
                    str(int(ship["id"])): {
                        "name": ship["name"],
                        "object_id": int(ship["objectId"]),
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
                    for ship in ships
                }
                captures: list[dict[str, Any]] = []
                probe_sample: dict[str, Any] | None = None
                probe_expected_ids: list[int] = []
                aircraft: dict[str, Any] | None = None
                beat_landings: list[dict[str, Any]] = []
                report["measurements"]["beat_landings"] = beat_landings
                phase_centers = dict(hold_after["phases"])

                def read_beat_phases() -> dict[str, float]:
                    phases = _read_ship_phases(
                        client,
                        verification_ships,
                        centers_s=phase_centers,
                        window_s=12.0,
                    )
                    phase_centers.update(phases)
                    return phases

                _progress(
                    report,
                    "sampling",
                    f"{len(parity_times)} parity boundaries and {len(capture_times)} captures",
                )
                for mission_time_s in all_times:
                    target_phase_s = float(lead_in_s) + mission_time_s
                    landing = advance_to_phase(
                        initial_freeze,
                        read_beat_phases,
                        target_phase_s,
                    )
                    landing_passed = beat_landing_passed(
                        landing["landing_errors_s"]
                    )
                    landing["mission_time_s"] = mission_time_s
                    landing["passed"] = landing_passed
                    beat_landings.append(landing)
                    report["checks"]["beats_landed_passed"] = bool(
                        report["checks"]["beats_landed_passed"]
                        and landing_passed
                    )
                    if mission_time_s == float(args.final_beat_time_s):
                        report["checks"]["final_beat_passed"] = landing_passed
                    _require_beat_landing(landing)
                    if mission_time_s == 0.0:
                        _place_aircraft_initial_pose(
                            client,
                            args.initial_aircraft_ned,
                            args.initial_aircraft_yaw,
                        )
                        aircraft_pose = _verify_aircraft_pose(
                            client,
                            ned_m=args.initial_aircraft_ned,
                            yaw_degrees=args.initial_aircraft_yaw,
                        )
                        report["measurements"]["aircraft_pose_at_t0"] = (
                            aircraft_pose
                        )
                        if not aircraft_pose["passed"]:
                            raise RuntimeError(
                                "aircraft pose verification failed at mission t=0"
                            )
                        aircraft = _sample_aircraft(
                            client,
                            mission_time_s,
                            expected_ned=args.initial_aircraft_ned,
                            expected_yaw_degrees=args.initial_aircraft_yaw,
                        )
                    if mission_time_s in parity_time_keys:
                        _sample_ship_parity(
                            client,
                            ships,
                            mission_time_s,
                            per_ship,
                            timestamp_indexes,
                            lead_in_s,
                        )
                    if mission_time_s == probe_time_key:
                        _place_aircraft_pose(
                            client,
                            tuple(args.segmentation_probe_ned),
                            args.segmentation_probe_yaw,
                        )
                        # Render one fresh frame after teleport while bounding drift.
                        initial_freeze.step(SEGMENTATION_PROBE_FLUSH_STEP_S)
                        measured_min_ship_phase_s = min(
                            read_beat_phases().values()
                        )
                        probe_aircraft_pose = _verify_aircraft_pose(
                            client,
                            ned_m=tuple(args.segmentation_probe_ned),
                            yaw_degrees=args.segmentation_probe_yaw,
                        )
                        _record_segmentation_probe_pose(
                            report,
                            mission_time_s,
                            measured_min_ship_phase_s,
                            probe_aircraft_pose,
                        )
                        probe_expected_ids = sorted(
                            expected_probe_visible_ship_ids(
                                ships,
                                mission_time_s,
                                lead_in_s,
                                probe_ned_m=tuple(args.segmentation_probe_ned),
                                probe_yaw_degrees=args.segmentation_probe_yaw,
                                timestamp_indexes=timestamp_indexes,
                            )
                        )
                        if not probe_expected_ids:
                            raise RuntimeError(
                                "segmentation probe pose is stale: fixture has no ships "
                                f"within {SEGMENTATION_PROBE_HALF_FOV_DEGREES:g} degrees "
                                f"bearing, the {SEGMENTATION_PROBE_PITCH_DOWN_DEGREES:g}-degree "
                                "pitch frustum, and "
                                f"{SEGMENTATION_PROBE_MAX_DISTANCE_M:g} m at "
                                f"mission_time_s={mission_time_s}"
                            )
                        probe_sample = _capture_sample(
                            client,
                            airsim,
                            mission_time_s,
                            output / "samples",
                            mapped_ids,
                            ships,
                            lead_in_s,
                            measured_min_ship_phase_s,
                            static_ids,
                            probe_aircraft_pose,
                        )
                        probe_sample.update(
                            {
                                "probe": True,
                                "probe_pose": {
                                    "ned_m": list(args.segmentation_probe_ned),
                                    "yaw_degrees": args.segmentation_probe_yaw,
                                },
                                "expected_visible_ship_ids": probe_expected_ids,
                            }
                        )
                        captures.append(probe_sample)
                    if mission_time_s in capture_time_keys:
                        _place_aircraft_initial_pose(
                            client,
                            args.initial_aircraft_ned,
                            args.initial_aircraft_yaw,
                        )
                        aircraft_pose = _verify_aircraft_pose(
                            client,
                            ned_m=args.initial_aircraft_ned,
                            yaw_degrees=args.initial_aircraft_yaw,
                        )
                        if not aircraft_pose["passed"]:
                            raise RuntimeError(
                                "aircraft pose verification failed before capture: "
                                f"mission_time_s={mission_time_s}"
                            )
                        measured_min_ship_phase_s = min(
                            read_beat_phases().values()
                        )
                        captures.append(
                            _capture_sample(
                                client,
                                airsim,
                                mission_time_s,
                                output / "samples",
                                mapped_ids,
                                ships,
                                lead_in_s,
                                measured_min_ship_phase_s,
                                static_ids,
                                aircraft_pose,
                            )
                        )

                if aircraft is None:
                    raise RuntimeError("aircraft t=0 sample was not requested")
                report["measurements"]["aircraft_initial_pose"] = aircraft
                report["checks"]["aircraft_initial_pose_passed"] = bool(
                    aircraft["passed"]
                )
                parity_position_max = max(
                    row["max_abs_position_error_m"] for row in per_ship.values()
                )
                parity_projection_max = max(
                    row["max_projection_error_m"] for row in per_ship.values()
                )
                parity_phase_max = max(
                    row["max_abs_phase_error_s"] for row in per_ship.values()
                )
                parity_heading_max = max(
                    row["max_heading_error_deg"] for row in per_ship.values()
                )
                parity_passed = all(
                    bool(row["passed"]) for row in per_ship.values()
                )
                report["measurements"]["ship_parity"] = {
                    "sample_times_s": parity_times,
                    "per_ship": per_ship,
                    "max_abs_position_error_m": parity_position_max,
                    "max_projection_error_m": parity_projection_max,
                    "max_abs_phase_error_s": parity_phase_max,
                    "max_heading_error_deg": parity_heading_max,
                }
                report["checks"]["parity_passed"] = parity_passed
                report["measurements"]["captures"] = captures
                report["checks"]["captures_passed"] = bool(captures) and all(
                    sample["dimensions_passed"]
                    and sample["timestamps_identical"]
                    for sample in captures
                )
                ship_visible = any(
                    any(count > 0 for count in sample["ship_pixel_counts"].values())
                    for sample in captures
                )
                decoded_ids_valid = all(
                    sample["decoded_ids_valid"] for sample in captures
                )
                probe_enabled = probe_time_key is not None
                probe_expectation_passed = (
                    probe_sample is not None
                    and any(
                        int(probe_sample["ship_pixel_counts"].get(str(ship_id), 0))
                        > 0
                        for ship_id in probe_expected_ids
                    )
                )
                report["measurements"]["segmentation"] = {
                    "at_least_one_ship_visible": ship_visible,
                    "all_nonzero_ids_valid": decoded_ids_valid,
                    "mapped_dynamic_object_ids": sorted(mapped_ids),
                    "probe": {
                        "enabled": probe_enabled,
                        "mission_time_s": probe_time_key,
                        "pose": {
                            "ned_m": list(args.segmentation_probe_ned),
                            "yaw_degrees": args.segmentation_probe_yaw,
                        },
                        "expected_visible_ship_ids": probe_expected_ids,
                        "passed": (
                            probe_expectation_passed if probe_enabled else None
                        ),
                    },
                }
                report["checks"]["segmentation_passed"] = (
                    ship_visible
                    and decoded_ids_valid
                    and (probe_expectation_passed if probe_enabled else True)
                )
            finally:
                primary_error = sys.exc_info()[1]
                _progress(report, "teardown", "freezing and stopping the engine")
                if initial_freeze is not None:
                    try:
                        initial_freeze.pause()
                    except Exception as exc:  # noqa: BLE001 - cleanup must continue
                        cleanup_errors.append(exc)
                if process is not None:
                    try:
                        from onr_physical_runtime.sim.processes import (
                            stop_process_group,
                        )

                        stop_process_group(process)
                    except Exception as exc:  # noqa: BLE001 - cleanup must continue
                        cleanup_errors.append(exc)
                    process = None
                if launch_clock is not None:
                    try:
                        launch_clock.close()
                    except Exception as exc:  # noqa: BLE001 - cleanup must continue
                        cleanup_errors.append(exc)
                    launch_clock = None
                cleanup_failure = reconcile_cleanup_failure(
                    report, primary_error, cleanup_errors
                )
                if primary_error is None and cleanup_failure is not None:
                    raise cleanup_failure
        report["checks"]["configuration_restored"] = swap.configuration_restored
    except Exception as exc:  # noqa: BLE001 - receipt records every live failure
        report["failure"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
    finally:
        if process is not None:
            try:
                from onr_physical_runtime.sim.processes import (
                    stop_process_group,
                )

                stop_process_group(process)
            except Exception as exc:  # noqa: BLE001 - receipt records cleanup failure
                if report["failure"] is None:
                    report["failure"] = {
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    }
            process = None
        if launch_clock is not None:
            try:
                launch_clock.close()
            except Exception as exc:  # noqa: BLE001 - receipt records cleanup failure
                if report["failure"] is None:
                    report["failure"] = {
                        "type": type(exc).__name__,
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    }
        if swap is not None:
            report["checks"]["configuration_restored"] = (
                swap.configuration_restored
            )
        required_checks = tuple(report["checks"].values())
        report["passed"] = report["failure"] is None and all(required_checks)
        report["finished_at"] = utc_now()
        _write_receipt(receipt_path, report)
        print(f"Certification receipt: {receipt_path}", flush=True)
    return report


def build_parser() -> argparse.ArgumentParser:
    """Build the certification CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--airsim-port", type=int, default=41461)
    parser.add_argument(
        "--lead-in-s",
        type=float,
        default=None,
        help="lead-in seconds; defaults to manifest.json lead_in_seconds",
    )
    parser.add_argument("--dense-end-s", type=float, default=60.0)
    parser.add_argument("--dense-step-ticks", type=int, default=1)
    parser.add_argument("--sparse-step-ticks", type=int, default=20)
    parser.add_argument(
        "--capture-times-s", type=float, nargs="+", default=[0.0, 30.0, 60.0]
    )
    parser.add_argument("--segmentation-probe-time-s", type=float, default=217.0)
    parser.add_argument(
        "--initial-aircraft-ned",
        type=float,
        nargs=3,
        default=list(INITIAL_AIRCRAFT_NED_M),
    )
    parser.add_argument(
        "--initial-aircraft-yaw",
        type=float,
        default=INITIAL_AIRCRAFT_YAW_DEGREES,
    )
    parser.add_argument(
        "--segmentation-probe-ned",
        type=float,
        nargs=3,
        default=list(SEGMENTATION_PROBE_NED_M),
    )
    parser.add_argument(
        "--segmentation-probe-yaw",
        type=float,
        default=SEGMENTATION_PROBE_YAW_DEGREES,
    )
    parser.add_argument(
        "--final-beat-time-s",
        type=float,
        default=299.5,
        help="mission time of the certification's final beat landing",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.output is None:
        args.output = Path(args.fixture) / "certification"
    report = run_certification(args)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
