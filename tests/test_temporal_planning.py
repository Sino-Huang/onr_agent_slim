from pathlib import Path

import pytest

from onr.application.temporal_planning import TemporalPlanning
from onr.contracts.planning import (
    ManeuverIntent,
    ManeuverParameter,
    MissionSpec,
    PlannerExecutionEvidence,
    PlannerChoice,
    PlannerExecutionResult,
    PlanningOutcome,
    ScheduledManeuver,
    TemporalAssignment,
    TemporalManeuver,
)


class FakeTemporalExecutor:
    def __init__(self, result: PlannerExecutionResult) -> None:
        self._result = result

    def execute(self, assets: object) -> PlannerExecutionResult:
        return self._result


@pytest.mark.parametrize(
    "terminal_outcome",
    (
        PlanningOutcome.SOLVED,
        PlanningOutcome.UNSOLVABLE,
        PlanningOutcome.INCOMPLETE,
        PlanningOutcome.TIMEOUT,
        PlanningOutcome.ERROR,
    ),
)
def test_temporal_planning_preserves_provenance_and_terminal_outcomes(
    terminal_outcome: PlanningOutcome,
) -> None:
    planner_choice = PlannerChoice(
        planning_profile="temporal",
        planner_id="minizinc",
    )
    survey_intent = ManeuverIntent(
        action="survey",
        parameters=(
            ManeuverParameter(name="area", value="alpha"),
            ManeuverParameter(name="altitude_m", value=120),
        ),
    )
    return_intent = ManeuverIntent(
        action="return-to-base",
        parameters=(ManeuverParameter(name="speed_mps", value=2),),
    )
    mission_spec = MissionSpec(
        mission_id="mission-1",
        objective="Survey the operating area and return",
        planner_choice=planner_choice,
        maneuvers=(
            TemporalManeuver(
                maneuver_id="survey",
                intent=survey_intent,
                dependencies=(),
                duration=4,
            ),
            TemporalManeuver(
                maneuver_id="return-to-base",
                intent=return_intent,
                dependencies=("survey",),
                duration=2,
            ),
        ),
        horizon=10,
        source_authority="mission-control",
    )
    executor_result = PlannerExecutionResult(
        outcome=terminal_outcome,
        evidence=PlannerExecutionEvidence(
            artifact_directory=Path("artifacts/temporal"),
            artifact_paths=(Path("artifacts/temporal/model.mzn"),),
            stdout_path=Path("artifacts/temporal/solver.stdout"),
            stderr_path=Path("artifacts/temporal/solver.stderr"),
        ),
        assignments=(
            (
                TemporalAssignment(
                    maneuver_id="return-to-base", start=4, duration=2
                ),
                TemporalAssignment(maneuver_id="survey", start=0, duration=4),
            )
            if terminal_outcome is PlanningOutcome.SOLVED
            else ()
        ),
    )
    planning = TemporalPlanning(executor=FakeTemporalExecutor(executor_result))

    result = planning.plan(
        mission_spec=mission_spec,
        plan_revision=7,
        mission_snapshot_id="snapshot-12",
    )

    assert result.outcome is terminal_outcome
    assert result.evidence == executor_result.evidence
    if terminal_outcome is not PlanningOutcome.SOLVED:
        assert result.normalized_plan is None
        return

    assert result.normalized_plan is not None
    assert result.normalized_plan.outcome is terminal_outcome
    assert result.normalized_plan.mission_spec == mission_spec
    assert result.normalized_plan.plan_revision == 7
    assert result.normalized_plan.mission_snapshot_id == "snapshot-12"
    assert result.normalized_plan.planner_choice == planner_choice

    assert result.normalized_plan.maneuvers == (
        ScheduledManeuver(
            maneuver_id="survey",
            intent=survey_intent,
            dependencies=(),
            start=0,
            duration=4,
        ),
        ScheduledManeuver(
            maneuver_id="return-to-base",
            intent=return_intent,
            dependencies=("survey",),
            start=4,
            duration=2,
        ),
    )
    assert result.normalized_plan.to_canonical_json() == (
        '{"maneuvers":['
        '{"dependencies":[],"duration":4,"intent":{"action":"survey",'
        '"parameters":{"altitude_m":120,"area":"alpha"}},'
        '"maneuver_id":"survey","start":0},'
        '{"dependencies":["survey"],"duration":2,"intent":'
        '{"action":"return-to-base","parameters":{"speed_mps":2}},'
        '"maneuver_id":"return-to-base","start":4}],'
        '"mission_snapshot_id":"snapshot-12",'
        '"mission_spec":{"horizon":10,"maneuvers":['
        '{"dependencies":["survey"],"duration":2,"intent":'
        '{"action":"return-to-base","parameters":{"speed_mps":2}},'
        '"maneuver_id":"return-to-base"},'
        '{"dependencies":[],"duration":4,"intent":{"action":"survey",'
        '"parameters":{"altitude_m":120,"area":"alpha"}},'
        '"maneuver_id":"survey"}],'
        '"mission_id":"mission-1",'
        '"objective":"Survey the operating area and return",'
        '"planner_choice":{"planner_id":"minizinc",'
        '"planning_profile":"temporal"},'
        '"source_authority":"mission-control"},'
        '"outcome":"solved","plan_revision":7,'
        '"planner_choice":{"planner_id":"minizinc",'
        '"planning_profile":"temporal"}}'
    )
