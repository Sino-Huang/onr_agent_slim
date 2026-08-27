"""Bounded observations and tick evidence for the deterministic demo environment."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, TypeAlias

from onr.contracts.transport import TransportEvent

EntityId: TypeAlias = str | int


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _entity_id(value: object, label: str) -> EntityId:
    if isinstance(value, bool) or not (
        (isinstance(value, int) and value > 0)
        or (isinstance(value, str) and bool(value.strip()))
    ):
        raise ValueError(f"{label} must be a positive integer or non-empty string")
    return value


def _time(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{label} must be a finite non-negative number")
    return result


def _position(value: object) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("observation position must contain x, y, and z")
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise TypeError("observation position must contain finite numbers")
        selected = float(item)
        if not math.isfinite(selected):
            raise ValueError("observation position must contain finite numbers")
        result.append(selected)
    return tuple(result)  # type: ignore[return-value]


def _uncertainty(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("uncertainty score must be a finite probability")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError("uncertainty score must be a finite probability")
    return result


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("event information keys must be strings")
            result[key] = _freeze_json(item)
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("event information must contain finite JSON values")


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class EntityObservation:
    """One current, bounded entity perception."""

    observation_id: str
    entity_id: EntityId
    position: tuple[float, float, float]
    observed_time: float
    uncertainty_score: float

    def __post_init__(self) -> None:
        _text(self.observation_id, "observation ID")
        _entity_id(self.entity_id, "observation entity ID")
        object.__setattr__(self, "position", _position(self.position))
        object.__setattr__(
            self, "observed_time", _time(self.observed_time, "observed time")
        )
        object.__setattr__(
            self, "uncertainty_score", _uncertainty(self.uncertainty_score)
        )

    @property
    def observation_kind(self) -> str:
        return "entity"

    def to_dict(self) -> dict[str, object]:
        return {
            "observation_kind": self.observation_kind,
            "observation_id": self.observation_id,
            "entity_id": self.entity_id,
            "position": list(self.position),
            "observed_time": self.observed_time,
            "uncertainty_score": self.uncertainty_score,
        }


@dataclass(frozen=True, slots=True)
class EventObservation:
    """One report event captured by the sensor within its field of view."""

    observation_id: str
    entity_id: EntityId
    position: tuple[float, float, float]
    observed_time: float
    uncertainty_score: float
    source_event_index: int
    event_type: str
    event_information: Mapping[str, object]
    event_time: float

    def __post_init__(self) -> None:
        _text(self.observation_id, "observation ID")
        _entity_id(self.entity_id, "observation entity ID")
        object.__setattr__(self, "position", _position(self.position))
        object.__setattr__(
            self, "observed_time", _time(self.observed_time, "observed time")
        )
        object.__setattr__(
            self, "uncertainty_score", _uncertainty(self.uncertainty_score)
        )
        if (
            isinstance(self.source_event_index, bool)
            or not isinstance(self.source_event_index, int)
            or self.source_event_index < 1
        ):
            raise ValueError("source event index must be a positive integer")
        _text(self.event_type, "event type")
        frozen = _freeze_json(self.event_information)
        if not isinstance(frozen, Mapping):
            raise TypeError("event information must be an object")
        object.__setattr__(self, "event_information", frozen)
        object.__setattr__(self, "event_time", _time(self.event_time, "event time"))

    @property
    def observation_kind(self) -> str:
        return "event"

    def to_dict(self) -> dict[str, object]:
        return {
            "observation_kind": self.observation_kind,
            "observation_id": self.observation_id,
            "entity_id": self.entity_id,
            "position": list(self.position),
            "observed_time": self.observed_time,
            "uncertainty_score": self.uncertainty_score,
            "source_event_index": self.source_event_index,
            "event_type": self.event_type,
            "event_information": _thaw(self.event_information),
            "event_time": self.event_time,
        }


Perception = EntityObservation | EventObservation


def perception_from_dict(value: Mapping[str, Any]) -> Perception:
    if not isinstance(value, Mapping):
        raise TypeError("perception must be an object")
    kind = value.get("observation_kind")
    fields = dict(value)
    fields.pop("observation_kind", None)
    if kind == "entity":
        return EntityObservation(**fields)
    if kind == "event":
        return EventObservation(**fields)
    raise ValueError("perception kind is invalid")


def perception_to_transport_event(
    mission_id: str,
    perception: Perception,
    *,
    sequence: int,
    schema_version: int = 1,
) -> TransportEvent:
    """Wrap one bounded perception for durable audit and belief consumers."""

    _text(mission_id, "perception Mission ID")
    if not isinstance(perception, (EntityObservation, EventObservation)):
        raise TypeError("perception event requires a typed observation")
    return TransportEvent(
        schema_version=schema_version,
        event_id=perception.observation_id,
        mission_id=mission_id,
        sequence=sequence,
        event_kind="event.observed"
        if isinstance(perception, EventObservation)
        else "entity.observed",
        payload=perception.to_dict(),
    )


@dataclass(frozen=True, slots=True)
class EnvironmentTickResult:
    """All evidence emitted by exactly one configured simulation tick."""

    current_time: float
    environment_data: Mapping[str, object]
    feedback_events: tuple[TransportEvent, ...] = ()
    perception_events: tuple[TransportEvent, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "current_time", _time(self.current_time, "tick time"))
        frozen = _freeze_json(self.environment_data)
        if not isinstance(frozen, Mapping):
            raise TypeError("tick environment data must be an object")
        object.__setattr__(self, "environment_data", frozen)
        feedback = tuple(self.feedback_events)
        perceptions = tuple(self.perception_events)
        if not all(isinstance(item, TransportEvent) for item in feedback + perceptions):
            raise TypeError("tick events must be TransportEvent records")
        object.__setattr__(self, "feedback_events", feedback)
        object.__setattr__(self, "perception_events", perceptions)

    def to_canonical_json(self) -> str:
        return json.dumps(
            {
                "current_time": self.current_time,
                "environment_data": _thaw(self.environment_data),
                "feedback_events": [item.to_dict() for item in self.feedback_events],
                "perception_events": [
                    item.to_dict() for item in self.perception_events
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )


def environment_mission_time(environment_data: Mapping[str, object]) -> float:
    """Read Mission time from physical v2 or the retained fake shape."""

    value = environment_data.get("mission_time_seconds")
    if value is None:
        scene = environment_data.get("scene_graph")
        value = (
            scene.get("mission_time_seconds") if isinstance(scene, Mapping) else None
        )
    return _time(value, "environment Mission time")


def environment_controlled_vehicle(
    environment_data: Mapping[str, object],
) -> Mapping[str, object]:
    """Read controlled-vehicle telemetry from physical v2 or fake evidence."""

    vehicle = environment_data.get("controlled_vehicle")
    if vehicle is None:
        scene = environment_data.get("scene_graph")
        vehicle = scene.get("drone") if isinstance(scene, Mapping) else None
    if not isinstance(vehicle, Mapping):
        raise TypeError("environment controlled vehicle must be an object")
    return vehicle


def environment_maneuver_lifecycle(
    environment_data: Mapping[str, object],
) -> Mapping[str, object] | None:
    """Read the separate physical lifecycle or fake current maneuver."""

    if "maneuver_lifecycle" in environment_data:
        lifecycle = environment_data["maneuver_lifecycle"]
    else:
        scene = environment_data.get("scene_graph")
        lifecycle = (
            scene.get("current_maneuver") if isinstance(scene, Mapping) else None
        )
    if lifecycle is None:
        return None
    if not isinstance(lifecycle, Mapping):
        raise TypeError("environment maneuver lifecycle must be an object or null")
    return lifecycle


def environment_world_model_info(
    environment_data: Mapping[str, object],
) -> Mapping[str, object]:
    """Return raw physical info[0], retaining fake scene graphs behind this seam."""

    world_model = environment_data.get("world_model_info")
    if world_model is None:
        world_model = environment_data.get("scene_graph")
    if not isinstance(world_model, Mapping):
        raise TypeError("environment world-model info must be an object")
    return world_model


__all__ = [
    "EntityId",
    "EntityObservation",
    "EnvironmentTickResult",
    "EventObservation",
    "Perception",
    "environment_controlled_vehicle",
    "environment_maneuver_lifecycle",
    "environment_mission_time",
    "environment_world_model_info",
    "perception_from_dict",
    "perception_to_transport_event",
]
