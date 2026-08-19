import hashlib
from pathlib import Path

from onr.adapters.minizinc import MiniZincExecutor
from onr.application.minizinc import translate_minizinc
from onr.application.temporal_planning import TemporalPlanning
from onr.contracts.planning import (
    ManeuverIntent,
    ManeuverParameter,
    MissionSpec,
    PlannerChoice,
    PlanningOutcome,
    ScheduledManeuver,
    TemporalManeuver,
)


def test_real_temporal_planning_is_canonical_and_preserves_source_authority(tmp_path) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    minizinc = (
        repository_root
        / "modules"
        / "MiniZincIDE-2.9.7-bundle-linux-x86_64"
        / "bin"
        / "minizinc"
    )
    assert minizinc.is_file(), f"bundled MiniZinc executable not found: {minizinc}"

    planner_choice = PlannerChoice(
        planning_profile="temporal",
        planner_id="minizinc",
    )
    survey = TemporalManeuver(
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
    )
    inspect = TemporalManeuver(
        maneuver_id="inspect",
        intent=ManeuverIntent(
            action="inspect",
            parameters=(ManeuverParameter(name="sensor", value="eo"),),
        ),
        dependencies=("survey",),
        duration=3,
    )
    return_to_base = TemporalManeuver(
        maneuver_id="return-to-base",
        intent=ManeuverIntent(
            action="return-to-base",
            parameters=(ManeuverParameter(name="speed_mps", value=2),),
        ),
        dependencies=("survey", "inspect"),
        duration=1,
    )
    first_spec = MissionSpec(
        mission_id="mission-real-1",
        objective="Survey, inspect, and return",
        planner_choice=planner_choice,
        maneuvers=(survey, inspect, return_to_base),
        horizon=10,
        source_authority="mission-control",
    )
    equivalent_spec = MissionSpec(
        mission_id="mission-real-1",
        objective="Survey, inspect, and return",
        planner_choice=planner_choice,
        maneuvers=(
            TemporalManeuver(
                maneuver_id="return-to-base",
                intent=return_to_base.intent,
                dependencies=("inspect", "survey"),
                duration=1,
            ),
            inspect,
            TemporalManeuver(
                maneuver_id="survey",
                intent=ManeuverIntent(
                    action="survey",
                    parameters=tuple(reversed(survey.intent.parameters)),
                ),
                dependencies=(),
                duration=2,
            ),
        ),
        horizon=10,
        source_authority="mission-control",
    )

    assert translate_minizinc(first_spec) == translate_minizinc(equivalent_spec)

    planning = TemporalPlanning(
        executor=MiniZincExecutor(
            executable=minizinc,
            artifact_root=tmp_path / "artifacts",
            timeout_seconds=10,
        )
    )
    first_event = planning.plan_event(
        mission_spec=first_spec,
        plan_revision=4,
        mission_snapshot_id="snapshot-real-1",
        event_id="event-real-plan-4-a",
        sequence=41,
    )
    equivalent_event = planning.plan_event(
        mission_spec=equivalent_spec,
        plan_revision=4,
        mission_snapshot_id="snapshot-real-1",
        event_id="event-real-plan-4-b",
        sequence=42,
    )

    expected_maneuvers = (
        ScheduledManeuver(
            maneuver_id="survey",
            intent=survey.intent,
            dependencies=(),
            start=0,
            duration=2,
        ),
        ScheduledManeuver(
            maneuver_id="inspect",
            intent=inspect.intent,
            dependencies=("survey",),
            start=2,
            duration=3,
        ),
        ScheduledManeuver(
            maneuver_id="return-to-base",
            intent=return_to_base.intent,
            dependencies=("inspect", "survey"),
            start=5,
            duration=1,
        ),
    )
    canonical_plan = (
        '{"maneuvers":['
        '{"dependencies":[],"duration":2,"intent":{"action":"survey",'
        '"parameters":{"altitude_m":120,"area":"alpha"}},'
        '"maneuver_id":"survey","start":0},'
        '{"dependencies":["survey"],"duration":3,"intent":'
        '{"action":"inspect","parameters":{"sensor":"eo"}},'
        '"maneuver_id":"inspect","start":2},'
        '{"dependencies":["inspect","survey"],"duration":1,"intent":'
        '{"action":"return-to-base","parameters":{"speed_mps":2}},'
        '"maneuver_id":"return-to-base","start":5}],'
        '"mission_snapshot_id":"snapshot-real-1",'
        '"mission_spec":{"horizon":10,"maneuvers":['
        '{"dependencies":["survey"],"duration":3,"intent":'
        '{"action":"inspect","parameters":{"sensor":"eo"}},'
        '"maneuver_id":"inspect"},'
        '{"dependencies":["inspect","survey"],"duration":1,"intent":'
        '{"action":"return-to-base","parameters":{"speed_mps":2}},'
        '"maneuver_id":"return-to-base"},'
        '{"dependencies":[],"duration":2,"intent":{"action":"survey",'
        '"parameters":{"altitude_m":120,"area":"alpha"}},'
        '"maneuver_id":"survey"}],'
        '"mission_id":"mission-real-1",'
        '"objective":"Survey, inspect, and return",'
        '"planner_choice":{"planner_id":"minizinc",'
        '"planning_profile":"temporal"},'
        '"source_authority":"mission-control"},'
        '"outcome":"solved","plan_revision":4,'
        '"planner_choice":{"planner_id":"minizinc",'
        '"planning_profile":"temporal"}}'
    )

    assert first_event.event_id == "event-real-plan-4-a"
    assert first_event.event_kind == "normalized-plan"
    assert first_event.contract_revision == 1
    assert first_event.sequence == 41
    assert first_event.outcome is PlanningOutcome.SOLVED
    assert first_event.payload.mission_id == "mission-real-1"
    assert first_event.payload.plan_revision == 4
    assert first_event.payload.source_authority == "mission-control"
    assert first_event.payload.normalized_plan.maneuvers == expected_maneuvers
    assert first_event.payload.normalized_plan.to_canonical_json() == canonical_plan
    assert first_event.payload.normalized_plan_document == canonical_plan
    assert (
        equivalent_event.payload.normalized_plan.to_canonical_json()
        == canonical_plan
    )
    expected_digest = hashlib.sha256(canonical_plan.encode("utf-8")).hexdigest()
    assert first_event.payload.normalized_plan_sha256 == expected_digest
    assert equivalent_event.payload.normalized_plan_sha256 == expected_digest
