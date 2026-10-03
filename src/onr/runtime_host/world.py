"""Loopback viewer reads, a run-local latest-only frame cache and camera capture.

Callers must do network and disk reads outside the Runtime Host state lock.
The viewer exposes state and images separately; world frames are accepted only
when the state version is unchanged across the image fetch.

AirSim runs also persist operator camera frames under ``world-frames/``: the
Run Worker's :class:`CameraCapture` writes them for scene-clock (perception)
runs and the AirSim follower process writes them for perception-off runs, both
through :class:`CameraFrameStore`. Readers serve those cached generations for
live and terminal runs without any network access; a live viewer camera image
still wins when the viewer itself actually provides one.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib
import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, TypedDict, cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .airsim_overlay import (
    PerceptionAnnotator,
    boxes_summary,
    render_annotated,
    scene_rgb,
    segmentation_ids,
)
from .run_files import JsonFileCache

logger = logging.getLogger(__name__)

MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024
SOURCES = {
    "world": "image/png",
    "camera_front_annotated": "image/jpeg",
    "camera_front": "image/jpeg",
    "camera_third_person": "image/jpeg",
}
"""Frame sources in operator display order, with their media types."""
VIEWER_ROUTES = {
    "world": "/api/frame",
    "camera_front": "/api/camera/front",
    "camera_third_person": "/api/camera/third_person",
}
VIEWER_CAMERA_SOURCES = ("camera_front", "camera_third_person")
"""Physical cameras: the viewer may serve them and the Run Worker captures them."""
CAMERA_SOURCES = ("camera_front_annotated", *VIEWER_CAMERA_SOURCES)
"""Disk-persisted camera generations under ``world-frames/``."""
CAPTURE_STATUS_FILE = "camera-capture.json"
CAPTURE_STATES = frozenset({"capturing", "unavailable", "stopped"})
AIRSIM_STATUS_FILE = "airsim.json"
AIRSIM_MODES = frozenset({"world_model_follower", "scene_clock"})
ANNOTATION_KINDS = frozenset({"ideal_segmentation", "perception_ideal", "perception_yolo"})
LOOPBACK = "127.0.0.1"
Fetch = Callable[[str, float, int], bytes]
_FileVersion = tuple[int, int, int]


class FrameUnavailableError(RuntimeError):
    """No live or cached frame is available for the requested source."""


@dataclass(frozen=True, slots=True)
class FrameResult:
    data: bytes
    etag: str
    sequence: int
    mission_time_seconds: float | None
    media_type: str


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch(url: str, timeout: float, max_bytes: int) -> bytes:
    # Neither proxy environment variables nor redirects may escape loopback.
    opener = build_opener(ProxyHandler({}), _NoRedirects())
    with opener.open(Request(url, method="GET"), timeout=timeout) as response:
        payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise FrameUnavailableError("viewer response exceeds size limit")
    return payload


def _etag(data: bytes) -> str:
    return '"' + hashlib.sha256(data).hexdigest() + '"'


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _seconds(value: object) -> bool:
    return value is None or (
        isinstance(value, (float, int)) and not isinstance(value, bool)
    )


@contextmanager
def _disk_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".cache.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _file_version(path: Path) -> _FileVersion | None:
    # Every atomic replace creates a new inode, so equal sizes never hide a write.
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_ino, stat.st_mtime_ns, stat.st_size


def _camera_paths(directory: Path, source: str) -> tuple[Path, Path]:
    return directory / f"{source}.jpg", directory / f"{source}.json"


class _State(TypedDict):
    mission_time_seconds: float | None
    flight_state: str | None
    active_maneuver: str | None
    state_version: int


def _state(payload: Mapping[str, object]) -> _State:
    version = payload.get("state_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise FrameUnavailableError("viewer state has no valid state_version")
    mission_time = payload.get("mission_time_seconds")
    if mission_time is not None and (
        isinstance(mission_time, bool) or not isinstance(mission_time, (float, int))
    ):
        raise FrameUnavailableError("viewer state has invalid mission time")
    active = payload.get("active_maneuver")
    if isinstance(active, dict):
        intent = active.get("intent")
        active = intent.get("action") if isinstance(intent, dict) else None
    flight = payload.get("flight_state")
    if active is not None and not isinstance(active, str):
        raise FrameUnavailableError("viewer state has invalid active maneuver")
    if flight is not None and not isinstance(flight, str):
        raise FrameUnavailableError("viewer state has invalid flight state")
    return {
        "mission_time_seconds": None if mission_time is None else float(mission_time),
        "flight_state": flight,
        "active_maneuver": active,
        "state_version": version,
    }


def _viewer_provides(
    cameras: Mapping[str, object] | None, source: str, requested: str | None
) -> bool:
    """Whether to fetch ``source`` from the viewer's native camera route."""
    if cameras is None:
        # Unknown inventory: only an explicit request asks the route itself.
        return source == requested
    entry = cameras.get(source.removeprefix("camera_"))
    if entry is None:
        return False
    sequence = entry.get("sequence") if isinstance(entry, dict) else None
    # The viewer advertises sequence 0 (frame_missing) before any upstream image.
    return not (_count(sequence) and sequence == 0)


