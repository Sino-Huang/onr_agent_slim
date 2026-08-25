"""Immutable Transport Events for public planning artifacts."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from onr.contracts.planning import NormalizedPlan, PlannerChoice, PlanningOutcome


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _freeze_json(value: object, label: str = "payload") -> object:
    """Validate and freeze a JSON value without accepting NaN or infinity."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} must contain finite JSON numbers")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} object keys must be strings")
            frozen[key] = _freeze_json(item, label)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, label) for item in value)
    raise ValueError(f"{label} must contain only JSON-safe values")


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        _json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _schema_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("schema version must be a positive integer")
    return value


def _sequence(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("sequence must be a non-negative integer")
    return value


def _payload(value: object) -> Mapping[str, object]:
    frozen = _freeze_json(value)
    if not isinstance(frozen, Mapping):
        raise ValueError("payload must be a JSON object")
    return frozen


class CommandStatus(StrEnum):
    """Wire status values for command receipts and outcomes."""

    ACCEPTED = "accepted"
    ALREADY_IN_FLIGHT = "already_in_flight"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TransportEvent:
    """Immutable, JSON-safe fact published to a mission stream."""

    schema_version: int
    event_id: str
    mission_id: str
    sequence: int
    event_kind: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        object.__setattr__(self, "event_id", _identity(self.event_id, "event ID"))
        object.__setattr__(self, "mission_id", _identity(self.mission_id, "mission ID"))
        object.__setattr__(self, "sequence", _sequence(self.sequence))
        object.__setattr__(self, "event_kind", _identity(self.event_kind, "event kind"))
        object.__setattr__(self, "payload", _payload(self.payload))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "mission_id": self.mission_id,
            "sequence": self.sequence,
            "event_kind": self.event_kind,
            "payload": _json_value(self.payload),
        }

    def to_canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TransportEvent":
        if not isinstance(value, Mapping) or set(value) != {
            "schema_version", "event_id", "mission_id", "sequence", "event_kind", "payload"
        }:
            raise ValueError("transport event contains unknown or missing fields")
        return cls(**value)

    @classmethod
    def from_json(cls, value: str) -> "TransportEvent":
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("transport event JSON is invalid") from exc
        return cls.from_dict(decoded)


@dataclass(frozen=True, slots=True)
class Command:
    """Immutable single-recipient command."""

    schema_version: int
    command_id: str
    correlation_id: str
    mission_id: str
    target_service: str
    command_kind: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        for name, label in (
            ("command_id", "command ID"),
            ("correlation_id", "correlation ID"),
            ("mission_id", "mission ID"),
            ("target_service", "target service"),
            ("command_kind", "command kind"),
        ):
            object.__setattr__(self, name, _identity(getattr(self, name), label))
        object.__setattr__(self, "payload", _payload(self.payload))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "command_id": self.command_id,
            "correlation_id": self.correlation_id,
            "mission_id": self.mission_id,
            "target_service": self.target_service,
            "command_kind": self.command_kind,
            "payload": _json_value(self.payload),
        }

    def to_canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Command":
        expected = {
            "schema_version", "command_id", "correlation_id", "mission_id",
            "target_service", "command_kind", "payload",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("command contains unknown or missing fields")
        return cls(**value)

    @classmethod
    def from_json(cls, value: str) -> "Command":
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("command JSON is invalid") from exc
        return cls.from_dict(decoded)


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    """Durable acknowledgement that a command was accepted by transport."""

    schema_version: int
    command_id: str
    correlation_id: str
    mission_id: str
    target_service: str
    status: str = CommandStatus.ACCEPTED

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        for name, label in (
            ("command_id", "command ID"),
            ("correlation_id", "correlation ID"),
            ("mission_id", "mission ID"),
            ("target_service", "target service"),
        ):
            object.__setattr__(self, name, _identity(getattr(self, name), label))
        try:
            status = CommandStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError("command receipt status must be accepted") from exc
        if status is not CommandStatus.ACCEPTED:
            raise ValueError("command receipt status must be accepted")
        object.__setattr__(self, "status", status)

    @property
    def receipt_id(self) -> str:
        return self.command_id

    def to_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def to_canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CommandReceipt":
        expected = {"schema_version", "command_id", "correlation_id", "mission_id", "target_service", "status"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("command receipt contains unknown or missing fields")
        return cls(**value)

    @classmethod
    def from_json(cls, value: str) -> "CommandReceipt":
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("command receipt JSON is invalid") from exc
        return cls.from_dict(decoded)


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """Durable terminal command outcome correlated to its command."""

    schema_version: int
    command_id: str
    correlation_id: str
    mission_id: str
    status: str = CommandStatus.COMPLETED
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema_version(self.schema_version))
        for name, label in (
            ("command_id", "command ID"),
            ("correlation_id", "correlation ID"),
            ("mission_id", "mission ID"),
        ):
            object.__setattr__(self, name, _identity(getattr(self, name), label))
        try:
            status = CommandStatus(self.status)
        except (TypeError, ValueError):
            raise ValueError(
                "command outcome status must be accepted, already_in_flight, "
                "completed, or failed"
            )
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "payload", _payload(self.payload))

    @property
    def outcome_id(self) -> str:
        return self.command_id

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "command_id": self.command_id,
            "correlation_id": self.correlation_id,
            "mission_id": self.mission_id,
            "status": self.status,
            "payload": _json_value(self.payload),
        }

    def to_canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CommandOutcome":
        expected = {"schema_version", "command_id", "correlation_id", "mission_id", "status", "payload"}
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("command outcome contains unknown or missing fields")
        return cls(**value)

    @classmethod
    def from_json(cls, value: str) -> "CommandOutcome":
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("command outcome JSON is invalid") from exc
        return cls.from_dict(decoded)


