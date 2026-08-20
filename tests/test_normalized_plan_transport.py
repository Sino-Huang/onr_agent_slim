from collections.abc import Mapping

from onr.contracts.planning import (
    ManeuverIntent,
    NormalizedPlan,
    PlannerChoice,
    PlanningOutcome,
    PlanProvenance,
    ScheduledManeuver,
    SymbolicPlanStep,
    VerifiableReference,
)
from onr.contracts.transport import (
    create_normalized_plan_transport_event,
    normalized_plan_transport_event_to_wire,
)


def test_provenance_only_normalized_plan_round_trips_with_verifiable_references() -> None:
    planner_choice = PlannerChoice("temporal", "minizinc")
    provenance = PlanProvenance(
        mission_id="mission-provenance-1",
        source_authority="mission-control",
        mission_intent=VerifiableReference("mission-input:1", "a" * 64),
        planning_decision=VerifiableReference("planner-choice:1", "b" * 64),
        environment_data=VerifiableReference("scene:1", "c" * 64),
        generated_assets={
            "model.mzn": VerifiableReference("artifacts/model.mzn", "d" * 64),
            "data.dzn": VerifiableReference("artifacts/data.dzn", "e" * 64),
        },
        solver_evidence={
            "stdout": VerifiableReference("artifacts/solver.stdout", "f" * 64),
        },
    )
    plan = NormalizedPlan(
        plan_revision=2,
        mission_snapshot_id="mission-provenance-1:snapshot:4",
        planner_choice=planner_choice,
        outcome=PlanningOutcome.SOLVED,
        maneuvers=(
            ScheduledManeuver(
                "survey",
                ManeuverIntent("survey"),
                (),
                0,
                3,
            ),
        ),
        provenance=provenance,
    )

    round_trip = NormalizedPlan.from_json(plan.to_canonical_json())
    event = create_normalized_plan_transport_event(
        plan,
        event_id="normalized-plan:provenance:2",
        sequence=4,
    )
    wire = normalized_plan_transport_event_to_wire(event)

    assert round_trip == plan
    assert round_trip.provenance == provenance
    assert event.contract_revision == 2
    assert wire.schema_version == 2
    assert event.mission_id == provenance.mission_id
    assert event.payload.source_authority == provenance.source_authority
    wire_plan = wire.payload["normalized_plan"]
    assert isinstance(wire_plan, Mapping)
    assert NormalizedPlan.from_dict(wire_plan) == plan
    assert "mission_spec" not in plan.to_dict()
    assert plan.to_dict()["provenance"] == {
        "mission_id": "mission-provenance-1",
        "source_authority": "mission-control",
        "mission_intent": {
            "reference": "mission-input:1",
            "sha256": "a" * 64,
        },
        "planning_decision": {
            "reference": "planner-choice:1",
            "sha256": "b" * 64,
        },
        "environment_data": {
            "reference": "scene:1",
            "sha256": "c" * 64,
        },
        "generated_assets": {
            "data.dzn": {
                "reference": "artifacts/data.dzn",
                "sha256": "e" * 64,
            },
            "model.mzn": {
                "reference": "artifacts/model.mzn",
                "sha256": "d" * 64,
            },
        },
        "solver_evidence": {
            "stdout": {
                "reference": "artifacts/solver.stdout",
                "sha256": "f" * 64,
            },
        },
    }



def test_symbolic_provenance_only_plan_round_trips_without_mission_spec() -> None:
    provenance = PlanProvenance(
        mission_id="mission-symbolic-provenance",
        source_authority="mission-control",
        mission_intent=VerifiableReference("mission-input:symbolic", "1" * 64),
        planning_decision=VerifiableReference("planner-choice:symbolic", "2" * 64),
        environment_data=VerifiableReference("scene:symbolic", "3" * 64),
        generated_assets={
            "domain.pddl": VerifiableReference("artifacts/domain.pddl", "4" * 64),
            "problem.pddl": VerifiableReference("artifacts/problem.pddl", "5" * 64),
        },
        solver_evidence={
            "plan": VerifiableReference("artifacts/sas_plan", "6" * 64),
        },
    )
    plan = NormalizedPlan(
        plan_revision=1,
        mission_snapshot_id="mission-symbolic-provenance:snapshot:1",
        planner_choice=PlannerChoice("symbolic", "fast-downward"),
        outcome=PlanningOutcome.SOLVED,
        maneuvers=(
            SymbolicPlanStep(
                0,
                "survey",
                ManeuverIntent("survey"),
                (),
                5,
            ),
        ),
        provenance=provenance,
    )

    assert NormalizedPlan.from_json(plan.to_canonical_json()) == plan
    assert plan.symbolic_steps[0].maneuver_id == "survey"
