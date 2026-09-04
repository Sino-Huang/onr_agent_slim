"""Read-only Socket.IO feed for live physical world-model frames."""

from __future__ import annotations

import base64
import threading
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import socketio

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _world_model_url(value: str) -> str:
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(
            "world-model URL must be a plain loopback HTTP origin"
        ) from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("world-model URL must be a plain loopback HTTP origin")
    return value.rstrip("/")


class WorldModelFeed:
    """Cache the latest world-model Socket.IO frame and public state."""

    def __init__(self, url: str, *, client: Any | None = None) -> None:
        self.url = _world_model_url(url)
        self._client = client or socketio.Client(
            logger=False,
            engineio_logger=False,
            reconnection=True,
        )
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._frame: bytes | None = None
        self._sequence = 0
        self._generation_timestamp_s: float | None = None
        self._state: dict[str, object] = {}
        self._error: str | None = None
        self._client.on("connect", self._on_connect)
        self._client.on("disconnect", self._on_disconnect)
        self._client.on("connect_error", self._on_connect_error)
        self._client.on("world_update", self._on_world_update)
        self._client.on("state_update", self._on_state_update)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="onr-viewer-world-model",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if getattr(self._client, "connected", False):
            self._client.disconnect()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def payload(self) -> dict[str, object]:
        with self._lock:
            result: dict[str, object] = {
                "available": self._frame is not None,
                "connected": self._connected,
                "status": (
                    "live"
                    if self._connected
                    else "stale"
                    if self._frame is not None
                    else "connecting"
                ),
                "sequence": self._sequence,
                "generation_timestamp_s": self._generation_timestamp_s,
                "state": dict(self._state),
            }
            if self._error is not None:
                result["error"] = self._error
            return result

    def frame(self) -> bytes | None:
        with self._lock:
            return None if self._frame is None else bytes(self._frame)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._client.connect(
                    self.url,
                    transports=("polling",),
                    wait_timeout=3,
                )
                self._client.wait()
            except Exception as exc:  # Socket.IO owns the concrete client errors.
                self._set_disconnected(str(exc))
            finally:
                if getattr(self._client, "connected", False):
                    self._client.disconnect()
            self._stop.wait(1.0)

    def _on_connect(self) -> None:
        with self._lock:
            self._connected = True
            self._error = None

    def _on_disconnect(self, *_: object) -> None:
        self._set_disconnected("world-model stream disconnected")

    def _on_connect_error(self, error: object) -> None:
        self._set_disconnected(str(error))

    def _set_disconnected(self, error: str) -> None:
        with self._lock:
            self._connected = False
            self._error = error

    def _on_world_update(self, payload: object) -> None:
        try:
            if not isinstance(payload, Mapping):
                raise TypeError("payload is not a mapping")
            sequence = payload["sequence"]
            if isinstance(sequence, bool) or not isinstance(sequence, int):
                raise TypeError("sequence is not an integer")
            if payload.get("encoding") != "image/png;base64":
                raise ValueError("frame encoding is not PNG base64")
            frame = base64.b64decode(str(payload["data_base64"]), validate=True)
            if not frame.startswith(_PNG_SIGNATURE):
                raise ValueError("frame is not a PNG")
            generated = payload.get("generation_timestamp_s")
            generation_timestamp_s = (
                float(generated) if isinstance(generated, (int, float)) else None
            )
        except (KeyError, TypeError, ValueError) as exc:
            with self._lock:
                self._error = f"invalid world_update: {exc}"
            return
        with self._lock:
            self._frame = frame
            self._sequence = sequence
            self._generation_timestamp_s = generation_timestamp_s
            self._error = None

    def _on_state_update(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            with self._lock:
                self._error = "invalid state_update: payload is not a mapping"
            return
        with self._lock:
            self._state = {str(key): value for key, value in payload.items()}


__all__ = ["WorldModelFeed"]
