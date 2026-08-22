"""Role- and mission-scoped capture of raw OpenAI-compatible responses."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any, cast
from urllib.parse import quote

import httpx

from onr.runtime.debug_context import current_llm_invocation_id

_CHECKPOINT_SECONDS = 1.0


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class _StreamRecord:
    def __init__(
        self,
        recorder: LLMResponseRecorder,
        path: Path,
        artifact: dict[str, Any],
    ) -> None:
        self._recorder = recorder
        self.path = path
        self.artifact = artifact
        self.buffer = b""
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.last_checkpoint = monotonic()
        self.persisted_delta = False
        self.finalized = False

    def set_status(self, status_code: int) -> None:
        self.artifact["status_code"] = status_code

    def feed(self, chunk: bytes) -> None:
        if self.finalized:
            return
        self.buffer += chunk
        while True:
            boundary = self._event_boundary()
            if boundary is None:
                return
            end, separator_length = boundary
            event = self.buffer[:end]
            self.buffer = self.buffer[end + separator_length :]
            self._event(event)
            if self.finalized:
                return

    def _event_boundary(self) -> tuple[int, int] | None:
        candidates = [
            (index, len(separator))
            for separator in (b"\r\n\r\n", b"\n\n")
            if (index := self.buffer.find(separator)) >= 0
        ]
        return min(candidates) if candidates else None

    def _event(self, event: bytes) -> None:
        try:
            text = event.decode("utf-8")
        except UnicodeDecodeError as exc:
            self.finalize("error", error=exc)
            return
        data = "\n".join(
            line[5:].lstrip()
            for line in text.replace("\r\n", "\n").split("\n")
            if line.startswith("data:")
        )
        if not data:
            return
        if data == "[DONE]":
            self.finalize("complete")
            return
        try:
            payload = json.loads(data)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self.finalize("error", error=exc)
            return
        if not isinstance(payload, Mapping):
            self.finalize("error", error=ValueError("SSE data is not an object"))
            return
        self._fold(payload)

    def _fold(self, payload: Mapping[str, object]) -> None:
        generated = False
        response_id = payload.get("id")
        model = payload.get("model")
        if isinstance(response_id, str):
            self.artifact["response_id"] = response_id
        if isinstance(model, str):
            self.artifact["model"] = model
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return
        choice = choices[0]
        if not isinstance(choice, Mapping):
            return
        finish_reason = choice.get("finish_reason")
        if isinstance(finish_reason, str):
            self.artifact["finish_reason"] = finish_reason
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            return
        for key in ("content", "reasoning", "reasoning_content"):
            value = delta.get(key)
            if isinstance(value, str):
                self.artifact[key] += value
                generated |= bool(value)
        details = delta.get("reasoning_details")
        if isinstance(details, list):
            current = self.artifact.get("reasoning_details")
            self.artifact["reasoning_details"] = [
                *(current if isinstance(current, list) else []),
                *details,
            ]
            generated |= bool(details)
        generated |= self._fold_function_call(delta.get("function_call"))
        raw_tool_calls = delta.get("tool_calls")
        if isinstance(raw_tool_calls, list):
            for item in raw_tool_calls:
                generated |= self._fold_tool_call(item)
            self.artifact["tool_calls"] = [
                self.tool_calls[index] for index in sorted(self.tool_calls)
            ]
        if generated or isinstance(finish_reason, str):
            self.checkpoint()

    def _fold_function_call(self, value: object) -> bool:
        if not isinstance(value, Mapping):
            return False
        current = self.artifact.get("function_call")
        call = dict(current) if isinstance(current, Mapping) else {}
        changed = False
        for key in ("name", "arguments"):
            part = value.get(key)
            if isinstance(part, str):
                call[key] = f"{call.get(key, '')}{part}"
                changed |= bool(part)
        self.artifact["function_call"] = call
        return changed

    def _fold_tool_call(self, value: object) -> bool:
        if not isinstance(value, Mapping):
            return False
        index = value.get("index")
        if type(index) is not int or index < 0:
            index = len(self.tool_calls)
        call = self.tool_calls.setdefault(index, {"index": index})
        changed = False
        for key in ("id", "type"):
            part = value.get(key)
            if isinstance(part, str):
                call[key] = part
                changed |= bool(part)
        function = value.get("function")
        if not isinstance(function, Mapping):
            return changed
        current = call.setdefault("function", {})
        for key in ("name", "arguments"):
            part = function.get(key)
            if isinstance(part, str):
                current[key] = f"{current.get(key, '')}{part}"
                changed |= bool(part)
        return changed

    def checkpoint(self) -> None:
        now = monotonic()
        if self.persisted_delta and now - self.last_checkpoint < _CHECKPOINT_SECONDS:
            return
        self.persisted_delta = True
        self.last_checkpoint = now
        self._recorder._replace(self)

    def finalize(
        self, completion_state: str, *, error: BaseException | None = None
    ) -> None:
        if self.finalized:
            return
        self.finalized = True
        self.artifact["completion_state"] = completion_state
        self.artifact["finished_at"] = _utc_now()
        if error is not None:
            self.artifact["error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
        self._recorder._replace(self)


class _ObservedStream(httpx.SyncByteStream):
    def __init__(self, stream: httpx.SyncByteStream, record: _StreamRecord) -> None:
        self._stream = stream
        self._record = record

    def __iter__(self) -> Iterator[bytes]:
        iterator = iter(self._stream)
        while True:
            try:
                chunk = next(iterator)
            except StopIteration:
                break
            except BaseException as exc:
                self._record.finalize("error", error=exc)
                raise
            try:
                self._record.feed(chunk)
            except Exception as exc:  # noqa: BLE001 - debug capture is fail-open
                self._record.finalize("error", error=exc)
            yield chunk
        if not self._record.finalized:
            self._record.finalize("partial")

    def close(self) -> None:
        if not self._record.finalized:
            self._record.finalize("partial")
        self._stream.close()


class _RecordingTransport(httpx.BaseTransport):
    def __init__(
        self, recorder: LLMResponseRecorder, transport: httpx.BaseTransport
    ) -> None:
        self._recorder = recorder
        self._transport = transport

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        record = self._recorder._start_request(request)
        try:
            response = self._transport.handle_request(request)
        except BaseException as exc:
            if record is not None:
                record.finalize("error", error=exc)
            raise
        if record is None:
            return response
        record.set_status(response.status_code)
        if response.status_code >= 400:
            record.finalize(
                "error",
                error=httpx.HTTPStatusError(
                    f"HTTP {response.status_code}", request=request, response=response
                ),
            )
            return response
        request_body = record.artifact.get("request")
        streaming = (
            isinstance(request_body, Mapping) and request_body.get("stream") is True
        )
        if streaming:
            response.stream = _ObservedStream(
                cast(httpx.SyncByteStream, response.stream), record
            )
            return response
        self._recorder._complete_json_response(response, record)
        return response

    def close(self) -> None:
        self._transport.close()


class LLMResponseRecorder:
    """Persist live snapshots of chat completion responses."""

    def __init__(
        self,
        root: Path,
        mission_id: str,
        *,
        role: str = "runtime",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._directory = (
            Path(root) / quote(role, safe="._-") / quote(mission_id, safe="._-")
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
        selected_transport = transport or httpx.HTTPTransport()
        self.http_client = httpx.Client(
            transport=_RecordingTransport(self, selected_transport)
        )

    def _start_request(self, request: httpx.Request) -> _StreamRecord | None:
        if not request.url.path.endswith("/chat/completions"):
            return None
        try:
            with self._lock:
                self._directory.mkdir(parents=True, exist_ok=True)
                self._sequence += 1
                sequence = self._sequence
                path = self._directory / f"{sequence:020d}.json"
                now = _utc_now()
                request_body = self._request_body(request)
                artifact = {
                    "schema_version": 2,
                    "sequence": sequence,
                    "invocation_id": current_llm_invocation_id(),
                    "request": request_body,
                    "response_id": None,
                    "model": (
                        request_body.get("model")
                        if isinstance(request_body, Mapping)
                        and isinstance(request_body.get("model"), str)
                        else None
                    ),
                    "status_code": None,
                    "finish_reason": None,
                    "content": "",
                    "function_call": None,
                    "reasoning": "",
                    "reasoning_content": "",
                    "reasoning_details": None,
                    "tool_calls": [],
                    "error": None,
                    "started_at": now,
                    "updated_at": now,
                    "finished_at": None,
                    "completion_state": "live",
                    "revision": 1,
                }
                self._write_atomic(path, artifact)
            return _StreamRecord(self, path, artifact)
        except Exception:  # noqa: BLE001 - debug capture is fail-open
            return None

    def _complete_json_response(
        self, response: httpx.Response, record: _StreamRecord
    ) -> None:
        try:
            response.read()
            body = response.json()
            if not isinstance(body, Mapping):
                raise TypeError("completion response is not an object")
            response_id = body.get("id")
            model = body.get("model")
            if isinstance(response_id, str):
                record.artifact["response_id"] = response_id
            if isinstance(model, str):
                record.artifact["model"] = model
            choices = body.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ValueError("completion response has no choices")
            choice = choices[0]
            if not isinstance(choice, Mapping):
                raise TypeError("completion choice is not an object")
            message = choice.get("message")
            if not isinstance(message, Mapping):
                raise TypeError("completion choice has no message")
            for key in (
                "content",
                "function_call",
                "reasoning",
                "reasoning_content",
                "reasoning_details",
                "tool_calls",
            ):
                record.artifact[key] = message.get(key)
            record.artifact["finish_reason"] = choice.get("finish_reason")
            record.finalize("complete")
        except Exception as exc:  # noqa: BLE001 - debug capture is fail-open
            record.finalize("error", error=exc)

    @staticmethod
    def _request_body(request: httpx.Request) -> dict[str, Any] | None:
        try:
            body = json.loads(request.content)
        except (ValueError, TypeError, UnicodeError, httpx.RequestNotRead):
            return None
        return body if isinstance(body, dict) else None

    def _replace(self, record: _StreamRecord) -> None:
        try:
            with self._lock:
                record.artifact["revision"] += 1
                record.artifact["updated_at"] = _utc_now()
                self._write_atomic(record.path, record.artifact)
        except Exception:  # noqa: BLE001 - debug capture is fail-open
            return

    @staticmethod
    def _write_atomic(path: Path, artifact: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(artifact, handle, sort_keys=True, separators=(",", ":"))
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
