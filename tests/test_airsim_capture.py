from __future__ import annotations

import ast
import hashlib
import inspect
import json
import math
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from onr.demo.airsim_reconstruction.capture import (
    FLUSH_SECONDS,
    BeatPhaseReader,
    DeferredCaptureWriter,
    DronePose,
    UnknownInstanceIds,
    _capture_landing_target,
    _enforce_landing_gate,
    _finite_minimum,
    _phase_errors,
    _prepare_capture_frame,
    build_parser,
    capture_image_set,
    capture_schedule,
    check_process_health,
    disk_preflight,
    load_drone_trajectory,
    load_recapture_ticks,
    pending_capture_ticks,
    resume_tick_complete,
    run_capture,
    sync_to_nfs,
    write_manifest_atomic,
)


def test_load_drone_trajectory_tick_zero_uses_canonical_metadata_pose(
    tmp_path: Path,
) -> None:
    metadata = tmp_path / "frame-metadata.json"
    observations = tmp_path / "observations"
    observations.mkdir()
    metadata.write_text(
        json.dumps(
            [
                {"tick": 0, "mission_time": 0.0, "position_ned": [9, 9, 9]},
                {"tick": 1, "mission_time": 0.5, "position_ned": [1, 2, -25]},
            ]
        )
    )
    (observations / "one-observation.json").write_text(
        json.dumps(
            {
                "observation_kind": "state",
                "mission_time_s": 0.5,
                "controlled_vehicle": {"heading_degrees": 90.0},
            }
        )
    )

    poses = load_drone_trajectory(metadata, observations)

    assert poses[0].ned_m == (9.0, 9.0, 9.0)
    assert poses[0].yaw_degrees == 0.0
    assert poses[1].yaw_degrees == 90.0


def test_disk_preflight_threshold(tmp_path: Path) -> None:
    required = 2 * 2 * 3 * 4 * 1024 * 1024
    assert disk_preflight(
        tmp_path, 2, disk_usage=lambda _: SimpleNamespace(free=required)
    )["required_free_bytes"] == required
    with pytest.raises(RuntimeError):
        disk_preflight(
            tmp_path, 2, disk_usage=lambda _: SimpleNamespace(free=required - 1)
        )


def test_atomic_manifest_preserves_old_file_on_replace_failure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "capture-manifest.json"
    write_manifest_atomic(path, {"version": 1})

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("crash")

    with pytest.raises(OSError):
        write_manifest_atomic(path, {"version": 2}, replace=fail_replace)
    assert json.loads(path.read_text()) == {"version": 1}
    assert (tmp_path / ".capture-manifest.json.tmp").is_file()


def test_phase_minimum_normalizes_nonfinite_values_for_strict_json() -> None:
    tick_record = {
        "measured_min_ship_phase_pre_flush_s": _finite_minimum((60.0, 60.1)),
        "measured_min_ship_phase_s": _finite_minimum((60.1, float("nan"))),
    }

    assert tick_record == {
        "measured_min_ship_phase_pre_flush_s": 60.0,
        "measured_min_ship_phase_s": None,
    }
    assert json.loads(json.dumps(tick_record, allow_nan=False)) == tick_record


