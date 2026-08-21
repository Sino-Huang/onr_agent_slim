"""Typed correlated messages exchanged between mission agents."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, cast


class AgentMessageKind(StrEnum):
    """Supported synchronous agent-to-agent request kinds."""

    INVOKE = "invoke"
    QUERY = "query"
    REPORT = "report"
    REPLAN = "replan"


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in cast(Mapping[object, object], value).items():
            if not isinstance(key, str):
                raise TypeError("agent message payload keys must be strings")
            result[key] = _freeze_json(item)
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("agent message payload must contain only JSON-safe values")


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class AgentMessage:
    """One immutable, correlated request between registered agent recipients."""

    message_id: str
    correlation_id: str
    mission_id: str
    plan_revision: int
    sender: str
    recipient: str
    kind: AgentMessageKind | str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        for value, label in (
            (self.message_id, "agent message ID"),
            (self.correlation_id, "agent message correlation ID"),
            (self.mission_id, "agent message Mission ID"),
            (self.sender, "agent message sender"),
            (self.recipient, "agent message recipient"),
        ):
            _text(value, label)
        if (
            isinstance(self.plan_revision, bool)
            or not isinstance(self.plan_revision, int)
            or self.plan_revision < 0
        ):
            raise ValueError("agent message plan revision must be non-negative")
        try:
            object.__setattr__(self, "kind", AgentMessageKind(self.kind))
        except (TypeError, ValueError) as exc:
            raise ValueError("agent message kind is invalid") from exc
        frozen = _freeze_json(self.payload)
        if not isinstance(frozen, Mapping):
            raise TypeError("agent message payload must be a JSON object")
        object.__setattr__(self, "payload", frozen)

    def to_dict(self) -> dict[str, object]:
        return {
            "message_id": self.message_id,
            "correlation_id": self.correlation_id,
            "mission_id": self.mission_id,
            "plan_revision": self.plan_revision,
            "sender": self.sender,
            "recipient": self.recipient,
            "kind": self.kind,
            "payload": _json_value(self.payload),
        }

    def to_canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AgentMessage:
        fields = {
            "message_id",
            "correlation_id",
            "mission_id",
            "plan_revision",
            "sender",
            "recipient",
            "kind",
            "payload",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError("agent message contains unknown or missing fields")
        return cls(**value)

    @classmethod
    def from_json(cls, value: str) -> AgentMessage:
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("agent message JSON is invalid") from exc
        if not isinstance(decoded, Mapping):
            raise TypeError("agent message JSON must contain an object")
        return cls.from_dict(decoded)


__all__ = ["AgentMessage", "AgentMessageKind"]
