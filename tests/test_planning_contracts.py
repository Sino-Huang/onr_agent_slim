import pytest

from onr.contracts.planning import (
    ManeuverIntent,
    ManeuverParameter,
    MissionSpec,
    PlannerChoice,
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
