"""Bounded live certification for the issue-65 AirSim reconstruction fixture."""

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
SKEW_TOLERANCE_S = 0.25
# Pause shortly after a discrete 0.5 s trajectory-row transition. The 0.08 s
# window ceiling leaves 0.12 s for pause RPC latency before the 0.20 s gate.
PAUSE_WINDOW_START_S = 0.03
PAUSE_WINDOW_END_S = 0.08
PAUSE_WINDOW_POLL_S = 0.005
PAUSE_ALIGNMENT_TOLERANCE_S = 0.20
PAUSE_ALIGNMENT_MAX_ATTEMPTS = 6
PHASE_COMPARISON_EPSILON_S = 1e-9


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


def image_pose_skew_s(
    image_timestamp_ns: int,
    prepared_mission_time_s: float,
    *,
    sensor_epoch_ns: int,
    mission_epoch_s: float,
) -> float:
    """Map an image sensor timestamp to mission time and return signed skew."""
    image_mission_time_s = float(mission_epoch_s) + (
        int(image_timestamp_ns) - int(sensor_epoch_ns)
    ) / 1e9
    return image_mission_time_s - float(prepared_mission_time_s)


def skew_within_tolerance(
    skew_s: float, tolerance_s: float = SKEW_TOLERANCE_S
) -> bool:
    """Return whether a signed image/pose skew is within its absolute bound."""
    return abs(float(skew_s)) <= float(tolerance_s)


def trajectory_row_to_ned(row: Sequence[float]) -> tuple[float, float, float]:
    """Apply ``trajectory_phase``'s centimetre/UE-up to NED conversion."""
    if len(row) < 3:
        raise ValueError("trajectory row must contain x, y, and z")
    return float(row[0]) / 100.0, float(row[1]) / 100.0, -float(row[2]) / 100.0


def effective_mission_epoch(epoch_raw_s: float, lead_in_s: float) -> float:
    """Translate the scene-clock epoch from lead-in time to mission time."""
    return float(epoch_raw_s) - float(lead_in_s)


def epoch_headroom_available(
    phase_at_pause_s: float, lead_in_s: float, *, margin_s: float = 2.0
) -> bool:
    """Return whether establish has enough lead-in remaining to certify its step."""
    return float(phase_at_pause_s) <= float(lead_in_s) - float(margin_s)


def pause_window_delay_s(
    scene_phase_s: float,
    *,
    tick_s: float = TICK_S,
    window_start_s: float = PAUSE_WINDOW_START_S,
    window_end_s: float = PAUSE_WINDOW_END_S,
) -> float:
    """Return seconds until the current or next post-tick pause window."""
    phase = float(scene_phase_s)
    if not math.isfinite(phase) or phase < 0.0:
        raise ValueError("scene phase must be finite and non-negative")
    if not 0.0 <= window_start_s <= window_end_s < tick_s:
        raise ValueError("pause window must lie within one positive tick")
    offset = phase % tick_s
    if offset < window_start_s - PHASE_COMPARISON_EPSILON_S:
        return window_start_s - offset
    if offset <= window_end_s + PHASE_COMPARISON_EPSILON_S:
        return 0.0
    return tick_s - offset + window_start_s


def pause_alignment_error_s(
    scene_phase_s: float, *, tick_s: float = TICK_S
) -> float:
    """Return distance from scene phase to the nearest trajectory-row boundary."""
    phase = float(scene_phase_s)
    if not math.isfinite(phase) or phase < 0.0:
        raise ValueError("scene phase must be finite and non-negative")
    offset = phase % tick_s
    return min(offset, tick_s - offset)


