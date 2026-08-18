"""Immutable public contracts for temporal mission planning."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


JsonScalar = str | int | float | bool | None
_MANEUVER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")


class PlanningProfile(StrEnum):
    """Declared planning semantics for one plan revision."""

    TEMPORAL = "temporal"


class PlanningOutcome(StrEnum):
    """Distinct terminal outcomes from one planner execution."""

    SOLVED = "solved"
    UNSOLVABLE = "unsolvable"
    INCOMPLETE = "incomplete"
    TIMEOUT = "timeout"
    ERROR = "error"


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not permitted: {value}")


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _require_json_scalar(value: object, label: str) -> JsonScalar:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"{label} must be a JSON scalar")
    return value


@dataclass(frozen=True, slots=True)
class PlannerChoice:
    """Semantic selection of temporal planning with MiniZinc."""

    planning_profile: PlanningProfile | str
    planner_id: str

    def __post_init__(self) -> None:
        try:
            profile = PlanningProfile(self.planning_profile)
        except (TypeError, ValueError) as exc:
            raise ValueError("planner choice must use temporal planning") from exc
        if profile is not PlanningProfile.TEMPORAL or self.planner_id != "minizinc":
            raise ValueError("planner choice must select temporal MiniZinc")
        object.__setattr__(self, "planning_profile", profile)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PlannerChoice:
        if not isinstance(value, Mapping):
            raise ValueError("planner choice must be a JSON object")
        expected_keys = {"planner_id", "planning_profile"}
        actual_keys = set(value)
        if actual_keys != expected_keys:
            raise ValueError("planner choice contains unknown or missing fields")
        planner_id = value["planner_id"]
        planning_profile = value["planning_profile"]
        if not isinstance(planner_id, str) or not isinstance(planning_profile, str):
            raise ValueError("planner choice fields must be strings")
        return cls(planner_id=planner_id, planning_profile=planning_profile)

    @classmethod
    def from_json(cls, value: str) -> PlannerChoice:
        try:
            decoded = json.loads(value, parse_constant=_reject_non_finite)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("planner choice must be valid JSON") from exc
        return cls.from_dict(decoded)

    def to_dict(self) -> dict[str, str]:
        return {
            "planner_id": self.planner_id,
            "planning_profile": str(self.planning_profile),
        }


@dataclass(frozen=True, slots=True)
class ManeuverParameter:
    """One typed parameter carried by an abstract maneuver intent."""

    name: str
    value: JsonScalar

    def __post_init__(self) -> None:
        _require_text(self.name, "maneuver parameter name")
        _require_json_scalar(self.value, "maneuver parameter value")


@dataclass(frozen=True, slots=True)
class ManeuverIntent:
    """Abstract maneuver action and immutable typed parameters."""

    action: str
    parameters: tuple[ManeuverParameter, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.action, "maneuver action")
        parameters = tuple(self.parameters)
        if not all(isinstance(item, ManeuverParameter) for item in parameters):
            raise ValueError("maneuver parameters must be ManeuverParameter records")
        names = [item.name for item in parameters]
        if len(set(names)) != len(names):
            raise ValueError("maneuver parameter names must be unique")
        object.__setattr__(self, "parameters", tuple(sorted(parameters, key=lambda item: item.name)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "parameters": {item.name: item.value for item in self.parameters},
        }


@dataclass(frozen=True, slots=True)
class TemporalManeuver:
    """Structured temporal maneuver requested by a Mission Specification."""

    maneuver_id: str
    intent: ManeuverIntent
    dependencies: tuple[str, ...]
    duration: int

    def __post_init__(self) -> None:
        if not isinstance(self.maneuver_id, str) or not _MANEUVER_ID.fullmatch(
            self.maneuver_id
        ):
            raise ValueError("maneuver ID must be a portable semantic identifier")
        if not isinstance(self.intent, ManeuverIntent):
            raise ValueError("maneuver intent must be a ManeuverIntent")
        dependencies = tuple(self.dependencies)
        if not all(
            isinstance(item, str) and _MANEUVER_ID.fullmatch(item)
            for item in dependencies
        ):
            raise ValueError("dependencies must be semantic maneuver IDs")
        if len(set(dependencies)) != len(dependencies):
            raise ValueError("maneuver dependencies must be unique")
        if self.maneuver_id in dependencies:
            raise ValueError("a maneuver cannot depend on itself")
        if isinstance(self.duration, bool) or not isinstance(self.duration, int):
            raise ValueError("maneuver duration must be an integer")
        if self.duration <= 0:
            raise ValueError("maneuver duration must be positive")
        object.__setattr__(self, "dependencies", tuple(sorted(dependencies)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "maneuver_id": self.maneuver_id,
            "intent": self.intent.to_dict(),
            "dependencies": self.dependencies,
            "duration": self.duration,
        }


@dataclass(frozen=True, slots=True)
class MissionSpec:
    """Immutable structured temporal description of a Mission."""

    mission_id: str
    objective: str
    planner_choice: PlannerChoice
    maneuvers: tuple[TemporalManeuver, ...]
    horizon: int
    source_authority: str

    def __post_init__(self) -> None:
        _require_text(self.mission_id, "mission ID")
        _require_text(self.objective, "mission objective")
        _require_text(self.source_authority, "source authority")
        if not isinstance(self.planner_choice, PlannerChoice):
            raise ValueError("planner choice must be a PlannerChoice")
        if isinstance(self.horizon, bool) or not isinstance(self.horizon, int):
            raise ValueError("mission horizon must be an integer")
        if self.horizon <= 0:
            raise ValueError("mission horizon must be positive")

        maneuvers = tuple(self.maneuvers)
        if not maneuvers or not all(
            isinstance(item, TemporalManeuver) for item in maneuvers
        ):
            raise ValueError("Mission Specification requires temporal maneuvers")
        maneuver_ids = [item.maneuver_id for item in maneuvers]
        if len(set(maneuver_ids)) != len(maneuver_ids):
            raise ValueError("maneuver IDs must be unique")
        declared_ids = set(maneuver_ids)
        if any(
            dependency not in declared_ids
            for maneuver in maneuvers
            for dependency in maneuver.dependencies
        ):
            raise ValueError("dependencies must identify Mission maneuvers")
        if any(item.duration > self.horizon for item in maneuvers):
            raise ValueError("maneuver duration cannot exceed the Mission horizon")
        _reject_dependency_cycles(maneuvers)

        object.__setattr__(
            self, "maneuvers", tuple(sorted(maneuvers, key=lambda item: item.maneuver_id))
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "objective": self.objective,
            "planner_choice": self.planner_choice.to_dict(),
            "maneuvers": [item.to_dict() for item in self.maneuvers],
            "horizon": self.horizon,
            "source_authority": self.source_authority,
        }

    def to_canonical_json(self) -> str:
        return _canonical_json(self.to_dict())


def _reject_dependency_cycles(maneuvers: tuple[TemporalManeuver, ...]) -> None:
    dependencies = {item.maneuver_id: item.dependencies for item in maneuvers}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(maneuver_id: str) -> None:
        if maneuver_id in visiting:
            raise ValueError("maneuver dependencies must be acyclic")
        if maneuver_id in visited:
            return
        visiting.add(maneuver_id)
        for dependency in dependencies[maneuver_id]:
            visit(dependency)
        visiting.remove(maneuver_id)
        visited.add(maneuver_id)

    for maneuver_id in dependencies:
        visit(maneuver_id)


@dataclass(frozen=True, slots=True)
class TemporalAssignment:
    """Planner-native timing assignment for one declared maneuver."""

    maneuver_id: str
    start: int
    duration: int

    def __post_init__(self) -> None:
        if not isinstance(self.maneuver_id, str) or not _MANEUVER_ID.fullmatch(
            self.maneuver_id
        ):
            raise ValueError("assignment maneuver ID is invalid")
        if isinstance(self.start, bool) or not isinstance(self.start, int):
            raise ValueError("assignment start must be an integer")
        if isinstance(self.duration, bool) or not isinstance(self.duration, int):
            raise ValueError("assignment duration must be an integer")
        if self.start < 0 or self.duration <= 0:
            raise ValueError("assignment timing must be non-negative and positive")


@dataclass(frozen=True, slots=True)
class PlannerExecutionResult:
    """Terminal timing result returned through the planner executor port."""

    outcome: PlanningOutcome | str
    assignments: tuple[TemporalAssignment, ...] = ()

    def __post_init__(self) -> None:
        outcome = PlanningOutcome(self.outcome)
        assignments = tuple(self.assignments)
        if not all(isinstance(item, TemporalAssignment) for item in assignments):
            raise ValueError("planner assignments must be TemporalAssignment records")
        if outcome is not PlanningOutcome.SOLVED and assignments:
            raise ValueError("only a solved planner result may contain assignments")
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "assignments", assignments)


@dataclass(frozen=True, slots=True)
class ScheduledManeuver:
    """Abstract maneuver intent placed on a normalized temporal schedule."""

    maneuver_id: str
    intent: ManeuverIntent
    dependencies: tuple[str, ...]
    start: int
    duration: int

    def __post_init__(self) -> None:
        if not isinstance(self.maneuver_id, str) or not _MANEUVER_ID.fullmatch(
            self.maneuver_id
        ):
            raise ValueError("scheduled maneuver ID is invalid")
        if not isinstance(self.intent, ManeuverIntent):
            raise ValueError("scheduled intent must be a ManeuverIntent")
        dependencies = tuple(self.dependencies)
        if not all(isinstance(item, str) for item in dependencies):
            raise ValueError("scheduled dependencies must be maneuver IDs")
        if isinstance(self.start, bool) or not isinstance(self.start, int):
            raise ValueError("scheduled start must be an integer")
        if isinstance(self.duration, bool) or not isinstance(self.duration, int):
            raise ValueError("scheduled duration must be an integer")
        if self.start < 0 or self.duration <= 0:
            raise ValueError("scheduled timing must be non-negative and positive")
        object.__setattr__(self, "dependencies", tuple(sorted(dependencies)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "maneuver_id": self.maneuver_id,
            "intent": self.intent.to_dict(),
            "dependencies": self.dependencies,
            "start": self.start,
            "duration": self.duration,
        }


@dataclass(frozen=True, slots=True)
class NormalizedPlan:
    """Canonical planner-independent plan outcome with provenance."""

    mission_spec: MissionSpec
    plan_revision: int
    mission_snapshot_id: str
    planner_choice: PlannerChoice
    outcome: PlanningOutcome | str
    maneuvers: tuple[ScheduledManeuver, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.plan_revision, bool) or not isinstance(
            self.plan_revision, int
        ):
            raise ValueError("plan revision must be an integer")
        if self.plan_revision < 0:
            raise ValueError("plan revision must be non-negative")
        _require_text(self.mission_snapshot_id, "Mission Snapshot ID")
        if self.planner_choice != self.mission_spec.planner_choice:
            raise ValueError("plan Planner Choice must match the Mission Specification")
        outcome = PlanningOutcome(self.outcome)
        maneuvers = tuple(self.maneuvers)
        if not all(isinstance(item, ScheduledManeuver) for item in maneuvers):
            raise ValueError("normalized maneuvers must be ScheduledManeuver records")
        if outcome is not PlanningOutcome.SOLVED and maneuvers:
            raise ValueError("only a solved Normalized Plan may contain maneuvers")
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(
            self,
            "maneuvers",
            tuple(sorted(maneuvers, key=lambda item: (item.start, item.maneuver_id))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_spec": self.mission_spec.to_dict(),
            "plan_revision": self.plan_revision,
            "mission_snapshot_id": self.mission_snapshot_id,
            "planner_choice": self.planner_choice.to_dict(),
            "outcome": str(self.outcome),
            "maneuvers": [item.to_dict() for item in self.maneuvers],
        }

    def to_canonical_json(self) -> str:
        return _canonical_json(self.to_dict())


@dataclass(frozen=True, slots=True)
class TemporalPlanningResult:
    """Public result of one temporal planning attempt."""

    outcome: PlanningOutcome
    normalized_plan: NormalizedPlan

    def __post_init__(self) -> None:
        if self.outcome is not self.normalized_plan.outcome:
            raise ValueError("planning result outcome must match its Normalized Plan")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
