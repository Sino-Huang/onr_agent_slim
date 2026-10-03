"""Run-scoped frame/state behavior against a deterministic fake viewer."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar
from urllib.parse import urlsplit

import pytest
from PIL import Image

from onr.runtime_host.world import (
    CameraCapture,
    FrameResult,
    FrameUnavailableError,
    WorldView,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8A"
    "AgMBApWl9j8AAAAASUVORK5CYII="
)


class FakeViewer:
    def __init__(self) -> None:
        self.online = True
        self.state: dict[str, Any] = {
            "mission_time_seconds": 143.5,
            "flight_state": "flying",
            "active_maneuver": {
                "command_id": "command-1",
                "intent": {"action": "navigate"},
            },
            "state_version": 812,
        }
        self.cameras = {"front": {"sequence": 19, "freshness": "fresh"}}
        self.frames = {"/api/frame": PNG, "/api/camera/front": b"\xff\xd8front\xff\xd9"}
        self.routes: list[str] = []
        self.advance_during_frame = False

    def fetch(self, url: str, timeout: float, max_bytes: int) -> bytes:
        target = urlsplit(url)
        assert target.hostname == "127.0.0.1"
        assert target.port == 5066
        assert 0 < timeout <= 0.5
        assert max_bytes <= 16 * 1024 * 1024
        route = target.path
        self.routes.append(route)
        if not self.online:
            raise ConnectionRefusedError("viewer stopped")
        if route == "/api/state":
            return json.dumps(self.state).encode("utf-8")
        if route == "/api/cameras":
            return json.dumps(self.cameras).encode("utf-8")
        if route not in self.frames:
            raise FileNotFoundError("no frame")
        if route == "/api/frame" and self.advance_during_frame:
            self.advance()
        return self.frames[route]

    def advance(self) -> None:
        self.state["state_version"] += 1
        self.state["mission_time_seconds"] += 1.0
        self.frames["/api/frame"] += b"next"


@pytest.fixture
def run_root(tmp_path: Path) -> Path:
    root = tmp_path / "run-world"
    root.mkdir()
    (root / "stack.json").write_text(
        json.dumps(
            {"viewer_port": 5066, "viewer_url": "https://not-the-target.invalid"}
        ),
        encoding="utf-8",
    )
    return root


def test_initial_section_fetches_world_and_projects_contract(run_root: Path) -> None:
    viewer = FakeViewer()
    view = WorldView(run_root, fetch=viewer.fetch)

    section = view.section()

    assert "/api/frame" in viewer.routes
    assert section == {
        "viewer": {"available": True, "reason": None},
        "state": {
            "mission_time_seconds": 143.5,
            "flight_state": "flying",
            "active_maneuver": "navigate",
            "state_version": 812,
        },
        "frames": [
            {"source": "world", "sequence": 812, "media_type": "image/png"},
            {"source": "camera_front", "sequence": 19, "media_type": "image/jpeg"},
        ],
    }
    assert (run_root / "world-frames" / "latest.png").read_bytes() == PNG


def test_state_without_image_does_not_advertise_world_frame(run_root: Path) -> None:
    viewer = FakeViewer()
    viewer.frames.pop("/api/frame")
    section = WorldView(run_root, fetch=viewer.fetch).section()

    assert section["viewer"]["available"] is True
    assert section["state"]["state_version"] == 812
    assert all(frame["source"] != "world" for frame in section["frames"])
    with pytest.raises(FrameUnavailableError):
        WorldView(run_root, fetch=viewer.fetch).frame()


def test_live_frame_etag_tracks_bytes_not_state_version(run_root: Path) -> None:
    viewer = FakeViewer()
    view = WorldView(run_root, fetch=viewer.fetch)
    initial = view.frame()
    assert view.frame().etag == initial.etag

    viewer.state["state_version"] = 813
    viewer.state["mission_time_seconds"] = 144.5
    same_image = view.frame()
    assert same_image.etag == initial.etag
    assert same_image.sequence == 813
    assert same_image.mission_time_seconds == 144.5

    viewer.advance()
    changed = view.frame()
    assert changed.data != initial.data
    assert changed.etag != initial.etag
    assert changed.sequence == 814
    assert changed.mission_time_seconds == 145.5
    assert view.section()["state"]["state_version"] == changed.sequence


@pytest.mark.parametrize(
    ("source", "route", "media_type", "camera_key"),
    [
        ("world", "/api/frame", "image/png", None),
        ("camera_front", "/api/camera/front", "image/jpeg", "front"),
        (
            "camera_third_person",
            "/api/camera/third_person",
            "image/jpeg",
            "third_person",
        ),
    ],
)
def test_frame_sources_use_run_loopback_routes(
    run_root: Path, source: str, route: str, media_type: str, camera_key: str | None
) -> None:
    viewer = FakeViewer()
    if camera_key is not None:
        viewer.cameras[camera_key] = {"sequence": 37}
        viewer.frames[route] = b"\xff\xd8camera\xff\xd9"
    frame = WorldView(run_root, fetch=viewer.fetch).frame(source)

    assert route in viewer.routes
    assert frame.data == viewer.frames[route]
    assert frame.media_type == media_type
    assert frame.sequence == (812 if source == "world" else 37)
    assert not any(
        other != route and other in viewer.routes
        for other in ("/api/frame", "/api/camera/front", "/api/camera/third_person")
    )


def test_unavailable_viewer_never_invents_state(run_root: Path) -> None:
    viewer = FakeViewer()
    viewer.online = False
    view = WorldView(run_root, fetch=viewer.fetch)

    section = view.section()
    assert section["viewer"]["available"] is False
    assert section["viewer"]["reason"]
    assert section["state"] is None
    assert section["frames"] == []
    with pytest.raises(FrameUnavailableError):
        view.frame()


def test_disk_final_frame_and_state_survive_reader_recreation(run_root: Path) -> None:
    viewer = FakeViewer()
    host_reader = WorldView(run_root, fetch=viewer.fetch)
    initial = host_reader.frame()
    viewer.advance()
    # The worker may capture final data using a separate WorldView instance.
    worker_reader = WorldView(run_root, fetch=viewer.fetch)
    worker_reader.capture_final()
    viewer.online = False

    for reader in (host_reader, WorldView(run_root, fetch=viewer.fetch)):
        final = reader.frame()
        section = reader.section()
        assert final.data == viewer.frames["/api/frame"]
        assert final.etag != initial.etag
        assert final.sequence == 813
        assert final.mission_time_seconds == 144.5
        assert section["state"] == {
            "mission_time_seconds": 144.5,
            "flight_state": "flying",
            "active_maneuver": "navigate",
            "state_version": 813,
        }
        assert section["viewer"]["available"] is False
        assert {
            "source": "world",
            "sequence": 813,
            "media_type": "image/png",
        } in section["frames"]

    metadata = json.loads((run_root / "world-frames" / "latest.json").read_text())
    assert metadata["sequence"] == metadata["state"]["state_version"] == 813
    assert (
        metadata["mission_time_seconds"]
        == metadata["state"]["mission_time_seconds"]
        == 144.5
    )
    assert metadata["etag"] == final.etag
    assert metadata["media_type"] == final.media_type


def test_camera_last_frame_remains_available_in_memory(run_root: Path) -> None:
    viewer = FakeViewer()
    reader = WorldView(run_root, fetch=viewer.fetch)
    live = reader.frame("camera_front")
    viewer.online = False
    assert reader.frame("camera_front") == live
    with pytest.raises(FrameUnavailableError):
        WorldView(run_root, fetch=viewer.fetch).frame("camera_front")


def test_frame_racing_state_change_does_not_commit_mismatched_metadata(
    run_root: Path,
) -> None:
    viewer = FakeViewer()
    reader = WorldView(run_root, fetch=viewer.fetch)
    original = reader.frame()
    viewer.advance_during_frame = True

    assert reader.frame() == original
    metadata = json.loads((run_root / "world-frames" / "latest.json").read_text())
    assert metadata["sequence"] == metadata["state"]["state_version"] == 812
    assert metadata["mission_time_seconds"] == 143.5
    viewer.online = False
    assert WorldView(run_root, fetch=viewer.fetch).frame() == original


def test_state_without_world_frame_survives_restart(run_root: Path) -> None:
    viewer = FakeViewer()
    viewer.frames.clear()
    WorldView(run_root, fetch=viewer.fetch).capture_final()
    viewer.online = False

    section = WorldView(run_root, fetch=viewer.fetch).section()
    assert section["state"]["state_version"] == 812
    assert section["frames"] == []


@pytest.mark.parametrize("port", [None, "5066", True, 0, 65536])
def test_invalid_stack_port_never_fetches_external_target(
    run_root: Path, port: object
) -> None:
    (run_root / "stack.json").write_text(
        json.dumps({"viewer_port": port}), encoding="utf-8"
    )
    viewer = FakeViewer()
    view = WorldView(run_root, fetch=viewer.fetch)

    assert view.section()["viewer"]["available"] is False
    with pytest.raises(FrameUnavailableError):
        view.frame()
    assert viewer.routes == []


def test_corrupt_disk_frame_is_not_served_with_unrelated_metadata(
    run_root: Path,
) -> None:
    viewer = FakeViewer()
    WorldView(run_root, fetch=viewer.fetch).capture_final()
    (run_root / "world-frames" / "latest.png").write_bytes(b"different image")
    viewer.online = False
    with pytest.raises(FrameUnavailableError):
        WorldView(run_root, fetch=viewer.fetch).frame()


def test_terminal_readers_ignore_viewer_port_reused_by_new_run(run_root: Path) -> None:
    viewer = FakeViewer()
    original_reader = WorldView(run_root, fetch=viewer.fetch)
    original_reader.capture_final()
    final_world = original_reader.frame(live=False)
    final_camera = original_reader.frame("camera_front", live=False)

    # Another mission now occupies exactly the previous run's viewer port.
    viewer.state["state_version"] = 900
    viewer.state["mission_time_seconds"] = 22.0
    viewer.state["active_maneuver"] = {
        "command_id": "command-2",
        "intent": {"action": "pursue"},
    }
    viewer.frames["/api/frame"] = PNG + b"other-run"
    viewer.frames["/api/camera/front"] = b"\xff\xd8other-run\xff\xd9"
    viewer.routes.clear()

    for reader in (original_reader, WorldView(run_root, fetch=viewer.fetch)):
        assert reader.frame(live=False) == final_world
        section = reader.section(live=False)
        assert section["state"]["state_version"] == 812
        assert section["state"]["mission_time_seconds"] == 143.5
        assert section["state"]["active_maneuver"] == "navigate"
        assert section["viewer"] == {"available": False, "reason": "run is terminal"}
    assert original_reader.frame("camera_front", live=False) == final_camera
    assert viewer.routes == []


def test_terminal_run_without_cache_does_not_fetch_newer_run(run_root: Path) -> None:
    viewer = FakeViewer()
    reader = WorldView(run_root, fetch=viewer.fetch)

    assert reader.section(live=False)["state"] is None
    assert reader.section(live=False)["frames"] == []
    with pytest.raises(FrameUnavailableError):
        reader.frame(live=False)
    assert viewer.routes == []


CAMERAS = {
    "camera_front": "front_center_custom",
    "camera_third_person": "third_person_demo",
}
RED, BLUE, GREEN = (255, 0, 0), (0, 0, 255), (0, 255, 0)
T = TypeVar("T")


class FakeAirSim:
    """``airsim``-shaped SDK boundary rendering solid RGB scenes per camera."""

    class ImageType:
        Scene = 0

    def __init__(self, scenes: dict[str, tuple[int, int, int] | None]) -> None:
        self.scenes = dict(scenes)
        self.requests: list[tuple[list[tuple[Any, ...]], str]] = []
        self.clients: list[tuple[str, int, int, int]] = []
        self.other_calls: list[str] = []
        self.error: Exception | None = None
        self.gate: threading.Event | None = None
        self.blocked = threading.Event()

    @staticmethod
    def ImageRequest(
        camera_name: str,
        image_type: int,
        pixels_as_float: bool = False,
        compress: bool = True,
    ) -> tuple[Any, ...]:
        return camera_name, image_type, pixels_as_float, compress

    def VehicleClient(
        self, ip: str = "", port: int = 41451, timeout_value: int = 3600
    ) -> FakeAirSimClient:
        self.clients.append((ip, port, timeout_value, threading.get_ident()))
        return FakeAirSimClient(self)


class FakeAirSimClient:
    client = None  # no msgpack-rpc session behind the fake

    def __init__(self, sdk: FakeAirSim) -> None:
        self._sdk = sdk

    def simGetImages(
        self, requests: list[tuple[Any, ...]], vehicle_name: str = ""
    ) -> list[SimpleNamespace]:
        sdk = self._sdk
        sdk.requests.append((list(requests), vehicle_name))
        if sdk.gate is not None:
            sdk.blocked.set()
            sdk.gate.wait()
        if sdk.error is not None:
            raise sdk.error
        responses = []
        for camera_name, *_ in requests:
            rgb = sdk.scenes.get(camera_name)
            if rgb is None:
                # AirSim's answer for a camera it cannot render.
                responses.append(SimpleNamespace(image_data_uint8=b"", width=0, height=0))
            else:
                bgr = bytes(reversed(rgb))
                responses.append(
                    SimpleNamespace(image_data_uint8=bgr * 8 * 6, width=8, height=6)
                )
        return responses

    def __getattr__(self, name: str) -> Any:
        self._sdk.other_calls.append(name)
        raise AttributeError(name)


def _settings(tmp_path: Path, *cameras: str) -> Path:
    path = tmp_path / "settings_airsim.json"
    path.write_text(
        json.dumps(
            {"Vehicles": {"SimpleFlight": {"Cameras": {name: {} for name in cameras}}}}
        ),
        encoding="utf-8",
    )
    return path


def _camera_capture(
    run_root: Path, sdk: FakeAirSim, settings: Path, viewer: FakeViewer
) -> CameraCapture:
    return CameraCapture(
        run_root,
        cameras=CAMERAS,
        vehicle_name="SimpleFlight",
        rpc_port=41451,
        viewer_port=5066,
        airsim_settings=settings,
        period_seconds=0.01,
        sdk=sdk,
        fetch=viewer.fetch,
    )


def _until(predicate: Callable[[], T | None], timeout: float = 15.0) -> T:
    deadline = time.monotonic() + timeout
    while (result := predicate()) is None or result is False:
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.005)
    return result


def _cached(reader: WorldView, source: str) -> FrameResult | None:
    try:
        return reader.frame(source, live=False)
    except FrameUnavailableError:
        return None


def _capture_status(reader: WorldView, state: str) -> dict[str, Any] | None:
    status = reader.section().get("camera_capture")
    return status if status is not None and status["state"] == state else None


def _center(frame: FrameResult) -> tuple[int, int, int]:
    image = Image.open(BytesIO(frame.data))
    assert image.format == "JPEG"
    assert image.size == (640, 480)
    return image.convert("RGB").getpixel((320, 240))  # type: ignore[return-value]


def _near(actual: tuple[int, int, int], expected: tuple[int, int, int]) -> bool:
    return all(abs(a - b) <= 8 for a, b in zip(actual, expected, strict=True))


def test_worker_capture_persists_actual_cameras_with_read_only_sdk_calls(
    run_root: Path, tmp_path: Path
) -> None:
    sdk = FakeAirSim({"front_center_custom": RED, "third_person_demo": BLUE})
    viewer = FakeViewer()
    capture = _camera_capture(
        run_root, sdk, _settings(tmp_path, *CAMERAS.values()), viewer
    )
    capture.start()
    reader = WorldView(run_root, fetch=viewer.fetch)
    front = _until(lambda: _cached(reader, "camera_front"))
    third = _until(lambda: _cached(reader, "camera_third_person"))
    assert capture.stop()

    assert _near(_center(front), RED) and _near(_center(third), BLUE)
    for frame in (front, third):
        assert frame.etag == '"' + hashlib.sha256(frame.data).hexdigest() + '"'
        assert frame.media_type == "image/jpeg"
        assert frame.sequence >= 1
        assert frame.mission_time_seconds == 143.5
    # Only image reads, from clients created in the capture thread.
    assert sdk.other_calls == []
    assert {client[:3] for client in sdk.clients} == {("127.0.0.1", 41451, 2)}
    assert threading.get_ident() not in {client[3] for client in sdk.clients}
    assert {
        (tuple(requests), vehicle) for requests, vehicle in sdk.requests
    } == {
        (
            (
                ("front_center_custom", 0, False, False),
                ("third_person_demo", 0, False, False),
            ),
            "SimpleFlight",
        )
    }
    assert set(viewer.routes) == {"/api/state"}

    # A terminal reader never contacts a viewer port reused by another run.
    viewer.frames["/api/camera/front"] = b"\xff\xd8other-run\xff\xd9"
    viewer.cameras["front"] = {"sequence": 99}
    viewer.routes.clear()
    terminal = WorldView(run_root, fetch=viewer.fetch)
    section = terminal.section(live=False)
    assert terminal.frame("camera_front", live=False) == front
    assert terminal.frame("camera_third_person", live=False) == third
    assert {
        "source": "camera_third_person",
        "sequence": third.sequence,
        "media_type": "image/jpeg",
    } in section["frames"]
    assert section["camera_capture"] == {
        "state": "stopped",
        "reason": None,
        "cameras": CAMERAS,
    }
    assert viewer.routes == []


def test_unchanged_scene_keeps_generation_and_new_pixels_publish_next(
    run_root: Path, tmp_path: Path
) -> None:
    sdk = FakeAirSim({"front_center_custom": RED, "third_person_demo": BLUE})
    viewer = FakeViewer()
    viewer.online = False  # mission time unknown is reported, never invented
    capture = _camera_capture(
        run_root, sdk, _settings(tmp_path, *CAMERAS.values()), viewer
    )
    capture.start()
    try:
        reader = WorldView(run_root, fetch=viewer.fetch)
        paused = _until(lambda: _cached(reader, "camera_front"))
        assert paused.mission_time_seconds is None
        seen = len(sdk.requests)
        _until(lambda: len(sdk.requests) >= seen + 3)
        # A paused scene renders identical pixels: same etag and sequence.
        assert reader.frame("camera_front", live=False) == paused

        sdk.scenes["front_center_custom"] = GREEN
        resumed = _until(
            lambda: (
                frame
                if (frame := _cached(reader, "camera_front")) is not None
                and frame.etag != paused.etag
                else None
            )
        )
    finally:
        assert capture.stop()
    assert _near(_center(resumed), GREEN)
    assert resumed.sequence > paused.sequence
    assert WorldView(run_root).frame("camera_front", live=False) == resumed


def test_sdk_failure_reports_unavailable_and_keeps_last_good_frame(
    run_root: Path, tmp_path: Path
) -> None:
    sdk = FakeAirSim({"front_center_custom": RED, "third_person_demo": BLUE})
    viewer = FakeViewer()
    viewer.cameras = {}  # the viewer itself has no camera images
    capture = _camera_capture(
        run_root, sdk, _settings(tmp_path, *CAMERAS.values()), viewer
    )
    capture.start()
    try:
        reader = WorldView(run_root, fetch=viewer.fetch)
        good = _until(lambda: _cached(reader, "camera_front"))
        _until(lambda: _capture_status(reader, "capturing"))
        sdk.error = TimeoutError("Request timed out")
        status = _until(lambda: _capture_status(reader, "unavailable"))
        assert status["reason"] == "airsim_capture_failed: TimeoutError"
        assert reader.frame("camera_front", live=False) == good

        sdk.error = None
        _until(lambda: _capture_status(reader, "capturing"))
    finally:
        assert capture.stop()


def test_unrenderable_or_unconfigured_camera_is_never_advertised(
    run_root: Path, tmp_path: Path
) -> None:
    sdk = FakeAirSim({"front_center_custom": RED, "third_person_demo": None})
    viewer = FakeViewer()
    viewer.cameras = {}
    capture = _camera_capture(
        run_root, sdk, _settings(tmp_path, *CAMERAS.values()), viewer
    )
    capture.start()
    reader = WorldView(run_root, fetch=viewer.fetch)
    _until(lambda: _cached(reader, "camera_front"))
    status = _until(lambda: _capture_status(reader, "unavailable"))
    assert capture.stop()
    assert status["reason"] == "airsim_frame_unavailable: camera_third_person"
    assert [
        frame["source"]
        for frame in reader.section(live=False)["frames"]
        if frame["source"] != "world"
    ] == ["camera_front"]

    # A camera absent from the engine settings is never requested at all.
    other_root = tmp_path / "run-unconfigured"
    other_root.mkdir()
    unconfigured = FakeAirSim({"front_center_custom": RED, "third_person_demo": BLUE})
    capture = _camera_capture(
        other_root, unconfigured, _settings(tmp_path, "front_center_custom"), viewer
    )
    capture.start()
    other = WorldView(other_root, fetch=viewer.fetch)
    _until(lambda: _cached(other, "camera_front"))
    status = _until(lambda: _capture_status(other, "unavailable"))
    assert capture.stop()
    assert status["reason"] == "camera_not_configured: third_person_demo"
    assert {
        camera_name
        for requests, _vehicle in unconfigured.requests
        for camera_name, *_ in requests
    } == {"front_center_custom"}
    assert _cached(other, "camera_third_person") is None


def test_stop_is_bounded_by_hung_sdk_and_never_persists_after_stop(
    run_root: Path, tmp_path: Path
) -> None:
    sdk = FakeAirSim({"front_center_custom": RED, "third_person_demo": BLUE})
    viewer = FakeViewer()
    capture = _camera_capture(
        run_root, sdk, _settings(tmp_path, *CAMERAS.values()), viewer
    )
    capture.start()
    reader = WorldView(run_root, fetch=viewer.fetch)
    before = _until(lambda: _cached(reader, "camera_front"))
    sdk.gate = threading.Event()
    assert sdk.blocked.wait(15)
    sdk.scenes["front_center_custom"] = GREEN

    started = time.monotonic()
    assert capture.stop(timeout=0.05) is False
    assert time.monotonic() - started < 1.0
    sdk.gate.set()  # the engine answers only after teardown began
    assert capture.stop()

    assert WorldView(run_root).frame("camera_front", live=False) == before
    assert WorldView(run_root).section(live=False)["camera_capture"]["state"] == (
        "stopped"
    )


def test_live_viewer_without_camera_images_serves_captured_cache(
    run_root: Path, tmp_path: Path
) -> None:
    sdk = FakeAirSim({"front_center_custom": RED, "third_person_demo": BLUE})
    viewer = FakeViewer()
    # The physical viewer's inventory before any upstream image (frame_missing).
    viewer.cameras = {
        "front": {"sequence": 0, "diagnostic": "frame_missing"},
        "third_person": {"sequence": 0, "diagnostic": "frame_missing"},
    }
    capture = _camera_capture(
        run_root, sdk, _settings(tmp_path, *CAMERAS.values()), viewer
    )
    capture.start()
    reader = WorldView(run_root, fetch=viewer.fetch)
    captured = _until(lambda: _cached(reader, "camera_third_person"))
    assert capture.stop()
    viewer.routes.clear()

    section = reader.section()
    assert section["viewer"]["available"] is True
    assert {frame["source"] for frame in section["frames"]} == {
        "world",
        "camera_front",
        "camera_third_person",
    }
    assert reader.frame("camera_third_person") == captured
    assert not {"/api/camera/front", "/api/camera/third_person"} & set(viewer.routes)

    # Once the viewer actually provides an image, its native route is used.
    viewer.cameras["third_person"] = {"sequence": 7}
    viewer.frames["/api/camera/third_person"] = b"\xff\xd8viewer\xff\xd9"
    native = reader.frame("camera_third_person")
    assert native.data == b"\xff\xd8viewer\xff\xd9" and native.sequence == 7