def pause_alignment_acceptable(
    scene_phase_s: float,
    *,
    tolerance_s: float = PAUSE_ALIGNMENT_TOLERANCE_S,
    tick_s: float = TICK_S,
) -> bool:
    """Return whether a frozen phase is close enough to a trajectory row."""
    return (
        pause_alignment_error_s(scene_phase_s, tick_s=tick_s)
        <= tolerance_s + PHASE_COMPARISON_EPSILON_S
    )


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
    decoded_ids: Iterable[int], mapped_object_ids: Iterable[int]
) -> bool:
    """Accept mapped dynamic IDs and the reserved static-instance range."""
    mapped = {int(value) for value in mapped_object_ids}
    return all(
        int(value) == 0 or int(value) in mapped or int(value) >= 21016
        for value in decoded_ids
    )


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
    """Collect mapped ship and passenger object IDs."""
    result: set[int] = set()
    for section_name in ("ships", "passengers"):
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
    scenario = fixture / "scenarios" / SCENARIO_NAME
    ships = scenario / "ships"
    required = {
        "mapping": fixture / "mapping.json",
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
    path: Path, process: Any, clock: Any, timeout_s: float = 120.0
) -> tuple[dict[str, Any], bytes]:
    """Wait for scenario metadata and for the unfrozen clock to reach its start."""
    deadline = time.monotonic() + timeout_s
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("engine exited before scenario_times.json was available")
        try:
            content = path.read_bytes()
            scenario_times = json.loads(content)
            phase_s = clock.time() - float(scenario_times["scenario_start_time"])
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
) -> float:
    """Wait until every expected fixture ship is present in the AirSim scene."""
    expected = {str(name) for name in expected_names}
    started = monotonic()
    deadline = started + float(timeout_s)
    missing = set(expected)
    while True:
        present = set(client.simListSceneObjects())
        missing = expected - present
        if not missing:
            return float(monotonic() - started)
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


def wait_for_pause_window(
    clock: Any,
    scenario_start_time_s: float,
    lead_in_s: float,
    *,
    poll_interval_s: float = PAUSE_WINDOW_POLL_S,
    sleep: Any = time.sleep,
) -> float:
    """Wait unfrozen for a post-row-boundary pause window with headroom."""
    while True:
        phase_s = clock.time() - float(scenario_start_time_s)
        if not epoch_headroom_available(phase_s, lead_in_s):
            raise RuntimeError(
                "insufficient lead-in headroom while waiting for pause alignment: "
                f"phase_s={phase_s:.6f}, required<={lead_in_s - 2.0:.6f}"
            )
        delay_s = pause_window_delay_s(phase_s)
        if delay_s == 0.0:
            return phase_s
        sleep(min(float(poll_interval_s), delay_s))


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


def _sample_ship_parity(
    client: Any,
    ships: Sequence[Mapping[str, Any]],
    mission_time_s: float,
    per_ship: dict[str, dict[str, Any]],
    timestamp_indexes: Mapping[int, Mapping[float, int]],
    lead_in_s: float,
) -> None:
    for ship in ships:
        expected_ned, expected_heading, index = expected_ship_pose(
            ship,
            mission_time_s,
            lead_in_s=lead_in_s,
            timestamp_index=timestamp_indexes[int(ship["id"])],
        )
        pose = client.simGetObjectPose(ship["name"])
        actual_ned = _vector(pose.position)
        actual_heading = _yaw_degrees(pose.orientation)
        position_error, heading_error = parity_errors(
            actual_ned, actual_heading, expected_ned, expected_heading
        )
        record = per_ship[str(int(ship["id"]))]
        record["max_abs_position_error_m"] = max(
            record["max_abs_position_error_m"], position_error
        )
        record["max_heading_error_deg"] = max(
            record["max_heading_error_deg"], heading_error
        )
        if position_error >= record["max_abs_position_error_m"]:
            record["worst_position_time_s"] = mission_time_s
            record["worst_position_pose_index"] = index
        if heading_error >= record["max_heading_error_deg"]:
            record["worst_heading_time_s"] = mission_time_s


