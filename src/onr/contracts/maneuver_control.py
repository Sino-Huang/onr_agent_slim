"""Typed, transient contracts owned by Maneuver Control.

These records describe a decision and an abstract maneuver request.  They do
not describe environment lifecycle state; that authority enters the system
only as :class:`onr.contracts.fsm.ManeuverFeedback`.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, cast

from onr.contracts.context_coordination import MissionSnapshot
from onr.contracts.environment import Perception, perception_from_dict
from onr.contracts.fsm import FSMStatus
from onr.contracts.hyper_agent import HyperHeartbeatDecision
from onr.contracts.planning import (
    JsonScalar,
    ManeuverIntent,
    ManeuverParameter,
)
from onr.contracts.transport import Command


class PhysicalAction(StrEnum):
    """The complete set of physical actions Maneuver Control may request."""

    NAVIGATE = "navigate"
    TAKEOFF = "takeoff"
    LAND = "land"
    SEARCH_AREA = "search_area"
    PURSUE = "pursue"
    INVESTIGATE = "investigate"


class NonPhysicalChoice(StrEnum):
    """A Maneuver Control choice which does not submit to the environment."""

    TRANSITION = "transition"
    REPLAN = "replan"
    REPORT = "report"
    QUERY = "query"
    NO_CHANGE = "no_change"
    CANCEL_MANEUVER = "cancel_maneuver"


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _string_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    result: dict[str, object] = {}
    items = cast(Mapping[object, object], value).items()
    for key, item in items:
        if not isinstance(key, str):
            raise ValueError(f"{label} keys must be strings")
        result[key] = item
    return result


def _scalar(value: object, label: str) -> JsonScalar:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"{label} must be a JSON scalar")


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        mapping = _string_mapping(value, "JSON object")
        return {key: _json_value(item) for key, item in mapping.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        mapping = _string_mapping(value, "JSON object")
        return MappingProxyType({key: _freeze(item) for key, item in mapping.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("JSON object must contain only JSON-safe values")


def _payload(value: object, label: str) -> Mapping[str, object]:
    frozen = _freeze(value)
    if not isinstance(frozen, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return cast(Mapping[str, object], frozen)


_LIFECYCLE_KEYS = frozenset({"lifecycle", "status", "completed", "cancelled"})


def _contains_lifecycle_claim(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, item in cast(Mapping[object, object], value).items():
            if key in _LIFECYCLE_KEYS or _contains_lifecycle_claim(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_lifecycle_claim(item) for item in value)
    return False


def _validate_json_payload(value: object, label: str) -> None:
    if isinstance(value, Mapping):
        for key, item in cast(Mapping[object, object], value).items():
            if not isinstance(key, str):
                raise ValueError(f"{label} keys must be strings")
            _validate_json_payload(item, label)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_json_payload(item, label)
        return
    if value is None or isinstance(value, (str, int, bool)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    raise ValueError(f"{label} must contain only JSON-safe values")


@dataclass(frozen=True, slots=True, init=False)
class ManeuverControlDecision:
    """One validated Maneuver Decision.

    Exactly one of ``transition_event``, ``physical_intent``, and ``choice``
    is selected.  A physical intent is deliberately singular, and payloads
    cannot smuggle in lifecycle assertions.
    """

    decision_id: str
    mission_id: str
    plan_revision: int
    transition_event: str | None
    maneuver_id: str | None
    physical_intent: ManeuverIntent | None
    choice: NonPhysicalChoice | None
    payload: Mapping[str, object]
    schema_version: int

    def __init__(
        self,
        decision_id: str,
        mission_id: str,
        plan_revision: int,
        transition_event: str | None = None,
        maneuver_id: str | None = None,
        physical_intent: ManeuverIntent | Mapping[str, object] | None = None,
        choice: NonPhysicalChoice | str | None = None,
        payload: Mapping[str, object] | None = None,
        schema_version: int = 1,
        *,
        physical_maneuver: ManeuverIntent | Mapping[str, object] | None = None,
        transition: str | None = None,
        action: str | None = None,
    ) -> None:
        if transition_event is None:
            transition_event = transition
        if physical_intent is None:
            physical_intent = physical_maneuver
        if physical_intent is None and action is not None:
            physical_intent = ManeuverIntent(action)
        if isinstance(physical_intent, Mapping):
            raw = dict(physical_intent)
            raw_action = raw.pop("action", None)
            if not isinstance(raw_action, str):
                raise ValueError("physical maneuver action must be a string")
            parameters = raw.pop("parameters", raw)
            if not isinstance(parameters, Mapping):
                raise ValueError("physical maneuver parameters must be a JSON object")
            physical_intent = ManeuverIntent(
                raw_action,
                tuple(
                    ManeuverParameter(name, _scalar(value, "maneuver parameter value"))
                    for name, value in _string_mapping(
                        parameters, "physical maneuver parameters"
                    ).items()
                ),
            )
        object.__setattr__(self, "decision_id", decision_id)
        object.__setattr__(self, "mission_id", mission_id)
        object.__setattr__(self, "plan_revision", plan_revision)
        object.__setattr__(self, "transition_event", transition_event)
        object.__setattr__(self, "maneuver_id", maneuver_id)
        object.__setattr__(self, "physical_intent", physical_intent)
        object.__setattr__(self, "choice", choice)
        object.__setattr__(self, "payload", {} if payload is None else payload)
        object.__setattr__(self, "schema_version", schema_version)
        self.__post_init__()

    def __post_init__(self) -> None:
        _text(self.decision_id, "maneuver decision ID")
        _text(self.mission_id, "maneuver decision mission ID")
        if (
            isinstance(self.plan_revision, bool)
            or not isinstance(self.plan_revision, int)
            or self.plan_revision < 0
        ):
            raise ValueError("maneuver control plan revision must be non-negative")
        if self.transition_event is not None:
            _text(self.transition_event, "maneuver control transition event")
        if self.maneuver_id is not None:
            _text(self.maneuver_id, "maneuver control maneuver ID")
        if self.physical_intent is not None and not isinstance(
            self.physical_intent, ManeuverIntent
        ):
            raise ValueError("physical intent must be a ManeuverIntent")
        if self.choice is not None:
            try:
                object.__setattr__(self, "choice", NonPhysicalChoice(self.choice))
            except (TypeError, ValueError) as exc:
                raise ValueError("maneuver control choice is invalid") from exc
        if self.transition_event is not None and self.choice is None:
            object.__setattr__(self, "choice", NonPhysicalChoice.TRANSITION)
        if (
            self.choice is NonPhysicalChoice.TRANSITION
            and self.transition_event is None
        ):
            raise ValueError("transition choice requires a transition event")
        if self.physical_intent is not None and (
            self.transition_event is not None
            or self.choice
            in (NonPhysicalChoice.TRANSITION, NonPhysicalChoice.CANCEL_MANEUVER)
        ):
            raise ValueError(
                "physical intent cannot be combined with transition or cancellation"
            )
        if self.transition_event is not None and self.choice not in (
            None,
            NonPhysicalChoice.TRANSITION,
        ):
            raise ValueError("a transition event must use the transition choice")
        if (
            self.physical_intent is None
            and self.transition_event is None
            and self.choice is None
        ):
            raise ValueError("maneuver decision must select an outcome")
        if self.physical_intent is not None:
            try:
                PhysicalAction(self.physical_intent.action)
            except (TypeError, ValueError) as exc:
                raise ValueError("maneuver control physical action is invalid") from exc
            if not self.maneuver_id:
                raise ValueError("a physical maneuver requires a maneuver ID")
        if (
            self.choice is NonPhysicalChoice.CANCEL_MANEUVER
            and self.maneuver_id is None
        ):
            raise ValueError("cancel_maneuver requires a maneuver ID")
        frozen = _payload(self.payload, "maneuver decision payload")
        _validate_json_payload(frozen, "maneuver decision payload")
        if _contains_lifecycle_claim(frozen):
            raise ValueError("maneuver decisions cannot claim lifecycle completion")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version <= 0
        ):
            raise ValueError("maneuver control schema version must be positive")
        object.__setattr__(self, "payload", frozen)

    @property
    def physical_maneuver(self) -> ManeuverIntent | None:
        return self.physical_intent

    @property
    def event(self) -> str | None:
        return self.transition_event

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "mission_id": self.mission_id,
            "plan_revision": self.plan_revision,
            "transition_event": self.transition_event,
            "maneuver_id": self.maneuver_id,
            "physical_intent": self.physical_intent.to_dict()
            if self.physical_intent
            else None,
            "choice": self.choice,
            "payload": _json_value(self.payload),
        }

    def to_canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ManeuverControlDecision:
        expected = {
            "schema_version",
            "decision_id",
            "mission_id",
            "plan_revision",
            "transition_event",
            "maneuver_id",
            "physical_intent",
            "choice",
            "payload",
        }
        if set(value) != expected:
            raise ValueError("maneuver decision contains unknown or missing fields")
        physical = value["physical_intent"]
        if isinstance(physical, Mapping):
            action = physical.get("action")
            parameters = physical.get("parameters", {})
            if not isinstance(action, str) or not isinstance(parameters, Mapping):
                raise ValueError("maneuver decision physical intent has invalid fields")
            physical = ManeuverIntent(
                action,
                tuple(
                    ManeuverParameter(name, _scalar(item, "maneuver parameter value"))
                    for name, item in _string_mapping(
                        parameters, "physical intent parameters"
                    ).items()
                ),
            )
        if physical is not None and not isinstance(physical, ManeuverIntent):
            raise ValueError("maneuver decision physical intent is invalid")
        decision_id = _text(value["decision_id"], "maneuver decision ID")
        mission_id = _text(value["mission_id"], "maneuver decision mission ID")
        transition = value["transition_event"]
        if transition is not None:
            transition = _text(transition, "maneuver decision transition event")
        maneuver_id = value["maneuver_id"]
        if maneuver_id is not None:
            maneuver_id = _text(maneuver_id, "maneuver decision maneuver ID")
        choice = value["choice"]
        if choice is not None:
            choice = _text(choice, "maneuver decision choice")
        return cls(
            decision_id,
            mission_id,
            _nonnegative_int(value["plan_revision"], "maneuver control plan revision"),
            transition_event=transition,
            maneuver_id=maneuver_id,
            physical_intent=physical,
            choice=choice,
            payload=_string_mapping(value["payload"], "maneuver decision payload"),
            schema_version=_positive_int(
                value["schema_version"], "maneuver decision schema version"
            ),
        )

    @classmethod
    def from_json(cls, value: str) -> ManeuverControlDecision:
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("maneuver decision JSON is invalid") from exc
        mapping = _string_mapping(decoded, "maneuver decision")
        return cls.from_dict(mapping)


@dataclass(frozen=True, slots=True)
class ManeuverCommand:
    """Abstract physical command; adapter responses are not lifecycle facts."""

    command_id: str
    correlation_id: str
    mission_id: str
    plan_revision: int
    maneuver_id: str
    intent: ManeuverIntent
    schema_version: int = 1
    mission_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.command_id, "maneuver command ID"),
            (self.correlation_id, "maneuver command correlation ID"),
            (self.mission_id, "maneuver command mission ID"),
            (self.maneuver_id, "maneuver command maneuver ID"),
        ):
            _text(value, label)
        if (
            isinstance(self.plan_revision, bool)
            or not isinstance(self.plan_revision, int)
            or self.plan_revision < 0
        ):
            raise ValueError("maneuver command plan revision must be non-negative")
        if not isinstance(self.intent, ManeuverIntent):
            raise ValueError("maneuver command intent must be a ManeuverIntent")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version <= 0
        ):
            raise ValueError("maneuver command schema version must be positive")
        if self.mission_snapshot_id is not None:
            _text(self.mission_snapshot_id, "maneuver command Mission Snapshot ID")
        try:
            PhysicalAction(self.intent.action)
        except (TypeError, ValueError) as exc:
            raise ValueError("maneuver command physical action is invalid") from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "command_id": self.command_id,
            "correlation_id": self.correlation_id,
            "mission_id": self.mission_id,
            "plan_revision": self.plan_revision,
            "maneuver_id": self.maneuver_id,
            "intent": self.intent.to_dict(),
            "mission_snapshot_id": self.mission_snapshot_id,
        }

    @property
    def action(self) -> str:
        return self.intent.action

    @property
    def parameters(self) -> tuple[ManeuverParameter, ...]:
        return self.intent.parameters

    @property
    def physical_intent(self) -> ManeuverIntent:
        return self.intent

    def to_command(self, target_service: str) -> Command:
        return Command(
            self.schema_version,
            self.command_id,
            self.correlation_id,
            self.mission_id,
            target_service,
            "maneuver",
            self.to_dict(),
        )

    def to_canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ManeuverCommand:
        expected = {
            "schema_version",
            "command_id",
            "correlation_id",
            "mission_id",
            "plan_revision",
            "maneuver_id",
            "intent",
        }
        extended = expected | {"mission_snapshot_id"}
        if set(value) not in (expected, extended):
            raise ValueError("maneuver command contains unknown or missing fields")
        intent = value["intent"]
        if not isinstance(intent, Mapping):
            raise ValueError("maneuver command intent is invalid")
        action = intent.get("action")
        parameters = intent.get("parameters", {})
        if not isinstance(action, str) or not isinstance(parameters, Mapping):
            raise ValueError("maneuver command intent is invalid")
        return cls(
            _text(value["command_id"], "maneuver command ID"),
            _text(value["correlation_id"], "maneuver command correlation ID"),
            _text(value["mission_id"], "maneuver command mission ID"),
            _nonnegative_int(value["plan_revision"], "maneuver command plan revision"),
            _text(value["maneuver_id"], "maneuver command maneuver ID"),
            ManeuverIntent(
                action,
                tuple(
                    ManeuverParameter(name, _scalar(item, "maneuver parameter value"))
                    for name, item in _string_mapping(
                        parameters, "maneuver command parameters"
                    ).items()
                ),
            ),
            _positive_int(value["schema_version"], "maneuver command schema version"),
            _optional_text(
                value.get("mission_snapshot_id"), "maneuver command Mission Snapshot ID"
            ),
        )

    @classmethod
    def from_json(cls, value: str) -> ManeuverCommand:
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("maneuver command JSON is invalid") from exc
        return cls.from_dict(_string_mapping(decoded, "maneuver command"))

    @classmethod
    def from_command(cls, command: Command) -> ManeuverCommand:
        if command.command_kind != "maneuver":
            raise ValueError("generic command is not a maneuver command")
        payload = command.payload
        intent = payload.get("intent")
        if not isinstance(intent, Mapping):
            raise ValueError("maneuver command intent is missing")
        action = intent.get("action")
        parameters = intent.get("parameters", {})
        plan_revision = payload.get("plan_revision")
        maneuver_id = payload.get("maneuver_id")
        if not isinstance(action, str) or not isinstance(parameters, Mapping):
            raise ValueError("maneuver command parameters are invalid")
        if (
            not isinstance(plan_revision, int)
            or isinstance(plan_revision, bool)
            or not isinstance(maneuver_id, str)
        ):
            raise ValueError("maneuver command context is invalid")
        return cls(
            command.command_id,
            command.correlation_id,
            command.mission_id,
            plan_revision,
            maneuver_id,
            ManeuverIntent(
                action,
                tuple(
                    ManeuverParameter(name, _scalar(value, "maneuver parameter value"))
                    for name, value in _string_mapping(
                        parameters, "maneuver command parameters"
                    ).items()
                ),
            ),
            command.schema_version,
            _optional_text(
                payload.get("mission_snapshot_id"),
                "maneuver command Mission Snapshot ID",
            ),
        )


@dataclass(frozen=True, slots=True)
class InvocationOverlay:
    """Immutable JSON-safe per-invocation input, never persisted as authority."""

    mission_id: str
    request_id: str
    values: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.mission_id, "invocation overlay mission ID")
        _text(self.request_id, "invocation overlay request ID")
        object.__setattr__(
            self, "values", _payload(self.values, "invocation overlay values")
        )

    @property
    def snapshot(self) -> object:
        return self.values.get("snapshot")

    @property
    def fsm_status(self) -> object:
        return self.values.get("fsm_status", self.values.get("status"))

    def to_dict(self) -> dict[str, object]:
        return {
            "mission_id": self.mission_id,
            "request_id": self.request_id,
            "values": _json_value(self.values),
        }


class ManeuverHeartbeatOutcome(StrEnum):
    """Allowed public outcomes of one tool-driven heartbeat."""

    COMPLETED = "completed"
    NO_CHANGE = "no_change"


@dataclass(frozen=True, slots=True)
class ManeuverInvocation:
    """Model-visible evidence injected for one live Maneuver heartbeat."""

    request_id: str
    correlation_id: str
    mission_id: str
    plan_revision: int
    statechart_reference: str
    fsm_status: FSMStatus
    environment_data: Mapping[str, object]
    trigger_identities: tuple[str, ...] = ()
    pending_perceptions: tuple[Perception, ...] = ()
    available_recipients: tuple[str, ...] = ()
    planning_snapshot: MissionSnapshot | None = None
    hyper_outcomes: tuple[HyperHeartbeatDecision, ...] = ()

    def __post_init__(self) -> None:
        for value, label in (
            (self.request_id, "Maneuver invocation request ID"),
            (self.correlation_id, "Maneuver invocation correlation ID"),
            (self.mission_id, "Maneuver invocation Mission ID"),
            (self.statechart_reference, "Maneuver invocation Statechart reference"),
        ):
            _text(value, label)
        if (
            isinstance(self.plan_revision, bool)
            or not isinstance(self.plan_revision, int)
            or self.plan_revision < 0
        ):
            raise ValueError("Maneuver invocation plan revision must be non-negative")
        if not isinstance(self.fsm_status, FSMStatus):
            raise TypeError("Maneuver invocation requires live FSMStatus")
        if self.fsm_status.mission_id != self.mission_id:
            raise ValueError("Maneuver invocation Mission identity is inconsistent")
        if self.fsm_status.plan_revision != self.plan_revision:
            raise ValueError("Maneuver invocation plan revision is inconsistent")
        frozen_environment = _payload(
            self.environment_data, "Maneuver invocation environment data"
        )
        object.__setattr__(self, "environment_data", frozen_environment)
        triggers = tuple(self.trigger_identities)
        if not all(isinstance(item, str) and item.strip() for item in triggers):
            raise ValueError(
                "Maneuver invocation triggers must be non-empty identities"
            )
        object.__setattr__(self, "trigger_identities", tuple(sorted(set(triggers))))
        perceptions = tuple(self.pending_perceptions)
        if not all(hasattr(item, "to_dict") for item in perceptions):
            raise TypeError("Maneuver pending perceptions must be typed perceptions")
        object.__setattr__(self, "pending_perceptions", perceptions)
        recipients = tuple(self.available_recipients)
        if not all(isinstance(item, str) and item.strip() for item in recipients):
            raise ValueError("Maneuver invocation recipients must be non-empty strings")
        if len(recipients) != len(set(recipients)):
            raise ValueError("Maneuver invocation recipients must be unique")
        object.__setattr__(self, "available_recipients", tuple(sorted(recipients)))
        if self.planning_snapshot is not None:
            if not isinstance(self.planning_snapshot, MissionSnapshot):
                raise TypeError(
                    "Maneuver invocation provenance must be a MissionSnapshot"
                )
            if self.planning_snapshot.mission_id != self.mission_id:
                raise ValueError(
                    "Maneuver invocation provenance Mission ID does not match"
                )
        outcomes = tuple(self.hyper_outcomes)
        if not all(
            isinstance(item, HyperHeartbeatDecision)
            and item.mission_id == self.mission_id
            for item in outcomes
        ):
            raise ValueError("Maneuver invocation Hyper outcomes are inconsistent")
        object.__setattr__(self, "hyper_outcomes", outcomes)

    @property
    def communication_outcomes(self) -> tuple[HyperHeartbeatDecision, ...]:
        return self.hyper_outcomes

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "request_id": self.request_id,
            "correlation_id": self.correlation_id,
            "mission_id": self.mission_id,
            "plan_revision": self.plan_revision,
            "statechart_reference": self.statechart_reference,
            "fsm_status": self.fsm_status.to_dict(),
            "environment_data": _json_value(self.environment_data),
            "trigger_identities": list(self.trigger_identities),
            "pending_perceptions": [
                item.to_dict() for item in self.pending_perceptions
            ],
            "available_recipients": list(self.available_recipients),
            "planning_snapshot": (
                self.planning_snapshot.to_dict()
                if self.planning_snapshot is not None
                else None
            ),
        }
        if self.hyper_outcomes:
            result["hyper_outcomes"] = [item.to_dict() for item in self.hyper_outcomes]
        return result

    def to_canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ManeuverInvocation:
        fields = {
            "request_id",
            "correlation_id",
            "mission_id",
            "plan_revision",
            "statechart_reference",
            "fsm_status",
            "environment_data",
            "trigger_identities",
            "pending_perceptions",
            "available_recipients",
            "planning_snapshot",
            "hyper_outcomes",
        }
        legacy_fields = fields - {"hyper_outcomes"}
        if not isinstance(value, Mapping) or set(value) not in (fields, legacy_fields):
            raise ValueError("Maneuver invocation contains unknown or missing fields")
        status = value["fsm_status"]
        environment = value["environment_data"]
        triggers = value["trigger_identities"]
        perceptions = value["pending_perceptions"]
        recipients = value["available_recipients"]
        snapshot = value["planning_snapshot"]
        outcomes = value.get("hyper_outcomes", ())
        if not isinstance(status, Mapping):
            raise TypeError("Maneuver invocation FSM status must be an object")
        if not isinstance(environment, Mapping):
            raise TypeError("Maneuver invocation environment data must be an object")
        if not isinstance(triggers, (list, tuple)):
            raise TypeError("Maneuver invocation triggers must be an array")
        if not isinstance(perceptions, (list, tuple)):
            raise TypeError("Maneuver pending perceptions must be an array")
        if not isinstance(recipients, (list, tuple)):
            raise TypeError("Maneuver invocation recipients must be an array")
        if snapshot is not None and not isinstance(snapshot, Mapping):
            raise TypeError("Maneuver invocation provenance must be an object or null")
        if not isinstance(outcomes, (list, tuple)):
            raise TypeError("Maneuver invocation Hyper outcomes must be an array")
        request_id = _text(value["request_id"], "Maneuver invocation request ID")
        correlation_id = _text(
            value["correlation_id"], "Maneuver invocation correlation ID"
        )
        mission_id = _text(value["mission_id"], "Maneuver invocation Mission ID")
        statechart_reference = _text(
            value["statechart_reference"], "Maneuver invocation Statechart reference"
        )
        return cls(
            request_id=request_id,
            correlation_id=correlation_id,
            mission_id=mission_id,
            plan_revision=_nonnegative_int(
                value["plan_revision"], "Maneuver invocation plan revision"
            ),
            statechart_reference=statechart_reference,
            fsm_status=FSMStatus.from_dict(status),
            environment_data=environment,
            trigger_identities=tuple(cast(tuple[str, ...], tuple(triggers))),
            pending_perceptions=tuple(
                perception_from_dict(cast(Mapping[str, Any], item))
                for item in perceptions
            ),
            available_recipients=tuple(cast(tuple[str, ...], tuple(recipients))),
            planning_snapshot=(
                MissionSnapshot.from_dict(cast(Mapping[str, Any], snapshot))
                if snapshot is not None
                else None
            ),
            hyper_outcomes=tuple(
                HyperHeartbeatDecision.from_dict(item) for item in outcomes
            ),
        )

    @classmethod
    def from_json(cls, value: str) -> ManeuverInvocation:
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Maneuver invocation JSON is invalid") from exc
        if not isinstance(decoded, Mapping):
            raise TypeError("Maneuver invocation JSON must contain an object")
        return cls.from_dict(decoded)


@dataclass(frozen=True, slots=True)
class ManeuverHeartbeatCompletion:
    """Small public completion returned after tool effects have been applied."""

    mission_id: str
    request_id: str
    outcome: ManeuverHeartbeatOutcome | str
    summary: str

    def __post_init__(self) -> None:
        _text(self.mission_id, "Maneuver heartbeat completion Mission ID")
        _text(self.request_id, "Maneuver heartbeat completion request ID")
        _text(self.summary, "Maneuver heartbeat completion summary")
        try:
            object.__setattr__(self, "outcome", ManeuverHeartbeatOutcome(self.outcome))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Maneuver heartbeat completion outcome is invalid"
            ) from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "mission_id": self.mission_id,
            "request_id": self.request_id,
            "outcome": self.outcome,
            "summary": self.summary,
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
    def from_dict(cls, value: Mapping[str, object]) -> ManeuverHeartbeatCompletion:
        if not isinstance(value, Mapping) or set(value) != {
            "mission_id",
            "request_id",
            "outcome",
            "summary",
        }:
            raise ValueError("Maneuver heartbeat completion has invalid fields")
        return cls(
            mission_id=_text(value["mission_id"], "Maneuver completion Mission ID"),
            request_id=_text(value["request_id"], "Maneuver completion request ID"),
            outcome=_text(value["outcome"], "Maneuver completion outcome"),
            summary=_text(value["summary"], "Maneuver completion summary"),
        )

    @classmethod
    def from_json(cls, value: str) -> ManeuverHeartbeatCompletion:
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Maneuver heartbeat completion JSON is invalid") from exc
        if not isinstance(decoded, Mapping):
            raise TypeError("Maneuver heartbeat completion JSON must contain an object")
        return cls.from_dict(decoded)


__all__ = [
    "InvocationOverlay",
    "ManeuverCommand",
    "ManeuverControlDecision",
    "ManeuverHeartbeatCompletion",
    "ManeuverHeartbeatOutcome",
    "ManeuverInvocation",
    "NonPhysicalChoice",
    "PhysicalAction",
]
