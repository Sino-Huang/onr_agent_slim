"""General mission-derived prior knowledge for belief initialization."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


def _freeze(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("prior knowledge must contain only finite JSON numbers")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("prior knowledge keys must be strings")
        return MappingProxyType(
            {str(key): _freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    raise ValueError("prior knowledge must contain only JSON values")


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class PriorKnowledgeClaim:
    claim_kind: str
    parameters: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.claim_kind, str) or not self.claim_kind.strip():
            raise ValueError("prior claim kind must be non-empty")
        if not isinstance(self.parameters, Mapping):
            raise TypeError("prior claim parameters must be an object")
        object.__setattr__(self, "parameters", _freeze(self.parameters))

    def to_dict(self) -> dict[str, object]:
        return {
            "claim_kind": self.claim_kind,
            "parameters": _plain(self.parameters),
        }

    @classmethod
    def from_dict(cls, value: object) -> PriorKnowledgeClaim:
        if not isinstance(value, Mapping) or set(value) != {"claim_kind", "parameters"}:
            raise ValueError("prior claim contains unknown or missing fields")
        claim_kind = value["claim_kind"]
        if not isinstance(claim_kind, str):
            raise TypeError("prior claim kind must be a string")
        return cls(claim_kind, value["parameters"])


@dataclass(frozen=True, slots=True)
class PriorKnowledge:
    belief_kind: str
    claims: tuple[PriorKnowledgeClaim, ...]
    schema_version: int = field(default=1, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.belief_kind, str) or not self.belief_kind.strip():
            raise ValueError("prior belief kind must be non-empty")
        claims = tuple(self.claims)
        if not claims or not all(
            isinstance(claim, PriorKnowledgeClaim) for claim in claims
        ):
            raise ValueError("prior knowledge requires typed claims")
        object.__setattr__(self, "claims", claims)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "belief_kind": self.belief_kind,
            "claims": [claim.to_dict() for claim in self.claims],
        }

    @classmethod
    def from_dict(cls, value: object) -> PriorKnowledge:
        if not isinstance(value, Mapping) or set(value) != {
            "schema_version",
            "belief_kind",
            "claims",
        }:
            raise ValueError("prior knowledge contains unknown or missing fields")
        if value["schema_version"] != 1 or isinstance(value["schema_version"], bool):
            raise ValueError("prior knowledge schema version must be exactly 1")
        claims = value["claims"]
        if not isinstance(claims, (list, tuple)):
            raise TypeError("prior knowledge claims must be an array")
        belief_kind = value["belief_kind"]
        if not isinstance(belief_kind, str):
            raise TypeError("prior belief kind must be a string")
        return cls(
            belief_kind=belief_kind,
            claims=tuple(PriorKnowledgeClaim.from_dict(claim) for claim in claims),
        )


__all__ = ["PriorKnowledge", "PriorKnowledgeClaim"]
