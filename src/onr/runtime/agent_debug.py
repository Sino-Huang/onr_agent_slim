"""Mission-scoped capture of completed agent model and tool invocations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import tempfile
from threading import Lock
from typing import Any
from urllib.parse import quote
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler


_PRIVATE_REASONING_KEYS = {"reasoning", "reasoning_content", "reasoning_details"}


def _json_safe(value: Any) -> Any:
    """Convert callback values to JSON data while dropping private reasoning."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (Path, UUID, datetime)):
        return str(value)
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
            if str(key) not in _PRIVATE_REASONING_KEYS
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_safe(model_dump(mode="json"))
        except TypeError:
            return _json_safe(model_dump())
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_safe(to_dict())
    return repr(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tool_names(invocation_params: object) -> list[str]:
    if not isinstance(invocation_params, Mapping):
        return []
    advertised = invocation_params.get("tools")
    if advertised is None:
        advertised = invocation_params.get("functions")
    if not isinstance(advertised, Sequence) or isinstance(advertised, (str, bytes)):
        return []
    names: list[str] = []
    for item in advertised:
        candidate: object = item
        if isinstance(item, Mapping) and isinstance(item.get("function"), Mapping):
            candidate = item["function"]
        name = candidate.get("name") if isinstance(candidate, Mapping) else getattr(candidate, "name", None)
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def _invocation_name(serialized: object, invocation_params: object, fallback: str) -> str:
    for candidate in (invocation_params, serialized):
        if not isinstance(candidate, Mapping):
            continue
        for key in ("name", "model_name", "model"):
            value = candidate.get(key)
            if isinstance(value, str) and value:
                return value
        identifier = candidate.get("id")
        if isinstance(identifier, Sequence) and not isinstance(identifier, (str, bytes)):
            if identifier and isinstance(identifier[-1], str):
                return identifier[-1]
    return fallback


class AgentDebugRecorder:
    """Persist durable v1 profiles and completed callback invocations."""

    def __init__(self, root: Path, mission_id: str) -> None:
        self._directory = Path(root) / quote(mission_id, safe="._-")
        self._lock = Lock()
        self._sequence = max(
            (
                int(path.stem)
                for path in self._directory.glob("[0-9]*.json")
                if path.stem.isdigit()
            ),
            default=0,
        )
        self._profiles: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, dict[str, Any]] = {}

    def record_profile(
        self,
        agent_role: str,
        skills: Sequence[Mapping[str, object]],
        tools: Sequence[str],
    ) -> None:
        artifact = {
            "schema_version": 1,
            "agent_role": agent_role,
            "skills": [
                {
                    "name": str(skill["name"]),
                    "version": str(skill["version"]),
                    "path": str(skill["path"]),
                }
                for skill in skills
            ],
            "tools": list(dict.fromkeys(tools)),
        }
        with self._lock:
            self._profiles[agent_role] = artifact
            path = self._directory / "profiles" / f"{quote(agent_role, safe='._-')}.json"
            self._write_atomic(path, artifact)

    def callback_for(self, agent_role: str) -> BaseCallbackHandler:
        return _AgentDebugCallback(self, agent_role)

    def _update_tools(self, agent_role: str, tools: Sequence[str]) -> None:
        if not tools:
            return
        with self._lock:
            profile = self._profiles.get(agent_role)
            if profile is None or profile["tools"] == list(tools):
                return
            updated = {**profile, "tools": list(dict.fromkeys(tools))}
            self._profiles[agent_role] = updated
            path = self._directory / "profiles" / f"{quote(agent_role, safe='._-')}.json"
            self._write_atomic(path, updated)

    def _start(
        self,
        *,
        run_id: UUID,
        parent_run_id: UUID | None,
        agent_role: str,
        kind: str,
        name: str,
        input_value: object,
    ) -> None:
        with self._lock:
            self._pending[str(run_id)] = {
                "parent_id": str(parent_run_id) if parent_run_id is not None else None,
                "agent_role": agent_role,
                "kind": kind,
                "name": name,
                "input": _json_safe(input_value),
                "started_at": _utc_now(),
            }

    def _finish(
        self,
        run_id: UUID,
        *,
        output: object = None,
        error: BaseException | None = None,
    ) -> None:
        with self._lock:
            pending = self._pending.pop(str(run_id), None)
            if pending is None:
                return
            self._sequence += 1
            artifact = {
                "schema_version": 1,
                "sequence": self._sequence,
                "invocation_id": str(run_id),
                **pending,
                "output": _json_safe(output),
                "error": _json_safe(error),
                "finished_at": _utc_now(),
            }
            path = self._directory / f"{self._sequence:020d}.json"
            self._write_atomic(path, artifact)

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


class _AgentDebugCallback(BaseCallbackHandler):
    def __init__(self, recorder: AgentDebugRecorder, agent_role: str) -> None:
        self._recorder = recorder
        self._agent_role = agent_role

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        invocation_params = kwargs.get("invocation_params")
        self._recorder._update_tools(self._agent_role, _tool_names(invocation_params))
        self._recorder._start(
            run_id=run_id,
            parent_run_id=parent_run_id,
            agent_role=self._agent_role,
            kind="llm",
            name=_invocation_name(serialized, invocation_params, "chat_model"),
            input_value={
                "messages": messages,
                "invocation_params": invocation_params,
            },
        )

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._recorder._finish(run_id, output=response)

    def on_llm_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: Any
    ) -> None:
        self._recorder._finish(run_id, error=error)

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self._recorder._start(
            run_id=run_id,
            parent_run_id=parent_run_id,
            agent_role=self._agent_role,
            kind="tool",
            name=_invocation_name(serialized, None, "tool"),
            input_value=inputs if inputs is not None else input_str,
        )

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._recorder._finish(run_id, output=output)

    def on_tool_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: Any
    ) -> None:
        self._recorder._finish(run_id, error=error)


__all__ = ["AgentDebugRecorder"]
