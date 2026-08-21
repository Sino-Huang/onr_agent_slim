"""Canonical, derived, non-authoritative planner-facing Mission interpretation."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from onr.contracts.planning import PlannerChoice, PlanningProfile


_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "mission_id",
        "source_authority",
        "objective",
        "rationale",
        "planner_choice",
        "details",
    }
)
_PROHIBITED_DETAIL_KEYS = frozenset(
    {
        "planner_assets",
        "generated_assets",
        "solver_input",
        "solver_output",
        "verification_evidence",
        "normalized_plan",
        "mission_spec",
    }
)


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _freeze_details(value: object, label: str) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} must contain only finite JSON numbers")
        return value
    if isinstance(value, Mapping):
        frozen = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} keys must be strings")
            if key in _PROHIBITED_DETAIL_KEYS:
                raise ValueError(f"{label} cannot contain planner-owned provenance")
            frozen[key] = _freeze_details(item, label)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_details(item, label) for item in value)
    raise ValueError(f"{label} must contain only JSON values")


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


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not permitted: {value}")


@dataclass(frozen=True, slots=True)
class PlanningIntent:
    """Immutable, derived, non-authoritative planner-facing interpretation."""

    mission_id: str
    source_authority: str
    objective: str
    rationale: str
    planner_choice: PlannerChoice
    details: Mapping[str, object]
    schema_version: int = field(default=1, init=False)

    def __post_init__(self) -> None:
        _require_text(self.mission_id, "mission ID")
        _require_text(self.source_authority, "source authority")
        _require_text(self.objective, "mission objective")
        _require_text(self.rationale, "planning rationale")
        if not isinstance(self.planner_choice, PlannerChoice):
            raise ValueError("planner choice must be a PlannerChoice")
        if (self.planner_choice.planning_profile, self.planner_choice.planner_id) not in (
            (PlanningProfile.TEMPORAL, "minizinc"),
            (PlanningProfile.SYMBOLIC, "fast-downward"),
        ):
            raise ValueError("planning intent requires a configured planner")
        if not isinstance(self.details, Mapping):
            raise ValueError("planning intent details must be a JSON object")
        if any(key in _TOP_LEVEL_FIELDS for key in self.details):
            raise ValueError("planning intent details cannot contain reserved top-level keys")
        object.__setattr__(self, "details", _freeze_details(self.details, "planning intent details"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "source_authority": self.source_authority,
            "objective": self.objective,
            "rationale": self.rationale,
            "planner_choice": self.planner_choice.to_dict(),
            "details": _json_value(self.details),
        }

    def to_canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PlanningIntent:
        if not isinstance(value, Mapping) or set(value) != _TOP_LEVEL_FIELDS:
            raise ValueError("planning intent contains unknown or missing fields")
        schema_version = value["schema_version"]
        if isinstance(schema_version, bool) or schema_version != 1:
            raise ValueError("planning intent schema version must be exactly 1")
        return cls(
            mission_id=value["mission_id"],
            source_authority=value["source_authority"],
            objective=value["objective"],
            rationale=value["rationale"],
            planner_choice=PlannerChoice.from_dict(value["planner_choice"]),
            details=value["details"],
        )

    @classmethod
    def from_json(cls, value: str) -> PlanningIntent:
        try:
            decoded = json.loads(value, parse_constant=_reject_non_finite)
        except (TypeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("planning intent must be valid JSON") from exc
        return cls.from_dict(decoded)
