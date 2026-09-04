from __future__ import annotations

import base64

import pytest

from onr.viewer.world_model import WorldModelFeed

_PNG = b"\x89PNG\r\n\x1a\nworld-model-frame"


class _FakeSocketClient:
    def __init__(self) -> None:
        self.connected = False
        self.handlers: dict[str, object] = {}

    def on(self, event: str, handler: object) -> None:
        self.handlers[event] = handler

    def disconnect(self) -> None:
        self.connected = False


def test_world_model_feed_caches_socket_frame_and_state() -> None:
    client = _FakeSocketClient()
    feed = WorldModelFeed("http://127.0.0.1:5066", client=client)

    client.handlers["connect"]()  # type: ignore[operator]
    client.handlers["world_update"](  # type: ignore[operator]
        {
            "sequence": 7,
            "encoding": "image/png;base64",
            "data_base64": base64.b64encode(_PNG).decode("ascii"),
            "generation_timestamp_s": 123.5,
        }
    )
    client.handlers["state_update"](  # type: ignore[operator]
        {"mission_id": "mission:demo", "mission_time_seconds": 4.5}
    )

    assert feed.frame() == _PNG
    assert feed.payload() == {
        "available": True,
        "connected": True,
        "status": "live",
        "sequence": 7,
        "generation_timestamp_s": 123.5,
        "state": {
            "mission_id": "mission:demo",
            "mission_time_seconds": 4.5,
        },
    }


def test_world_model_feed_rejects_invalid_frame_without_replacing_cache() -> None:
    client = _FakeSocketClient()
    feed = WorldModelFeed("http://localhost:5066", client=client)

    client.handlers["world_update"](  # type: ignore[operator]
        {
            "sequence": 1,
            "encoding": "image/png;base64",
            "data_base64": base64.b64encode(b"not-a-png").decode("ascii"),
        }
    )

    assert feed.frame() is None
    payload = feed.payload()
    assert payload["available"] is False
    assert payload["sequence"] == 0
    assert str(payload["error"]).startswith("invalid world_update:")


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:5066",
        "http://example.com:5066",
        "http://127.0.0.1:5066/path",
        "http://127.0.0.1:not-a-port",
        "http://user:password@127.0.0.1:5066",
    ],
)
def test_world_model_feed_accepts_only_plain_loopback_origin(url: str) -> None:
    with pytest.raises(ValueError, match="plain loopback HTTP origin"):
        WorldModelFeed(url, client=_FakeSocketClient())
