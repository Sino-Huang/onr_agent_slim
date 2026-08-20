import pytest

from onr.contracts.planning import (
    ManeuverIntent,
    NormalizedPlan,
    PlannerChoice,
    PlanningOutcome,
    PlanningProfile,
    PlanProvenance,
    SymbolicManeuver,
    SymbolicPlanStep,
    VerifiableReference,
)


def test_planner_choice_is_semantic_and_rejects_executable_paths() -> None:
    malformed = (
        '{"planner_id":"minizinc","planning_profile":"temporal",'
        '"path":"/usr/local/bin/minizinc"}'
    )
    with pytest.raises(ValueError):
        PlannerChoice.from_json(malformed)
    with pytest.raises(ValueError):
        PlannerChoice(
            planning_profile="temporal",
            planner_id="/usr/local/bin/minizinc",
        )


def test_symbolic_contracts_are_canonical_and_keep_steps_ordered() -> None:
    survey = SymbolicManeuver(
        maneuver_id="survey",
        intent=ManeuverIntent("survey"),
        dependencies=(),
        cost=4,
    )
    report = SymbolicManeuver(
        maneuver_id="report",
        intent=ManeuverIntent("report"),
        dependencies=("survey",),
        cost=1,
    )
    planner_choice = PlannerChoice("symbolic", "fast-downward")
    provenance = PlanProvenance(
        mission_id="mission-symbolic",
        source_authority="mission-control",
        mission_intent=VerifiableReference("mission-input:symbolic", "1" * 64),
        planning_decision=VerifiableReference("planner-choice:symbolic", "2" * 64),
        operational_scene_graph=VerifiableReference("scene:symbolic", "3" * 64),
        generated_assets={
            "domain.pddl": VerifiableReference("domain.pddl", "4" * 64),
            "problem.pddl": VerifiableReference("problem.pddl", "5" * 64),
        },
        solver_evidence={
            "plan": VerifiableReference("sas_plan", "6" * 64),
        },
    )
    plan = NormalizedPlan(
        plan_revision=1,
        mission_snapshot_id="snapshot-1",
        planner_choice=planner_choice,
        outcome=PlanningOutcome.SOLVED,
        maneuvers=(
            SymbolicPlanStep(0, "survey", survey.intent, (), 4),
            SymbolicPlanStep(1, "report", report.intent, ("survey",), 1),
        ),
        provenance=provenance,
    )

    assert '"duration"' not in plan.to_canonical_json()
    assert '"start"' not in plan.to_canonical_json()

    assert (
        PlannerChoice.from_json('{"planner_id":null,"planning_profile":"symbolic"}')
        == PlannerChoice.unsupported_symbolic()
    )
    with pytest.raises(ValueError):
        PlannerChoice.from_json(
            '{"planner_id":"fast-downward","planning_profile":"symbolic",'
            '"executable":"/usr/bin/fast-downward.py"}'
        )
    with pytest.raises(ValueError):
        NormalizedPlan(
            plan_revision=1,
            mission_snapshot_id="snapshot-1",
            planner_choice=planner_choice,
            outcome=PlanningOutcome.SOLVED,
            maneuvers=(
                SymbolicPlanStep(1, "survey", survey.intent, (), 4),
                SymbolicPlanStep(2, "report", report.intent, ("survey",), 1),
            ),
            provenance=provenance,
        )


def test_planner_choice_routes_only_to_matching_mission_contract() -> None:
    assert tuple(PlanningProfile) == (
        PlanningProfile.TEMPORAL,
        PlanningProfile.SYMBOLIC,
    )
    unsupported_choice = PlannerChoice.unsupported_symbolic()
    assert unsupported_choice.to_dict() == {
        "planner_id": None,
        "planning_profile": "symbolic",
    }
    with pytest.raises(ValueError, match="unsupported planning profile"):
        PlannerChoice("hybrid", None)
