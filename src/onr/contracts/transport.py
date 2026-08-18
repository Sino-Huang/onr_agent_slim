"""Immutable Transport Events for public planning artifacts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from onr.contracts.planning import NormalizedPlan, PlannerChoice, PlanningOutcome


@dataclass(frozen=True, slots=True)
class NormalizedPlanTransportPayload:
    """Immutable canonical Normalized Plan document and provenance."""

    mission_id: str
    plan_revision: int
    mission_snapshot_id: str
    planner_choice: PlannerChoice
    source_authority: str
    outcome: PlanningOutcome
    normalized_plan: NormalizedPlan
    normalized_plan_document: str
    normalized_plan_sha256: str


@dataclass(frozen=True, slots=True)
class NormalizedPlanTransportEvent:
    """Revisioned Transport Event for one Normalized Plan outcome."""

    event_id: str
    sequence: int
    payload: NormalizedPlanTransportPayload
    event_kind: str = field(default="normalized-plan", init=False)
    contract_revision: int = field(default=1, init=False)

    @property
    def mission_id(self) -> str:
        return self.payload.mission_id

    @property
    def plan_revision(self) -> int:
        return self.payload.plan_revision

    @property
    def outcome(self) -> PlanningOutcome:
        return self.payload.outcome

    @property
    def normalized_plan(self) -> NormalizedPlan:
        return self.payload.normalized_plan

    @property
    def normalized_plan_sha256(self) -> str:
        return self.payload.normalized_plan_sha256


def create_normalized_plan_transport_event(
    normalized_plan: NormalizedPlan,
    *,
    event_id: str,
    sequence: int,
) -> NormalizedPlanTransportEvent:
    """Create a stable Transport Event for an exact Normalized Plan document."""

    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event ID must be a non-empty string")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("event sequence must be a non-negative integer")

    document = normalized_plan.to_canonical_json()
    payload = NormalizedPlanTransportPayload(
        mission_id=normalized_plan.mission_spec.mission_id,
        plan_revision=normalized_plan.plan_revision,
        mission_snapshot_id=normalized_plan.mission_snapshot_id,
        planner_choice=normalized_plan.planner_choice,
        source_authority=normalized_plan.mission_spec.source_authority,
        outcome=PlanningOutcome(normalized_plan.outcome),
        normalized_plan=normalized_plan,
        normalized_plan_document=document,
        normalized_plan_sha256=hashlib.sha256(document.encode("utf-8")).hexdigest(),
    )
    return NormalizedPlanTransportEvent(
        event_id=event_id,
        sequence=sequence,
        payload=payload,
    )