def _seed_completed_ticks(tmp_path: Path, count: int) -> dict[str, Any]:
    manifest: dict[str, Any] = {"ticks": {}}
    for tick in range(count):
        images = []
        for label in ("front-rgb", "front-seg", "third-rgb"):
            path = tmp_path / f"tick-{tick:04d}-{label}.png"
            path.write_bytes(f"{tick}:{label}".encode())
            images.append(
                {
                    "path": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        manifest["ticks"][str(tick)] = {"images": images}
    return manifest


def test_resume_fast_forwards_complete_ticks_and_captures_first_incomplete(
    tmp_path: Path,
) -> None:
    manifest = _seed_completed_ticks(tmp_path, 6)
    pending = pending_capture_ticks(
        list(range(7)), manifest, tmp_path, resume=True
    )
    advanced: list[int] = []
    captured: list[int] = []

    for tick, should_capture in capture_schedule(list(range(7)), pending):
        advanced.append(tick)
        if should_capture:
            captured.append(tick)

    assert advanced == list(range(7))
    assert captured == [6]
    assert resume_tick_complete(0, manifest, tmp_path)


def test_capture_target_is_flush_compensated_only_for_capture_ticks() -> None:
    assert _capture_landing_target(123.5, True) == pytest.approx(123.4)
    assert _capture_landing_target(123.5, False) == 123.5


def test_recapture_ticks_force_noncontiguous_complete_ticks_pending(
    tmp_path: Path,
) -> None:
    manifest = _seed_completed_ticks(tmp_path, 6)
    recapture_path = tmp_path / "recapture.json"
    recapture_path.write_text("[4, 1, 4]\n", encoding="utf-8")
    recapture = load_recapture_ticks(recapture_path)

    pending = pending_capture_ticks(
        list(range(6)),
        manifest,
        tmp_path,
        resume=True,
        recapture_ticks=recapture,
    )

    assert recapture == {1, 4}
    assert pending == [1, 4]
    assert capture_schedule(list(range(6)), pending) == [
        (0, False),
        (1, True),
        (2, False),
        (3, False),
        (4, True),
        (5, False),
    ]
    args = build_parser().parse_args(
        ["--output", str(tmp_path), "--resume", "--recapture-ticks", str(recapture_path)]
    )
    assert args.recapture_ticks == recapture_path


@pytest.mark.parametrize("gate_name", ["pre-flush", "post-flush acquisition"])
def test_resume_landing_failure_skips_complete_tick_without_mutating_manifest(
    tmp_path: Path,
    gate_name: str,
) -> None:
    manifest = _seed_completed_ticks(tmp_path, 1)
    before = json.dumps(manifest, sort_keys=True)
    landing = {"landing_errors_s": {"1": 0.27}}

    assert not _enforce_landing_gate(
        0,
        landing,
        resume=True,
        capture_required=False,
        manifest=manifest,
        output_dir=tmp_path,
        gate_name=gate_name,
    )
    assert json.dumps(manifest, sort_keys=True) == before


def test_new_landing_failure_remains_a_hard_error(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="beat landing failed at capture tick 0"):
        _enforce_landing_gate(
            0,
            {"landing_errors_s": {"1": 0.27}},
            resume=True,
            capture_required=True,
            manifest={"ticks": {}},
            output_dir=tmp_path,
        )


def test_post_flush_all_ship_gate_passes_and_fails_at_quarter_second(
    tmp_path: Path,
) -> None:
    target = 60.0
    passing = _phase_errors({"1": 60.25, "2": 59.75}, target)
    assert _enforce_landing_gate(
        0,
        {"landing_errors_s": passing},
        resume=False,
        capture_required=True,
        manifest={"ticks": {}},
        output_dir=tmp_path,
        gate_name="post-flush acquisition",
    )

    failing = _phase_errors({"1": 60.2501, "2": 60.0}, target)
    with pytest.raises(
        RuntimeError,
        match="post-flush acquisition beat landing failed at capture tick 0",
    ):
        _enforce_landing_gate(
            0,
            {"landing_errors_s": failing},
            resume=False,
            capture_required=True,
            manifest={"ticks": {}},
            output_dir=tmp_path,
            gate_name="post-flush acquisition",
        )


def test_prepare_capture_frame_resets_kinematics_flushes_and_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    pose = DronePose(0, 0.0, (1.0, 2.0, -25.0), 90.0)

    class Vector:
        def __init__(self, x: float, y: float, z: float) -> None:
            self.xyz = (x, y, z)

    class KinematicsState:
        def __init__(self) -> None:  # real msgpack type takes no arguments
            pass

    fake_airsim = SimpleNamespace(
        Vector3r=Vector,
        KinematicsState=KinematicsState,
        to_quaternion=lambda roll, pitch, yaw: (roll, pitch, yaw),
    )
    monkeypatch.setitem(sys.modules, "airsim", fake_airsim)

    class Client:
        def enableApiControl(self, enabled: bool, *, vehicle_name: str) -> None:
            events.append(("enable", enabled, vehicle_name))

        def simSetKinematics(
            self, state: KinematicsState, *, ignore_collision: bool
        ) -> None:
            events.append(("reset", state, ignore_collision))

    client = Client()

    class Freeze:
        def step(self, seconds: float) -> None:
            events.append(("flush", seconds))

    class Reader:
        def read_full(self) -> dict[str, float]:
            events.append("read-phases")
            return {"1": 60.1}

    phases = _prepare_capture_frame(
        client, Freeze(), Reader(), pose  # type: ignore[arg-type]
    )

    assert events[0] == ("enable", True, "SimpleFlight")
    reset, state, ignore_collision = events[1]
    assert reset == "reset"
    assert ignore_collision is True
    assert state.position.xyz == pose.ned_m
    assert state.orientation == pytest.approx((0.0, 0.0, math.pi / 2.0))
    assert state.linear_velocity.xyz == (0.0, 0.0, 0.0)
    assert state.angular_velocity.xyz == (0.0, 0.0, 0.0)
    assert state.linear_acceleration.xyz == (0.0, 0.0, 0.0)
    assert state.angular_acceleration.xyz == (0.0, 0.0, 0.0)
    assert events[2:] == [
        ("flush", FLUSH_SECONDS),
        "read-phases",
    ]
    assert phases == {"1": 60.1}


def test_capture_loop_advances_and_skips_before_frame_preparation() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(run_capture)))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    advance = next(
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "advance_to_phase"
    )
    set_target = next(
        node
        for node in calls
        if isinstance(node.func, ast.Attribute) and node.func.attr == "set_target"
    )
    prepare = next(
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "_prepare_capture_frame"
    )
    capture = next(
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "capture_image_set"
    )
    verify = next(
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "_verify_drone_pose"
    )
    skip = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.op, ast.Not)
        and isinstance(node.test.operand, ast.Name)
        and node.test.operand.id == "should_capture"
    )

    assert any(isinstance(node, ast.Continue) for node in skip.body)
    assert FLUSH_SECONDS == 0.1
    assert (
        set_target.lineno
        < advance.lineno
        < skip.lineno
        < prepare.lineno
        < verify.lineno
        < capture.lineno
    )


