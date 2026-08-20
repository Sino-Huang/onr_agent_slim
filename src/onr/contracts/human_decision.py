"""Durable contracts for operator decisions that pause Mission Runs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class HumanDecisionCategory(StrEnum):
    TRANSLATION_REPAIR_EXHAUSTED = "translation_repair_exhausted"
    UNSOLVABLE = "unsolvable"
    INSUFFICIENT_ENVIRONMENT_DATA = "insufficient_environment_data"
    TIMEOUT = "timeout"


class HumanDecisionAction(StrEnum):
    RETRY_TRANSLATION = "retry_translation"
    REVISE_MISSION_INTENT = "revise_mission_intent"
    WAIT_FOR_ENVIRONMENT_DATA = "wait_for_environment_data"
    RETRY_PLANNER = "retry_planner"
    END_MISSION_RUN = "end_mission_run"


class HumanDecisionDisposition(StrEnum):
    RESUME = "resume"
    END = "end"


@dataclass(frozen=True, slots=True)
class RunCheckpoint:
    checkpoint_id: str
    mission_id: str
    mission_run_id: str
    continuation: str
    schema_version: int = field(default=1, init=False)

    def __post_init__(self) -> None:
        for value, label in (
            (self.checkpoint_id, "checkpoint ID"),
            (self.mission_id, "mission ID"),
            (self.mission_run_id, "Mission Run ID"),
            (self.continuation, "checkpoint continuation"),
        ):
            _text(value, label)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "checkpoint_id": self.checkpoint_id,
            "mission_id": self.mission_id,
            "mission_run_id": self.mission_run_id,
            "continuation": self.continuation,
        }

    def to_canonical_json(self) -> str:
        return _canonical(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunCheckpoint":
        if (
            set(value)
            != {
                "schema_version",
                "checkpoint_id",
                "mission_id",
                "mission_run_id",
                "continuation",
            }
            or value["schema_version"] != 1
        ):
            raise ValueError("Run Checkpoint contains unknown or missing fields")
        return cls(
            value["checkpoint_id"],
            value["mission_id"],
            value["mission_run_id"],
            value["continuation"],
        )

    @classmethod
    def from_json(cls, value: str) -> "RunCheckpoint":
        decoded = json.loads(value)
        if not isinstance(decoded, Mapping):
            raise ValueError("Run Checkpoint JSON must be an object")
        return cls.from_dict(decoded)


@dataclass(frozen=True, slots=True)
class HumanDecisionRequest:
    request_id: str
    mission_id: str
    mission_run_id: str
    category: HumanDecisionCategory | str
    correlation_id: str
    checkpoint_id: str
    evidence_references: tuple[str, ...]
    permitted_actions: tuple[HumanDecisionAction | str, ...]
    schema_version: int = field(default=1, init=False)

    def __post_init__(self) -> None:
        for value, label in (
            (self.request_id, "Human Decision Request ID"),
            (self.mission_id, "mission ID"),
            (self.mission_run_id, "Mission Run ID"),
            (self.correlation_id, "correlation ID"),
            (self.checkpoint_id, "checkpoint ID"),
        ):
            _text(value, label)
        references = tuple(self.evidence_references)
        if not references or not all(
            isinstance(item, str) and item.strip() for item in references
        ):
            raise ValueError("evidence references must be non-empty strings")
        actions = tuple(HumanDecisionAction(item) for item in self.permitted_actions)
        if not actions or len(set(actions)) != len(actions):
            raise ValueError("permitted Human Decision actions must be unique")
        object.__setattr__(self, "category", HumanDecisionCategory(self.category))
        object.__setattr__(self, "evidence_references", references)
        object.__setattr__(self, "permitted_actions", actions)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "mission_id": self.mission_id,
            "mission_run_id": self.mission_run_id,
            "category": str(self.category),
            "correlation_id": self.correlation_id,
            "checkpoint_id": self.checkpoint_id,
            "evidence_references": list(self.evidence_references),
            "permitted_actions": [str(item) for item in self.permitted_actions],
        }

    def to_canonical_json(self) -> str:
        return _canonical(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HumanDecisionRequest":
        expected = {
            "schema_version",
            "request_id",
            "mission_id",
            "mission_run_id",
            "category",
            "correlation_id",
            "checkpoint_id",
            "evidence_references",
            "permitted_actions",
        }
        if set(value) != expected or value["schema_version"] != 1:
            raise ValueError(
                "Human Decision Request contains unknown or missing fields"
            )
        return cls(
            request_id=value["request_id"],
            mission_id=value["mission_id"],
            mission_run_id=value["mission_run_id"],
            category=value["category"],
            correlation_id=value["correlation_id"],
            checkpoint_id=value["checkpoint_id"],
            evidence_references=tuple(value["evidence_references"]),
            permitted_actions=tuple(value["permitted_actions"]),
        )

    @classmethod
    def from_json(cls, value: str) -> "HumanDecisionRequest":
        decoded = json.loads(value)
        if not isinstance(decoded, Mapping):
            raise ValueError("Human Decision Request JSON must be an object")
        return cls.from_dict(decoded)


@dataclass(frozen=True, slots=True)
class HumanDecision:
    decision_id: str
    request_id: str
    mission_id: str
    mission_run_id: str
    action: HumanDecisionAction | str
    schema_version: int = field(default=1, init=False)

    def __post_init__(self) -> None:
        for value, label in (
            (self.decision_id, "Human Decision ID"),
            (self.request_id, "Human Decision Request ID"),
            (self.mission_id, "mission ID"),
            (self.mission_run_id, "Mission Run ID"),
        ):
            _text(value, label)
        object.__setattr__(self, "action", HumanDecisionAction(self.action))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "request_id": self.request_id,
            "mission_id": self.mission_id,
            "mission_run_id": self.mission_run_id,
            "action": str(self.action),
        }

    def to_canonical_json(self) -> str:
        return _canonical(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HumanDecision":
        expected = {
            "schema_version",
            "decision_id",
            "request_id",
            "mission_id",
            "mission_run_id",
            "action",
        }
        if set(value) != expected or value["schema_version"] != 1:
            raise ValueError("Human Decision contains unknown or missing fields")
        return cls(
            value["decision_id"],
            value["request_id"],
            value["mission_id"],
            value["mission_run_id"],
            value["action"],
        )

    @classmethod
    def from_json(cls, value: str) -> "HumanDecision":
        decoded = json.loads(value)
        if not isinstance(decoded, Mapping):
            raise ValueError("Human Decision JSON must be an object")
        return cls.from_dict(decoded)


@dataclass(frozen=True, slots=True)
class HumanDecisionResolution:
    decision: HumanDecision
    disposition: HumanDecisionDisposition | str
    checkpoint: RunCheckpoint | None = None

    def __post_init__(self) -> None:
        disposition = HumanDecisionDisposition(self.disposition)
        if disposition is HumanDecisionDisposition.RESUME and self.checkpoint is None:
            raise ValueError("resume resolution requires a Run Checkpoint")
        if disposition is HumanDecisionDisposition.END and self.checkpoint is not None:
            raise ValueError("end resolution cannot contain a Run Checkpoint")
        object.__setattr__(self, "disposition", disposition)


__all__ = [
    "HumanDecision",
    "HumanDecisionAction",
    "HumanDecisionCategory",
    "HumanDecisionDisposition",
    "HumanDecisionRequest",
    "HumanDecisionResolution",
    "RunCheckpoint",
]
