"""Role- and mission-scoped capture of raw OpenAI-compatible responses."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from threading import Lock
from typing import Any
from urllib.parse import quote

import httpx


class LLMResponseRecorder:
    """Persist selected fields from raw chat completion responses."""

    def __init__(
        self,
        root: Path,
        mission_id: str,
        *,
        role: str = "runtime",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._directory = (
            Path(root)
            / quote(role, safe="._-")
            / quote(mission_id, safe="._-")
        )
        self._lock = Lock()
        self._sequence = max(
            (
                int(path.stem)
                for path in self._directory.glob("[0-9]*.json")
                if path.stem.isdigit()
            ),
            default=0,
        )
        self.http_client = httpx.Client(
            transport=transport,
            event_hooks={"response": [self._record_response]},
        )

    def _record_response(self, response: httpx.Response) -> None:
        if not response.request.url.path.endswith("/chat/completions"):
            return
        try:
            response.read()
            body = response.json()
            if not isinstance(body, dict):
                return
            choices = body.get("choices")
            if not isinstance(choices, list) or not choices:
                return
            choice = choices[0]
            if not isinstance(choice, dict):
                return
            message = choice.get("message")
            if not isinstance(message, dict):
                return
            artifact = {
                "schema_version": 1,
                "request": self._request_body(response.request),
                "response_id": body.get("id"),
                "model": body.get("model"),
                "status_code": response.status_code,
                "finish_reason": choice.get("finish_reason"),
                "content": message.get("content"),
                "function_call": message.get("function_call"),
                "reasoning": message.get("reasoning"),
                "reasoning_content": message.get("reasoning_content"),
                "reasoning_details": message.get("reasoning_details"),
                "tool_calls": message.get("tool_calls"),
            }
            self._write(artifact)
        except (OSError, ValueError, TypeError, json.JSONDecodeError, httpx.HTTPError):
            return

    @staticmethod
    def _request_body(request: httpx.Request) -> dict[str, Any] | None:
        try:
            body = json.loads(request.content)
        except (ValueError, TypeError, UnicodeError, httpx.RequestNotRead):
            return None
        return body if isinstance(body, dict) else None

    def _write(self, artifact: dict[str, Any]) -> None:
        with self._lock:
            self._directory.mkdir(parents=True, exist_ok=True)
            self._sequence += 1
            path = self._directory / f"{self._sequence:020d}.json"
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=self._directory
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(
                        artifact,
                        handle,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, path)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)

    def close(self) -> None:
        self.http_client.close()


__all__ = ["LLMResponseRecorder"]
