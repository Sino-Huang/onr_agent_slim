import pytest

from onr.contracts.planning import (
    ManeuverIntent,
    ManeuverParameter,
    MissionSpec,
    NormalizedPlan,
    PlannerChoice,
    PlanningOutcome,
    PlanningProfile,
    SymbolicManeuver,
    SymbolicMissionSpec,
    SymbolicPlanStep,
    TemporalManeuver,
)


def test_temporal_planner_choice_and_mission_spec_are_semantic_and_canonical() -> None:
    planner_choice = PlannerChoice(
        planning_profile="temporal",
        planner_id="minizinc",
    )
    mission_spec = MissionSpec(
        mission_id="mission-1",
        objective="Survey the operating area",
        planner_choice=planner_choice,
        maneuvers=(
            TemporalManeuver(
                maneuver_id="survey",
                intent=ManeuverIntent(
                    action="survey",
                    parameters=(
                        ManeuverParameter(name="area", value="alpha"),
                        ManeuverParameter(name="altitude_m", value=120),
                    ),
                ),
                dependencies=(),
                duration=2,
            ),
            TemporalManeuver(
                maneuver_id="hold-position",
                intent=ManeuverIntent(action="hold-position"),
                dependencies=("survey",),
                duration=1,
            ),
        ),
        horizon=10,
        source_authority="mission-control",
    )

    assert tuple(item.maneuver_id for item in mission_spec.maneuvers) == (
        "hold-position",
        "survey",
    )
    with pytest.raises(AttributeError):
        setattr(mission_spec, "maneuvers", mission_spec.maneuvers[:1])

    assert mission_spec.to_canonical_json() == (
        '{"horizon":10,'
        '"maneuvers":['
        '{"dependencies":["survey"],"duration":1,"intent":'
        '{"action":"hold-position","parameters":{}},'
        '"maneuver_id":"hold-position"},'
        '{"dependencies":[],"duration":2,"intent":{"action":"survey",'
        '"parameters":{"altitude_m":120,"area":"alpha"}},'
        '"maneuver_id":"survey"}],'
        '"mission_id":"mission-1","objective":"Survey the operating area",'
        '"planner_choice":{"planner_id":"minizinc",'
        '"planning_profile":"temporal"},'
        '"source_authority":"mission-control"}'
    )

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
    mission = SymbolicMissionSpec(
        mission_id="mission-symbolic",
        objective="Survey and report",
        planner_choice=PlannerChoice("symbolic", "fast-downward"),
        maneuvers=(survey, report),
        source_authority="mission-control",
        domain_revision=2,
    )
    plan = NormalizedPlan(
        mission_spec=mission,
        plan_revision=1,
        mission_snapshot_id="snapshot-1",
        planner_choice=mission.planner_choice,
        outcome=PlanningOutcome.SOLVED,
        maneuvers=(
            SymbolicPlanStep(0, "survey", survey.intent, (), 4),
            SymbolicPlanStep(1, "report", report.intent, ("survey",), 1),
        ),
    )

    assert tuple(item.maneuver_id for item in mission.maneuvers) == (
        "report",
        "survey",
    )
    assert tuple(item.maneuver_id for item in plan.maneuvers) == ("survey", "report")
    assert mission.to_canonical_json() == (
        '{"domain_revision":2,"maneuvers":['
        '{"cost":1,"dependencies":["survey"],"intent":'
        '{"action":"report","parameters":{}},"maneuver_id":"report"},'
        '{"cost":4,"dependencies":[],"intent":'
        '{"action":"survey","parameters":{}},"maneuver_id":"survey"}],'
        '"mission_id":"mission-symbolic","objective":"Survey and report",'
        '"planner_choice":{"planner_id":"fast-downward",'
        '"planning_profile":"symbolic"},"source_authority":"mission-control"}'
    )
    assert '"duration"' not in plan.to_canonical_json()
    assert '"start"' not in plan.to_canonical_json()

    assert PlannerChoice.from_json(
        '{"planner_id":null,"planning_profile":"symbolic"}'
    ) == PlannerChoice.unsupported_symbolic()
    unsupported_spec = SymbolicMissionSpec(
        mission_id="mission-unsupported-symbolic",
        objective="Unsupported symbolic planner choice",
        planner_choice=PlannerChoice.unsupported_symbolic(),
        maneuvers=(survey,),
        source_authority="mission-control",
    )
    assert unsupported_spec.planner_choice.planner_id is None
    with pytest.raises(ValueError):
        PlannerChoice.from_json(
            '{"planner_id":"fast-downward","planning_profile":"symbolic",'
            '"executable":"/usr/bin/fast-downward.py"}'
        )
    with pytest.raises(ValueError):
        NormalizedPlan(
            mission_spec=mission,
            plan_revision=1,
            mission_snapshot_id="snapshot-1",
            planner_choice=mission.planner_choice,
            outcome=PlanningOutcome.SOLVED,
            maneuvers=(
                SymbolicPlanStep(1, "survey", survey.intent, (), 4),
                SymbolicPlanStep(2, "report", report.intent, ("survey",), 1),
            ),
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
    temporal_maneuver = TemporalManeuver(
        maneuver_id="survey",
        intent=ManeuverIntent("survey"),
        dependencies=(),
        duration=1,
    )
    with pytest.raises(ValueError, match="temporal"):
        MissionSpec(
            mission_id="misrouted-temporal",
            objective="Must not reach MiniZinc",
            planner_choice=PlannerChoice("symbolic", "fast-downward"),
            maneuvers=(temporal_maneuver,),
            horizon=2,
            source_authority="mission-control",
        )
    with pytest.raises(ValueError, match="symbolic"):
        SymbolicMissionSpec(
            mission_id="misrouted-symbolic",
            objective="Must not reach Fast Downward",
            planner_choice=PlannerChoice("temporal", "minizinc"),
            maneuvers=(
                SymbolicManeuver(
                    maneuver_id="survey",
                    intent=ManeuverIntent("survey"),
                    dependencies=(),
                    cost=1,
                ),
            ),
            source_authority="mission-control",
        )


@pytest.mark.parametrize(
    ("first_id", "second_id"),
    (("Survey", "survey"), ("a", "a-")),
)
def test_symbolic_mission_rejects_pddl_normalized_maneuver_id_collisions(
    first_id: str, second_id: str
) -> None:
    with pytest.raises(ValueError, match="PDDL normalization"):
        SymbolicMissionSpec(
            mission_id="case-collision",
            objective="Reject ambiguous PDDL action names",
            planner_choice=PlannerChoice("symbolic", "fast-downward"),
            maneuvers=(
                SymbolicManeuver(first_id, ManeuverIntent("survey-a"), (), 1),
                SymbolicManeuver(second_id, ManeuverIntent("survey-b"), (), 1),
            ),
            source_authority="mission-control",
        )
