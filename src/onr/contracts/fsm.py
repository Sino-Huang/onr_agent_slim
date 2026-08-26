"""Immutable declarative Statechart and FSM execution contracts.

The asset deliberately contains topology and finite JSON context only; it never
contains Python source, callbacks, or serialized runtime objects.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from onr.contracts.planning import PlanningProfile


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _json_value(value: object, label: str = "JSON value") -> object:
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
                raise TypeError(f"{label} object keys must be strings")
            frozen[key] = _json_value(item, label)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_json_value(item, label) for item in value)
    raise ValueError(f"{label} must contain only JSON-safe values")


def _as_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _as_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_as_json(item) for item in value]
    return value


def _canonical(value: object) -> str:
    return json.dumps(
        _as_json(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode(value: str, label: str) -> Mapping[str, Any]:
    def reject_non_finite(item: str) -> None:
        raise ValueError(item)

    try:
        decoded = json.loads(value, parse_constant=reject_non_finite)
    except (TypeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{label} JSON is invalid") from exc
    if not isinstance(decoded, Mapping):
        raise TypeError(f"{label} JSON must be an object")
    return decoded


def _payload(value: object, label: str) -> Mapping[str, object]:
    frozen = _json_value(value, label)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{label} must be a JSON object")
    return frozen


def _mapping_json(value: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in value.items():
        result[key] = _as_json(item)
    return result


def _strict_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{label} contains unknown or missing fields")


@dataclass(frozen=True, slots=True, init=False)
class FSMEvent:
    """Stable, immutable event identity and JSON-safe event payload."""

    event_id: str
    event_kind: str
    payload: Mapping[str, object]
    schema_version: int = 1

    def __init__(
        self,
        event_id: str | None = None,
        event_kind: str | None = None,
        payload: Mapping[str, object] | None = None,
        schema_version: int = 1,
        *,
        identity: str | None = None,
        kind: str | None = None,
    ) -> None:
        object.__setattr__(self, "event_id", event_id if event_id is not None else identity)
        object.__setattr__(self, "event_kind", event_kind if event_kind is not None else kind)
        object.__setattr__(self, "payload", {} if payload is None else payload)
        object.__setattr__(self, "schema_version", schema_version)
        self.__post_init__()

    def __post_init__(self) -> None:
        _text(self.event_id, "FSM event ID")
        _text(self.event_kind, "FSM event kind")
        _positive_int(self.schema_version, "FSM event schema version")
        object.__setattr__(self, "payload", _payload(self.payload, "FSM event payload"))

    @property
    def kind(self) -> str:
        return self.event_kind

    @property
    def identity(self) -> str:
        return self.event_id

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_kind": self.event_kind,
            "payload": _mapping_json(self.payload),
        }

    def to_canonical_json(self) -> str:
        return _canonical(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FSMEvent:
        _strict_fields(value, {"schema_version", "event_id", "event_kind", "payload"}, "FSM Event")
        return cls(**value)

    @classmethod
    def from_json(cls, value: str) -> FSMEvent:
        return cls.from_dict(_decode(value, "FSM Event"))


@dataclass(frozen=True, slots=True, init=False)
class ManeuverFeedback:
    """Authoritative environment lifecycle evidence for one maneuver."""

    feedback_id: str
    mission_id: str
    maneuver_id: str
    lifecycle: str
    payload: Mapping[str, object] = MappingProxyType({})
    schema_version: int = 1

    def __init__(
        self,
        feedback_id: str | None = None,
        mission_id: str = "",
        maneuver_id: str = "",
        lifecycle: str | None = None,
        payload: Mapping[str, object] | None = None,
        schema_version: int = 1,
        *,
        event_id: str | None = None,
        status: str | None = None,
    ) -> None:
        object.__setattr__(self, "feedback_id", feedback_id if feedback_id is not None else event_id)
        object.__setattr__(self, "mission_id", mission_id)
        object.__setattr__(self, "maneuver_id", maneuver_id)
        object.__setattr__(self, "lifecycle", lifecycle if lifecycle is not None else status)
        object.__setattr__(self, "payload", MappingProxyType({}) if payload is None else payload)
        object.__setattr__(self, "schema_version", schema_version)
        self.__post_init__()

    def __post_init__(self) -> None:
        for value, label in (
            (self.feedback_id, "maneuver feedback ID"),
            (self.mission_id, "maneuver feedback mission ID"),
            (self.maneuver_id, "maneuver feedback maneuver ID"),
            (self.lifecycle, "maneuver feedback lifecycle"),
        ):
            _text(value, label)
        if self.lifecycle not in {"active", "completed", "failed", "cancelled"}:
            raise ValueError("maneuver feedback lifecycle is invalid")
        _positive_int(self.schema_version, "maneuver feedback schema version")
        object.__setattr__(self, "payload", _payload(self.payload, "maneuver feedback payload"))

    @property
    def event_id(self) -> str:
        return self.feedback_id

    @property
    def event_kind(self) -> str:
        return "maneuver-feedback"

    @property
    def status(self) -> str:
        return self.lifecycle

    @property
    def lifecycle_fact(self) -> str:
        return self.lifecycle

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "feedback_id": self.feedback_id,
            "mission_id": self.mission_id,
            "maneuver_id": self.maneuver_id,
            "lifecycle": self.lifecycle,
            "payload": _mapping_json(self.payload),
        }

    def to_canonical_json(self) -> str:
        return _canonical(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ManeuverFeedback:
        _strict_fields(
            value,
            {"schema_version", "feedback_id", "mission_id", "maneuver_id", "lifecycle", "payload"},
            "Maneuver Feedback",
        )
        return cls(**value)

    @classmethod
    def from_json(cls, value: str) -> ManeuverFeedback:
        return cls.from_dict(_decode(value, "Maneuver Feedback"))


@dataclass(frozen=True, slots=True, init=False)
class ManeuverDecision:
    """A decision that authorizes at most one transition or physical maneuver."""

    decision_id: str
    mission_id: str
    transition_event: str | None = None
    maneuver_id: str | None = None
    physical_maneuver: Mapping[str, object] | None = None
    payload: Mapping[str, object] = MappingProxyType({})
    schema_version: int = 1

    def __init__(
        self,
        decision_id: str | None = None,
        mission_id: str = "",
        transition_event: str | None = None,
        maneuver_id: str | None = None,
        physical_maneuver: Mapping[str, object] | None = None,
        payload: Mapping[str, object] | None = None,
        schema_version: int = 1,
        *,
        event_id: str | None = None,
        event: str | None = None,
        transition: str | None = None,
        lifecycle: str | None = None,
        physical_action: Mapping[str, object] | None = None,
    ) -> None:
        object.__setattr__(self, "decision_id", decision_id if decision_id is not None else event_id)
        object.__setattr__(self, "mission_id", mission_id)
        object.__setattr__(
            self,
            "transition_event",
            transition_event if transition_event is not None else (event if event is not None else transition),
        )
        object.__setattr__(self, "maneuver_id", maneuver_id)
        object.__setattr__(
            self,
            "physical_maneuver",
            physical_maneuver if physical_maneuver is not None else physical_action,
        )
        object.__setattr__(self, "payload", MappingProxyType({}) if payload is None else payload)
        object.__setattr__(self, "schema_version", schema_version)
        if lifecycle is not None:
            object.__setattr__(self, "payload", {"lifecycle": lifecycle})
        self.__post_init__()

    def __post_init__(self) -> None:
        _text(self.decision_id, "maneuver decision ID")
        _text(self.mission_id, "maneuver decision mission ID")
        if self.transition_event is not None:
            _text(self.transition_event, "maneuver decision transition event")
        if self.maneuver_id is not None:
            _text(self.maneuver_id, "maneuver decision maneuver ID")
        if self.transition_event is None and self.maneuver_id is None and self.physical_maneuver is None:
            raise ValueError("maneuver decision must authorize a transition or maneuver")
        if self.transition_event is not None and self.physical_maneuver is not None:
            raise ValueError("maneuver decision may authorize one transition or physical maneuver")
        if self.physical_maneuver is not None:
            object.__setattr__(
                self,
                "physical_maneuver",
                _payload(self.physical_maneuver, "maneuver decision physical maneuver"),
            )
        payload = _payload(self.payload, "maneuver decision payload")
        if any(key in payload for key in ("lifecycle", "status", "completed")):
            raise ValueError("maneuver decisions cannot claim lifecycle completion")
        _positive_int(self.schema_version, "maneuver decision schema version")
        object.__setattr__(self, "payload", payload)

    @property
    def event_id(self) -> str:
        return self.decision_id

    @property
    def event_kind(self) -> str:
        return "maneuver-decision"

    @property
    def event(self) -> str | None:
        return self.transition_event

    @property
    def transition(self) -> str | None:
        return self.transition_event

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "mission_id": self.mission_id,
            "transition_event": self.transition_event,
            "maneuver_id": self.maneuver_id,
            "physical_maneuver": _mapping_json(self.physical_maneuver) if self.physical_maneuver is not None else None,
            "payload": _mapping_json(self.payload),
        }

    def to_canonical_json(self) -> str:
        return _canonical(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ManeuverDecision:
        _strict_fields(
            value,
            {"schema_version", "decision_id", "mission_id", "transition_event", "maneuver_id", "physical_maneuver", "payload"},
            "Maneuver Decision",
        )
        return cls(**value)

    @classmethod
    def from_json(cls, value: str) -> ManeuverDecision:
        return cls.from_dict(_decode(value, "Maneuver Decision"))


@dataclass(frozen=True, slots=True)
class StatechartTransition:
    """One declarative edge in a Statechart topology."""

    event: str
    source: str
    target: str
    context: Mapping[str, object] = MappingProxyType({})

    def __post_init__(self) -> None:
        _text(self.event, "transition event")
        _text(self.source, "transition source")
        _text(self.target, "transition target")
        object.__setattr__(
            self,
            "context",
            _payload(self.context, "Statechart transition context"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "event": self.event,
            "source": self.source,
            "target": self.target,
            "context": _mapping_json(self.context),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> StatechartTransition:
        _strict_fields(
            value,
            {"event", "source", "target", "context"},
            "Statechart transition",
        )
        return cls(**value)


@dataclass(frozen=True, slots=True)
class Statechart:
    """Validated immutable JSON topology carrying one plan revision's semantics."""

    mission_id: str
    plan_revision: int
    mission_snapshot_id: str
    planning_profile: str
    entry_state: str
    states: tuple[str, ...]
    transitions: tuple[StatechartTransition, ...]
    terminal_states: tuple[str, ...]
    state_context: Mapping[str, Mapping[str, object]]
    schema_version: int = 2

    def __post_init__(self) -> None:
        _text(self.mission_id, "Statechart mission ID")
        _non_negative_int(self.plan_revision, "Statechart plan revision")
        _text(self.mission_snapshot_id, "Statechart mission snapshot ID")
        try:
            PlanningProfile(self.planning_profile)
        except (TypeError, ValueError) as exc:
            raise ValueError("Statechart planning profile is invalid") from exc
        _text(self.entry_state, "Statechart entry state")
        states = tuple(self.states)
        if not states or not all(isinstance(item, str) and item.strip() for item in states):
            raise ValueError("Statechart states must be non-empty strings")
        if len(set(states)) != len(states):
            raise ValueError("Statechart states must be unique")
        if self.entry_state not in states:
            raise ValueError("Statechart entry state must be declared")
        terminal_states = tuple(self.terminal_states)
        if not terminal_states:
            raise ValueError("Statechart terminal states must be explicit")
        if not all(item in states for item in terminal_states):
            raise ValueError("Statechart terminal states must be declared")
        if len(set(terminal_states)) != len(terminal_states):
            raise ValueError("Statechart terminal states must be unique")
        transitions = tuple(self.transitions)
        if not all(isinstance(item, StatechartTransition) for item in transitions):
            raise ValueError("Statechart transitions must be StatechartTransition records")
        if any(item.source not in states or item.target not in states for item in transitions):
            raise ValueError("Statechart transitions must reference declared states")
        transition_keys = [(item.event, item.source) for item in transitions]
        if len(set(transition_keys)) != len(transition_keys):
            raise ValueError("Statechart cannot contain duplicate event edges")
        if len({item.event for item in transitions}) != len(transitions):
            raise ValueError("Statechart transition events must be globally unique")
        target_keys = [(item.source, item.target) for item in transitions]
        if len(set(target_keys)) != len(target_keys):
            raise ValueError("Statechart cannot contain duplicate source-target edges")
        reachable = {self.entry_state}
        while True:
            expanded = reachable | {
                item.target for item in transitions if item.source in reachable
            }
            if expanded == reachable:
                break
            reachable = expanded
        if reachable != set(states):
            raise ValueError("Statechart states must be reachable from the entry state")
        can_reach_terminal = set(terminal_states)
        while True:
            expanded = can_reach_terminal | {
                item.source
                for item in transitions
                if item.target in can_reach_terminal
            }
            if expanded == can_reach_terminal:
                break
            can_reach_terminal = expanded
        if can_reach_terminal != set(states):
            raise ValueError("Statechart states must reach a terminal state")
        raw_context = self.state_context
        if not isinstance(raw_context, Mapping) or set(raw_context) != set(states):
            raise ValueError("Statechart context must describe every state")
        state_context: dict[str, Mapping[str, object]] = {}
        for state, context in raw_context.items():
            state_context[state] = _payload(
                context, f"Statechart context for {state}"
            )
        if self.schema_version != 2:
            raise ValueError("Statechart schema version must be 2")
        object.__setattr__(self, "states", states)
        object.__setattr__(self, "transitions", transitions)
        object.__setattr__(self, "terminal_states", terminal_states)
        object.__setattr__(self, "state_context", MappingProxyType(state_context))
        object.__setattr__(self, "planning_profile", str(PlanningProfile(self.planning_profile)))

    @property
    def initial_state(self) -> str:
        return self.entry_state

    @property
    def statechart_revision(self) -> int:
        return self.plan_revision

    def context_for(self, state: str) -> Mapping[str, object]:
        return self.state_context[state]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "plan_revision": self.plan_revision,
            "mission_snapshot_id": self.mission_snapshot_id,
            "planning_profile": self.planning_profile,
            "entry_state": self.entry_state,
            "states": list(self.states),
            "transitions": [item.to_dict() for item in self.transitions],
            "terminal_states": list(self.terminal_states),
            "state_context": {
                state: _mapping_json(context)
                for state, context in sorted(self.state_context.items())
            },
        }

    def to_canonical_json(self) -> str:
        return _canonical(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Statechart:
        expected = {
            "schema_version",
            "mission_id",
            "plan_revision",
            "mission_snapshot_id",
            "planning_profile",
            "entry_state",
            "terminal_states",
            "states",
            "state_context",
            "transitions",
        }
        _strict_fields(value, expected, "Statechart")
        states = value["states"]
        transitions = value["transitions"]
        terminal_states = value["terminal_states"]
        if (
            not isinstance(states, (list, tuple))
            or not isinstance(transitions, (list, tuple))
            or not isinstance(terminal_states, (list, tuple))
        ):
            raise TypeError("Statechart states and transitions must be arrays")
        return cls(
            mission_id=value["mission_id"],
            plan_revision=value["plan_revision"],
            mission_snapshot_id=value["mission_snapshot_id"],
            planning_profile=value["planning_profile"],
            entry_state=value["entry_state"],
            states=tuple(states),
            transitions=tuple(StatechartTransition.from_dict(item) for item in transitions),
            terminal_states=tuple(terminal_states),
            state_context=value["state_context"],
            schema_version=value["schema_version"],
        )

    @classmethod
    def from_json(cls, value: str) -> Statechart:
        return cls.from_dict(_decode(value, "Statechart"))


@dataclass(frozen=True, slots=True)
class TransitionCandidate:
    """An enabled event exposed to Maneuver Control."""

    event: str
    source: str
    target: str
    transition_context: Mapping[str, object] = MappingProxyType({})
    source_state_context: Mapping[str, object] = MappingProxyType({})
    target_state_context: Mapping[str, object] = MappingProxyType({})
    schema_version: int = 2

    def __post_init__(self) -> None:
        _text(self.event, "transition candidate event")
        _text(self.source, "transition candidate source")
        _text(self.target, "transition candidate target")
        object.__setattr__(
            self,
            "transition_context",
            _payload(self.transition_context, "transition context"),
        )
        object.__setattr__(
            self,
            "source_state_context",
            _payload(self.source_state_context, "transition source-state context"),
        )
        object.__setattr__(
            self,
            "target_state_context",
            _payload(self.target_state_context, "transition target-state context"),
        )
        if self.schema_version != 2:
            raise ValueError("transition candidate schema version must be 2")

    @property
    def event_name(self) -> str:
        return self.event

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event": self.event,
            "source": self.source,
            "target": self.target,
            "transition_context": _mapping_json(self.transition_context),
            "source_state_context": _mapping_json(self.source_state_context),
            "target_state_context": _mapping_json(self.target_state_context),
        }

    def to_canonical_json(self) -> str:
        return _canonical(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TransitionCandidate:
        _strict_fields(
            value,
            {
                "schema_version",
                "event",
                "source",
                "target",
                "transition_context",
                "source_state_context",
                "target_state_context",
            },
            "Transition Candidate",
        )
        return cls(**value)

    @classmethod
    def from_json(cls, value: str) -> TransitionCandidate:
        return cls.from_dict(_decode(value, "Transition Candidate"))


@dataclass(frozen=True, slots=True)
class FSMExecutionRecord:
    """Durable JSON control state used to reconstruct an FSM Runner."""

    mission_id: str
    plan_revision: int
    statechart_revision: int
    active_state: str
    active_configuration: tuple[str, ...] = ()
    last_applied_event: str | None = None
    transition_history: tuple[str, ...] = ()
    superseded_plan_revision: int | None = None
    record_revision: int = 1
    last_applied_event_identity: str | None = None
    applied_event_identities: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        _text(self.mission_id, "execution record mission ID")
        _non_negative_int(self.plan_revision, "execution record plan revision")
        _non_negative_int(self.statechart_revision, "execution record statechart revision")
        _text(self.active_state, "execution record active state")
        configuration = tuple(self.active_configuration) or (self.active_state,)
        if not all(isinstance(item, str) and item for item in configuration):
            raise ValueError("execution record configuration must contain state names")
        history = tuple(self.transition_history)
        if not all(isinstance(item, str) and item for item in history):
            raise ValueError("execution record transition history must contain events")
        if self.last_applied_event is not None:
            _text(self.last_applied_event, "execution record last event")
        if self.last_applied_event_identity is not None:
            _text(self.last_applied_event_identity, "execution record last event identity")
        if self.superseded_plan_revision is not None:
            _non_negative_int(self.superseded_plan_revision, "execution record superseded revision")
        applied_ids = tuple(self.applied_event_identities)
        if not all(isinstance(item, str) and item for item in applied_ids):
            raise ValueError("execution record applied event identities must be strings")
        _positive_int(self.record_revision, "execution record revision")
        _positive_int(self.schema_version, "execution record schema version")
        object.__setattr__(self, "active_configuration", configuration)
        object.__setattr__(self, "transition_history", history)
        object.__setattr__(self, "applied_event_identities", applied_ids)

    @property
    def active_plan_revision(self) -> int:
        return self.plan_revision

    @property
    def last_event(self) -> str | None:
        return self.last_applied_event

    @property
    def applied_event_ids(self) -> tuple[str, ...]:
        return self.applied_event_identities

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "plan_revision": self.plan_revision,
            "statechart_revision": self.statechart_revision,
            "active_state": self.active_state,
            "active_configuration": list(self.active_configuration),
            "last_applied_event": self.last_applied_event,
            "transition_history": list(self.transition_history),
            "superseded_plan_revision": self.superseded_plan_revision,
            "record_revision": self.record_revision,
            "last_applied_event_identity": self.last_applied_event_identity,
            "applied_event_identities": list(self.applied_event_identities),
        }

    def to_canonical_json(self) -> str:
        return _canonical(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FSMExecutionRecord:
        expected = {
            "schema_version", "mission_id", "plan_revision", "statechart_revision",
            "active_state", "active_configuration", "last_applied_event",
            "transition_history", "superseded_plan_revision", "record_revision",
            "last_applied_event_identity", "applied_event_identities",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("FSM Execution Record contains unknown or missing fields")
        if (
            not isinstance(value["active_configuration"], (list, tuple))
            or not isinstance(value["transition_history"], (list, tuple))
            or not isinstance(value["applied_event_identities"], (list, tuple))
        ):
            raise TypeError("FSM Execution Record arrays are invalid")
        payload = dict(value)
        payload["active_configuration"] = tuple(value["active_configuration"])
        payload["transition_history"] = tuple(value["transition_history"])
        payload["applied_event_identities"] = tuple(value["applied_event_identities"])
        return cls(**payload)

    @classmethod
    def from_json(cls, value: str) -> FSMExecutionRecord:
        return cls.from_dict(_decode(value, "FSM Execution Record"))


@dataclass(frozen=True, slots=True)
class FSMStatus:
    """Published current FSM control state and enabled transition choices."""

    mission_id: str
    plan_revision: int
    statechart_revision: int
    active_state: str
    transition_candidates: tuple[TransitionCandidate, ...] = ()
    status: str = "ready"
    superseded_plan_revision: int | None = None
    last_applied_event: str | None = None
    schema_version: int = 1
    active_state_context: Mapping[str, object] = MappingProxyType({})
    state_entry_revision: int = 1

    def __post_init__(self) -> None:
        _text(self.mission_id, "FSM status mission ID")
        _non_negative_int(self.plan_revision, "FSM status plan revision")
        _non_negative_int(self.statechart_revision, "FSM status statechart revision")
        _text(self.active_state, "FSM status active state")
        candidates = tuple(self.transition_candidates)
        if not all(isinstance(item, TransitionCandidate) for item in candidates):
            raise ValueError("FSM status candidates must be Transition Candidate records")
        _text(self.status, "FSM status kind")
        if self.superseded_plan_revision is not None:
            _non_negative_int(self.superseded_plan_revision, "FSM status superseded revision")
        if self.last_applied_event is not None:
            _text(self.last_applied_event, "FSM status last event")
        _positive_int(self.schema_version, "FSM status schema version")
        _positive_int(self.state_entry_revision, "FSM status state-entry revision")
        object.__setattr__(self, "transition_candidates", candidates)
        object.__setattr__(
            self,
            "active_state_context",
            _payload(self.active_state_context, "FSM active-state context"),
        )

    @property
    def enabled_events(self) -> tuple[str, ...]:
        return tuple(item.event for item in self.transition_candidates)

    @property
    def enabled_transition_candidates(self) -> tuple[TransitionCandidate, ...]:
        return self.transition_candidates

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "plan_revision": self.plan_revision,
            "statechart_revision": self.statechart_revision,
            "active_state": self.active_state,
            "transition_candidates": [item.to_dict() for item in self.transition_candidates],
            "status": self.status,
            "superseded_plan_revision": self.superseded_plan_revision,
            "last_applied_event": self.last_applied_event,
            "active_state_context": _mapping_json(self.active_state_context),
            "state_entry_revision": self.state_entry_revision,
        }

    def to_canonical_json(self) -> str:
        return _canonical(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FSMStatus:
        expected = {
            "schema_version", "mission_id", "plan_revision", "statechart_revision",
            "active_state", "transition_candidates", "status",
            "superseded_plan_revision", "last_applied_event", "active_state_context",
            "state_entry_revision",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("FSM Status contains unknown or missing fields")
        candidates = value["transition_candidates"]
        if not isinstance(candidates, (list, tuple)):
            raise TypeError("FSM Status arrays are invalid")
        payload = dict(value)
        payload["transition_candidates"] = tuple(
            TransitionCandidate.from_dict(item) for item in candidates
        )
        return cls(**payload)

    @classmethod
    def from_json(cls, value: str) -> FSMStatus:
        return cls.from_dict(_decode(value, "FSM Status"))