@dataclass(frozen=True, slots=True)
class NormalizedPlanTransportPayload:
    """Immutable transport metadata and one Normalized Plan."""

    mission_id: str
    plan_revision: int
    mission_snapshot_id: str
    planner_choice: PlannerChoice
    source_authority: str
    outcome: PlanningOutcome
    normalized_plan: NormalizedPlan


@dataclass(frozen=True, slots=True)
class NormalizedPlanTransportEvent:
    """Revisioned Transport Event for one Normalized Plan outcome."""

    event_id: str
    sequence: int
    payload: NormalizedPlanTransportPayload
    event_kind: str = field(default="normalized-plan", init=False)
    contract_revision: int = field(default=2, init=False)

    @property
    def mission_id(self) -> str:
        return self.payload.mission_id

    @property
    def plan_revision(self) -> int:
        return self.payload.plan_revision

    @property
    def outcome(self) -> PlanningOutcome:
        return self.payload.outcome

    @property
    def normalized_plan(self) -> NormalizedPlan:
        return self.payload.normalized_plan

def create_normalized_plan_transport_event(
    normalized_plan: NormalizedPlan,
    *,
    event_id: str,
    sequence: int,
) -> NormalizedPlanTransportEvent:
    """Create a stable Transport Event for one Normalized Plan."""

    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event ID must be a non-empty string")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("event sequence must be a non-negative integer")

    payload = NormalizedPlanTransportPayload(
        mission_id=normalized_plan.mission_id,
        plan_revision=normalized_plan.plan_revision,
        mission_snapshot_id=normalized_plan.mission_snapshot_id,
        planner_choice=normalized_plan.planner_choice,
        source_authority=normalized_plan.source_authority,
        outcome=PlanningOutcome(normalized_plan.outcome),
        normalized_plan=normalized_plan,
    )
    return NormalizedPlanTransportEvent(
        event_id=event_id,
        sequence=sequence,
        payload=payload,
    )


def normalized_plan_transport_event_to_wire(
    event: NormalizedPlanTransportEvent,
    *,
    correlation_id: str | None = None,
) -> TransportEvent:
    """Convert the typed planning event to its JSON-safe transport envelope."""

    if not isinstance(event, NormalizedPlanTransportEvent):
        raise TypeError("event must be a NormalizedPlanTransportEvent")
    payload = event.payload
    wire_payload: dict[str, object] = {
        "mission_id": payload.mission_id,
        "mission_snapshot_id": payload.mission_snapshot_id,
        "plan_revision": payload.plan_revision,
        "planner_choice": payload.planner_choice.to_dict(),
        "source_authority": payload.source_authority,
        "outcome": str(payload.outcome),
        "normalized_plan": payload.normalized_plan.to_dict(),
    }
    if correlation_id is not None:
        wire_payload["correlation_id"] = _identity(correlation_id, "correlation ID")
    return TransportEvent(
        schema_version=event.contract_revision,
        event_id=event.event_id,
        mission_id=event.mission_id,
        sequence=event.sequence,
        event_kind=event.event_kind,
        payload=wire_payload,
    )
