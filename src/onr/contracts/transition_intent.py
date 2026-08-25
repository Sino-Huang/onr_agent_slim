"""Agent-owned transition selection contracts."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, cast


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in cast(Mapping[object, object], value).items():
            if not isinstance(key, str):
                raise TypeError("transition intent condition keys must be strings")
            result[key] = _freeze_json(item)
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("transition intent values must be finite JSON values")


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


class TransitionIntentStatus(StrEnum):
    """Lifecycle of one selected transition target."""

    SELECTED = "selected"
    CONSUMED = "consumed"
    INVALIDATED = "invalidated"


class TransitionAssessment(StrEnum):
    """Maneuver's allowed semantic condition assessments."""

    SATISFIED = "satisfied"
    SATISFIED_WITH_UNCERTAINTY = "satisfied_with_uncertainty"


@dataclass(frozen=True, slots=True)
class TransitionIntent:
    """Durable selection of one exact live Statechart target."""

    intent_id: str
    mission_id: str
    plan_revision: int
    statechart_revision: int
    source_state: str
    target_state: str
    condition: Mapping[str, object]
    state_entry_revision: int
    selection_revision: int
    selected_at: float
    rationale: str
    status: TransitionIntentStatus | str = TransitionIntentStatus.SELECTED
    superseded_intent: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        for value, label in (
            (self.intent_id, "Transition Intent ID"),
            (self.mission_id, "Transition Intent Mission ID"),
            (self.source_state, "Transition Intent source state"),
            (self.target_state, "Transition Intent target state"),
            (self.rationale, "Transition Intent rationale"),
        ):
            _text(value, label)
        _nonnegative_int(self.plan_revision, "Transition Intent plan revision")
        _nonnegative_int(
            self.statechart_revision, "Transition Intent Statechart revision"
        )
        _nonnegative_int(
            self.state_entry_revision, "Transition Intent state-entry revision"
        )
        _nonnegative_int(
            self.selection_revision, "Transition Intent selection revision"
        )
        if (
            isinstance(self.selected_at, bool)
            or not isinstance(self.selected_at, (int, float))
            or not math.isfinite(float(self.selected_at))
            or float(self.selected_at) < 0
        ):
            raise ValueError("Transition Intent selection time must be non-negative")
        if not isinstance(self.condition, Mapping):
            raise TypeError("Transition Intent condition must be an object")
        object.__setattr__(
            self, "condition", _freeze_json(self.condition)
        )
        try:
            object.__setattr__(self, "status", TransitionIntentStatus(self.status))
        except (TypeError, ValueError) as exc:
            raise ValueError("Transition Intent status is invalid") from exc
        if self.superseded_intent is not None:
            _text(self.superseded_intent, "superseded Transition Intent")
        if self.schema_version != 1:
            raise ValueError("Transition Intent schema version must be 1")

    @property
    def is_selected(self) -> bool:
        return self.status is TransitionIntentStatus.SELECTED

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "intent_id": self.intent_id,
            "mission_id": self.mission_id,
            "plan_revision": self.plan_revision,
            "statechart_revision": self.statechart_revision,
            "source_state": self.source_state,
            "target_state": self.target_state,
            "condition": _json_value(self.condition),
            "state_entry_revision": self.state_entry_revision,
            "selection_revision": self.selection_revision,
            "selected_at": float(self.selected_at),
            "rationale": self.rationale,
            "status": self.status,
            "superseded_intent": self.superseded_intent,
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
    def from_dict(cls, value: Mapping[str, Any]) -> TransitionIntent:
        fields = {
            "schema_version",
            "intent_id",
            "mission_id",
            "plan_revision",
            "statechart_revision",
            "source_state",
            "target_state",
            "condition",
            "state_entry_revision",
            "selection_revision",
            "selected_at",
            "rationale",
            "status",
            "superseded_intent",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError("Transition Intent contains unknown or missing fields")
        return cls(**value)

    @classmethod
    def from_json(cls, value: str) -> TransitionIntent:
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Transition Intent JSON is invalid") from exc
        if not isinstance(decoded, Mapping):
            raise TypeError("Transition Intent JSON must contain an object")
        return cls.from_dict(decoded)


@dataclass(frozen=True, slots=True)
class ManeuverTransitionCandidate:
    """One model-visible target and its unchanged Statechart condition."""

    target_state: str
    condition: Mapping[str, object]

    def __post_init__(self) -> None:
        _text(self.target_state, "Maneuver transition target")
        if not isinstance(self.condition, Mapping):
            raise TypeError("Maneuver transition condition must be an object")
        object.__setattr__(self, "condition", _freeze_json(self.condition))

    def to_dict(self) -> dict[str, object]:
        return {
            "target_state": self.target_state,
            "condition": _json_value(self.condition),
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> ManeuverTransitionCandidate:
        if not isinstance(value, Mapping) or set(value) != {
            "target_state",
            "condition",
        }:
            raise ValueError("Maneuver transition candidate has invalid fields")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class ManeuverFSMContext:
    """Focused model-visible view of the currently active Statechart state."""

    current_state: str
    current_state_context: Mapping[str, object]
    transition_candidates: tuple[ManeuverTransitionCandidate, ...]
    state_entry_revision: int
    transition_intent: TransitionIntent | None = None

    def __post_init__(self) -> None:
        _text(self.current_state, "Maneuver current state")
        if not isinstance(self.current_state_context, Mapping):
            raise TypeError("Maneuver current-state context must be an object")
        object.__setattr__(
            self,
            "current_state_context",
            _freeze_json(self.current_state_context),
        )
        candidates = tuple(self.transition_candidates)
        if not all(isinstance(item, ManeuverTransitionCandidate) for item in candidates):
            raise TypeError("Maneuver transition candidates must be typed records")
        object.__setattr__(self, "transition_candidates", candidates)
        _nonnegative_int(self.state_entry_revision, "Maneuver state-entry revision")
        if self.transition_intent is not None and not isinstance(
            self.transition_intent, TransitionIntent
        ):
            raise TypeError("Maneuver transition intent must be typed")

    def to_dict(self) -> dict[str, object]:
        return {
            "current_state": self.current_state,
            "current_state_context": _json_value(self.current_state_context),
            "transition_candidates": [
                item.to_dict() for item in self.transition_candidates
            ],
            "state_entry_revision": self.state_entry_revision,
            "transition_intent": (
                self.transition_intent.to_dict()
                if self.transition_intent is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ManeuverFSMContext:
        fields = {
            "current_state",
            "current_state_context",
            "transition_candidates",
            "state_entry_revision",
            "transition_intent",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError("Maneuver FSM context contains unknown or missing fields")
        raw_candidates = value["transition_candidates"]
        raw_intent = value["transition_intent"]
        if not isinstance(raw_candidates, (list, tuple)):
            raise TypeError("Maneuver transition candidates must be an array")
        if raw_intent is not None and not isinstance(raw_intent, Mapping):
            raise TypeError("Maneuver transition intent must be an object or null")
        return cls(
            current_state=value["current_state"],
            current_state_context=value["current_state_context"],
            transition_candidates=tuple(
                ManeuverTransitionCandidate.from_dict(item)
                for item in raw_candidates
            ),
            state_entry_revision=value["state_entry_revision"],
            transition_intent=(
                TransitionIntent.from_dict(raw_intent)
                if raw_intent is not None
                else None
            ),
        )


__all__ = [
    "ManeuverFSMContext",
    "ManeuverTransitionCandidate",
    "TransitionAssessment",
    "TransitionIntent",
    "TransitionIntentStatus",
]
