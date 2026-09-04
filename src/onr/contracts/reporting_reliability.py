"""Immutable Mission 1 reporting-reliability belief contracts."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from onr.contracts.bayesian_belief import canonical_sha256
from onr.contracts.bayesian_belief import canonical_json


def _probability(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a probability")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(f"{label} must be a finite probability")
    return result


def _interval(value: object, label: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{label} must contain two probabilities")
    lower = _probability(value[0], f"{label} lower bound")
    upper = _probability(value[1], f"{label} upper bound")
    if lower > upper:
        raise ValueError(f"{label} is reversed")
    return lower, upper


@dataclass(frozen=True, order=True, slots=True)
class ShipReportingReliability:
    entity_id: int
    mean: float
    variance: float
    credible_interval: tuple[float, float]
    honest_probability: float
    expected_omission_probability: float
    expected_variance_reduction: float
    outcome_counts: Mapping[str, int]

    def __post_init__(self) -> None:
        if isinstance(self.entity_id, bool) or not isinstance(self.entity_id, int) or self.entity_id <= 0:
            raise ValueError("reporting entity ID must be a positive integer")
        object.__setattr__(self, "mean", _probability(self.mean, "corruption mean"))
        if isinstance(self.variance, bool) or not isinstance(self.variance, (int, float)) or not math.isfinite(float(self.variance)) or self.variance < 0:
            raise ValueError("corruption variance must be finite and non-negative")
        object.__setattr__(self, "variance", float(self.variance))
        object.__setattr__(self, "credible_interval", _interval(self.credible_interval, "corruption credible interval"))
        object.__setattr__(self, "honest_probability", _probability(self.honest_probability, "honest probability"))
        object.__setattr__(self, "expected_omission_probability", _probability(self.expected_omission_probability, "expected omission probability"))
        if isinstance(self.expected_variance_reduction, bool) or not isinstance(self.expected_variance_reduction, (int, float)) or not math.isfinite(float(self.expected_variance_reduction)) or self.expected_variance_reduction < 0:
            raise ValueError("expected variance reduction must be finite and non-negative")
        object.__setattr__(self, "expected_variance_reduction", float(self.expected_variance_reduction))
        counts = dict(self.outcome_counts)
        if set(counts) != {"clean", "altered", "omitted"} or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts.values()):
            raise ValueError("reporting outcome counts are invalid")
        object.__setattr__(self, "outcome_counts", counts)

    def to_dict(self) -> dict[str, object]:
        return {
            "entity_id": self.entity_id,
            "mean": self.mean,
            "variance": self.variance,
            "credible_interval": list(self.credible_interval),
            "honest_probability": self.honest_probability,
            "expected_omission_probability": self.expected_omission_probability,
            "expected_variance_reduction": self.expected_variance_reduction,
            "outcome_counts": dict(self.outcome_counts),
        }

    @classmethod
    def from_dict(cls, value: object) -> "ShipReportingReliability":
        if not isinstance(value, Mapping):
            raise ValueError("ship reporting reliability must be an object")
        return cls(
            entity_id=value["entity_id"],
            mean=value["mean"],
            variance=value["variance"],
            credible_interval=value["credible_interval"],
            honest_probability=value["honest_probability"],
            expected_omission_probability=value["expected_omission_probability"],
            expected_variance_reduction=value["expected_variance_reduction"],
            outcome_counts=value["outcome_counts"],
        )


@dataclass(frozen=True, slots=True)
class SharedOmissionReliability:
    mean: float
    variance: float
    credible_interval: tuple[float, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "mean", _probability(self.mean, "omission mean"))
        if isinstance(self.variance, bool) or not isinstance(self.variance, (int, float)) or not math.isfinite(float(self.variance)) or self.variance < 0:
            raise ValueError("omission variance must be finite and non-negative")
        object.__setattr__(self, "variance", float(self.variance))
        object.__setattr__(self, "credible_interval", _interval(self.credible_interval, "omission credible interval"))

    def to_dict(self) -> dict[str, object]:
        return {
            "mean": self.mean,
            "variance": self.variance,
            "credible_interval": list(self.credible_interval),
        }

    @classmethod
    def from_dict(cls, value: object) -> "SharedOmissionReliability":
        if not isinstance(value, Mapping):
            raise ValueError("shared omission reliability must be an object")
        return cls(value["mean"], value["variance"], value["credible_interval"])


@dataclass(frozen=True, slots=True)
class ReportingReliabilitySnapshot:
    schema_version: int
    belief_kind: str
    mission_id: str
    belief_revision: int
    input_event_id: str
    input_revision: int
    created_at: str
    ships: tuple[ShipReportingReliability, ...]
    omission: SharedOmissionReliability
    content_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.belief_kind != "reporting_reliability":
            raise ValueError("unsupported reporting reliability snapshot")
        if not isinstance(self.mission_id, str) or not self.mission_id.strip():
            raise ValueError("snapshot Mission ID must be non-empty")
        if isinstance(self.belief_revision, bool) or not isinstance(self.belief_revision, int) or self.belief_revision < 1:
            raise ValueError("snapshot belief revision must be positive")
        if not isinstance(self.input_event_id, str) or not self.input_event_id.strip():
            raise ValueError("snapshot input event ID must be non-empty")
        if isinstance(self.input_revision, bool) or not isinstance(self.input_revision, int) or self.input_revision < 0:
            raise ValueError("snapshot input revision must be non-negative")
        timestamp = datetime.fromisoformat(self.created_at)
        if timestamp.tzinfo is None:
            raise ValueError("snapshot creation time must include a timezone")
        ships = tuple(sorted(self.ships))
        if not ships or len({ship.entity_id for ship in ships}) != len(ships):
            raise ValueError("snapshot ships must be non-empty and unique")
        object.__setattr__(self, "ships", ships)
        if not isinstance(self.omission, SharedOmissionReliability):
            raise TypeError("snapshot omission belief is invalid")
        if self.content_sha256 != canonical_sha256(self.content_dict()):
            raise ValueError("snapshot content hash does not match")

    def content_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "belief_kind": self.belief_kind,
            "mission_id": self.mission_id,
            "belief_revision": self.belief_revision,
            "input_event_id": self.input_event_id,
            "input_revision": self.input_revision,
            "created_at": self.created_at,
            "ships": [ship.to_dict() for ship in self.ships],
            "omission": self.omission.to_dict(),
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.content_dict(), "content_sha256": self.content_sha256}

    def to_canonical_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def create(
        cls,
        *,
        mission_id: str,
        belief_revision: int,
        input_event_id: str,
        input_revision: int,
        created_at: str,
        ships: Iterable[ShipReportingReliability],
        omission: SharedOmissionReliability,
    ) -> "ReportingReliabilitySnapshot":
        selected_ships = tuple(sorted(ships))
        content = {
            "schema_version": 1,
            "belief_kind": "reporting_reliability",
            "mission_id": mission_id,
            "belief_revision": belief_revision,
            "input_event_id": input_event_id,
            "input_revision": input_revision,
            "created_at": created_at,
            "ships": [ship.to_dict() for ship in selected_ships],
            "omission": omission.to_dict(),
        }
        return cls(
            schema_version=1,
            belief_kind="reporting_reliability",
            mission_id=mission_id,
            belief_revision=belief_revision,
            input_event_id=input_event_id,
            input_revision=input_revision,
            created_at=created_at,
            ships=selected_ships,
            omission=omission,
            content_sha256=canonical_sha256(content),
        )

    @classmethod
    def from_dict(cls, value: object) -> "ReportingReliabilitySnapshot":
        if not isinstance(value, Mapping):
            raise ValueError("reporting reliability snapshot must be an object")
        return cls(
            schema_version=value["schema_version"],
            belief_kind=value["belief_kind"],
            mission_id=value["mission_id"],
            belief_revision=value["belief_revision"],
            input_event_id=value["input_event_id"],
            input_revision=value["input_revision"],
            created_at=value["created_at"],
            ships=tuple(ShipReportingReliability.from_dict(item) for item in value["ships"]),
            omission=SharedOmissionReliability.from_dict(value["omission"]),
            content_sha256=value["content_sha256"],
        )


__all__ = [
    "ReportingReliabilitySnapshot",
    "SharedOmissionReliability",
    "ShipReportingReliability",
]
