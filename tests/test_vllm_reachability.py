from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

import pytest

from onr.adapters.vllm_reachability import (
    VLLMReachabilityError,
    probe_vllm_reachability,
)


MODEL = "google/gemma-4-31B-it"


class _Handler(BaseHTTPRequestHandler):
    model_ids = [MODEL]
    completion_payload: object = {"choices": [{"message": {"content": "O"}}]}
    requests: list[tuple[str, dict[str, Any] | None]] = []

    def _send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self.__class__.requests.append((self.path, None))
        if self.path == "/v1/models":
            self._send_json({"data": [{"id": model} for model in self.model_ids]})
        elif self.path == "/health":
            self._send_json({"status": "ok"})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(length))
        self.__class__.requests.append((self.path, payload))
        self._send_json(self.completion_payload)

    def log_message(self, format: str, *args: object) -> None:
        _ = format, args
        pass


@pytest.fixture
def fake_server() -> Any:
    _Handler.requests = []
    _Handler.completion_payload = {"choices": [{"message": {"content": "O"}}]}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_probe_checks_model_health_and_real_completion(fake_server: str) -> None:
    probe_vllm_reachability(fake_server, MODEL, api_key="test-key", timeout=1)

    assert [path for path, _ in _Handler.requests] == [
        "/v1/models",
        "/health",
        "/v1/chat/completions",
    ]
    completion = _Handler.requests[-1][1]
    assert completion is not None
    assert completion["model"] == MODEL
    assert completion["max_tokens"] == 1


def test_probe_reports_missing_model_and_response_diagnostics(fake_server: str) -> None:
    _Handler.model_ids = ["other-model"]
    try:
        with pytest.raises(VLLMReachabilityError, match=f"model={MODEL}") as error:
            probe_vllm_reachability(fake_server, MODEL, timeout=1)
    finally:
        _Handler.model_ids = [MODEL]

    message = str(error.value)
    assert "/v1/models" in message
    assert "other-model" in message


def test_probe_rejects_malformed_successful_completion(fake_server: str) -> None:
    _Handler.completion_payload = {"id": "completion-without-choices"}

    with pytest.raises(VLLMReachabilityError, match=f"model={MODEL}") as error:
        probe_vllm_reachability(fake_server, MODEL, timeout=1)

    message = str(error.value)
    assert "/v1/chat/completions" in message
    assert "completion-without-choices" in message
