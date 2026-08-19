"""Immutable public contracts for temporal mission planning."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from pathlib import Path
from typing import Any, cast


JsonScalar = str | int | float | bool | None
_MANEUVER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")


class PlanningProfile(StrEnum):
    """Declared planning semantics for one plan revision."""

    TEMPORAL = "temporal"
    SYMBOLIC = "symbolic"


class PlanningOutcome(StrEnum):
    """Distinct terminal outcomes from one planner execution."""

    SOLVED = "solved"
    UNSOLVABLE = "unsolvable"
    INCOMPLETE = "incomplete"
    TIMEOUT = "timeout"
    ERROR = "error"
    UNSUPPORTED = "unsupported"


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


def _strict_object(value: object, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{label} contains unknown or missing fields")
    return cast(Mapping[str, Any], value)


def _decode_json(value: str, label: str) -> Any:
    try:
        return json.loads(value, parse_constant=_reject_non_finite)
    except (TypeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{label} must be valid JSON") from exc


def _maneuver_intent_from_dict(value: object) -> ManeuverIntent:
    data = _strict_object(value, {"action", "parameters"}, "maneuver intent")
    action = data["action"]
    parameters = data["parameters"]
    if not isinstance(action, str) or not isinstance(parameters, Mapping):
        raise ValueError("maneuver intent fields have invalid types")
    parsed = tuple(
        ManeuverParameter(name, _require_json_scalar(item, "maneuver parameter value"))
        for name, item in parameters.items()
        if isinstance(name, str)
    )
    if len(parsed) != len(parameters):
        raise ValueError("maneuver parameter names must be strings")
    return ManeuverIntent(action, parsed)


@dataclass(frozen=True, slots=True)
class PlannerChoice:
    """Semantic planner selection without executable paths."""

    planning_profile: PlanningProfile | str
    planner_id: str | None

    def __post_init__(self) -> None:
        try:
            profile = PlanningProfile(self.planning_profile)
        except (TypeError, ValueError) as exc:
            raise ValueError("planner choice has an unsupported planning profile") from exc
        if profile is PlanningProfile.TEMPORAL and self.planner_id != "minizinc":
            raise ValueError("temporal planner choice must select minizinc")
        if profile is PlanningProfile.SYMBOLIC and self.planner_id not in (
            "fast-downward",
            None,
        ):
            raise ValueError("unsupported symbolic planner")
        object.__setattr__(self, "planning_profile", profile)

    @classmethod
    def unsupported_symbolic(cls) -> PlannerChoice:
        """Return an explicit symbolic choice with no supported planner."""

        return cls(PlanningProfile.SYMBOLIC, None)

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
        if not isinstance(planning_profile, str) or not (
            planner_id is None or isinstance(planner_id, str)
        ):
            raise ValueError("planner choice fields have invalid types")
        return cls(planner_id=planner_id, planning_profile=planning_profile)

    @classmethod
    def from_json(cls, value: str) -> PlannerChoice:
        try:
            decoded = json.loads(value, parse_constant=_reject_non_finite)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("planner choice must be valid JSON") from exc
        return cls.from_dict(decoded)

    def to_dict(self) -> dict[str, str | None]:
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
        if self.planner_choice.planning_profile is not PlanningProfile.TEMPORAL:
            raise ValueError("temporal Mission Specification requires temporal planning")
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

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MissionSpec:
        data = _strict_object(
            value,
            {"mission_id", "objective", "planner_choice", "maneuvers", "horizon", "source_authority"},
            "Mission Specification",
        )
        raw_maneuvers = data["maneuvers"]
        if not isinstance(raw_maneuvers, (list, tuple)):
            raise ValueError("Mission Specification maneuvers must be an array")
        maneuvers = []
        for raw in raw_maneuvers:
            item = _strict_object(
                raw, {"maneuver_id", "intent", "dependencies", "duration"}, "temporal maneuver"
            )
            dependencies = item["dependencies"]
            if not isinstance(dependencies, (list, tuple)):
                raise ValueError("temporal maneuver dependencies must be an array")
            maneuvers.append(
                TemporalManeuver(
                    item["maneuver_id"],
                    _maneuver_intent_from_dict(item["intent"]),
                    tuple(dependencies),
                    item["duration"],
                )
            )
        return cls(
            mission_id=data["mission_id"],
            objective=data["objective"],
            planner_choice=PlannerChoice.from_dict(data["planner_choice"]),
            maneuvers=tuple(maneuvers),
            horizon=data["horizon"],
            source_authority=data["source_authority"],
        )

    @classmethod
    def from_json(cls, value: str) -> MissionSpec:
        return cls.from_dict(_decode_json(value, "Mission Specification"))


def _reject_dependency_cycles(
    maneuvers: tuple[TemporalManeuver | SymbolicManeuver, ...],
) -> None:
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
class SymbolicManeuver:
    """Structured symbolic maneuver with an action cost, never a duration."""

    maneuver_id: str
    intent: ManeuverIntent
    dependencies: tuple[str, ...]
    cost: int

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
        if isinstance(self.cost, bool) or not isinstance(self.cost, int) or self.cost <= 0:
            raise ValueError("maneuver cost must be a positive integer")
        object.__setattr__(self, "dependencies", tuple(sorted(dependencies)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "maneuver_id": self.maneuver_id,
            "intent": self.intent.to_dict(),
            "dependencies": self.dependencies,
            "cost": self.cost,
        }


@dataclass(frozen=True, slots=True)
class SymbolicMissionSpec:
    """Immutable structured description of a symbolic Mission."""

    mission_id: str
    objective: str
    planner_choice: PlannerChoice
    maneuvers: tuple[SymbolicManeuver, ...]
    source_authority: str
    domain_revision: int = 1

    def __post_init__(self) -> None:
        _require_text(self.mission_id, "mission ID")
        _require_text(self.objective, "mission objective")
        _require_text(self.source_authority, "source authority")
        if not isinstance(self.planner_choice, PlannerChoice):
            raise ValueError("planner choice must be a PlannerChoice")
        if self.planner_choice.planning_profile is not PlanningProfile.SYMBOLIC:
            raise ValueError("symbolic Mission Specification requires symbolic planning")
        if (
            isinstance(self.domain_revision, bool)
            or not isinstance(self.domain_revision, int)
            or self.domain_revision <= 0
        ):
            raise ValueError("domain revision must be a positive integer")

        maneuvers = tuple(self.maneuvers)
        if not maneuvers or not all(isinstance(item, SymbolicManeuver) for item in maneuvers):
            raise ValueError("Mission Specification requires symbolic maneuvers")
        maneuver_ids = [item.maneuver_id for item in maneuvers]
        if len(set(maneuver_ids)) != len(maneuver_ids):
            raise ValueError("maneuver IDs must be unique")
        normalized_ids = {
            maneuver_id.lower().rstrip("-") for maneuver_id in maneuver_ids
        }
        if len(normalized_ids) != len(maneuver_ids):
            raise ValueError(
                "symbolic maneuver IDs must be unique after PDDL normalization"
            )
        declared_ids = set(maneuver_ids)
        if any(
            dependency not in declared_ids
            for maneuver in maneuvers
            for dependency in maneuver.dependencies
        ):
            raise ValueError("dependencies must identify Mission maneuvers")
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
            "source_authority": self.source_authority,
            "domain_revision": self.domain_revision,
        }

    def to_canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SymbolicMissionSpec:
        data = _strict_object(
            value,
            {
                "mission_id",
                "objective",
                "planner_choice",
                "maneuvers",
                "source_authority",
                "domain_revision",
            },
            "Symbolic Mission Specification",
        )
        raw_maneuvers = data["maneuvers"]
        if not isinstance(raw_maneuvers, (list, tuple)):
            raise ValueError("Symbolic Mission Specification maneuvers must be an array")
        maneuvers = []
        for raw in raw_maneuvers:
            item = _strict_object(
                raw, {"maneuver_id", "intent", "dependencies", "cost"}, "symbolic maneuver"
            )
            dependencies = item["dependencies"]
            if not isinstance(dependencies, (list, tuple)):
                raise ValueError("symbolic maneuver dependencies must be an array")
            maneuvers.append(
                SymbolicManeuver(
                    item["maneuver_id"],
                    _maneuver_intent_from_dict(item["intent"]),
                    tuple(dependencies),
                    item["cost"],
                )
            )
        return cls(
            mission_id=data["mission_id"],
            objective=data["objective"],
            planner_choice=PlannerChoice.from_dict(data["planner_choice"]),
            maneuvers=tuple(maneuvers),
            source_authority=data["source_authority"],
            domain_revision=data["domain_revision"],
        )

    @classmethod
    def from_json(cls, value: str) -> SymbolicMissionSpec:
        return cls.from_dict(_decode_json(value, "Symbolic Mission Specification"))


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
class PlannerExecutionEvidence:
    """Filesystem evidence persisted for one planner execution."""

    artifact_directory: Path
    artifact_paths: tuple[Path, ...]
    stdout_path: Path
    stderr_path: Path

    def __post_init__(self) -> None:
        artifact_directory = Path(self.artifact_directory).resolve()
        artifact_paths = tuple(Path(path).resolve() for path in self.artifact_paths)
        stdout_path = Path(self.stdout_path).resolve()
        stderr_path = Path(self.stderr_path).resolve()
        object.__setattr__(self, "artifact_directory", artifact_directory)
        object.__setattr__(self, "artifact_paths", artifact_paths)
        object.__setattr__(self, "stdout_path", stdout_path)
        object.__setattr__(self, "stderr_path", stderr_path)


@dataclass(frozen=True, slots=True)
class PlannerExecutionResult:
    """Terminal timing result returned through the planner executor port."""

    outcome: PlanningOutcome | str
    assignments: tuple[TemporalAssignment, ...] = ()
    evidence: PlannerExecutionEvidence | None = None

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
class SymbolicActionCall:
    """One ordered action call emitted by a symbolic planner."""

    action: str
    arguments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.action, str) or not _MANEUVER_ID.fullmatch(self.action):
            raise ValueError("symbolic action name is invalid")
        arguments = tuple(self.arguments)
        if not all(isinstance(item, str) and item for item in arguments):
            raise ValueError("symbolic action arguments must be non-empty strings")
        object.__setattr__(self, "arguments", arguments)

    @property
    def maneuver_id(self) -> str:
        return self.action


@dataclass(frozen=True, slots=True)
class SymbolicPlannerExecutionResult:
    """Terminal result returned through the symbolic planner executor port."""

    outcome: PlanningOutcome | str
    action_calls: tuple[SymbolicActionCall, ...] = ()
    total_plan_cost: int = 0
    evidence: PlannerExecutionEvidence | None = None

    def __post_init__(self) -> None:
        outcome = PlanningOutcome(self.outcome)
        action_calls = tuple(self.action_calls)
        if not all(isinstance(item, SymbolicActionCall) for item in action_calls):
            raise ValueError("symbolic action calls must be SymbolicActionCall records")
        if (
            isinstance(self.total_plan_cost, bool)
            or not isinstance(self.total_plan_cost, int)
            or self.total_plan_cost < 0
        ):
            raise ValueError("total plan cost must be a non-negative integer")
        if outcome is not PlanningOutcome.SOLVED and (
            action_calls or self.total_plan_cost
        ):
            raise ValueError("only a solved symbolic result may contain a plan")
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "action_calls", action_calls)

    @property
    def actions(self) -> tuple[SymbolicActionCall, ...]:
        return self.action_calls


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
class SymbolicPlanStep:
    """One ordered normalized symbolic maneuver, with no timing fields."""

    step_index: int
    maneuver_id: str
    intent: ManeuverIntent
    dependencies: tuple[str, ...]
    cost: int

    def __post_init__(self) -> None:
        if isinstance(self.step_index, bool) or not isinstance(self.step_index, int):
            raise ValueError("symbolic step index must be an integer")
        if self.step_index < 0:
            raise ValueError("symbolic step index must be non-negative")
        if not isinstance(self.maneuver_id, str) or not _MANEUVER_ID.fullmatch(
            self.maneuver_id
        ):
            raise ValueError("symbolic step maneuver ID is invalid")
        if not isinstance(self.intent, ManeuverIntent):
            raise ValueError("symbolic step intent must be a ManeuverIntent")
        dependencies = tuple(self.dependencies)
        if not all(
            isinstance(item, str) and _MANEUVER_ID.fullmatch(item)
            for item in dependencies
        ):
            raise ValueError("symbolic step dependencies must be maneuver IDs")
        if len(set(dependencies)) != len(dependencies):
            raise ValueError("symbolic step dependencies must be unique")
        if self.maneuver_id in dependencies:
            raise ValueError("a symbolic step cannot depend on itself")
        if isinstance(self.cost, bool) or not isinstance(self.cost, int) or self.cost <= 0:
            raise ValueError("symbolic step cost must be a positive integer")
        object.__setattr__(self, "dependencies", tuple(sorted(dependencies)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_index": self.step_index,
            "maneuver_id": self.maneuver_id,
            "intent": self.intent.to_dict(),
            "dependencies": self.dependencies,
            "cost": self.cost,
        }


@dataclass(frozen=True, slots=True)
class VerifiableReference:
    """One external planning artifact reference bound to its content digest."""

    reference: str
    sha256: str

    def __post_init__(self) -> None:
        _require_text(self.reference, "planning evidence reference")
        if not isinstance(self.sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.sha256
        ):
            raise ValueError("planning evidence SHA-256 must be a lowercase digest")

    def to_dict(self) -> dict[str, str]:
        return {"reference": self.reference, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "VerifiableReference":
        data = _strict_object(
            value, {"reference", "sha256"}, "verifiable reference"
        )
        return cls(data["reference"], data["sha256"])


@dataclass(frozen=True, slots=True)
class PlanProvenance:
    """Reference-only authority and evidence for a provenance Normalized Plan."""

    mission_id: str
    source_authority: str
    mission_intent: VerifiableReference
    planning_decision: VerifiableReference
    operational_scene_graph: VerifiableReference
    generated_assets: Mapping[str, VerifiableReference]
    solver_evidence: Mapping[str, VerifiableReference]

    def __post_init__(self) -> None:
        _require_text(self.mission_id, "provenance mission ID")
        _require_text(self.source_authority, "provenance source authority")
        for value, label in (
            (self.mission_intent, "Mission Intent"),
            (self.planning_decision, "Planning Decision"),
            (self.operational_scene_graph, "Operational Scene Graph"),
        ):
            if not isinstance(value, VerifiableReference):
                raise ValueError(f"{label} provenance must be verifiable")
        for name in ("generated_assets", "solver_evidence"):
            values = getattr(self, name)
            if not isinstance(values, Mapping) or not values:
                raise ValueError(f"{name} provenance must be a non-empty mapping")
            copied = dict(values)
            if not all(
                isinstance(key, str)
                and key.strip()
                and isinstance(item, VerifiableReference)
                for key, item in copied.items()
            ):
                raise ValueError(f"{name} provenance is invalid")
            object.__setattr__(self, name, MappingProxyType(copied))

    def to_dict(self) -> dict[str, object]:
        return {
            "mission_id": self.mission_id,
            "source_authority": self.source_authority,
            "mission_intent": self.mission_intent.to_dict(),
            "planning_decision": self.planning_decision.to_dict(),
            "operational_scene_graph": self.operational_scene_graph.to_dict(),
            "generated_assets": {
                key: value.to_dict()
                for key, value in sorted(self.generated_assets.items())
            },
            "solver_evidence": {
                key: value.to_dict()
                for key, value in sorted(self.solver_evidence.items())
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanProvenance":
        data = _strict_object(
            value,
            {
                "mission_id",
                "source_authority",
                "mission_intent",
                "planning_decision",
                "operational_scene_graph",
                "generated_assets",
                "solver_evidence",
            },
            "plan provenance",
        )

        def references(raw: object, label: str) -> dict[str, VerifiableReference]:
            if not isinstance(raw, Mapping):
                raise ValueError(f"{label} must be an object")
            return {
                key: VerifiableReference.from_dict(item)
                for key, item in raw.items()
                if isinstance(key, str) and isinstance(item, Mapping)
            }

        generated = references(data["generated_assets"], "generated assets")
        solver = references(data["solver_evidence"], "solver evidence")
        if len(generated) != len(data["generated_assets"]) or len(solver) != len(
            data["solver_evidence"]
        ):
            raise ValueError("plan provenance reference mappings are invalid")
        return cls(
            mission_id=data["mission_id"],
            source_authority=data["source_authority"],
            mission_intent=VerifiableReference.from_dict(data["mission_intent"]),
            planning_decision=VerifiableReference.from_dict(
                data["planning_decision"]
            ),
            operational_scene_graph=VerifiableReference.from_dict(
                data["operational_scene_graph"]
            ),
            generated_assets=generated,
            solver_evidence=solver,
        )


@dataclass(frozen=True, slots=True)
class NormalizedPlan:
    """Canonical planner-independent plan outcome with provenance."""

    mission_spec: MissionSpec | SymbolicMissionSpec | None
    plan_revision: int
    mission_snapshot_id: str
    planner_choice: PlannerChoice
    outcome: PlanningOutcome | str
    maneuvers: tuple[ScheduledManeuver | SymbolicPlanStep, ...] = ()
    provenance: PlanProvenance | None = None

    def __post_init__(self) -> None:
        if isinstance(self.plan_revision, bool) or not isinstance(
            self.plan_revision, int
        ):
            raise ValueError("plan revision must be an integer")
        if self.plan_revision < 0:
            raise ValueError("plan revision must be non-negative")
        _require_text(self.mission_snapshot_id, "Mission Snapshot ID")
        if (self.mission_spec is None) == (self.provenance is None):
            raise ValueError(
                "Normalized Plan requires exactly one legacy specification or provenance"
            )
        if self.mission_spec is not None:
            if self.planner_choice != self.mission_spec.planner_choice:
                raise ValueError(
                    "plan Planner Choice must match the Mission Specification"
                )
        elif not isinstance(self.provenance, PlanProvenance):
            raise ValueError("provenance-only Normalized Plan is invalid")
        outcome = PlanningOutcome(self.outcome)
        maneuvers = tuple(self.maneuvers)
        temporal = isinstance(self.mission_spec, MissionSpec) or (
            self.mission_spec is None
            and self.planner_choice.planning_profile
            is PlanningProfile.TEMPORAL
        )
        if temporal:
            if not all(isinstance(item, ScheduledManeuver) for item in maneuvers):
                raise ValueError("temporal normalized maneuvers must be ScheduledManeuver records")
        elif self.mission_spec is None:
            if not all(isinstance(item, SymbolicPlanStep) for item in maneuvers):
                raise ValueError(
                    "symbolic normalized maneuvers must be SymbolicPlanStep records"
                )
            symbolic_maneuvers = cast(tuple[SymbolicPlanStep, ...], maneuvers)
            if outcome is PlanningOutcome.SOLVED and tuple(
                item.step_index for item in symbolic_maneuvers
            ) != tuple(range(len(symbolic_maneuvers))):
                raise ValueError(
                    "symbolic normalized steps must have contiguous indices"
                )
        elif isinstance(self.mission_spec, SymbolicMissionSpec):
            if not all(isinstance(item, SymbolicPlanStep) for item in maneuvers):
                raise ValueError("symbolic normalized maneuvers must be SymbolicPlanStep records")
            symbolic_maneuvers = cast(tuple[SymbolicPlanStep, ...], maneuvers)
            if outcome is PlanningOutcome.SOLVED:
                declared = {item.maneuver_id: item for item in self.mission_spec.maneuvers}
                if len(symbolic_maneuvers) != len(declared) or {
                    item.maneuver_id for item in symbolic_maneuvers
                } != set(declared):
                    raise ValueError("symbolic normalized plan must contain each maneuver once")
                if tuple(item.step_index for item in symbolic_maneuvers) != tuple(
                    range(len(symbolic_maneuvers))
                ):
                    raise ValueError("symbolic normalized steps must have contiguous indices")
                if any(
                    item.dependencies != declared[item.maneuver_id].dependencies
                    or item.cost != declared[item.maneuver_id].cost
                    or item.intent != declared[item.maneuver_id].intent
                    for item in symbolic_maneuvers
                ):
                    raise ValueError("symbolic normalized steps must match declared maneuvers")
        else:
            raise ValueError("normalized plan mission specification is invalid")
        if outcome is not PlanningOutcome.SOLVED and maneuvers:
            raise ValueError("only a solved Normalized Plan may contain maneuvers")
        object.__setattr__(self, "outcome", outcome)
        if temporal:
            maneuvers = tuple(
                sorted(
                    cast(tuple[ScheduledManeuver, ...], maneuvers),
                    key=lambda item: (item.start, item.maneuver_id),
                )
            )
        object.__setattr__(self, "maneuvers", maneuvers)


    @property
    def mission_id(self) -> str:
        if self.mission_spec is not None:
            return self.mission_spec.mission_id
        return cast(PlanProvenance, self.provenance).mission_id

    @property
    def source_authority(self) -> str:
        if self.mission_spec is not None:
            return self.mission_spec.source_authority
        return cast(PlanProvenance, self.provenance).source_authority

    @property
    def symbolic_steps(self) -> tuple[SymbolicPlanStep, ...]:
        if not all(isinstance(item, SymbolicPlanStep) for item in self.maneuvers):
            return ()
        return cast(tuple[SymbolicPlanStep, ...], self.maneuvers)

    def to_dict(self) -> dict[str, Any]:
        result = {
            "plan_revision": self.plan_revision,
            "mission_snapshot_id": self.mission_snapshot_id,
            "planner_choice": self.planner_choice.to_dict(),
            "outcome": str(self.outcome),
            "maneuvers": [item.to_dict() for item in self.maneuvers],
        }
        if self.mission_spec is not None:
            result["mission_spec"] = self.mission_spec.to_dict()
        else:
            result["provenance"] = cast(PlanProvenance, self.provenance).to_dict()
        return result

    def to_canonical_json(self) -> str:
        return _canonical_json(self.to_dict())
    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NormalizedPlan":
        if not isinstance(value, Mapping):
            raise ValueError("Normalized Plan must be an object")
        common = {
            "plan_revision",
            "mission_snapshot_id",
            "planner_choice",
            "outcome",
            "maneuvers",
        }
        authority = set(value) - common
        if authority not in ({"mission_spec"}, {"provenance"}):
            raise ValueError(
                "Normalized Plan contains unknown or missing authority fields"
            )
        choice = PlannerChoice.from_dict(value["planner_choice"])
        mission_spec: MissionSpec | SymbolicMissionSpec | None = None
        provenance: PlanProvenance | None = None
        if "mission_spec" in value:
            if not isinstance(value["mission_spec"], Mapping):
                raise ValueError("Mission Specification must be an object")
            if choice.planning_profile is PlanningProfile.TEMPORAL:
                mission_spec = MissionSpec.from_dict(value["mission_spec"])
            else:
                mission_spec = SymbolicMissionSpec.from_dict(value["mission_spec"])
        else:
            if not isinstance(value["provenance"], Mapping):
                raise ValueError("Plan Provenance must be an object")
            provenance = PlanProvenance.from_dict(value["provenance"])

        raw_maneuvers = value["maneuvers"]
        if not isinstance(raw_maneuvers, (list, tuple)):
            raise ValueError("Normalized Plan maneuvers must be an array")

        def dependencies(raw: object) -> tuple[str, ...]:
            if not isinstance(raw, (list, tuple)) or not all(
                isinstance(item, str) for item in raw
            ):
                raise ValueError("Normalized Plan dependencies are invalid")
            return tuple(raw)

        if choice.planning_profile is PlanningProfile.TEMPORAL:
            maneuvers: tuple[ScheduledManeuver | SymbolicPlanStep, ...] = tuple(
                ScheduledManeuver(
                    item["maneuver_id"],
                    _maneuver_intent_from_dict(item["intent"]),
                    dependencies(item["dependencies"]),
                    item["start"],
                    item["duration"],
                )
                for raw in raw_maneuvers
                for item in (
                    _strict_object(
                        raw,
                        {
                            "maneuver_id",
                            "intent",
                            "dependencies",
                            "start",
                            "duration",
                        },
                        "scheduled maneuver",
                    ),
                )
            )
        else:
            maneuvers = tuple(
                SymbolicPlanStep(
                    item["step_index"],
                    item["maneuver_id"],
                    _maneuver_intent_from_dict(item["intent"]),
                    dependencies(item["dependencies"]),
                    item["cost"],
                )
                for raw in raw_maneuvers
                for item in (
                    _strict_object(
                        raw,
                        {
                            "step_index",
                            "maneuver_id",
                            "intent",
                            "dependencies",
                            "cost",
                        },
                        "symbolic plan step",
                    ),
                )
            )
        return cls(
            mission_spec=mission_spec,
            plan_revision=value["plan_revision"],
            mission_snapshot_id=value["mission_snapshot_id"],
            planner_choice=choice,
            outcome=value["outcome"],
            maneuvers=maneuvers,
            provenance=provenance,
        )

    @classmethod
    def from_json(cls, value: str) -> "NormalizedPlan":
        decoded = _decode_json(value, "Normalized Plan")
        if not isinstance(decoded, Mapping):
            raise ValueError("Normalized Plan JSON must be an object")
        return cls.from_dict(decoded)


@dataclass(frozen=True, slots=True)
class TemporalPlanningResult:
    """Public result of one temporal planning attempt."""

    outcome: PlanningOutcome
    normalized_plan: NormalizedPlan | None
    evidence: PlannerExecutionEvidence | None = None

    def __post_init__(self) -> None:
        if self.normalized_plan is None:
            if self.outcome is PlanningOutcome.SOLVED:
                raise ValueError("a solved planning result requires a Normalized Plan")
            return
        if self.outcome is not PlanningOutcome.SOLVED:
            raise ValueError("only a solved planning result may contain a Normalized Plan")
        if self.normalized_plan.outcome is not PlanningOutcome.SOLVED:
            raise ValueError("planning result outcome must match its Normalized Plan")


@dataclass(frozen=True, slots=True)
class SymbolicPlanningResult:
    """Public result of one symbolic planning attempt."""

    outcome: PlanningOutcome
    normalized_plan: NormalizedPlan | None
    evidence: PlannerExecutionEvidence | None = None

    def __post_init__(self) -> None:
        if self.normalized_plan is None:
            if self.outcome is PlanningOutcome.SOLVED:
                raise ValueError("a solved planning result requires a Normalized Plan")
            return
        if self.outcome is not PlanningOutcome.SOLVED:
            raise ValueError("only a solved planning result may contain a Normalized Plan")
        if self.normalized_plan.outcome is not PlanningOutcome.SOLVED:
            raise ValueError("planning result outcome must match its Normalized Plan")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