def _sample_aircraft(client: Any, mission_time_s: float) -> dict[str, Any]:
    state = client.getMultirotorState(vehicle_name=VEHICLE_NAME)
    kinematics = state.kinematics_estimated
    actual_ned = _vector(kinematics.position)
    actual_heading = _yaw_degrees(kinematics.orientation)
    expected_ned = [0.0, 0.0, -25.0]
    position_error, heading_error = parity_errors(
        actual_ned, actual_heading, expected_ned, 270.0
    )
    return {
        "mission_time_s": mission_time_s,
        "sensor_timestamp_ns": int(state.timestamp),
        "expected_ned_m": expected_ned,
        "expected_yaw_degrees": 270.0,
        "actual": _pose_record(kinematics.position, kinematics.orientation),
        "max_abs_position_error_m": position_error,
        "heading_error_deg": heading_error,
        "passed": pose_within_tolerance(position_error, heading_error),
    }


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
    clock_context: Mapping[str, Any],
    samples_directory: Path,
    mapped_ids: set[int],
) -> dict[str, Any]:
    requests = [
        airsim.ImageRequest("front_center_custom", airsim.ImageType.Scene, False, False),
        airsim.ImageRequest(
            "front_center_custom", airsim.ImageType.Segmentation, False, False
        ),
        airsim.ImageRequest("third_person_demo", airsim.ImageType.Scene, False, False),
    ]
    host_started_ns = time.time_ns()
    responses = client.simGetImages(requests, vehicle_name=VEHICLE_NAME)
    host_received_ns = time.time_ns()
    if len(responses) != 3:
        raise RuntimeError(f"expected three image responses, received {len(responses)}")

    labels = ("front-rgb", "front-seg", "third-rgb")
    response_records: list[dict[str, Any]] = []
    dimensions_passed = True
    skew_passed = True
    segmentation_counts: Counter[int] | None = None
    for label, response in zip(labels, responses):
        width, height = int(response.width), int(response.height)
        dimensions_passed = dimensions_passed and (width, height) == (1920, 1080)
        skew_s = image_pose_skew_s(
            int(response.time_stamp),
            mission_time_s,
            sensor_epoch_ns=int(clock_context["sensor_epoch_ns"]),
            mission_epoch_s=float(clock_context["mission_epoch_s"]),
        )
        skew_passed = skew_passed and skew_within_tolerance(skew_s)
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
                "image_pose_skew_s": skew_s,
                "skew_passed": skew_within_tolerance(skew_s),
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
    ship_counts = {
        str(object_id): int(segmentation_counts.get(object_id, 0))
        for object_id in range(1, 21)
    }
    return {
        "mission_time_s": mission_time_s,
        "host_request_started_ns": host_started_ns,
        "host_received_ns": host_received_ns,
        "host_received_utc": utc_now(),
        "responses": response_records,
        "dimensions_passed": dimensions_passed,
        "skew_passed": skew_passed,
        "ship_pixel_counts": ship_counts,
        "decoded_nonzero_ids": nonzero_ids,
        "decoded_ids_valid": segmentation_ids_valid(nonzero_ids, mapped_ids),
    }


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
            "epoch_headroom": False,
            "ships_spawned": False,
            "scene_clock_connected": False,
            "epoch_covers_zero": False,
            "parity_passed": False,
            "aircraft_initial_pose_passed": False,
            "captures_passed": False,
            "segmentation_passed": False,
            "coverage_passed": False,
            "final_prepare_passed": False,
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
    owner = None
    initial_freeze = None
    swap: EngineConfigSwap | None = None
    try:
        lead_in_s = resolve_lead_in_s(
            args.lead_in_s, fixture / "manifest.json"
        )
        report["measurements"]["lead_in_s"] = lead_in_s
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
        ships = _load_ships(paths["ships"])
        timestamp_indexes = {
            int(ship["id"]): trajectory_timestamp_index(ship) for ship in ships
        }
        status_dir = output / "status"
        scenario_times_path = status_dir / SCENARIO_NAME / "scenario_times.json"
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
                    process = launch_engine(
                        ENGINE_EXECUTABLE,
                        paths["settings"],
                        output / "engine.log",
                        cuda_device=2,
                    )
                report["provenance"]["engine_command"] = [
                    str(ENGINE_EXECUTABLE),
                    "-vulkan",
                    "-RenderOffscreen",
                    "-ResX=640",
                    "-ResY=480",
                    "-windowed",
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
                    scenario_times_path, process, launch_clock
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
                spawn_wait_s = wait_for_ship_spawns(
                    patient_client,
                    (ship["name"] for ship in ships),
                    timeout_s=180.0,
                    poll_interval_s=1.0,
                )
                report["measurements"]["spawn_wait_s"] = spawn_wait_s
                report["checks"]["ships_spawned"] = True
                initial_freeze = FullFreeze(launch_clock, patient_client)
                pause_alignment_attempts: list[dict[str, Any]] = []
                report["measurements"]["pause_alignment_attempts"] = (
                    pause_alignment_attempts
                )
                pause_rpc_attempts = 0
                scenario_start_time_s = float(
                    scenario_times["scenario_start_time"]
                )
                paused_clock_status: Mapping[str, Any] | None = None
                phase_at_pause_s = float("nan")
                alignment_error_s = float("inf")
                for alignment_attempt in range(
                    1, PAUSE_ALIGNMENT_MAX_ATTEMPTS + 1
                ):
                    phase_at_window_s = wait_for_pause_window(
                        launch_clock, scenario_start_time_s, lead_in_s
                    )
                    try:
                        rpc_attempts = pause_patiently(
                            initial_freeze,
                            max_attempts=3,
                            retry_delay_s=5.0,
                        )
                    except PatientPauseError as exc:
                        pause_rpc_attempts += exc.attempts
                        report["measurements"]["pause_attempts"] = (
                            pause_rpc_attempts
                        )
                        pause_alignment_attempts.append(
                            {
                                "attempt": alignment_attempt,
                                "phase_at_window_s": phase_at_window_s,
                                "pause_rpc_attempts": exc.attempts,
                                "error": str(exc),
                            }
                        )
                        raise
                    pause_rpc_attempts += rpc_attempts
                    report["measurements"]["pause_attempts"] = (
                        pause_rpc_attempts
                    )
                    current_clock_status = launch_clock.status()
                    paused_clock_status = current_clock_status
                    phase_at_pause_s = (
                        float(current_clock_status["frozen_ns"]) / 1e9
                        - scenario_start_time_s
                    )
                    alignment_error_s = pause_alignment_error_s(
                        phase_at_pause_s
                    )
                    aligned = pause_alignment_acceptable(phase_at_pause_s)
                    has_headroom = epoch_headroom_available(
                        phase_at_pause_s, lead_in_s
                    )
                    pause_alignment_attempts.append(
                        {
                            "attempt": alignment_attempt,
                            "phase_at_window_s": phase_at_window_s,
                            "frozen_phase_s": phase_at_pause_s,
                            "alignment_error_s": alignment_error_s,
                            "pause_rpc_attempts": rpc_attempts,
                            "aligned": aligned,
                            "epoch_headroom": has_headroom,
                        }
                    )
                    if not has_headroom:
                        raise RuntimeError(
                            "insufficient lead-in headroom at initial pause: "
                            f"phase_at_pause_s={phase_at_pause_s:.6f}, "
                            f"required<={lead_in_s - 2.0:.6f}"
                        )
                    if aligned:
                        break
                    if alignment_attempt == PAUSE_ALIGNMENT_MAX_ATTEMPTS:
                        raise RuntimeError(
                            "tick-aligned initial pause failed after "
                            f"{PAUSE_ALIGNMENT_MAX_ATTEMPTS} attempts; "
                            f"final_phase_s={phase_at_pause_s:.6f}, "
                            f"alignment_error_s={alignment_error_s:.6f}"
                        )
                    initial_freeze.resume()
                assert paused_clock_status is not None
                pause_ack = time.monotonic()
                report["checks"]["initial_pause_acknowledged"] = True
                report["measurements"]["launch_to_pause_ack_s"] = (
                    pause_ack - launch_started
                )
                report["measurements"]["clock_at_initial_pause"] = (
                    paused_clock_status
                )
                report["measurements"]["phase_at_pause_s"] = phase_at_pause_s
                report["measurements"]["pause_alignment_error_s"] = (
                    alignment_error_s
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

                _progress(report, "scene-clock", "connecting asynchronous follower")
                from onr_physical_runtime.sim.experimental_freeze.scene_clock import (
                    SceneClock,
                )
                from onr_physical_runtime.synchronization import NedPose

                initial_pose = NedPose(
                    north_m=0.0,
                    east_m=0.0,
                    down_m=-25.0,
                    yaw_degrees=270.0,
                )
                owner = SceneClock.connect(
                    state_path,
                    scenario_times_path,
                    paths["ships"],
                    vehicle=VEHICLE_NAME,
                    initial_pose=initial_pose,
                    runtime_id="issue65-bounded-certification",
                    mode="asynchronous_follower",
                    grid_cell_m=10.0,
                    airsim_port=int(args.airsim_port),
                    report_path=output / "scene-epoch.json",
                )
                report["checks"]["scene_clock_connected"] = True
                launch_clock.close()
                launch_clock = None
                initial_freeze = None
                client = owner.freeze.client
                epoch_raw_s = float(owner.clock_context["mission_epoch_s"])
                mission_epoch_effective_s = effective_mission_epoch(
                    epoch_raw_s, lead_in_s
                )
                owner.clock_context["mission_epoch_s"] = mission_epoch_effective_s
                owner._last_request = mission_epoch_effective_s
                context = dict(owner.clock_context)
                report["measurements"]["scene_clock_context"] = context
                report["measurements"]["epoch_raw_s"] = epoch_raw_s
                report["measurements"]["mission_epoch_effective_s"] = (
                    mission_epoch_effective_s
                )
                if mission_epoch_effective_s > 0.0:
                    raise RuntimeError(
                        "effective scene-clock epoch does not cover mission zero: "
                        f"epoch_raw_s={epoch_raw_s:.6f}, "
                        f"lead_in_s={lead_in_s:.6f}, "
                        f"mission_epoch_effective_s={mission_epoch_effective_s:.6f}"
                    )
                try:
                    owner.prepare(0.0)
                except Exception as exc:
                    raise RuntimeError(
                        "effective scene-clock epoch could not prepare mission zero"
                    ) from exc
                report["checks"]["epoch_covers_zero"] = True

                parity_times = certification_times(
                    float(args.dense_end_s),
                    int(args.dense_step_ticks),
                    int(args.sparse_step_ticks),
                )
                capture_times = sorted({_time_key(value) for value in args.capture_times_s})
                all_times = sorted(
                    {_time_key(value) for value in parity_times} | set(capture_times)
                )
                parity_time_keys = {_time_key(value) for value in parity_times}
                capture_time_keys = set(capture_times)
                per_ship = {
                    str(int(ship["id"])): {
                        "name": ship["name"],
                        "object_id": int(ship["objectId"]),
                        "max_abs_position_error_m": 0.0,
                        "max_heading_error_deg": 0.0,
                        "worst_position_time_s": None,
                        "worst_position_pose_index": None,
                        "worst_heading_time_s": None,
                    }
                    for ship in ships
                }
                captures: list[dict[str, Any]] = []
                aircraft: dict[str, Any] | None = None
                _progress(
                    report,
                    "sampling",
                    f"{len(parity_times)} parity boundaries and {len(capture_times)} captures",
                )
                for mission_time_s in all_times:
                    owner.prepare(mission_time_s)
                    if mission_time_s == 299.5:
                        report["checks"]["final_prepare_passed"] = True
                        report["measurements"]["phase_at_final_prepare_s"] = (
                            float(owner.freeze.clock.status()["frozen_ns"]) / 1e9
                            - float(scenario_times["scenario_start_time"])
                        )
                    if mission_time_s == 0.0:
                        aircraft = _sample_aircraft(client, mission_time_s)
                    if mission_time_s in parity_time_keys:
                        _sample_ship_parity(
                            client,
                            ships,
                            mission_time_s,
                            per_ship,
                            timestamp_indexes,
                            lead_in_s,
                        )
                    if mission_time_s in capture_time_keys:
                        captures.append(
                            _capture_sample(
                                client,
                                airsim,
                                mission_time_s,
                                context,
                                output / "samples",
                                mapped_ids,
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
                parity_heading_max = max(
                    row["max_heading_error_deg"] for row in per_ship.values()
                )
                parity_passed = pose_within_tolerance(
                    parity_position_max, parity_heading_max
                )
                report["measurements"]["ship_parity"] = {
                    "sample_times_s": parity_times,
                    "per_ship": per_ship,
                    "max_abs_position_error_m": parity_position_max,
                    "max_heading_error_deg": parity_heading_max,
                }
                report["checks"]["parity_passed"] = parity_passed
                report["measurements"]["captures"] = captures
                report["checks"]["captures_passed"] = bool(captures) and all(
                    sample["dimensions_passed"] and sample["skew_passed"]
                    for sample in captures
                )
                ship_visible = any(
                    any(count > 0 for count in sample["ship_pixel_counts"].values())
                    for sample in captures
                )
                decoded_ids_valid = all(
                    sample["decoded_ids_valid"] for sample in captures
                )
                report["measurements"]["segmentation"] = {
                    "at_least_one_ship_visible": ship_visible,
                    "all_nonzero_ids_valid": decoded_ids_valid,
                    "mapped_dynamic_object_ids": sorted(mapped_ids),
                }
                report["checks"]["segmentation_passed"] = (
                    ship_visible and decoded_ids_valid
                )
            finally:
                primary_error = sys.exc_info()[1]
                _progress(report, "teardown", "freezing and stopping the engine")
                if owner is not None:
                    try:
                        owner.close()
                    except Exception as exc:  # noqa: BLE001 - cleanup must continue
                        cleanup_errors.append(exc)
                    owner = None
                elif initial_freeze is not None:
                    try:
                        initial_freeze.pause()
                    except Exception as exc:  # noqa: BLE001 - cleanup must continue
                        cleanup_errors.append(exc)
                if process is not None:
                    try:
                        from onr_physical_runtime.sim.experimental_freeze import (
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
                from onr_physical_runtime.sim.experimental_freeze import (
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