def test_advance_to_phase_returns_early_for_positive_overshoot() -> None:
    from onr.demo.airsim_reconstruction.beats import advance_to_phase

    class Freeze:
        def __init__(self) -> None:
            self.steps: list[float] = []

        def step(self, seconds: float) -> None:
            self.steps.append(seconds)

    freeze = Freeze()
    result = advance_to_phase(
        freeze,
        lambda: {"1": 10.27},
        10.0,
        sleep=lambda _: None,
        monotonic=lambda: 0.0,
    )

    assert result["early_return"] is True
    assert result["landing_errors_s"] == {"1": pytest.approx(0.27)}
    assert freeze.steps == []


def _tick_with_tasks(tick: int) -> tuple[dict[str, Any], list[tuple]]:
    images = []
    tasks = []
    for label in ("front-rgb", "front-seg", "third-rgb"):
        record = {
            "kind": label,
            "path": f"tick-{tick:04d}-{label}.png",
            "sha256": None,
        }
        images.append(record)
        tasks.append((bytes([1, 2, 3]), 1, 1, record))
    return {"tick": tick, "images": images}, tasks


def test_deferred_writer_creates_images_and_complete_manifest(tmp_path: Path) -> None:
    record, tasks = _tick_with_tasks(0)
    manifest = {"ticks": {"0": record}}
    writer = DeferredCaptureWriter(tmp_path, manifest)

    writer.enqueue_tick(0, tasks)
    writer.close()

    stored = json.loads((tmp_path / "capture-manifest.json").read_text())
    assert all(image["sha256"] for image in stored["ticks"]["0"]["images"])
    assert len(list(tmp_path.glob("tick-0000-*.png"))) == 3


