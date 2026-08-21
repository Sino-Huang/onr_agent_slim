from collections.abc import Mapping

from onr.contracts.planning import (
    ManeuverIntent,
    NormalizedPlan,
    PlannerChoice,
    PlanningOutcome,
    ScheduledManeuver,
    SymbolicPlanStep,
)
from onr.contracts.transport import (
    create_normalized_plan_transport_event,
    normalized_plan_transport_event_to_wire,
)


def test_normalized_plan_round_trips_without_digest_or_duplicate_document() -> None:
    plan = NormalizedPlan(
        mission_id="mission-plan-1",
        source_authority="mission-control",
        plan_revision=2,
        mission_snapshot_id="mission-plan-1:snapshot:4",
        planner_choice=PlannerChoice("temporal", "minizinc"),
        outcome=PlanningOutcome.SOLVED,
        maneuvers=(
            ScheduledManeuver("survey", ManeuverIntent("survey"), (), 0, 3),
        ),
    )

    round_trip = NormalizedPlan.from_json(plan.to_canonical_json())
    event = create_normalized_plan_transport_event(
        plan, event_id="normalized-plan:plan:2", sequence=4
    )
    wire = normalized_plan_transport_event_to_wire(event)

    assert round_trip == plan
    assert event.contract_revision == 2
    assert wire.schema_version == 2
    assert event.mission_id == plan.mission_id
    assert event.payload.source_authority == plan.source_authority
    wire_plan = wire.payload["normalized_plan"]
    assert isinstance(wire_plan, Mapping)
    assert NormalizedPlan.from_dict(wire_plan) == plan
    assert "provenance" not in plan.to_dict()
    assert "normalized_plan_document" not in wire.payload
    assert "normalized_plan_sha256" not in wire.payload


def test_symbolic_plan_round_trips_with_direct_mission_authority() -> None:
    plan = NormalizedPlan(
        mission_id="mission-symbolic",
        source_authority="mission-control",
        plan_revision=1,
        mission_snapshot_id="mission-symbolic:snapshot:1",
        planner_choice=PlannerChoice("symbolic", "fast-downward"),
        outcome=PlanningOutcome.SOLVED,
        maneuvers=(
            SymbolicPlanStep(0, "survey", ManeuverIntent("survey"), (), 5),
        ),
    )

    assert NormalizedPlan.from_json(plan.to_canonical_json()) == plan
    assert plan.symbolic_steps[0].maneuver_id == "survey"
    assert plan.mission_id == "mission-symbolic"
    assert plan.source_authority == "mission-control"
