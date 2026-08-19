from collections.abc import Mapping
import hashlib

from onr.application.temporal_planning import TemporalPlanning
from onr.contracts.planning import (
    ManeuverIntent,
    ManeuverParameter,
    MissionSpec,
    NormalizedPlan,
    PlanProvenance,
    VerifiableReference,
    PlannerChoice,
    PlannerExecutionResult,
    PlanningOutcome,
    ScheduledManeuver,
    TemporalAssignment,
    SymbolicPlanStep,
    TemporalManeuver,
)
from onr.contracts.transport import (
    create_normalized_plan_transport_event,
    normalized_plan_transport_event_to_wire,
)


_CANONICAL_NORMALIZED_PLAN = (
    '{"maneuvers":['
    '{"dependencies":[],"duration":4,"intent":{"action":"survey",'
    '"parameters":{"altitude_m":120}},'
    '"maneuver_id":"survey","start":0}],'
    '"mission_snapshot_id":"snapshot-9",'
    '"mission_spec":{"horizon":10,"maneuvers":['
    '{"dependencies":[],"duration":4,"intent":{"action":"survey",'
    '"parameters":{"altitude_m":120}},"maneuver_id":"survey"}],'
    '"mission_id":"mission-transport-1","objective":"Survey alpha",'
    '"planner_choice":{"planner_id":"minizinc",'
    '"planning_profile":"temporal"},'
    '"source_authority":"mission-control"},'
    '"outcome":"solved","plan_revision":3,'
    '"planner_choice":{"planner_id":"minizinc",'
    '"planning_profile":"temporal"}}'
)
_CANONICAL_NORMALIZED_PLAN_SHA256 = hashlib.sha256(
    _CANONICAL_NORMALIZED_PLAN.encode("utf-8")
).hexdigest()


class SolvedExecutor:
    def execute(self, assets: object) -> PlannerExecutionResult:
        return PlannerExecutionResult(
            outcome=PlanningOutcome.SOLVED,
            assignments=(
                TemporalAssignment(maneuver_id="survey", start=0, duration=4),
            ),
        )


def test_normalized_plan_transport_event_preserves_revision_and_identity() -> None:
    planner_choice = PlannerChoice(
        planning_profile="temporal",
        planner_id="minizinc",
    )
    mission_spec = MissionSpec(
        mission_id="mission-transport-1",
        objective="Survey alpha",
        planner_choice=planner_choice,
        maneuvers=(
            TemporalManeuver(
                maneuver_id="survey",
                intent=ManeuverIntent(
                    action="survey",
                    parameters=(
                        ManeuverParameter(name="altitude_m", value=120),
                    ),
                ),
                dependencies=(),
                duration=4,
            ),
        ),
        horizon=10,
        source_authority="mission-control",
    )
    normalized_plan = TemporalPlanning(executor=SolvedExecutor()).plan(
        mission_spec=mission_spec,
        plan_revision=3,
        mission_snapshot_id="snapshot-9",
    ).normalized_plan
    assert normalized_plan is not None
    assert normalized_plan.to_canonical_json() == _CANONICAL_NORMALIZED_PLAN
    assert NormalizedPlan.from_json(_CANONICAL_NORMALIZED_PLAN) == normalized_plan

    event = create_normalized_plan_transport_event(
        normalized_plan,
        event_id="event-normalized-plan-3",
        sequence=19,
    )

    assert event.event_id == "event-normalized-plan-3"
    assert event.event_kind == "normalized-plan"
    assert event.contract_revision == 1
    assert event.sequence == 19
    assert event.mission_id == "mission-transport-1"
    assert event.plan_revision == 3
    assert event.outcome is PlanningOutcome.SOLVED
    assert event.payload.mission_snapshot_id == "snapshot-9"
    assert event.payload.planner_choice == planner_choice
    assert event.payload.source_authority == "mission-control"
    assert event.normalized_plan == normalized_plan
    assert event.payload.normalized_plan_document == _CANONICAL_NORMALIZED_PLAN
    assert event.normalized_plan_sha256 == _CANONICAL_NORMALIZED_PLAN_SHA256


def test_provenance_only_normalized_plan_round_trips_with_verifiable_references() -> None:
    planner_choice = PlannerChoice("temporal", "minizinc")
    provenance = PlanProvenance(
        mission_id="mission-provenance-1",
        source_authority="mission-control",
        mission_intent=VerifiableReference("mission-input:1", "a" * 64),
        planning_decision=VerifiableReference("planner-choice:1", "b" * 64),
        operational_scene_graph=VerifiableReference("scene:1", "c" * 64),
        generated_assets={
            "model.mzn": VerifiableReference("artifacts/model.mzn", "d" * 64),
            "data.dzn": VerifiableReference("artifacts/data.dzn", "e" * 64),
        },
        solver_evidence={
            "stdout": VerifiableReference("artifacts/solver.stdout", "f" * 64),
        },
    )
    plan = NormalizedPlan(
        mission_spec=None,
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
    assert round_trip.mission_spec is None
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
        "operational_scene_graph": {
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
        operational_scene_graph=VerifiableReference("scene:symbolic", "3" * 64),
        generated_assets={
            "domain.pddl": VerifiableReference("artifacts/domain.pddl", "4" * 64),
            "problem.pddl": VerifiableReference("artifacts/problem.pddl", "5" * 64),
        },
        solver_evidence={
            "plan": VerifiableReference("artifacts/sas_plan", "6" * 64),
        },
    )
    plan = NormalizedPlan(
        mission_spec=None,
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
