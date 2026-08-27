"""Generic immutable contracts for uncertain binary Bayesian beliefs."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from onr.contracts.environment import EntityId


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


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer greater than or equal to {minimum}")
    return value


def _probability(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a probability")
    selected = float(value)
    if not math.isfinite(selected) or not 0.0 <= selected <= 1.0:
        raise ValueError(f"{label} must be a finite probability")
    return selected


def _strict(value: object, fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{label} contains unknown or missing fields")
    return value


def canonical_json(value: object) -> str:
    """Encode JSON using the canonical representation used for belief hashes."""

    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Bayesian belief content is not JSON-safe") from exc


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, order=True, slots=True)
class BeliefKey:
    """Identity of one generic binary belief variable."""

    entity_id: EntityId
    risk_type: str

    def __post_init__(self) -> None:
        _entity_id(self.entity_id, "entity ID")
        _text(self.risk_type, "risk type")

    def to_dict(self) -> dict[str, object]:
        return {"entity_id": self.entity_id, "risk_type": self.risk_type}

    @classmethod
    def from_dict(cls, value: object) -> "BeliefKey":
        data = _strict(value, {"entity_id", "risk_type"}, "belief key")
        return cls(entity_id=data["entity_id"], risk_type=data["risk_type"])


@dataclass(frozen=True, order=True, slots=True)
class EntityAssociation:
    """Probability that an uncertain observation belongs to one entity."""

    entity_id: EntityId
    weight: float

    def __post_init__(self) -> None:
        _entity_id(self.entity_id, "association entity ID")
        object.__setattr__(self, "weight", _probability(self.weight, "association weight"))

    def to_dict(self) -> dict[str, object]:
        return {"entity_id": self.entity_id, "weight": self.weight}

    @classmethod
    def from_dict(cls, value: object) -> "EntityAssociation":
        data = _strict(value, {"entity_id", "weight"}, "entity association")
        return cls(entity_id=data["entity_id"], weight=data["weight"])


@dataclass(frozen=True, slots=True)
class RiskObservation:
    """One uncertain observation with marginalized entity association."""

    event_id: str
    input_revision: int
    risk_type: str
    associations: tuple[EntityAssociation, ...]
    likelihood_given_risk: float
    likelihood_given_safe: float

    def __post_init__(self) -> None:
        _text(self.event_id, "input event ID")
        _integer(self.input_revision, "input revision")
        _text(self.risk_type, "risk type")
        associations = tuple(self.associations)
        if not associations or not all(isinstance(item, EntityAssociation) for item in associations):
            raise ValueError("observation associations must contain typed candidates")
        entity_ids = [item.entity_id for item in associations]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("observation association entity IDs must be unique")
        if not math.isclose(
            math.fsum(item.weight for item in associations),
            1.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("observation association weights must sum to one")
        object.__setattr__(self, "associations", tuple(sorted(associations)))
        object.__setattr__(
            self,
            "likelihood_given_risk",
            _probability(self.likelihood_given_risk, "risk likelihood"),
        )
        object.__setattr__(
            self,
            "likelihood_given_safe",
            _probability(self.likelihood_given_safe, "safe likelihood"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "input_revision": self.input_revision,
            "risk_type": self.risk_type,
            "associations": [item.to_dict() for item in self.associations],
            "likelihood_given_risk": self.likelihood_given_risk,
            "likelihood_given_safe": self.likelihood_given_safe,
        }

    @classmethod
    def from_dict(cls, value: object) -> "RiskObservation":
        fields = {
            "event_id",
            "input_revision",
            "risk_type",
            "associations",
            "likelihood_given_risk",
            "likelihood_given_safe",
        }
        data = _strict(value, fields, "risk observation")
        raw_associations = data["associations"]
        if not isinstance(raw_associations, (list, tuple)):
            raise ValueError("observation associations must be an array")
        return cls(
            event_id=data["event_id"],
            input_revision=data["input_revision"],
            risk_type=data["risk_type"],
            associations=tuple(EntityAssociation.from_dict(item) for item in raw_associations),
            likelihood_given_risk=data["likelihood_given_risk"],
            likelihood_given_safe=data["likelihood_given_safe"],
        )


@dataclass(frozen=True, order=True, slots=True)
class RiskAssignment:
    """A required value within a forbidden logical combination."""

    key: BeliefKey
    is_risk: bool

    def __post_init__(self) -> None:
        if not isinstance(self.key, BeliefKey):
            raise ValueError("risk assignment key must be a BeliefKey")
        if not isinstance(self.is_risk, bool):
            raise ValueError("risk assignment value must be boolean")

    def to_dict(self) -> dict[str, object]:
        return {"key": self.key.to_dict(), "is_risk": self.is_risk}

    @classmethod
    def from_dict(cls, value: object) -> "RiskAssignment":
        data = _strict(value, {"key", "is_risk"}, "risk assignment")
        return cls(key=BeliefKey.from_dict(data["key"]), is_risk=data["is_risk"])


@dataclass(frozen=True, slots=True)
class ForbiddenBeliefCombination:
    """Typed logical constraint that forbids one joint risk assignment."""

    constraint_id: str
    assignments: tuple[RiskAssignment, ...]

    def __post_init__(self) -> None:
        _text(self.constraint_id, "constraint ID")
        assignments = tuple(self.assignments)
        if not assignments or not all(isinstance(item, RiskAssignment) for item in assignments):
            raise ValueError("forbidden combination must contain typed assignments")
        keys = [item.key for item in assignments]
        if len(keys) != len(set(keys)):
            raise ValueError("forbidden combination may assign each belief key only once")
        object.__setattr__(self, "assignments", tuple(sorted(assignments)))

    def to_dict(self) -> dict[str, object]:
        return {
            "constraint_id": self.constraint_id,
            "assignments": [item.to_dict() for item in self.assignments],
        }

    @classmethod
    def from_dict(cls, value: object) -> "ForbiddenBeliefCombination":
        data = _strict(value, {"constraint_id", "assignments"}, "forbidden combination")
        raw_assignments = data["assignments"]
        if not isinstance(raw_assignments, (list, tuple)):
            raise ValueError("forbidden combination assignments must be an array")
        return cls(
            constraint_id=data["constraint_id"],
            assignments=tuple(RiskAssignment.from_dict(item) for item in raw_assignments),
        )


@dataclass(frozen=True, order=True, slots=True)
class BeliefMarginal:
    """Posterior marginal probability for one binary belief key."""

    key: BeliefKey
    probability_risk: float

    def __post_init__(self) -> None:
        if not isinstance(self.key, BeliefKey):
            raise ValueError("belief marginal key must be a BeliefKey")
        object.__setattr__(
            self,
            "probability_risk",
            _probability(self.probability_risk, "risk marginal"),
        )

    def to_dict(self) -> dict[str, object]:
        return {**self.key.to_dict(), "probability_risk": self.probability_risk}

    @classmethod
    def from_dict(cls, value: object) -> "BeliefMarginal":
        data = _strict(
            value,
            {"entity_id", "risk_type", "probability_risk"},
            "belief marginal",
        )
        return cls(
            key=BeliefKey(data["entity_id"], data["risk_type"]),
            probability_risk=data["probability_risk"],
        )


@dataclass(frozen=True, slots=True)
class BayesianBeliefSnapshot:
    """Hash-addressed JSON-safe posterior snapshot for one mission revision."""

    schema_version: int
    mission_id: str
    belief_revision: int
    input_event_id: str
    input_revision: int
    created_at: str
    marginals: tuple[BeliefMarginal, ...]
    content_sha256: str

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise ValueError("unsupported Bayesian belief snapshot schema version")
        _text(self.mission_id, "mission ID")
        _integer(self.belief_revision, "belief revision", minimum=1)
        _text(self.input_event_id, "input event ID")
        _integer(self.input_revision, "input revision")
        _text(self.created_at, "snapshot creation time")
        try:
            timestamp = datetime.fromisoformat(self.created_at)
        except ValueError as exc:
            raise ValueError("snapshot creation time must be ISO-8601") from exc
        if timestamp.tzinfo is None:
            raise ValueError("snapshot creation time must include a timezone")
        marginals = tuple(self.marginals)
        if not marginals or not all(isinstance(item, BeliefMarginal) for item in marginals):
            raise ValueError("snapshot must contain typed belief marginals")
        keys = [item.key for item in marginals]
        if len(keys) != len(set(keys)):
            raise ValueError("snapshot belief marginal keys must be unique")
        object.__setattr__(self, "marginals", tuple(sorted(marginals)))
        if (
            not isinstance(self.content_sha256, str)
            or len(self.content_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.content_sha256)
        ):
            raise ValueError("snapshot content hash must be a lowercase SHA-256 digest")
        if self.content_sha256 != canonical_sha256(self.content_dict()):
            raise ValueError("snapshot content hash does not match its canonical content")

    def content_dict(self) -> dict[str, object]:
        """Return canonical hash content, deliberately excluding the hash itself."""

        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "belief_revision": self.belief_revision,
            "input_event_id": self.input_event_id,
            "input_revision": self.input_revision,
            "created_at": self.created_at,
            "marginals": [item.to_dict() for item in self.marginals],
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
        marginals: Iterable[BeliefMarginal],
    ) -> "BayesianBeliefSnapshot":
        selected_marginals = tuple(sorted(marginals))
        content = {
            "schema_version": 1,
            "mission_id": mission_id,
            "belief_revision": belief_revision,
            "input_event_id": input_event_id,
            "input_revision": input_revision,
            "created_at": created_at,
            "marginals": [item.to_dict() for item in selected_marginals],
        }
        return cls(
            schema_version=1,
            mission_id=mission_id,
            belief_revision=belief_revision,
            input_event_id=input_event_id,
            input_revision=input_revision,
            created_at=created_at,
            marginals=selected_marginals,
            content_sha256=canonical_sha256(content),
        )

    @classmethod
    def from_dict(cls, value: object) -> "BayesianBeliefSnapshot":
        fields = {
            "schema_version",
            "mission_id",
            "belief_revision",
            "input_event_id",
            "input_revision",
            "created_at",
            "marginals",
            "content_sha256",
        }
        data = _strict(value, fields, "Bayesian belief snapshot")
        raw_marginals = data["marginals"]
        if not isinstance(raw_marginals, (list, tuple)):
            raise ValueError("snapshot marginals must be an array")
        return cls(
            schema_version=data["schema_version"],
            mission_id=data["mission_id"],
            belief_revision=data["belief_revision"],
            input_event_id=data["input_event_id"],
            input_revision=data["input_revision"],
            created_at=data["created_at"],
            marginals=tuple(BeliefMarginal.from_dict(item) for item in raw_marginals),
            content_sha256=data["content_sha256"],
        )

    @classmethod
    def from_json(cls, value: str) -> "BayesianBeliefSnapshot":
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Bayesian belief snapshot JSON is invalid") from exc
        return cls.from_dict(decoded)


__all__ = [
    "BayesianBeliefSnapshot",
    "BeliefKey",
    "BeliefMarginal",
    "EntityAssociation",
    "ForbiddenBeliefCombination",
    "RiskAssignment",
    "RiskObservation",
    "canonical_json",
    "canonical_sha256",
]