def test_bounded_writer_queue_backpressure_does_not_deadlock(tmp_path: Path) -> None:
    manifest: dict[str, Any] = {"ticks": {}}
    writer = DeferredCaptureWriter(tmp_path, manifest, maxsize=1)
    for tick in range(3):
        record, tasks = _tick_with_tasks(tick)
        manifest["ticks"][str(tick)] = record
        writer.enqueue_tick(tick, tasks)

    writer.close()

    assert len(list(tmp_path.glob("tick-*.png"))) == 9


def test_poll_subset_is_used_only_inside_poll_band() -> None:
    calls: list[int] = []
    phase = [0.0]
    ships = [{"id": value} for value in range(20)]

    def fake_read(
        _client: object,
        selected: list[dict[str, int]],
        **_kwargs: object,
    ) -> dict[str, float]:
        calls.append(len(selected))
        phase[0] = 1.5 if len(calls) == 1 else 1.8
        return {str(row["id"]): phase[0] for row in selected}

    reader = BeatPhaseReader(
        object(), ships, ships[:3], {str(value): 0.0 for value in range(20)}, read_fn=fake_read
    )
    reader.set_target(2.0)

    reader()
    reader()
    reader.read_full()

    assert calls == [20, 3, 20]


def test_sync_to_nfs_copies_images_and_manifest(tmp_path: Path) -> None:
    staging, output = tmp_path / "staging", tmp_path / "output"
    staging.mkdir()
    (staging / "tick-0000-front-rgb.png").write_bytes(b"png")
    (staging / "capture-manifest.json").write_text('{"ticks": {}}\n')

    sync_to_nfs(staging, output)

    assert (output / "tick-0000-front-rgb.png").read_bytes() == b"png"
    assert json.loads((output / "capture-manifest.json").read_text()) == {
        "ticks": {}
    }


class _Vector:
    x_val = 0.0
    y_val = 0.0
    z_val = 0.0
    w_val = 1.0


class _Response:
    width = 1
    height = 1
    time_stamp = 123
    camera_position = _Vector()
    camera_orientation = _Vector()

    def __init__(self, data: bytes) -> None:
        self.image_data_uint8 = data


class _AirSim:
    class ImageType:
        Scene = 0
        Segmentation = 5

    class ImageRequest:
        def __init__(self, *args: object) -> None:
            self.args = args


class _CaptureClient:
    def simGetImages(
        self, requests: list[object], *, vehicle_name: str
    ) -> list[_Response]:
        del requests, vehicle_name
        return [_Response(bytes(3)), _Response(bytes([0, 3, 231])), _Response(bytes(3))]


def test_capture_unknown_id_records_then_raises(tmp_path: Path) -> None:
    manifest: dict[str, Any] = {"ticks": {"0": {"tick": 0}}}
    writer = DeferredCaptureWriter(tmp_path, manifest)
    with pytest.raises(UnknownInstanceIds) as failure:
        capture_image_set(
            _CaptureClient(),
            _AirSim,
            writer,
            manifest["ticks"]["0"],
            {0},
            set(),
            set(),
            [],
            sleep=lambda _: None,
        )
    writer.close()
    assert failure.value.record["unknown_ids"] == [999]


class _Process:
    def __init__(self, code: int | None) -> None:
        self.code = code

    def poll(self) -> int | None:
        return self.code


class _HealthClient:
    def getServerVersion(self) -> int:
        return 1


def test_health_check_raises_with_tick() -> None:
    with pytest.raises(RuntimeError, match="tick 7"):
        check_process_health(_Process(1), _HealthClient(), 7)