class WorldView:
    """Latest viewer state/images for one run.

    World images and Run-Worker-captured camera images survive restart; camera
    images read live from the viewer are memory-only.
    ``fetch(url, timeout_seconds, max_bytes) -> bytes`` may be injected by tests.
    Every URL is built from the numeric viewer port in the run's stack.json.
    Network failures preserve the last successful state and source frames.
    """

    def __init__(
        self, run_root: Path, *, fetch: Fetch | None = None, timeout: float = 0.5
    ) -> None:
        if timeout <= 0:
            raise ValueError("viewer timeout must be positive")
        self._root = Path(run_root)
        self._directory = self._root / "world-frames"
        self._fetch = fetch or _fetch
        self._timeout = timeout
        self._lock = Lock()
        self._json_files = JsonFileCache[dict[str, object]](
            dict, max_bytes=MAX_JSON_BYTES
        )
        self._frames: dict[str, FrameResult] = {}
        self._last_state: _State | None = None
        self._disk_version: _FileVersion | None = None
        self._camera_versions: dict[str, _FileVersion] = {}
        self._capture_status: dict[str, object] | None = None
        self._airsim_status: dict[str, object] | None = None
        self._persisted_world: FrameResult | None = None
        self._persisted_state: _State | None = None

    def section(self, *, live: bool = True) -> dict[str, Any]:
        """Return v1.3 world data; terminal runs must request ``live=False``."""
        with self._lock:
            self._load_cached()
            available, reason = (
                self._refresh(None) if live else (False, "run is terminal")
            )
            section: dict[str, Any] = {
                "viewer": {"available": available, "reason": reason},
                "state": None if self._last_state is None else dict(self._last_state),
                "frames": [
                    {
                        "source": source,
                        "sequence": frame.sequence,
                        "media_type": frame.media_type,
                    }
                    for source in SOURCES
                    if (frame := self._frames.get(source)) is not None
                ],
            }
            status = self._capture_status
            if status is not None:
                if not live and status["state"] == "capturing":
                    # A worker that died mid-capture never wrote "stopped".
                    status = {**status, "state": "stopped"}
                section["camera_capture"] = status
            airsim = self._airsim_status
            if airsim is not None:
                if not live and airsim["state"] == "capturing":
                    airsim = {**airsim, "state": "stopped"}
                section["airsim"] = airsim
            return section

    def frame(self, source: str = "world", *, live: bool = True) -> FrameResult:
        """Return source bytes; ``live=False`` never contacts a reused port."""
        if source not in SOURCES:
            raise FrameUnavailableError(f"unknown frame source: {source}")
        with self._lock:
            self._load_cached()
            if live:
                self._refresh(source)
            result = self._frames.get(source)
            if result is None:
                raise FrameUnavailableError(f"frame unavailable: {source}")
            return result

    def capture_final(self) -> None:
        """Capture final world/state before the worker tears down the viewer."""
        self.section()

    def _url(self, route: str) -> str:
        plan = self._json_files.get(self._root / "stack.json")
        port = None if plan is None else plan.get("viewer_port")
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
        ):
            raise FrameUnavailableError("run has no viewer port")
        return f"http://{LOOPBACK}:{port}{route}"

    def _get(self, route: str, max_bytes: int) -> bytes:
        try:
            data = self._fetch(self._url(route), self._timeout, max_bytes)
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise FrameUnavailableError("viewer unavailable") from exc
        if not data or len(data) > max_bytes:
            raise FrameUnavailableError("viewer response missing or too large")
        return data

    def _json(self, route: str) -> dict[str, object]:
        try:
            result = json.loads(self._get(route, MAX_JSON_BYTES))
        except (ValueError, UnicodeError) as exc:
            raise FrameUnavailableError("viewer returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise FrameUnavailableError("viewer returned invalid JSON object")
        return result

    def _refresh(self, source: str | None) -> tuple[bool, str | None]:
        try:
            state = _state(self._json("/api/state"))
        except FrameUnavailableError as exc:
            return False, str(exc)
        self._last_state = state
        cameras: dict[str, object] | None = None
        if source != "world":
            try:
                cameras = self._json("/api/cameras")
            except FrameUnavailableError:
                pass
        requested = (["world"] if source in (None, "world") else []) + [
            key
            for key in VIEWER_CAMERA_SOURCES
            if source in (None, key) and _viewer_provides(cameras, key, source)
        ]
        world_state: _State | None = None
        for key in requested:
            route, media_type = VIEWER_ROUTES[key], SOURCES[key]
            try:
                data = self._get(route, MAX_FRAME_BYTES)
                frame_state = state
                sequence = state["state_version"]
                if key == "world":
                    after = _state(self._json("/api/state"))
                    self._last_state = after
                    if after["state_version"] != state["state_version"]:
                        raise FrameUnavailableError(
                            "viewer state changed during frame fetch"
                        )
                    frame_state = after
                    world_state = after
                else:
                    camera = (cameras or {}).get(key.removeprefix("camera_"))
                    camera_sequence = (
                        camera.get("sequence") if isinstance(camera, dict) else None
                    )
                    if isinstance(camera_sequence, int) and not isinstance(
                        camera_sequence, bool
                    ):
                        sequence = camera_sequence
                previous = self._frames.get(key)
                etag = (
                    previous.etag
                    if previous is not None and previous.data == data
                    else _etag(data)
                )
                self._frames[key] = FrameResult(
                    data,
                    etag,
                    sequence,
                    frame_state["mission_time_seconds"],
                    media_type,
                )
            except FrameUnavailableError:
                continue
        self._persist(world_state)
        return True, None

    def _persist(self, world_state: _State | None) -> None:
        frame = self._frames.get("world") if world_state is not None else None
        if frame == self._persisted_world and self._last_state == self._persisted_state:
            return
        with _disk_lock(self._directory):
            if frame is not None and frame != self._persisted_world:
                if (
                    self._persisted_world is None
                    or frame.etag != self._persisted_world.etag
                ):
                    _atomic_write(self._directory / "latest.png", frame.data)
                metadata = {
                    "sequence": frame.sequence,
                    "mission_time_seconds": frame.mission_time_seconds,
                    "etag": frame.etag,
                    "media_type": frame.media_type,
                    "state": world_state,
                }
                _atomic_write(
                    self._directory / "latest.json",
                    json.dumps(metadata).encode("utf-8"),
                )
                self._disk_version = _file_version(self._directory / "latest.json")
                self._persisted_world = frame
            if (
                self._last_state is not None
                and self._last_state != self._persisted_state
            ):
                _atomic_write(
                    self._directory / "last-state.json",
                    json.dumps(self._last_state).encode("utf-8"),
                )
                self._persisted_state = self._last_state

    def _load_cached(self) -> None:
        if not self._directory.exists():
            return
        with _disk_lock(self._directory):
            stored_state = self._json_files.get(self._directory / "last-state.json")
            if stored_state is not None:
                try:
                    self._last_state = _state(stored_state)
                    self._persisted_state = self._last_state
                except FrameUnavailableError:
                    pass
            self._load_world()
            for source in CAMERA_SOURCES:
                self._load_camera(source)
            self._capture_status = _capture_status(
                self._json_files.get(self._directory / CAPTURE_STATUS_FILE)
            )
            self._airsim_status = airsim_status(
                self._json_files.get(self._directory / AIRSIM_STATUS_FILE)
            )

    def _load_world(self) -> None:
        version = _file_version(self._directory / "latest.json")
        if version is None or version == self._disk_version:
            return
        metadata = self._json_files.get(self._directory / "latest.json")
        if metadata is None:
            return
        try:
            path = self._directory / "latest.png"
            if path.stat().st_size > MAX_FRAME_BYTES:
                return
            data = path.read_bytes()
            stored_frame_state = metadata["state"]
            if not isinstance(stored_frame_state, dict):
                return
            state = _state(stored_frame_state)
            if (
                not data
                or metadata["etag"] != _etag(data)
                or metadata["sequence"] != state["state_version"]
                or metadata["mission_time_seconds"] != state["mission_time_seconds"]
                or metadata["media_type"] != "image/png"
            ):
                return
            frame = FrameResult(
                data,
                _etag(data),
                state["state_version"],
                state["mission_time_seconds"],
                "image/png",
            )
        except (OSError, KeyError, TypeError, FrameUnavailableError):
            return
        self._frames["world"] = frame
        self._persisted_world = frame
        if self._last_state is None:
            self._last_state = state
        self._disk_version = version

    def _load_camera(self, source: str) -> None:
        image_path, metadata_path = _camera_paths(self._directory, source)
        version = _file_version(metadata_path)
        if version is None or version == self._camera_versions.get(source):
            return
        metadata = self._json_files.get(metadata_path)
        if metadata is None:
            return
        # Files change only under the disk lock; a mismatch is corruption.
        self._camera_versions[source] = version
        try:
            if image_path.stat().st_size > MAX_FRAME_BYTES:
                return
            data = image_path.read_bytes()
        except OSError:
            return
        sequence = metadata.get("sequence")
        mission_time = metadata.get("mission_time_seconds")
        if (
            not data.startswith(b"\xff\xd8")
            or metadata.get("source") != source
            or metadata.get("media_type") != "image/jpeg"
            or metadata.get("etag") != _etag(data)
            or not _count(sequence)
            or not _seconds(mission_time)
        ):
            return
        self._frames[source] = FrameResult(
            data,
            _etag(data),
            cast(int, sequence),
            None if mission_time is None else float(cast(float, mission_time)),
            "image/jpeg",
        )


def _capture_status(payload: Mapping[str, object] | None) -> dict[str, object] | None:
    if payload is None:
        return None
    state = payload.get("state")
    reason = payload.get("reason")
    cameras = payload.get("cameras")
    if (
        state not in CAPTURE_STATES
        or (reason is not None and not isinstance(reason, str))
        or not isinstance(cameras, dict)
    ):
        return None
    return {"state": state, "reason": reason, "cameras": dict(cameras)}


def _optional_number(value: object) -> bool:
    return value is None or (
        isinstance(value, (float, int))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _annotation_status(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    objects = value.get("objects")
    if (
        value.get("kind") not in ANNOTATION_KINDS
        or not isinstance(value.get("disclosure"), str)
        or value.get("match") not in ("exact", "none")
        or not _count(objects)
        or not _optional_number(value.get("perception_mission_time_seconds"))
    ):
        return None
    return {
        key: value[key]
        for key in ("kind", "disclosure", "match", "objects", "perception_mission_time_seconds")
    }


AIRSIM_NUMBER_FIELDS = (
    "frame_mission_time_seconds",
    "world_mission_time_seconds",
    "lag_seconds",
    "ship_phase_error_seconds",
)


def airsim_status(payload: Mapping[str, object] | None) -> dict[str, object] | None:
    """The contract ``world.airsim`` object, or ``None`` when absent/invalid."""

    if payload is None:
        return None
    reason = payload.get("reason")
    annotation = payload.get("annotation")
    checked = None if annotation is None else _annotation_status(annotation)
    if (
        payload.get("mode") not in AIRSIM_MODES
        or payload.get("perception") not in ("off", "ideal", "yolo")
        or payload.get("state") not in CAPTURE_STATES
        or (reason is not None and not isinstance(reason, str))
        or not all(_optional_number(payload.get(key)) for key in AIRSIM_NUMBER_FIELDS)
        or (annotation is not None and checked is None)
    ):
        return None
    return {
        "mode": payload["mode"],
        "perception": payload["perception"],
        "state": payload["state"],
        "reason": reason,
        **{key: payload.get(key) for key in AIRSIM_NUMBER_FIELDS},
        "annotation": checked,
    }


class CameraFrameStore:
    """Latest-only AirSim camera generations and status under ``world-frames/``.

    ``cameras`` maps each persisted source (including ``camera_front_annotated``)
    to the AirSim camera it shows. One store writes a run's camera frames:
    :class:`CameraCapture` for scene-clock runs, the follower for perception-off
    runs. Identical pixels are not a new generation; a recreated store continues
    the run's sequence from disk.
    """

    def __init__(
        self, run_root: Path, *, cameras: Mapping[str, str], vehicle_name: str
    ) -> None:
        if not cameras or set(cameras) - set(CAMERA_SOURCES):
            raise ValueError("camera frame store sources must be camera sources")
        self.directory = Path(run_root) / "world-frames"
        self._cameras = dict(cameras)
        self._vehicle = vehicle_name
        self._sequence = 0
        self._etags: dict[str, str] = {}
        self._status: tuple[str, str | None] | None = None
        self._airsim: dict[str, object] | None = None

    @property
    def sequence(self) -> int:
        return self._sequence

    def load_persisted(self) -> None:
        if not self.directory.exists():
            return
        with _disk_lock(self.directory):
            for source in self._cameras:
                try:
                    metadata = json.loads(
                        _camera_paths(self.directory, source)[1].read_bytes()
                    )
                    sequence, etag = metadata["sequence"], metadata["etag"]
                except (OSError, ValueError, KeyError, TypeError):
                    continue
                if _count(sequence) and isinstance(etag, str):
                    self._sequence = max(self._sequence, sequence)
                    self._etags[source] = etag

    def persist(
        self,
        captured: Mapping[str, bytes],
        mission_time: float | None,
        extra: Mapping[str, Mapping[str, object]] | None = None,
        *,
        stopped: Callable[[], bool] = lambda: False,
    ) -> None:
        """Write each changed JPEG and its metadata as one generation."""

        if not captured:
            return
        self._sequence += 1
        with _disk_lock(self.directory):
            if stopped():
                return
            for source, data in captured.items():
                etag = _etag(data)
                if self._etags.get(source) == etag:
                    continue
                image_path, metadata_path = _camera_paths(self.directory, source)
                _atomic_write(image_path, data)
                _atomic_write(
                    metadata_path,
                    json.dumps(
                        {
                            "source": source,
                            "sequence": self._sequence,
                            "etag": etag,
                            "media_type": "image/jpeg",
                            "mission_time_seconds": mission_time,
                            "camera_name": self._cameras[source],
                            "vehicle_name": self._vehicle,
                            "captured_at_unix": time.time(),
                            **((extra or {}).get(source) or {}),
                        }
                    ).encode("utf-8"),
                )
                self._etags[source] = etag

    def report(self, state: str, reason: str | None) -> None:
        """Record a ``camera-capture.json`` transition (best-effort)."""

        if self._status == (state, reason):
            return
        cameras = {
            source: name
            for source, name in self._cameras.items()
            if source in VIEWER_CAMERA_SOURCES
        }
        try:
            with _disk_lock(self.directory):
                _atomic_write(
                    self.directory / CAPTURE_STATUS_FILE,
                    json.dumps(
                        {
                            "state": state,
                            "reason": reason,
                            "cameras": cameras,
                            "vehicle_name": self._vehicle,
                            "updated_at_unix": time.time(),
                        }
                    ).encode("utf-8"),
                )
        except OSError:
            return  # retried on the next transition or tick
        self._status = (state, reason)

    def write_airsim(self, status: Mapping[str, object]) -> None:
        """Atomically replace ``airsim.json`` when the contract fields change."""

        checked = airsim_status(status)
        if checked is None:
            raise ValueError("invalid AirSim status")
        if checked == self._airsim:
            return
        try:
            with _disk_lock(self.directory):
                _atomic_write(
                    self.directory / AIRSIM_STATUS_FILE,
                    json.dumps({**status, "updated_at_unix": time.time()}).encode("utf-8"),
                )
        except OSError:
            return
        self._airsim = checked


class CameraCapture:
    """Run-Worker-owned, best-effort, latest-only AirSim camera capture.

    Used for scene-clock (perception) runs. Captures the configured Scene
    cameras at about ``1 / period_seconds`` Hz and persists each changed JPEG
    plus metadata under ``world-frames/``. The only SDK call is
    ``simGetImages``: no API control, arming, camera pose, pause or clock
    writes. With an ``annotator`` it also requests the front segmentation when
    needed and publishes ``camera_front_annotated`` with the perception
    module's output for the same frame. The msgpack-rpc client binds a Tornado
    IOLoop to its creating thread, so it is created inside the capture thread.
    ``sdk`` (an ``airsim`` compatible module) and ``fetch`` may be injected.
    """

    def __init__(
        self,
        run_root: Path,
        *,
        cameras: Mapping[str, str],
        vehicle_name: str,
        rpc_port: int,
        viewer_port: int,
        airsim_settings: Path,
        annotator: PerceptionAnnotator | None = None,
        period_seconds: float = 0.5,
        sdk_timeout_seconds: int = 2,
        sdk: Any | None = None,
        fetch: Fetch | None = None,
    ) -> None:
        if set(cameras) - set(VIEWER_CAMERA_SOURCES) or not cameras:
            raise ValueError("camera capture sources must be physical camera sources")
        if period_seconds <= 0 or sdk_timeout_seconds <= 0:
            raise ValueError("camera capture period and timeout must be positive")
        self._cameras = dict(cameras)
        self._annotator = annotator if "camera_front" in cameras else None
        persisted = dict(cameras)
        if self._annotator is not None:
            persisted["camera_front_annotated"] = cameras["camera_front"]
        self._store = CameraFrameStore(
            run_root, cameras=persisted, vehicle_name=vehicle_name
        )
        self._vehicle = vehicle_name
        self._rpc_port = rpc_port
        self._state_url = f"http://{LOOPBACK}:{viewer_port}/api/state"
        self._settings = Path(airsim_settings)
        self._period = period_seconds
        self._sdk_timeout = sdk_timeout_seconds
        self._fetch = fetch or _fetch
        self._stop = Event()
        self._thread = Thread(target=self._run, name="camera-capture", daemon=True)
        self._sdk = sdk
        self._encode: Callable[..., bytes] | None = None
        self._load_error: str | None = None
        try:
            # Import here, not in the thread: module imports stay single-threaded.
            if self._sdk is None:
                self._sdk = importlib.import_module("airsim")
            from onr_physical_runtime.sim.viewer_frames import encode_bgr_jpeg

            self._encode = encode_bgr_jpeg
        except Exception as exc:  # noqa: BLE001 - images are best-effort
            self._load_error = f"airsim_sdk_unavailable: {type(exc).__name__}"

    def start(self) -> None:
        if self._load_error is not None:
            self._report("unavailable", self._load_error)
            return
        self._thread.start()

    def stop(self, timeout: float | None = None) -> bool:
        """Stop capturing; return whether the thread finished within ``timeout``.

        No frame is persisted after this call. The default bound covers one
        in-flight SDK request so a hung engine cannot stall worker teardown.
        """
        self._stop.set()
        if self._thread.ident is None:
            return True
        self._thread.join(
            self._sdk_timeout + self._period + 2.0 if timeout is None else timeout
        )
        return not self._thread.is_alive()

    def _run(self) -> None:
        sdk = self._sdk
        assert sdk is not None  # start() rejects a failed SDK import.
        client: Any = None
        failure: str | None = None
        try:
            cameras = self._configured_cameras()
            unconfigured = ", ".join(
                sorted(
                    name
                    for source, name in self._cameras.items()
                    if source not in cameras
                )
            )
            if not cameras:
                failure = f"camera_not_configured: {unconfigured}"
                return
            self._store.load_persisted()
            delay = 0.0
            while not self._stop.wait(delay):
                started = time.monotonic()
                try:
                    if client is None:
                        client = sdk.VehicleClient(
                            ip=LOOPBACK,
                            port=self._rpc_port,
                            timeout_value=self._sdk_timeout,
                        )
                    failure = self._capture(client, cameras)
                    if failure is None and unconfigured:
                        failure = f"camera_not_configured: {unconfigured}"
                except Exception as exc:  # noqa: BLE001 - SDK errors vary by state
                    _close_client(client)
                    client = None
                    failure = f"airsim_capture_failed: {type(exc).__name__}"
                if failure is None:
                    self._report("capturing", None)
                else:
                    self._report("unavailable", failure)
                delay = max(0.0, self._period - (time.monotonic() - started))
        finally:
            _close_client(client)
            self._report("stopped", failure)

    def _configured_cameras(self) -> dict[str, str]:
        try:
            settings = json.loads(self._settings.read_text(encoding="utf-8"))
            configured = settings["Vehicles"][self._vehicle]["Cameras"]
        except (OSError, ValueError, KeyError, TypeError):
            return {}
        if not isinstance(configured, dict):
            return {}
        return {
            source: name
            for source, name in self._cameras.items()
            if name in configured
        }

    def _capture(self, client: Any, cameras: Mapping[str, str]) -> str | None:
        sdk = self._sdk
        assert sdk is not None
        annotator = self._annotator if "camera_front" in cameras else None
        segmentation = annotator is not None and annotator.needs_segmentation
        before = self._mission_time()
        requests = [
            sdk.ImageRequest(name, sdk.ImageType.Scene, False, False)
            for name in cameras.values()
        ]
        if segmentation:
            requests.append(
                sdk.ImageRequest(
                    cameras["camera_front"], sdk.ImageType.Segmentation, False, False
                )
            )
        responses = client.simGetImages(requests, vehicle_name=self._vehicle)
        if len(responses) != len(requests):
            return "airsim_capture_incomplete"
        after = self._mission_time() if annotator is not None else before
        # A Mission time that moved during the request leaves the frame's time unknown.
        mission_time = before if before == after else None
        encode = cast(Callable[..., bytes], self._encode)
        captured: dict[str, bytes] = {}
        extra: dict[str, dict[str, object]] = {}
        scene: dict[str, Any] = {}
        for source, response in zip(cameras, responses, strict=False):
            try:
                captured[source] = encode(
                    bytes(response.image_data_uint8),
                    int(response.width),
                    int(response.height),
                )
            except (AttributeError, OSError, TypeError, ValueError):
                # AirSim answers an unrenderable camera with an empty image.
                continue
            scene[source] = response
            stamp = getattr(response, "time_stamp", None)
            if isinstance(stamp, int) and not isinstance(stamp, bool):
                extra[source] = {"sensor_timestamp_ns": stamp}
        if annotator is not None and "camera_front" in scene:
            self._annotate(
                annotator,
                scene["camera_front"],
                responses[-1] if segmentation else None,
                mission_time,
                captured,
                extra,
            )
        if captured:
            self._store.persist(captured, before, extra, stopped=self._stop.is_set)
        missing = sorted(set(cameras) - set(scene))
        if not missing:
            return None
        return "airsim_frame_unavailable: " + ", ".join(missing)

    def _annotate(
        self,
        annotator: PerceptionAnnotator,
        front: Any,
        segmentation: Any | None,
        mission_time: float | None,
        captured: dict[str, bytes],
        extra: dict[str, dict[str, object]],
    ) -> None:
        stamp = int(getattr(front, "time_stamp", 0) or 0)
        try:
            id_map = (
                None
                if segmentation is None
                else segmentation_ids(segmentation, (int(front.height), int(front.width)))
            )
            annotation = annotator.annotate(
                sensor_timestamp_ns=stamp,
                mission_time_seconds=mission_time,
                id_map=id_map,
            )
            captured["camera_front_annotated"] = render_annotated(
                scene_rgb(front), annotation
            )
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Unable to annotate the AirSim front camera: %s", exc)
            return
        extra["camera_front_annotated"] = {
            "sensor_timestamp_ns": stamp,
            "annotation": annotation.status(),
            "boxes": boxes_summary(annotation.boxes),
        }
        self._store.write_airsim(
            {
                "mode": "scene_clock",
                "perception": annotator.perception,
                "state": "capturing",
                "reason": None,
                "frame_mission_time_seconds": mission_time,
                "world_mission_time_seconds": mission_time,
                "lag_seconds": None,
                "ship_phase_error_seconds": None,
                "annotation": annotation.status(),
            }
        )

    def _mission_time(self) -> float | None:
        """Viewer Mission time, if known."""
        try:
            payload = json.loads(self._fetch(self._state_url, 0.25, MAX_JSON_BYTES))
            if isinstance(payload, dict):
                return _state(payload)["mission_time_seconds"]
        except (
            HTTPError,
            URLError,
            OSError,
            TimeoutError,
            ValueError,
            UnicodeError,
            FrameUnavailableError,
        ):
            pass
        return None

    def _report(self, state: str, reason: str | None) -> None:
        self._store.report(state, reason)
        if self._annotator is None or state == "capturing":
            return
        self._store.write_airsim(
            {
                "mode": "scene_clock",
                "perception": self._annotator.perception,
                "state": state,
                "reason": reason,
                **dict.fromkeys(AIRSIM_NUMBER_FIELDS),
                "annotation": None,
            }
        )


def _close_client(client: Any) -> None:
    """Close an msgpack-rpc session and the IOLoop it bound to this thread."""
    session = getattr(client, "client", None)
    if session is None:
        return
    try:
        session.close()
    except Exception:
        logger.exception("Unable to close AirSim camera RPC session")
    ioloop = getattr(getattr(session, "_loop", None), "_ioloop", None)
    try:
        if ioloop is not None:
            ioloop.close(all_fds=True)
    except Exception:
        logger.exception("Unable to close AirSim camera event loop")


__all__ = [
    "CameraCapture",
    "CameraFrameStore",
    "FrameResult",
    "FrameUnavailableError",
    "WorldView",
    "airsim_status",
]
