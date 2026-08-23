"""Immutable public evidence for planner selection and translation attempts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from onr.contracts.planning import PlannerChoice, PlannerPlan
from onr.contracts.planning_intent import PlanningIntent
from onr.contracts.transport import TransportEvent


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _string_mapping(value: object, label: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    items: dict[str, str] = {}
    for key, item in value.items():
        _text(key, f"{label} key")
        items[key] = _text(item, f"{label} value")
    return MappingProxyType(items)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _decode(value: str, label: str) -> Mapping[str, Any]:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be valid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return decoded


@dataclass(frozen=True, slots=True)
class PlannerChoiceRecord:
    """Public planner decision bound to raw Mission Input provenance."""

    decision_id: str
    mission_id: str
    planner_choice: PlannerChoice
    rationale: str
    schema_version: int = field(default=1, init=False)

    def __post_init__(self) -> None:
        _text(self.decision_id, "planner choice decision ID")
        _text(self.mission_id, "planner choice Mission ID")
        if not isinstance(self.planner_choice, PlannerChoice):
            raise ValueError("planner choice record requires a PlannerChoice")
        _text(self.rationale, "planner choice rationale")

    @classmethod
    def from_planning_intent(cls, intent: PlanningIntent) -> "PlannerChoiceRecord":
        if not isinstance(intent, PlanningIntent):
            raise TypeError("planner choice record requires a PlanningIntent")
        return cls(
            decision_id=f"planner-choice:{intent.mission_id}",
            mission_id=intent.mission_id,
            planner_choice=intent.planner_choice,
            rationale=intent.rationale,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "mission_id": self.mission_id,
            "planner_choice": self.planner_choice.to_dict(),
            "rationale": self.rationale,
        }

    def to_canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlannerChoiceRecord":
        expected = {
            "schema_version",
            "decision_id",
            "mission_id",
            "planner_choice",
            "rationale",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("planner choice record contains unknown or missing fields")
        if value["schema_version"] != 1:
            raise ValueError("planner choice record schema version must be exactly 1")
        return cls(
            decision_id=value["decision_id"],
            mission_id=value["mission_id"],
            planner_choice=PlannerChoice.from_dict(value["planner_choice"]),
            rationale=value["rationale"],
        )

    @classmethod
    def from_json(cls, value: str) -> "PlannerChoiceRecord":
        return cls.from_dict(_decode(value, "planner choice record"))


class TranslationAttemptOutcome(StrEnum):
    """Public terminal classification for one generated asset set."""

    REJECTED = "rejected"
    ACCEPTED = "accepted"


@dataclass(frozen=True, slots=True)
class PlannerGenerationAttempt:
    """Immutable public evidence for one planner-native generation attempt."""

    attempt_id: str
    decision_id: str
    mission_id: str
    planner_choice: PlannerChoice
    rationale: str
    mission_snapshot_id: str
    translator_id: str
    translator_version: str
    outcome: TranslationAttemptOutcome | str
    asset_references: Mapping[str, str] = field(default_factory=dict)
    schema_version: int = field(default=1, init=False)

    def __post_init__(self) -> None:
        _text(self.attempt_id, "generation attempt ID")
        _text(self.decision_id, "generation attempt decision ID")
        _text(self.mission_id, "generation attempt Mission ID")
        if not isinstance(self.planner_choice, PlannerChoice):
            raise ValueError("generation attempt requires a PlannerChoice")
        _text(self.rationale, "generation attempt rationale")
        _text(self.mission_snapshot_id, "generation attempt Mission Snapshot ID")
        _text(self.translator_id, "translator ID")
        _text(self.translator_version, "translator version")
        try:
            outcome = TranslationAttemptOutcome(self.outcome)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "generation attempt outcome must be accepted or rejected"
            ) from exc
        references = _string_mapping(self.asset_references, "asset references")
        if outcome is TranslationAttemptOutcome.ACCEPTED and not references:
            raise ValueError("accepted generation attempt requires asset references")
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "asset_references", references)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "decision_id": self.decision_id,
            "mission_id": self.mission_id,
            "planner_choice": self.planner_choice.to_dict(),
            "rationale": self.rationale,
            "mission_snapshot_id": self.mission_snapshot_id,
            "translator_id": self.translator_id,
            "translator_version": self.translator_version,
            "outcome": str(self.outcome),
            "asset_references": dict(self.asset_references),
        }

    def to_canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlannerGenerationAttempt":
        expected = {
            "schema_version",
            "attempt_id",
            "decision_id",
            "mission_id",
            "planner_choice",
            "rationale",
            "mission_snapshot_id",
            "translator_id",
            "translator_version",
            "outcome",
            "asset_references",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("generation attempt contains unknown or missing fields")
        if value["schema_version"] != 1:
            raise ValueError("generation attempt schema version must be exactly 1")
        return cls(
            attempt_id=value["attempt_id"],
            decision_id=value["decision_id"],
            mission_id=value["mission_id"],
            planner_choice=PlannerChoice.from_dict(value["planner_choice"]),
            rationale=value["rationale"],
            mission_snapshot_id=value["mission_snapshot_id"],
            translator_id=value["translator_id"],
            translator_version=value["translator_version"],
            outcome=value["outcome"],
            asset_references=value["asset_references"],
        )

    @classmethod
    def from_json(cls, value: str) -> "PlannerGenerationAttempt":
        return cls.from_dict(_decode(value, "generation attempt"))


@dataclass(frozen=True, slots=True)
class PlannerRevisionEvidence:
    """Accepted planner-native plan and Statechart references for one revision."""

    planner_plan: PlannerPlan
    planner_plan_reference: str
    accepted_statechart_reference: str
    mission_snapshot_id: str
    plan_revision: int
    schema_version: int = field(default=1, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.planner_plan, PlannerPlan):
            raise TypeError("planner revision evidence requires a PlannerPlan")
        _text(self.planner_plan_reference, "PlannerPlan reference")
        _text(self.accepted_statechart_reference, "accepted Statechart reference")
        _text(self.mission_snapshot_id, "planner revision Mission Snapshot ID")
        if (
            isinstance(self.plan_revision, bool)
            or not isinstance(self.plan_revision, int)
            or self.plan_revision < 1
        ):
            raise ValueError("planner revision must be a positive integer")
        if (
            self.planner_plan.plan_revision != self.plan_revision
            or self.planner_plan.mission_snapshot_id != self.mission_snapshot_id
        ):
            raise ValueError("planner revision evidence identity is inconsistent")

    @property
    def mission_id(self) -> str:
        return self.planner_plan.mission_id

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "planner_plan": self.planner_plan.to_dict(),
            "planner_plan_reference": self.planner_plan_reference,
            "accepted_statechart_reference": self.accepted_statechart_reference,
            "mission_snapshot_id": self.mission_snapshot_id,
            "plan_revision": self.plan_revision,
        }

    def to_canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PlannerRevisionEvidence:
        expected = {
            "schema_version",
            "planner_plan",
            "planner_plan_reference",
            "accepted_statechart_reference",
            "mission_snapshot_id",
            "plan_revision",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError(
                "planner revision evidence contains unknown or missing fields"
            )
        if value["schema_version"] != 1:
            raise ValueError(
                "planner revision evidence schema version must be exactly 1"
            )
        return cls(
            planner_plan=PlannerPlan.from_dict(value["planner_plan"]),
            planner_plan_reference=value["planner_plan_reference"],
            accepted_statechart_reference=value["accepted_statechart_reference"],
            mission_snapshot_id=value["mission_snapshot_id"],
            plan_revision=value["plan_revision"],
        )


def planner_revision_to_transport_event(
    evidence: PlannerRevisionEvidence,
    *,
    event_id: str,
    sequence: int,
) -> TransportEvent:
    if not isinstance(evidence, PlannerRevisionEvidence):
        raise TypeError("planner revision event requires PlannerRevisionEvidence")
    return TransportEvent(
        schema_version=1,
        event_id=event_id,
        mission_id=evidence.mission_id,
        sequence=sequence,
        event_kind="planner-revision",
        payload=evidence.to_dict(),
    )


__all__ = [
    "PlannerChoiceRecord",
    "PlannerGenerationAttempt",
    "PlannerRevisionEvidence",
    "TranslationAttemptOutcome",
    "planner_revision_to_transport_event",
]
