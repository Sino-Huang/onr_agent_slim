from onr.application.minizinc import translate_minizinc
from onr.contracts.planning import (
    ManeuverIntent,
    MissionSpec,
    PlannerChoice,
    TemporalManeuver,
)


def test_temporal_translation_is_independent_of_maneuver_and_dependency_order() -> None:
    planner_choice = PlannerChoice(
        planning_profile="temporal",
        planner_id="minizinc",
    )
    survey = TemporalManeuver(
        maneuver_id="survey",
        intent=ManeuverIntent(action="survey"),
        dependencies=(),
        duration=3,
    )
    hold = TemporalManeuver(
        maneuver_id="hold-position",
        intent=ManeuverIntent(action="hold-position"),
        dependencies=("survey",),
        duration=1,
    )
    first = MissionSpec(
        mission_id="mission-1",
        objective="Survey the operating area and return",
        planner_choice=planner_choice,
        maneuvers=(
            TemporalManeuver(
                maneuver_id="return-to-base",
                intent=ManeuverIntent(action="return-to-base"),
                dependencies=("survey", "hold-position"),
                duration=2,
            ),
            survey,
            hold,
        ),
        horizon=10,
        source_authority="mission-control",
    )
    equivalent = MissionSpec(
        mission_id="mission-1",
        objective="Survey the operating area and return",
        planner_choice=planner_choice,
        maneuvers=(
            hold,
            TemporalManeuver(
                maneuver_id="return-to-base",
                intent=ManeuverIntent(action="return-to-base"),
                dependencies=("hold-position", "survey"),
                duration=2,
            ),
            survey,
        ),
        horizon=10,
        source_authority="mission-control",
    )

    first_assets = translate_minizinc(first)
    equivalent_assets = translate_minizinc(equivalent)

    assert first_assets == equivalent_assets
    assert first_assets == {
        "model.mzn": (
            b"int: maneuver_count;\n"
            b"set of int: MANEUVERS = 1..maneuver_count;\n"
            b"int: horizon;\n"
            b"array[MANEUVERS] of string: maneuver_ids;\n"
            b"array[MANEUVERS] of int: durations;\n"
            b"array[MANEUVERS] of set of MANEUVERS: dependencies;\n"
            b"array[MANEUVERS] of var 0..horizon: start;\n"
            b"constraint forall(i in MANEUVERS)(start[i] + durations[i] <= horizon);\n"
            b"constraint forall(i in MANEUVERS, dependency in dependencies[i])(\n"
            b"  start[i] >= start[dependency] + durations[dependency]\n"
            b");\n"
            b"var 0..horizon: makespan;\n"
            b"constraint makespan = max(i in MANEUVERS)(start[i] + durations[i]);\n"
            b"solve :: int_search(start, input_order, indomain_min, complete)\n"
            b"  minimize makespan;\n"
            b"output [\n"
            b'  "{\\"assignments\\":[",\n'
            b"  concat([\n"
            b'    "{\\"maneuver_id\\":\\"" ++ maneuver_ids[i] ++\n'
            b'    "\\",\\"start\\":" ++ show(start[i]) ++\n'
            b'    ",\\"duration\\":" ++ show(durations[i]) ++ "}" ++\n'
            b'    (if i < maneuver_count then "," else "" endif)\n'
            b"    | i in MANEUVERS\n"
            b"  ]),\n"
            b'  "]}"\n'
            b"];\n"
        ),
        "data.dzn": (
            b"maneuver_count = 3;\n"
            b"horizon = 10;\n"
            b'maneuver_ids = ["hold-position","return-to-base","survey"];\n'
            b"durations = [1,2,3];\n"
            b"dependencies = [{3},{1,3},{}];\n"
        ),
    }
