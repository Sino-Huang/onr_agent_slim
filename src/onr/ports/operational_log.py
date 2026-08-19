"""Safe, mission-scoped operational logging seam."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from types import MappingProxyType
from typing import Protocol, TypeAlias, cast


JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

_SAFE_DETAIL_KEYS = {
    "adapter_submission", "command_id", "correlation_id", "environment",
    "error_type", "event_id", "event_kind", "lifecycle", "maneuver_id", "operation",
    "plan_revision", "planner", "request_id", "revision", "sequence", "service",
    "snapshot_id", "source", "state", "status", "target_service", "topic", "transition",
    "transport_event_id", "transport_sequence", "timer_due",
}
_SECRET_RE = re.compile(
    r"(?i)(?:api[_ -]?key|secret|token|password|authorization)\s*[:=]\s*[^\s,;]+|"
    r"\b(?:sk|pk)-[A-Za-z0-9_-]{12,}\b|Bearer\s+[A-Za-z0-9._-]+"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _safe_value(value: object) -> JSONValue | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _SECRET_RE.sub("[REDACTED]", value)[:512]
    return None


def _safe_details(details: Mapping[str, object] | None) -> dict[str, JSONValue]:
    if not details:
        return {}
    result: dict[str, JSONValue] = {}
    for key in sorted(details):
        if key not in _SAFE_DETAIL_KEYS:
            continue
        value = _safe_value(details[key])
        if value is not None:
            result[key] = value
    return result


def _freeze(value: JSONValue) -> JSONValue:
    if isinstance(value, dict):
        return cast(JSONValue, MappingProxyType({key: _freeze(item) for key, item in value.items()}))
    if isinstance(value, list):
        return cast(JSONValue, tuple(_freeze(item) for item in value))
    return value


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class OperationalLogRecord:
    """Immutable, JSON-safe metadata for one mission operation."""

    schema_version: int
    record_id: str
    mission_id: str
    sequence: int
    event_time: str
    source: str
    event_kind: str
    outcome: str
    details: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported operational log schema version")
        if not self.mission_id.strip() or not self.source.strip() or not self.event_kind.strip():
            raise ValueError("operational log identity fields must be non-empty")
        if isinstance(self.sequence, bool) or self.sequence < 1:
            raise ValueError("operational log sequence must be positive")
        if not self.record_id.strip() or not self.outcome.strip():
            raise ValueError("operational log record ID and outcome must be non-empty")
        object.__setattr__(self, "details", _freeze(_safe_details(self.details)))

    @classmethod
    def create(
        cls,
        mission_id: str,
        source: str,
        event_kind: str,
        outcome: str,
        *,
        details: Mapping[str, object] | None = None,
        sequence: int = 1,
        event_time: str | None = None,
        record_id: str | None = None,
    ) -> "OperationalLogRecord":
        return cls(
            1,
            record_id or f"{mission_id}:{sequence}:{event_kind}",
            mission_id,
            sequence,
            event_time or _utc_now(),
            source,
            event_kind,
            outcome,
            _safe_details(details),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "record_id": self.record_id,
            "mission_id": self.mission_id,
            "sequence": self.sequence,
            "event_time": self.event_time,
            "source": self.source,
            "event_kind": self.event_kind,
            "outcome": self.outcome,
            "details": _json_value(self.details),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "OperationalLogRecord":
        details = value.get("details", {})
        if not isinstance(details, Mapping):
            raise ValueError("operational log details must be a mapping")
        schema_version = value.get("schema_version")
        sequence = value.get("sequence")
        if isinstance(schema_version, bool) or not isinstance(schema_version, (str, int)):
            raise ValueError("operational log schema version must be an integer")
        if isinstance(sequence, bool) or not isinstance(sequence, (str, int)):
            raise ValueError("operational log sequence must be an integer")
        return cls(
            int(cast(str | int, schema_version)), str(value["record_id"]), str(value["mission_id"]),
            int(cast(str | int, sequence)), str(value["event_time"]), str(value["source"]),
            str(value["event_kind"]), str(value["outcome"]), _safe_details(details),
        )


class OperationalLog(Protocol):
    """Append-only mission log with replay from a sequence cursor."""

    def append(self, record: OperationalLogRecord) -> OperationalLogRecord: ...

    def emit(
        self,
        mission_id: str,
        source: str,
        event_kind: str,
        outcome: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> OperationalLogRecord: ...

    def replay(self, mission_id: str, *, after_sequence: int = 0) -> tuple[OperationalLogRecord, ...]: ...

    def read_after_sequence(self, mission_id: str, sequence: int) -> tuple[OperationalLogRecord, ...]: ...
