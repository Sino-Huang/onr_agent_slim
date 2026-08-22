import pytest

from onr.contracts.planning import (
    PlannerChoice,
    PlannerPlan,
    PlanningOutcome,
    PlanningProfile,
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


def test_planner_plan_is_a_maneuver_free_reference_envelope() -> None:
    planner_choice = PlannerChoice("symbolic", "fast-downward")
    plan = PlannerPlan(
        mission_id="mission-symbolic",
        source_authority="mission-control",
        plan_revision=1,
        mission_snapshot_id="snapshot-1",
        planner_choice=planner_choice,
        outcome=PlanningOutcome.SOLVED,
        planner_native_plan_artifact_reference="/artifacts/sas_plan",
    )
    assert PlannerPlan.from_json(plan.to_canonical_json()) == plan
    assert set(plan.to_dict()) == {
        "mission_id",
        "source_authority",
        "plan_revision",
        "mission_snapshot_id",
        "planner_choice",
        "outcome",
        "planner_native_plan_artifact_reference",
    }
    assert all(
        term not in plan.to_canonical_json()
        for term in ("maneuvers", "cost", "digest", "sha")
    )

    assert (
        PlannerChoice.from_json('{"planner_id":null,"planning_profile":"symbolic"}')
        == PlannerChoice.unsupported_symbolic()
    )
    with pytest.raises(ValueError):
        PlannerChoice.from_json(
            '{"planner_id":"fast-downward","planning_profile":"symbolic",'
            '"executable":"/usr/bin/fast-downward.py"}'
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
